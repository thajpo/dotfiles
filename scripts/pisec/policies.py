"""Bounded project automation policies used by worker and merge guards."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .models import ConflictError, InvalidRequestError

WORKER_PROFILES = frozenset({"worker-default"})
WORK_MODES = frozenset({"FAST", "RIP", "BUILD", "MAJOR"})
POLICY_DOMAIN_RE = re.compile(r"^(?:\*\.)?[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def _policy_object(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > 16:
        raise InvalidRequestError(f"{name} must be a bounded object")
    return dict(value)


def _string_list(value: Any, *, name: str, allowed: frozenset[str] | None = None, limit: int = 32) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > limit:
        raise InvalidRequestError(f"{name} must be a non-empty bounded list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 512 or any(ord(char) < 0x20 for char in item):
            raise InvalidRequestError(f"{name} contains invalid text")
        if allowed is not None and item not in allowed:
            raise InvalidRequestError(f"{name} contains an unsupported value")
        result.append(item)
    if len(result) != len(set(result)):
        raise InvalidRequestError(f"{name} contains duplicates")
    return result


def _positive_int(value: Any, *, name: str, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise InvalidRequestError(f"{name} must be between 1 and {maximum}")
    return value


def normalize_worker_policy(mode: str, value: Any) -> dict[str, Any]:
    policy = _policy_object(value, "worker creation policy")
    allowed = {"workerLimit", "approvedWorkModes"}
    if set(policy) - allowed:
        raise InvalidRequestError("worker creation policy contains unsupported fields")
    result: dict[str, Any] = {}
    if "workerLimit" in policy:
        result["workerLimit"] = _positive_int(policy["workerLimit"], name="workerLimit", maximum=128)
    if "approvedWorkModes" in policy:
        result["approvedWorkModes"] = _string_list(policy["approvedWorkModes"], name="approvedWorkModes", allowed=WORK_MODES)
    if mode == "bounded_auto" and "workerLimit" not in result:
        raise InvalidRequestError("bounded_auto requires workerLimit")
    return result


def normalize_merge_policy(mode: str, value: Any) -> dict[str, Any]:
    policy = _policy_object(value, "merge policy")
    allowed = {"allowedTargetBranches", "requiredChecks", "requiredVerificationCommands", "maxChangedFiles", "maxDiffBytes"}
    if set(policy) - allowed:
        raise InvalidRequestError("merge policy contains unsupported fields")
    result: dict[str, Any] = {}
    if "allowedTargetBranches" in policy:
        branches = _string_list(policy["allowedTargetBranches"], name="allowedTargetBranches")
        if any(branch.startswith("-") for branch in branches):
            raise InvalidRequestError("allowedTargetBranches contains an unsafe branch")
        result["allowedTargetBranches"] = branches
    checks = policy.get("requiredChecks", policy.get("requiredVerificationCommands"))
    if "requiredChecks" in policy and "requiredVerificationCommands" in policy:
        if policy["requiredChecks"] != policy["requiredVerificationCommands"]:
            raise InvalidRequestError("required merge checks have conflicting aliases")
    if checks is not None:
        result["requiredChecks"] = _string_list(checks, name="requiredChecks")
    if "maxChangedFiles" in policy:
        result["maxChangedFiles"] = _positive_int(policy["maxChangedFiles"], name="maxChangedFiles", maximum=10000)
    if "maxDiffBytes" in policy:
        result["maxDiffBytes"] = _positive_int(policy["maxDiffBytes"], name="maxDiffBytes", maximum=16 * 1024 * 1024)
    if mode == "checked_auto" and "allowedTargetBranches" not in result:
        raise InvalidRequestError("checked_auto requires allowedTargetBranches")
    return result


def enforce_worker_creation_policy(store: Any, project: Mapping[str, Any], scope: Mapping[str, Any]) -> None:
    if project.get("worker_creation_policy") != "bounded_auto":
        return
    policy = normalize_worker_policy("bounded_auto", project.get("worker_creation_policy_json"))
    workstream_id = str(scope["workstreamId"])
    active = store.conn.execute(
        "SELECT COUNT(*) FROM workstreams WHERE project_id=? AND kind='worker' AND desired_state='active' AND workstream_id<>?",
        (project["project_id"], workstream_id),
    ).fetchone()[0]
    if int(active) >= policy["workerLimit"]:
        raise ConflictError("bounded worker creation policy limit has been reached")
    if scope.get("executionProfile", "worker-default") != "worker-default":
        raise ConflictError("only the worker-default execution profile is supported")
    approved_modes = policy.get("approvedWorkModes")
    if approved_modes is not None and scope.get("workMode") not in approved_modes:
        raise ConflictError("worker mode is outside the bounded project policy")


def enforce_merge_policy(project: Mapping[str, Any], *, target_branch: str, completion_packet: Mapping[str, Any]) -> dict[str, Any]:
    if project.get("merge_policy") != "checked_auto":
        return {}
    policy = normalize_merge_policy("checked_auto", project.get("merge_policy_json"))
    if target_branch not in policy["allowedTargetBranches"]:
        raise ConflictError("merge target branch is outside the checked project policy")
    if "requiredChecks" in policy:
        commands = {item.get("command") for item in completion_packet.get("verification", []) if isinstance(item, Mapping)}
        missing = [item for item in policy["requiredChecks"] if item not in commands]
        if missing:
            raise ConflictError("completion packet is missing required merge checks", detail={"missingChecks": missing})
    return policy
