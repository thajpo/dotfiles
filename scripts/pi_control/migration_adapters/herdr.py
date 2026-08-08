"""Read-only bounded Herdr presentation observation."""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import Any, Callable, Sequence

from .base import AdapterRecord, AdapterResult, redact
from .processes import _run


def observe(runner: Callable[[Sequence[str]], Any] | None = None) -> AdapterResult:
    args = ("herdr", "status", "--json")
    try:
        code, stdout, stderr = _run(args, runner, timeout=10.0)
    except (OSError, subprocess.TimeoutExpired) as error:
        return AdapterResult("herdr", 1, "unavailable", "herdr-read-only-v1", reason=str(error)[:512], error_code="CP_ADAPTER_UNAVAILABLE")
    if code != 0:
        return AdapterResult("herdr", 1, "error", "herdr-read-only-v1", reason=stderr[:512], error_code="CP_INVALID_REQUEST")
    try: value = redact(json.loads(stdout))
    except json.JSONDecodeError: return AdapterResult("herdr", 1, "error", "herdr-read-only-v1", reason="Herdr output was not JSON", error_code="CP_INVALID_REQUEST")
    if value in ({}, [], None): return AdapterResult("herdr", 1, "empty", "herdr-read-only-v1")
    identity = {"snapshot": hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    digest = "sha256:" + identity["snapshot"]
    rid = "rec_" + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
    record = AdapterRecord(rid, "herdr", 1, "presentation", "herdr status", digest, "presentation-observation", identity, value if isinstance(value, dict) else {"value": value}, "observed", (), "native restore remains observation-only")
    return AdapterResult("herdr", 1, "observed", "herdr-read-only-v1", (record,))


observe_herdr = observe
__all__ = ["observe", "observe_herdr"]
