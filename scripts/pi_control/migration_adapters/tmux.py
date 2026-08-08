"""Read-only exact tmux presentation observation."""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import Any, Callable, Sequence

from .base import AdapterRecord, AdapterResult, redact
from .processes import _run


def observe(runner: Callable[[Sequence[str]], Any] | None = None) -> AdapterResult:
    args = ("tmux", "list-panes", "-a", "-F", "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_pid}\t#{pane_title}")
    try:
        code, stdout, stderr = _run(args, runner, timeout=10.0)
    except (OSError, subprocess.TimeoutExpired) as error:
        return AdapterResult("tmux", 1, "unavailable", "tmux-read-only-v1", reason=str(error)[:512], error_code="CP_ADAPTER_UNAVAILABLE")
    if code != 0:
        return AdapterResult("tmux", 1, "error", "tmux-read-only-v1", reason=stderr[:512], error_code="CP_INVALID_REQUEST")
    rows = []
    for line in stdout.splitlines()[:4096]:
        fields = line.split("\t")
        if len(fields) >= 4: rows.append(redact({"session": fields[0], "window": fields[1], "pane": fields[2], "pid": fields[3], "title": fields[4] if len(fields) > 4 else ""}))
    if not rows: return AdapterResult("tmux", 1, "empty", "tmux-read-only-v1")
    records = []
    for row in rows:
        identity = {"session": row.get("session"), "window": row.get("window"), "pane": row.get("pane")}
        digest = "sha256:" + hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        rid = "rec_" + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
        records.append(AdapterRecord(rid, "tmux", 1, "pane", "tmux list-panes", digest, "presentation-observation", identity, row, "observed", (), "pane locator never creates lifecycle identity"))
    return AdapterResult("tmux", 1, "observed", "tmux-read-only-v1", tuple(records))


observe_tmux = observe
__all__ = ["observe", "observe_tmux"]
