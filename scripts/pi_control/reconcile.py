"""Read-only project/working-copy observation and SQLite projection for Phase 3."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
from typing import Any, Mapping

from .events import append_event_in_transaction
from .git_adapter import GitObservationError, GitRepositoryObservation, GitWorktreeObservation, observe_repository
from .models import bounded_text, new_id, utc_now
from .operations import mutate_with_event
from .project_policy import PolicyError, ProjectPolicy, load_policy
from .locks import shared_observation_lock


@dataclass(frozen=True)
class ProjectObservation:
    project_id: str | None
    repository: GitRepositoryObservation | None
    state: str
    trust_mode: str | None
    policy_hash: str | None
    error_code: str | None = None
    error_detail: str | None = None
    observed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "state": self.state,
            "trust_mode": self.trust_mode,
            "policy_hash": self.policy_hash,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "observed_at": self.observed_at,
            "repository": self.repository.as_dict() if self.repository else None,
            "provenance": "controller-project-observation-v1",
        }


def _state_for_repository(observation: GitRepositoryObservation) -> str:
    return "ready" if observation.head_oid is not None or observation.is_bare else "unknown"


def _safe_error(error: BaseException) -> str:
    return str(error).replace("\x00", "")[:512]


def observe_project(repository: os.PathLike[str] | str, policy: ProjectPolicy | None = None, *, project_id: str | None = None) -> ProjectObservation:
    policy = policy or load_policy()
    observed_at = utc_now()
    try:
        git = observe_repository(repository)
        trust = policy.trust_for_repository(Path(git.top_level or git.repository_path))
        return ProjectObservation(project_id, git, _state_for_repository(git), trust, policy.policy_hash, observed_at=observed_at)
    except GitObservationError as error:
        state = "missing" if error.kind == "missing" else "error"
        return ProjectObservation(project_id, None, state, None, policy.policy_hash, "CP_GIT_OBSERVATION", _safe_error(error), observed_at)
    except PolicyError as error:
        return ProjectObservation(project_id, None, "error", None, policy.policy_hash, "CP_POLICY_INVALID", _safe_error(error), observed_at)


def _project_payload(observation: ProjectObservation) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "state": observation.state,
        "trust_mode": observation.trust_mode,
        "policy_hash": observation.policy_hash,
        "error_code": observation.error_code,
        "error_detail": observation.error_detail,
        "observed_at": observation.observed_at,
        "provenance": "controller-project-observation-v1",
    }
    if observation.repository:
        git = observation.repository
        payload.update({
            "repository_path": git.repository_path,
            "top_level": git.top_level,
            "git_dir": git.git_dir,
            "common_dir": git.common_dir,
            "object_format": git.object_format,
            "head_oid": git.head_oid,
            "tree_oid": git.tree_oid,
            "branch_ref": git.branch_ref,
            "dirty": git.dirty,
            "status_hash": git.status_hash,
        })
    return payload


def _working_copy_state(item: GitWorktreeObservation) -> str:
    if not item.exists:
        return "missing"
    if item.state == "error":
        return "error"
    if item.state == "unknown":
        return "unknown"
    if item.status and any(line and not line.startswith("# branch.") for line in item.status.splitlines()):
        return "dirty"
    return "ready"


def _working_copy_payload(item: GitWorktreeObservation, *, managed: bool, effective_mode: str | None) -> dict[str, Any]:
    return {
        "path": item.path,
        "head_oid": item.head_oid,
        "branch_ref": item.branch_ref,
        "detached": item.detached,
        "bare": item.bare,
        "exists": item.exists,
        "state": _working_copy_state(item),
        "managed": managed,
        "effective_mode": effective_mode,
        "common_dir": item.common_dir,
        "git_dir": item.git_dir,
        "object_format": item.object_format,
        "status_hash": hashlib.sha256((item.status or "").encode("utf-8")).hexdigest() if item.status is not None else None,
        "provenance": "controller-working-copy-observation-v1",
    }


def _project_row(store: Any, project_id: str) -> Mapping[str, Any]:
    row = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if row is None:
        raise KeyError(f"project not found: {project_id}")
    return row


def register_project(
    store: Any,
    repository: os.PathLike[str] | str,
    display_name: str,
    *,
    policy: ProjectPolicy | None = None,
) -> dict[str, Any]:
    """Explicitly register an observed repository; never mutates Git."""

    policy = policy or load_policy()
    observation = observe_project(repository, policy)
    if observation.repository is None:
        raise GitObservationError(observation.error_detail or "repository observation failed", kind="error")
    git = observation.repository
    common = Path(git.common_dir)
    device, inode = common.stat().st_dev, common.stat().st_ino
    existing = store.conn.execute("SELECT * FROM projects WHERE git_common_dir=?", (str(common),)).fetchone()
    if existing is not None:
        return dict(existing)
    project_id = new_id("prj")
    working_copy_id = new_id("wc")
    now = utc_now()
    name = bounded_text(display_name, name="display_name", limit=512)
    primary = git.top_level or git.repository_path
    effective_mode = policy.effective_mode(observation.trust_mode or policy.default_mode)
    wc_state = "dirty" if git.dirty else ("ready" if git.head_oid else "unknown")
    with store.transaction():
        store.conn.execute(
            "INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, name, str(common), device, inode, primary, git.object_format, observation.trust_mode, policy.policy_hash, "active", observation.state, 1, now, now, observation.observed_at, None, None),
        )
        # Registration records an observation, not an adoption.  Later phases
        # explicitly make a working copy controller-owned before writing it.
        store.conn.execute(
            "INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,git_dir,branch_ref,expected_head_oid,expected_tree_oid,effective_mode,desired_state,observed_state,writer_epoch,resource_version,controller_owned,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (working_copy_id, project_id, name, "primary", "personal", primary, git.git_dir, git.branch_ref, git.head_oid, git.tree_oid, effective_mode, "present", wc_state, 0, 1, 0, now, now, observation.observed_at, None, None),
        )
        append_event_in_transaction(store.conn, event_kind="project.registered", resource_type="project", resource_id=project_id, resource_version=1, payload=_project_payload(observation))
    return dict(store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone())


def inspect_project(store: Any, project_id: str, *, policy: ProjectPolicy | None = None) -> dict[str, Any]:
    row = _project_row(store, project_id)
    policy = policy or load_policy()
    with shared_observation_lock(store.state_root, "project-" + project_id, create=False):
        observation = observe_project(row["primary_checkout"], policy, project_id=project_id)
    result = observation.as_dict()
    result["registered"] = {key: row[key] for key in row.keys()}
    if observation.repository:
        expected_common = str(row["git_common_dir"])
        common_matches = observation.repository.common_dir == expected_common
        format_matches = observation.repository.object_format == row["object_format"]
        primary = store.conn.execute(
            "SELECT expected_head_oid,expected_tree_oid,branch_ref FROM working_copies WHERE project_id=? AND kind='primary' ORDER BY created_at LIMIT 1",
            (project_id,),
        ).fetchone()
        anchor_matches = primary is not None and all(
            expected == actual
            for expected, actual in (
                (primary[0], observation.repository.head_oid),
                (primary[1], observation.repository.tree_oid),
                (primary[2], observation.repository.branch_ref),
            )
            if expected is not None
        )
        anchors_available = primary is not None and any(value is not None for value in primary)
        if not common_matches or not format_matches or (anchors_available and not anchor_matches):
            result["state"] = "drifted"
            result["drift"] = {
                "common_dir_matches": common_matches,
                "object_format_matches": format_matches,
                "anchors_available": anchors_available,
                "anchors_match": anchor_matches,
            }
    return result


def inventory_working_copies(store: Any, project_id: str, *, policy: ProjectPolicy | None = None) -> dict[str, Any]:
    row = _project_row(store, project_id)
    policy = policy or load_policy()
    with shared_observation_lock(store.state_root, "project-" + project_id, create=False):
        observation = observe_project(row["primary_checkout"], policy, project_id=project_id)
    managed_rows = {str(item["path"]): item for item in store.conn.execute("SELECT * FROM working_copies WHERE project_id=?", (project_id,))}
    entries: list[dict[str, Any]] = []
    if observation.repository:
        for item in observation.repository.worktrees:
            managed = managed_rows.get(item.path)
            common_matches = item.common_dir is not None and item.common_dir == row["git_common_dir"]
            effective = None
            state = _working_copy_state(item)
            if managed is not None:
                effective = str(managed["effective_mode"]) if common_matches and item.exists and state != "error" else None
                anchor_matches = all(
                    expected is None or expected == actual
                    for expected, actual in (
                        (managed["expected_head_oid"], item.head_oid),
                        (managed["expected_tree_oid"], item.tree_oid),
                        (managed["branch_ref"], item.branch_ref),
                    )
                )
                if state not in {"missing", "error"} and (not common_matches or not anchor_matches):
                    state = "drifted"
                entry = _working_copy_payload(item, managed=True, effective_mode=effective)
                entry.update({"working_copy_id": managed["working_copy_id"], "controller_owned": bool(managed["controller_owned"]), "state": state, "anchors_match": anchor_matches})
            else:
                # An unmanaged but verified linked worktree is an observation;
                # it does not inherit controller ownership or trusted writes.
                effective = policy.effective_mode(str(row["trust_mode"]), None) if common_matches and item.exists and state != "error" else None
                entry = _working_copy_payload(item, managed=False, effective_mode=effective)
                observed_state = state if (common_matches or state in {"missing", "error"}) else "drifted"
                entry.update({"working_copy_id": None, "controller_owned": False, "state": observed_state})
            entry["common_dir_matches"] = common_matches
            entries.append(entry)
    if not entries and observation.repository:
        item = GitWorktreeObservation(
            path=observation.repository.top_level or observation.repository.repository_path,
            head_oid=observation.repository.head_oid,
            tree_oid=observation.repository.tree_oid,
            branch_ref=observation.repository.branch_ref,
            detached=observation.repository.branch_ref is None,
            bare=observation.repository.is_bare,
            exists=True,
            git_dir=observation.repository.git_dir,
            common_dir=observation.repository.common_dir,
            object_format=observation.repository.object_format,
            status=observation.repository.status,
            state="ready" if observation.repository.head_oid is not None else "unknown",
        )
        entries.append(_working_copy_payload(item, managed=bool(managed_rows), effective_mode=None))
    return {
        "project_id": project_id,
        "state": observation.state,
        "observed_at": observation.observed_at,
        "worktrees": entries,
        "provenance": "controller-working-copy-inventory-v1",
    }


def reconcile_observe_only(store: Any, project_id: str, *, policy: ProjectPolicy | None = None) -> dict[str, Any]:
    """Persist only normalized observations/events; never repairs external state."""

    row = _project_row(store, project_id)
    policy = policy or load_policy()
    with shared_observation_lock(store.state_root, "project-" + project_id):
        observation = observe_project(row["primary_checkout"], policy, project_id=project_id)
    inventory = inventory_working_copies(store, project_id, policy=policy)
    project_state = observation.state
    primary_observation = next(
        (item for item in inventory["worktrees"] if item.get("path") == row["primary_checkout"]),
        None,
    )
    if primary_observation is not None:
        if primary_observation.get("state") == "drifted":
            project_state = "drifted"
        elif primary_observation.get("state") == "error" and project_state not in {"missing", "error"}:
            project_state = "error"
        elif primary_observation.get("state") == "missing" and project_state == "ready":
            project_state = "missing"
    persisted_observation = replace(observation, state=project_state)
    now = utc_now()
    with store.transaction():
        current = store.conn.execute("SELECT resource_version FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if current is None:
            raise KeyError(project_id)
        new_version = int(current[0]) + 1
        git = observation.repository
        updates = {
            "observed_state": project_state,
            "last_reconciled_at": observation.observed_at,
            "updated_at": now,
            "resource_version": new_version,
            "error_code": observation.error_code,
            "error_detail": observation.error_detail,
        }
        store.conn.execute(
            "UPDATE projects SET observed_state=?,last_reconciled_at=?,updated_at=?,resource_version=?,error_code=?,error_detail=? WHERE project_id=? AND resource_version=?",
            (updates["observed_state"], updates["last_reconciled_at"], updates["updated_at"], updates["resource_version"], updates["error_code"], updates["error_detail"], project_id, int(current[0])),
        )
        append_event_in_transaction(store.conn, event_kind="project.observed", resource_type="project", resource_id=project_id, resource_version=new_version, payload=_project_payload(persisted_observation))
        managed = list(store.conn.execute("SELECT * FROM working_copies WHERE project_id=?", (project_id,)))
        by_path = {str(item["path"]): item for item in inventory["worktrees"]}
        for managed_row in managed:
            item = by_path.get(str(managed_row["path"]))
            state = ("error" if observation.state == "error" else "missing") if item is None else str(item.get("state", "unknown"))
            wc_version = int(managed_row["resource_version"]) + 1
            store.conn.execute(
                "UPDATE working_copies SET observed_state=?,last_reconciled_at=?,updated_at=?,resource_version=?,error_code=?,error_detail=? WHERE working_copy_id=? AND resource_version=?",
                (state, now, now, wc_version, None, None, managed_row["working_copy_id"], int(managed_row["resource_version"])),
            )
            payload = item or {"path": managed_row["path"], "state": "missing", "provenance": "controller-working-copy-inventory-v1"}
            append_event_in_transaction(store.conn, event_kind="working_copy.observed", resource_type="working_copy", resource_id=managed_row["working_copy_id"], resource_version=wc_version, payload=payload)
    return inspect_project(store, project_id, policy=policy) | {"working_copies": inventory}


def plan_rebind(store: Any, project_id: str, candidate: os.PathLike[str] | str, *, policy: ProjectPolicy | None = None) -> dict[str, Any]:
    """Return an explicit rebind proof/plan without changing the registration."""

    row = _project_row(store, project_id)
    policy = policy or load_policy()
    observation = observe_project(candidate, policy, project_id=project_id)
    if observation.repository is None:
        return {"project_id": project_id, "candidate": str(candidate), "proof": "unavailable", "state": "ambiguous", "error": observation.error_detail}
    candidate_git = observation.repository
    old_common = Path(str(row["git_common_dir"]))
    new_common = Path(candidate_git.common_dir)
    old_identity = (int(row["git_common_device"]), int(row["git_common_inode"]))
    new_stat = new_common.stat()
    identity_matches = old_identity == (new_stat.st_dev, new_stat.st_ino)
    format_matches = candidate_git.object_format == row["object_format"]
    same_common = str(new_common) == str(old_common)
    anchor = store.conn.execute(
        "SELECT expected_head_oid,expected_tree_oid,branch_ref FROM working_copies WHERE project_id=? AND kind='primary' ORDER BY created_at LIMIT 1",
        (project_id,),
    ).fetchone()
    anchor_values = {
        "head_oid": anchor[0] if anchor is not None else None,
        "tree_oid": anchor[1] if anchor is not None else None,
        "branch_ref": anchor[2] if anchor is not None else None,
    }
    anchors_available = anchor is not None and any(value is not None for value in anchor_values.values())
    anchor_matches = anchors_available and all(
        expected == actual
        for expected, actual in (
            (anchor_values["head_oid"], candidate_git.head_oid),
            (anchor_values["tree_oid"], candidate_git.tree_oid),
            (anchor_values["branch_ref"], candidate_git.branch_ref),
        )
        if expected is not None
    )
    # Device/inode plus exact known Git anchors is the strongest local proof.
    # A copied clone may reproduce every object/branch value but cannot match
    # the registered common-directory identity; it therefore remains an
    # explicit, human-authorized rebind rather than an automatic identity move.
    if same_common and identity_matches and format_matches and anchor_matches:
        proof = "same-identity"
    elif identity_matches and format_matches and anchor_matches:
        proof = "explicit-intent-required"
    elif not identity_matches and format_matches and anchor_matches:
        proof = "copied-clone-or-unproven"
    elif identity_matches and format_matches:
        proof = "anchor-mismatch"
    else:
        proof = "insufficient-proof"
    return {
        "project_id": project_id,
        "candidate": candidate_git.as_dict(),
        "proof": proof,
        "requires_user_intent": proof != "same-identity",
        "identity_matches": identity_matches,
        "object_format_matches": format_matches,
        "anchor_matches": anchor_matches,
        "anchors_available": anchors_available,
        "known_anchors": anchor_values,
        "source_unchanged": True,
        "provenance": "controller-rebind-plan-v1",
    }


__all__ = [
    "ProjectObservation", "inspect_project", "inventory_working_copies", "observe_project",
    "plan_rebind", "reconcile_observe_only", "register_project",
]
