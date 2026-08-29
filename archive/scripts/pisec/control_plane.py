"""Cross-process serialization for Pisec control-plane mutations."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import threading
import time
from functools import wraps
from typing import Any, Callable, Iterator, TypeVar

from .fsutil import _secure_tree
from .models import ConflictError, PisecError


LOCK_NAME = "control-plane.lock"
INHERITED_LOCK_FD_ENV = "PISEC_CONTROL_PLANE_LOCK_FD"
DEFAULT_TIMEOUT = 30.0
_PROCESS_LOCK = threading.RLock()
_LOCAL_STATE = threading.local()
_Return = TypeVar("_Return")


def _lock_path(state_root: Path | str) -> Path:
    root = Path(state_root)
    locks = root / "locks"
    _secure_tree(root, locks)
    path = locks / LOCK_NAME
    try:
        descriptor = path.lstat()
    except FileNotFoundError:
        return path
    if (
        stat.S_ISLNK(descriptor.st_mode)
        or not stat.S_ISREG(descriptor.st_mode)
        or descriptor.st_uid != os.geteuid()
        or stat.S_IMODE(descriptor.st_mode) != 0o600
    ):
        raise PisecError("Pisec control-plane lock is unsafe")
    return path


def _inherited_lock_descriptor(path: Path) -> int | None:
    """Validate an updater-owned lock descriptor inherited across exec."""
    raw = os.environ.get(INHERITED_LOCK_FD_ENV)
    if raw is None:
        return None
    try:
        descriptor = int(raw)
        inherited = os.fstat(descriptor)
        current = path.stat()
    except (OSError, TypeError, ValueError) as error:
        raise PisecError("inherited Pisec control-plane lock is invalid") from error
    if (
        descriptor < 0
        or not stat.S_ISREG(inherited.st_mode)
        or inherited.st_uid != os.geteuid()
        or stat.S_IMODE(inherited.st_mode) != 0o600
        or (inherited.st_dev, inherited.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise PisecError("inherited Pisec control-plane lock is invalid")
    try:
        # An inherited descriptor shares the updater's open file description,
        # so this succeeds without releasing or replacing the updater's lock.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise PisecError("inherited Pisec control-plane lock is not held") from error
    return descriptor


@contextmanager
def control_plane_lock(state_root: Path | str, *, timeout: float = DEFAULT_TIMEOUT, on_wait: Callable[[], None] | None = None) -> Iterator[None]:
    """Serialize topology and runtime-generation mutations across processes.

    The local re-entrant guard prevents nested Pisec calls in one process from
    opening a second file descriptor.  The file lock is the cross-process
    boundary; it is released by the kernel if the owner exits unexpectedly.
    Worker activity and ordinary runtime reports do not call this context.
    """
    path = _lock_path(state_root)
    with _PROCESS_LOCK:
        depth = int(getattr(_LOCAL_STATE, "depth", 0))
        if depth:
            _LOCAL_STATE.depth = depth + 1
            try:
                yield
            finally:
                _LOCAL_STATE.depth = depth
            return
        inherited = _inherited_lock_descriptor(path)
        if inherited is not None:
            _LOCAL_STATE.depth = 1
            try:
                yield
            finally:
                _LOCAL_STATE.depth = 0
            return
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            deadline = time.monotonic() + max(0.0, float(timeout))
            announced_wait = False
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as error:
                    if on_wait is not None and not announced_wait:
                        on_wait()
                        announced_wait = True
                    if time.monotonic() >= deadline:
                        raise ConflictError("Pisec control-plane lock is busy") from error
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            _LOCAL_STATE.depth = 1
            try:
                yield
            finally:
                _LOCAL_STATE.depth = 0
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def control_plane_mutation(function: Callable[..., _Return]) -> Callable[..., _Return]:
    """Decorate a store-backed operation that changes control-plane state."""
    @wraps(function)
    def guarded(store: Any, *args: Any, **kwargs: Any) -> _Return:
        with control_plane_lock(store.state_root):
            return function(store, *args, **kwargs)

    return guarded
