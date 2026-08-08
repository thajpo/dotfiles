"""Host-owned project policy observation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from ..project_policy import PolicyError, load_policy
from .base import AdapterError, AdapterRecord, AdapterResult, safe_read, redact


def observe(source: os.PathLike[str] | str) -> AdapterResult:
    path = Path(source).expanduser()
    try:
        data, _ = safe_read(path)
        policy = load_policy(json.loads(data), base_dir=path.parent)
        normalized = redact(policy.as_dict())
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        identity = {"path": str(path.absolute()), "policyHash": policy.policy_hash}
        record_id = "rec_" + hashlib.sha256(json.dumps({"adapter": "policy", "identity": identity}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
        record = AdapterRecord(record_id, "policy", 1, "policy", str(path.absolute()), digest, "policy-observation", identity, normalized, "observed")
        return AdapterResult("policy", 1, "observed", "host-policy-v1", (record,))
    except FileNotFoundError:
        return AdapterResult("policy", 1, "unavailable", "host-policy-v1", reason="policy source is unavailable", error_code="CP_ADAPTER_UNAVAILABLE")
    except (OSError, ValueError, PolicyError, AdapterError) as error:
        return AdapterResult("policy", 1, "error", "host-policy-v1", reason=str(error)[:512], error_code="CP_INVALID_REQUEST")


observe_policy = observe
__all__ = ["observe", "observe_policy"]
