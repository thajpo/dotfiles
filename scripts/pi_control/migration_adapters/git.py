"""Read-only Git source-authority adapter."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..git_adapter import GitObservationError, observe_repository
from .base import AdapterRecord, AdapterResult, redact


def observe(repository: os.PathLike[str] | str) -> AdapterResult:
    try:
        value = observe_repository(repository).as_dict()
        observed_at = value.pop("observed_at", None)
        normalized = redact(value)
        source = str(Path(repository).expanduser().absolute())
        digest = "sha256:" + hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        identity = {"commonDir": normalized.get("common_dir"), "objectFormat": normalized.get("object_format"), "repository": source}
        record_id = "rec_" + hashlib.sha256(json.dumps({"adapter": "git", "identity": identity, "digest": digest}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
        record = AdapterRecord(record_id, "git", 1, "git-repository", source, digest, "project-observation", identity, normalized, "observed", (), "status text retained only as bounded evidence")
        return AdapterResult("git", 1, "observed", "git-read-only-v1", (record,))
    except GitObservationError as error:
        state = "unavailable" if error.kind in {"missing", "adapter-unavailable"} else "error"
        code = "CP_ADAPTER_UNAVAILABLE" if state == "unavailable" else "CP_INVALID_REQUEST"
        return AdapterResult("git", 1, state, "git-read-only-v1", reason=str(error)[:512], error_code=code)


observe_git = observe
__all__ = ["observe", "observe_git"]
