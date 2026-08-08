"""Installed/staged build evidence adapter; no activation or byte mutation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .base import AdapterError, AdapterRecord, AdapterResult, safe_read, redact


def observe(manifest: os.PathLike[str] | str) -> AdapterResult:
    path = Path(manifest).expanduser()
    try:
        data, _ = safe_read(path)
        value = json.loads(data)
        if not isinstance(value, dict):
            raise AdapterError("build manifest is not an object")
        normalized = redact(value)
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        identity = {"path": str(path.absolute()), "buildId": value.get("buildId"), "manifestDigest": value.get("manifestDigest")}
        record_id = "rec_" + hashlib.sha256(json.dumps({"adapter": "installed_build", "identity": identity, "digest": digest}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
        record = AdapterRecord(record_id, "installed_build", 1, "build-manifest", str(path.absolute()), digest, "installed-build-observation", identity, normalized, "observed")
        return AdapterResult("installed_build", 1, "observed", "installed-build-v1", (record,))
    except FileNotFoundError:
        return AdapterResult("installed_build", 1, "unavailable", "installed-build-v1", reason="build manifest is unavailable", error_code="CP_ADAPTER_UNAVAILABLE")
    except (OSError, UnicodeError, ValueError, AdapterError) as error:
        return AdapterResult("installed_build", 1, "error", "installed-build-v1", reason=str(error)[:512], error_code="CP_INVALID_REQUEST")


observe_installed_build = observe
__all__ = ["observe", "observe_installed_build"]
