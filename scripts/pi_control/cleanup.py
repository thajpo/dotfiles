"""Separate exact cleanup planning/apply adapter."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Callable, Mapping, Sequence

from .errors import ConstraintError, IdempotencyConflictError
from .models import canonical_json

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class CleanupPlan:
    payload: dict[str, Any]
    plan_hash: str

    def as_dict(self) -> dict[str, Any]: return {**self.payload, "planHash": self.plan_hash}


def plan_cleanup(*, project_id: str, resources: Sequence[Mapping[str, Any]]) -> CleanupPlan:
    actions = []
    for resource in resources:
        if not isinstance(resource, Mapping) or resource.get("controllerOwned") is not True or resource.get("live") is True or resource.get("dirty") is True:
            raise ConstraintError("cleanup resource is not an exact safe controller-owned resource")
        resource_id = resource.get("resourceId")
        path = resource.get("path")
        digest = resource.get("digest")
        if not isinstance(resource_id, str) or not isinstance(path, str) or not path.startswith("/"):
            raise ConstraintError("cleanup resource identity is invalid")
        if not _valid_digest(digest):
            raise ConstraintError("cleanup resource digest is missing or invalid")
        actions.append({"resourceId": resource_id, "path": path, "expectedDigest": digest, "kind": resource.get("kind", "resource")})
    payload = {"schemaVersion": 1, "projectId": project_id, "actions": sorted(actions, key=lambda item: item["resourceId"]), "broadDelete": False}
    return CleanupPlan(payload, "sha256:" + hashlib.sha256(canonical_json(payload).encode()).hexdigest())


def apply_cleanup(plan: CleanupPlan | Mapping[str, Any], *, authorization: Mapping[str, Any], observed: Mapping[str, Mapping[str, Any]], remover: Callable[[str], Any] | None = None) -> dict[str, Any]:
    payload = plan.payload if isinstance(plan, CleanupPlan) else dict(plan)
    expected = plan.plan_hash if isinstance(plan, CleanupPlan) else payload.get("planHash")
    computed = "sha256:" + hashlib.sha256(canonical_json({key: value for key, value in payload.items() if key != "planHash"}).encode()).hexdigest()
    if expected != computed: raise IdempotencyConflictError("cleanup-plan", existing_digest=str(expected), request_digest=computed)
    if authorization.get("kind") != "cleanup" or authorization.get("state") != "active": raise ConstraintError("cleanup authorization is invalid")
    if payload.get("broadDelete") is not False or remover is None: raise ConstraintError("cleanup requires an exact host remover")
    removed = []
    for action in payload.get("actions", []):
        expected_digest = action.get("expectedDigest")
        current = observed.get(action["resourceId"])
        if (
            not _valid_digest(expected_digest)
            or current is None
            or not _valid_digest(current.get("digest"))
            or current.get("controllerOwned") is not True
            or current.get("live") is True
            or current.get("dirty") is True
            or current.get("digest") != expected_digest
        ):
            raise ConstraintError("cleanup observation changed or is uncertain")
        remover(action["path"])
        removed.append(action["resourceId"])
    return {"state": "succeeded", "planHash": computed, "removed": removed, "broadDelete": False}


__all__ = ["CleanupPlan", "apply_cleanup", "plan_cleanup"]
