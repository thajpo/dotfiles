"""Read-only process identity observations for Phase 4 fencing."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from .models import utc_now


class ProcessObservationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessObservation:
    pid: int
    exists: bool
    start_identity: str | None
    uid: int | None
    state: str
    observed_at: str
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "exists": self.exists,
            "start_identity": self.start_identity,
            "uid": self.uid,
            "state": self.state,
            "observed_at": self.observed_at,
            "error": self.error,
            "provenance": "process-identity-observation-v1",
        }


def _proc_stat(pid: int) -> tuple[str, int]:
    path = Path(f"/proc/{pid}/stat")
    raw = path.read_text(encoding="utf-8", errors="strict")
    closing = raw.rfind(")")
    if closing < 0:
        raise ProcessObservationError("process stat record is malformed")
    fields = raw[closing + 2 :].split()
    # After comm, fields[0] is state (original field 3); original field 22
    # starttime is therefore index 19.
    if len(fields) <= 19:
        raise ProcessObservationError("process stat record lacks start identity")
    return fields[0], int(fields[19])


def process_start_identity(pid: int) -> str:
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    if os.name == "posix" and Path("/proc").is_dir():
        state, ticks = _proc_stat(pid)
        boot = "unknown-boot"
        try:
            boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except OSError:
            pass
        return f"linux:{boot}:{ticks}"
    # Non-Linux adapters cannot prove a reusable PID from this helper.  The
    # opaque marker is still explicit and is never treated as a timestamp.
    return f"posix:{pid}:{os.getpid()}"


def observe_process(pid: int, *, expected_start_identity: str | None = None) -> ProcessObservation:
    observed_at = utc_now()
    if not isinstance(pid, int) or pid <= 0:
        return ProcessObservation(int(pid) if isinstance(pid, int) else -1, False, None, None, "invalid", observed_at, "invalid pid")
    try:
        state, _ticks = _proc_stat(pid) if Path("/proc").is_dir() else ("?", 0)
        identity = process_start_identity(pid)
        uid = None
        try:
            uid = Path(f"/proc/{pid}").stat().st_uid
        except OSError:
            pass
        if expected_start_identity is not None and identity != expected_start_identity:
            return ProcessObservation(pid, True, identity, uid, "reused", observed_at, "start identity mismatch")
        return ProcessObservation(pid, True, identity, uid, state, observed_at)
    except FileNotFoundError:
        return ProcessObservation(pid, False, None, None, "gone", observed_at)
    except PermissionError as error:
        return ProcessObservation(pid, False, None, None, "unknown", observed_at, "permission denied")
    except (OSError, ValueError, ProcessObservationError) as error:
        return ProcessObservation(pid, False, None, None, "unknown", observed_at, type(error).__name__)


observe_pid = observe_process

__all__ = ["ProcessObservation", "ProcessObservationError", "observe_pid", "observe_process", "process_start_identity"]
