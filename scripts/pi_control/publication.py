"""Separate, exact publication planning/apply adapter."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable, Mapping, Sequence
import re

from .errors import ConstraintError, IdempotencyConflictError
from .models import canonical_json


@dataclass(frozen=True)
class PublicationPlan:
    payload: dict[str, Any]
    plan_hash: str

    def as_dict(self) -> dict[str, Any]: return {**self.payload, "planHash": self.plan_hash}


def plan_publication(*, project_id: str, source_ref: str, remote: str, target_ref: str, expected_oid: str, current_oid: str, remote_name: str = "origin") -> PublicationPlan:
    if not all(isinstance(item, str) and item for item in (project_id, source_ref, remote, target_ref, expected_oid, current_oid)): raise ConstraintError("publication plan fields are invalid")
    if remote_name != "origin" or remote != "origin": raise ConstraintError("publication is limited to the configured origin")
    oid_pattern = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    if oid_pattern.fullmatch(expected_oid) is None or oid_pattern.fullmatch(current_oid) is None: raise ConstraintError("publication OID is invalid")
    for ref in (source_ref, target_ref):
        if not ref.startswith("refs/heads/") or ref.endswith("/") or ".." in ref or "@{" in ref or any(char in ref for char in " ~^:?*\\\\[]"):
            raise ConstraintError("publication ref is invalid")
    payload = {"schemaVersion": 1, "projectId": project_id, "remote": remote, "remoteName": remote_name, "sourceRef": source_ref, "targetRef": target_ref, "expectedOid": expected_oid, "currentOid": current_oid, "force": False, "network": "host-authorized-only"}
    plan_hash = "sha256:" + hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    return PublicationPlan(payload, plan_hash)


def apply_publication(plan: PublicationPlan | Mapping[str, Any], *, authorization: Mapping[str, Any], observed: Mapping[str, Any], runner: Callable[[Sequence[str]], Any] | None = None) -> dict[str, Any]:
    payload = plan.payload if isinstance(plan, PublicationPlan) else dict(plan)
    expected_hash = plan.plan_hash if isinstance(plan, PublicationPlan) else payload.get("planHash")
    computed = "sha256:" + hashlib.sha256(canonical_json({key: value for key, value in payload.items() if key != "planHash"}).encode()).hexdigest()
    if expected_hash != computed: raise IdempotencyConflictError("publication-plan", existing_digest=str(expected_hash), request_digest=computed)
    if authorization.get("kind") != "publish" or authorization.get("state") != "active": raise ConstraintError("publication authorization is invalid")
    observed_source = observed.get("sourceOid", observed.get("sourceRefOid"))
    observed_source_ref = observed.get("sourceRef", observed.get("sourceRefName"))
    if (
        observed.get("remote") != payload.get("remote")
        or observed.get("currentOid") != payload.get("currentOid")
        or observed_source_ref != payload.get("sourceRef")
        or observed_source != payload.get("expectedOid")
    ):
        raise ConstraintError("publication source or target state changed since planning")
    if payload.get("force") is not False: raise ConstraintError("forced publication is forbidden")
    if runner is None: raise ConstraintError("publication requires an explicit host adapter")
    ancestry = runner(("git", "merge-base", "--is-ancestor", payload["currentOid"], payload["expectedOid"]))
    ancestry_code = int(getattr(ancestry, "returncode", ancestry[0] if isinstance(ancestry, tuple) else 1))
    if ancestry_code != 0: raise ConstraintError("publication would not be a fast-forward update")
    result = runner((
        "git", "push", "--porcelain",
        f"--force-with-lease={payload['targetRef']}:{payload['currentOid']}",
        payload["remote"], f"{payload['expectedOid']}:{payload['targetRef']}",
    ))
    code = int(getattr(result, "returncode", result[0] if isinstance(result, tuple) else 1))
    if code != 0: raise ConstraintError("publication adapter refused the exact plan")
    return {"state": "succeeded", "planHash": computed, "remote": payload["remote"], "targetRef": payload["targetRef"], "force": False}


__all__ = ["PublicationPlan", "apply_publication", "plan_publication"]
