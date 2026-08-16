"""Process-lifetime writer fencing for controller-owned working copies."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Any


class WriterLockError(RuntimeError):
    pass


def writer_lock_available(state_root: str | Path, working_copy_id: str) -> bool:
    """Read-only writer-lock availability probe.

    Never mutates the lock file: it opens the durable lock without creating
    it, attempts a non-blocking exclusive flock, and releases it immediately.
    The probe exists so recovery can prove that no live writer holds the
    working copy before clearing a stale controller claim.
    """
    directory = Path(state_root) / "locks"
    try:
        directory_info = directory.lstat()
    except FileNotFoundError:
        return True
    if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.geteuid() or stat.S_IMODE(directory_info.st_mode) != 0o700:
        raise WriterLockError("writer lock directory is unsafe")
    path = directory / f"{working_copy_id}.lock"
    try:
        descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return True
    except OSError as error:
        raise WriterLockError("writer lock cannot be observed") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise WriterLockError("writer lock file is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    finally:
        os.close(descriptor)


@dataclass
class WriterLock:
    path: Path
    working_copy_id: str
    generation: int
    _handle: Any

    @classmethod
    def acquire(cls, state_root: str | Path, working_copy_id: str, generation: int) -> "WriterLock":
        if not isinstance(generation, int) or generation < 1:
            raise WriterLockError("writer generation must be positive")
        directory = Path(state_root) / "locks"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        directory_info = directory.lstat()
        if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.geteuid() or stat.S_IMODE(directory_info.st_mode) != 0o700:
            raise WriterLockError("writer lock directory is unsafe")
        path = directory / f"{working_copy_id}.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        handle = os.fdopen(descriptor, "a+")
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            handle.close()
            raise WriterLockError("writer lock file is unsafe")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise WriterLockError("working copy already has an active writer") from error
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"workingCopyId": working_copy_id, "generation": generation, "pid": os.getpid()}, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
        return cls(path, working_copy_id, generation, handle)

    def close(self) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None

    def __enter__(self) -> "WriterLock":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


__all__ = ["WriterLock", "WriterLockError", "writer_lock_available"]
