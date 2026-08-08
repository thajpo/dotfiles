"""Read-only ordered observation locks for Phase 3.

Phase 3 never takes an exclusive lifecycle/writer lock.  The helper is a small
adapter for shared observation coordination; its file content is diagnostic and
never controller authority.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import stat
from typing import Iterator

_LOCK_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")


def secure_directory_fd(parent: os.PathLike[str] | str, *, create: bool) -> int | None:
    """Walk an absolute directory path with no-follow directory descriptors.

    Each child is opened relative to the already-open parent. A concurrent
    rename can move the retained directory but cannot redirect creation through
    a replacement symlink. The returned final descriptor stays authoritative
    until the caller closes it.
    """

    absolute = Path(os.path.abspath(parent))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    current_fd = os.open(absolute.anchor or os.sep, flags)
    try:
        for component in absolute.parts[1:]:
            try:
                child_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    os.close(current_fd)
                    return None
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = child_fd
        info = os.fstat(current_fd)
        uid = os.geteuid() if hasattr(os, "geteuid") else os.getuid()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError("private directory is unsafe")
        if create:
            os.fchmod(current_fd, 0o700)
        return current_fd
    except RuntimeError:
        try:
            os.close(current_fd)
        except OSError:
            pass
        raise
    except OSError as error:
        try:
            os.close(current_fd)
        except OSError:
            pass
        raise RuntimeError("directory path is unsafe or unavailable") from error
    except BaseException:
        try:
            os.close(current_fd)
        except OSError:
            pass
        raise


def secure_lock_directory(parent: Path, *, create: bool) -> int | None:
    """Open a user-owned lock directory and retain its directory FD."""

    return secure_directory_fd(parent, create=create)


class ObservationLock:
    def __init__(self, state_root: os.PathLike[str] | str, resource_name: str, *, create: bool = True):
        if _LOCK_NAME.fullmatch(resource_name) is None:
            raise ValueError("lock resource name is invalid")
        self.state_root = Path(state_root)
        self.resource_name = resource_name
        self.create = create
        self.parent = self.state_root / "locks"
        self.path = self.parent / (resource_name + ".lock")
        self._handle = None
        self._directory_fd: int | None = None

    def __enter__(self) -> "ObservationLock":
        directory_fd = secure_lock_directory(self.parent, create=self.create)
        if directory_fd is None:
            return self
        self._directory_fd = directory_fd
        flags = os.O_RDWR | (os.O_CREAT if self.create else 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            try:
                fd = os.open(self.path.name, flags, 0o600, dir_fd=directory_fd)
            except FileNotFoundError:
                if not self.create:
                    os.close(directory_fd)
                    self._directory_fd = None
                    return self
                raise
            self._handle = os.fdopen(fd, "a+", encoding="utf-8")
            if self.create:
                os.fchmod(fd, 0o600)
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_SH)
            return self
        except BaseException:
            os.close(directory_fd)
            self._directory_fd = None
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None
            if self._directory_fd is not None:
                os.close(self._directory_fd)
                self._directory_fd = None


@contextmanager
def shared_observation_lock(state_root: os.PathLike[str] | str, resource_name: str, *, create: bool = True) -> Iterator[ObservationLock]:
    with ObservationLock(state_root, resource_name, create=create) as lock:
        yield lock


ReadObservationLock = ObservationLock

__all__ = ["ObservationLock", "ReadObservationLock", "secure_directory_fd", "secure_lock_directory", "shared_observation_lock"]
