"""Bounded command runner for disposable greenfield system tests."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import subprocess
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


__all__ = ["CommandExecutionError", "CommandRecord", "_run"]
