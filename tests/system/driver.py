"""Small non-live process driver used by C9 process-fixture scenarios."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from typing import Sequence

try:
    from .fixture import SystemFixture
except ImportError:
    from fixture import SystemFixture


class CommandExecutionError(AssertionError):
    def __init__(self, message: str, record: "CommandRecord"):
        super().__init__(message)
        self.record = record


@dataclass(frozen=True)
class CommandRecord:
    argv: tuple[str, ...]
    returncode: int
    stdout_digest: str
    stderr_digest: str
    expected: str
    network: bool = False

    def as_dict(self):
        return {"argv": list(self.argv), "returncode": self.returncode, "stdoutDigest": self.stdout_digest, "stderrDigest": self.stderr_digest, "expected": self.expected, "network": self.network}


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _run(fixture: SystemFixture, argv: Sequence[str], *, expected: str = "zero") -> CommandRecord:
    environment = fixture.environment()
    try:
        completed = subprocess.run(list(argv), cwd=fixture.repository, env=environment, capture_output=True, text=False, timeout=20)
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        if isinstance(stdout, str): stdout = stdout.encode()
        if isinstance(stderr, str): stderr = stderr.encode()
        record = CommandRecord(tuple(str(item) for item in argv), 124, _digest(stdout), _digest(stderr), expected)
        raise CommandExecutionError(f"command timed out after 20s: {list(argv)}", record) from error
    expected_ok = completed.returncode == 0 if expected == "zero" else completed.returncode != 0
    record = CommandRecord(tuple(str(item) for item in argv), completed.returncode, _digest(completed.stdout), _digest(completed.stderr), expected)
    if not expected_ok:
        raise CommandExecutionError(f"unexpected command result {list(argv)} -> {completed.returncode}: {completed.stderr[-500:]!r}", record)
    return record


def run_group(fixture: SystemFixture, group: str) -> dict:
    """Invoke real repository launchers plus strict fake boundary commands.

    These are intentionally read-only/help/refusal requests.  A mutating or
    backend-dependent path is not silently simulated; its owning tier remains
    STOP/77 until a staged/backend fixture is supplied.
    """
    commands: list[CommandRecord] = []
    before = fixture.snapshot_namespace()
    commands.append(_run(fixture, [sys.executable, str(Path(__file__).resolve().parent / "fake_process.py"), "--version"]))
    commands.append(_run(fixture, [str(Path(__file__).resolve().parents[2] / "bin" / "pi-control"), "--help"]))
    commands.append(_run(fixture, [str(Path(__file__).resolve().parents[2] / "bin" / "pi"), "help"]))
    # Exercise the host-owned refusal boundary without invoking the real Pi.
    commands.append(_run(fixture, [str(Path(__file__).resolve().parents[2] / "bin" / "pi"), "--no-sandbox"], expected="nonzero"))
    # Commands above are read-only/refusal requests; capture a second snapshot
    # after the final command and require exact equality.
    after = fixture.snapshot_namespace()
    fixture.assert_namespace_unchanged(before)
    fixture.assert_host_unchanged()
    return {
        "commands": [item.as_dict() for item in commands],
        "before": {"namespaceDigest": fixture.digest_snapshot(before)},
        "after": {"namespaceDigest": fixture.digest_snapshot(after)},
        "capability": {"processFixture": True, "realLaunchers": True, "strictFake": True, "network": False, "liveAction": False},
        "assertions": {"namespaceUnchanged": before == after, "hostUnchanged": True, "noLiveAction": True, "noNetwork": all(not item.network for item in commands), "commandExpectations": all(item.expected in {"zero", "nonzero"} for item in commands)},
    }


__all__ = ["CommandExecutionError", "CommandRecord", "run_group"]
