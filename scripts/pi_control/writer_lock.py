"""Process-lifetime writer fencing for controller-owned working copies."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
from typing import Any


class WriterLockError(RuntimeError):
    pass


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
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{working_copy_id}.lock"
        handle = path.open("a+")
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


__all__ = ["WriterLock", "WriterLockError"]
