"""Read-only bounded Docker observation; no adoption or cleanup."""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import Any, Callable, Sequence

from .base import AdapterRecord, AdapterResult, redact
from .processes import _run


def observe(runner: Callable[[Sequence[str]], Any] | None = None) -> AdapterResult:
    args = ("docker", "ps", "--no-trunc", "--format", "{{json .}}")
    try:
        code, stdout, stderr = _run(args, runner, timeout=10.0)
    except (OSError, subprocess.TimeoutExpired) as error:
        return AdapterResult("docker", 1, "unavailable", "docker-read-only-v1", reason=str(error)[:512], error_code="CP_ADAPTER_UNAVAILABLE")
    if code != 0:
        return AdapterResult("docker", 1, "error", "docker-read-only-v1", reason=stderr[:512], error_code="CP_INVALID_REQUEST")
    rows = []
    for line in stdout.splitlines()[:4096]:
        try: value = redact(json.loads(line))
        except json.JSONDecodeError: return AdapterResult("docker", 1, "error", "docker-read-only-v1", reason="Docker output was not JSON", error_code="CP_INVALID_REQUEST")
        if isinstance(value, dict): rows.append(value)
    if not rows: return AdapterResult("docker", 1, "empty", "docker-read-only-v1")
    records = []
    for row in rows:
        identity = {"id": row.get("ID") or row.get("Id"), "name": row.get("Names")}
        digest = "sha256:" + hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        rid = "rec_" + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
        records.append(AdapterRecord(rid, "docker", 1, "container", "docker ps", digest, "container-observation", identity, row, "observed", (), "unlabeled/forged containers remain observations"))
    return AdapterResult("docker", 1, "observed", "docker-read-only-v1", tuple(records))


observe_docker = observe
__all__ = ["observe", "observe_docker"]
