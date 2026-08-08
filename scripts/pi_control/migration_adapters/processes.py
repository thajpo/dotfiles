"""Read-only process observation with a fixed command shape."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import Any, Callable, Sequence

from .base import AdapterRecord, AdapterResult, redact

CommandRunner = Callable[[Sequence[str]], Any]


def _run(args: Sequence[str], runner: CommandRunner | None, timeout: float = 5.0) -> tuple[int, str, str]:
    if runner is not None:
        result = runner(tuple(args))
        if isinstance(result, tuple): return int(result[0]), str(result[1]), str(result[2])
        return int(getattr(result, "returncode", 0)), str(getattr(result, "stdout", "")), str(getattr(result, "stderr", ""))
    result = subprocess.run(list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, check=False, shell=False, env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"})
    return result.returncode, result.stdout, result.stderr


def observe(runner: CommandRunner | None = None) -> AdapterResult:
    args = ("ps", "-eo", "pid=,ppid=,etimes=,args=")
    try:
        code, stdout, stderr = _run(args, runner)
    except (OSError, subprocess.TimeoutExpired) as error:
        return AdapterResult("processes", 1, "unavailable", "process-read-only-v1", reason=str(error)[:512], error_code="CP_ADAPTER_UNAVAILABLE")
    if code != 0:
        return AdapterResult("processes", 1, "error", "process-read-only-v1", reason=stderr[:512], error_code="CP_INVALID_REQUEST")
    rows = []
    for line in stdout.splitlines()[:10000]:
        fields = line.strip().split(None, 3)
        if not fields: continue
        rows.append(redact({"pid": fields[0], "ppid": fields[1] if len(fields) > 1 else None, "elapsed": fields[2] if len(fields) > 2 else None, "argv": fields[3] if len(fields) > 3 else None}))
    if not rows: return AdapterResult("processes", 1, "empty", "process-read-only-v1")
    records = []
    for row in rows:
        identity = {"pid": row.get("pid"), "startIdentity": row.get("elapsed")}
        digest = "sha256:" + hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        rid = "rec_" + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
        records.append(AdapterRecord(rid, "processes", 1, "process", "ps", digest, "process-observation", identity, row, "observed", (), "never active run/writer authority"))
    return AdapterResult("processes", 1, "observed", "process-read-only-v1", tuple(records))


observe_processes = observe
__all__ = ["observe", "observe_processes"]
