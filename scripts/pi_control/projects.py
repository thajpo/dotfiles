"""Project registration, scoped status, and the durable work index."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .events import append_event_in_transaction
from .git_adapter import GitObservationError, observe_repository
from .models import bounded_text, canonical_json, new_id, utc_now, validate_id, validate_pi_session_id
from .project_policy import ProjectPolicy, load_policy


def _project(store: Any, project_id: str) -> Mapping[str, Any]:
    validate_id(project_id, prefix="prj")
    row = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if row is None:
        raise KeyError(f"project not found: {project_id}")
    return row


def register_project(store: Any, repository: str | Path, display_name: str | None = None, *, policy: ProjectPolicy | None = None) -> dict[str, Any]:
    """Register one repository using only fresh controller state.

    Git observation is read-only.  A repository with a different Git common
    directory receives a different project identity, even when its checkout
    path happens to have the same basename.
    """
    policy = policy or load_policy()
    observation = observe_repository(repository)
    common = Path(observation.common_dir).resolve(strict=True)
    primary = Path(observation.top_level or observation.repository_path).resolve(strict=True)
    existing = store.conn.execute("SELECT * FROM projects WHERE git_common_dir=?", (str(common),)).fetchone()
    if existing is not None:
        return dict(existing)
    project_id = new_id("prj")
    working_copy_id = new_id("wc")
    conversation_id = new_id("conv")
    now = utc_now()
    name = bounded_text(display_name or primary.name, name="display_name", limit=512)
    trust = policy.trust_for_repository(primary)
    effective_mode = policy.effective_mode(trust)
    wc_state = "dirty" if observation.dirty else ("ready" if observation.head_oid else "unknown")
    session_dir = Path(store.state_root) / "sessions" / project_id
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / "secretary.jsonl"
    with store.transaction():
        store.conn.execute(
            "INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, name, str(common), common.stat().st_dev, common.stat().st_ino, str(primary), observation.object_format, trust, policy.policy_hash, "active", "ready" if observation.head_oid or observation.is_bare else "unknown", 1, now, now, now, None, None),
        )
        store.conn.execute(
            "INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,git_dir,branch_ref,expected_head_oid,expected_tree_oid,effective_mode,desired_state,observed_state,writer_epoch,active_writer_run_id,resource_version,controller_owned,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (working_copy_id, project_id, name, "primary", "personal", str(primary), observation.git_dir, observation.branch_ref, observation.head_oid, observation.tree_oid, effective_mode, "present", wc_state, 0, None, 1, 0, now, now, now, None, None),
        )
        store.conn.execute(
            "INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (conversation_id, project_id, working_copy_id, "secretary", f"{name} secretary", f"pi-secretary-{project_id}", str(session_file), "active", "ready", 1, now, now, now, None, None),
        )
        append_event_in_transaction(store.conn, event_kind="project.registered", resource_type="project", resource_id=project_id, resource_version=1, payload={"projectId": project_id, "repository": str(primary), "gitCommonDir": str(common), "authority": "greenfield-controller"})
    return dict(store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone())


def project_status(store: Any, project_id: str) -> dict[str, Any]:
    project = _project(store, project_id)
    worktrees = [dict(row) for row in store.conn.execute("SELECT * FROM working_copies WHERE project_id=? ORDER BY created_at", (project_id,))]
    conversations = [dict(row) for row in store.conn.execute("SELECT * FROM conversations WHERE project_id=? ORDER BY created_at", (project_id,))]
    runs = [dict(row) for row in store.conn.execute("SELECT * FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT 64", (project_id,))]
    messages = [dict(row) for row in store.conn.execute("SELECT * FROM project_messages WHERE project_id=? ORDER BY created_at DESC LIMIT 64", (project_id,))]
    attention = [dict(row) for row in store.conn.execute("SELECT * FROM attention WHERE project_id=? AND state='open' ORDER BY created_at", (project_id,))]
    return {"project": dict(project), "workingCopies": worktrees, "conversations": conversations, "runs": runs, "messages": messages, "attention": attention, "source": "pi-system-sqlite"}


def work_index(store: Any, project_id: str) -> dict[str, list[dict[str, Any]]]:
    _project(store, project_id)
    rows: dict[str, list[dict[str, Any]]] = {key: [] for key in ("Working now", "Investigations", "Changes ready for review", "Changes ready to merge", "Needs attention", "Integrated recently", "Unmanaged Git work")}
    for row in store.conn.execute("SELECT * FROM conversations WHERE project_id=? AND desired_state='active' ORDER BY updated_at DESC", (project_id,)):
        item = {"id": row["conversation_id"], "title": row["display_name"], "agentType": row["role"], "state": row["observed_state"], "lastUsefulUpdate": row["updated_at"], "focus": row["conversation_id"], "userActionRequired": False}
        if row["role"] in {"personal", "workstream", "integration"}:
            rows["Working now"].append(item)
        elif row["role"] == "secretary":
            rows["Working now"].append(item)
    for row in store.conn.execute("SELECT * FROM runs WHERE project_id=? AND observed_state IN ('running','needs_attention') ORDER BY updated_at DESC"):
        rows["Working now"].append({"id": row["run_id"], "title": "active run", "agentType": "run", "state": row["observed_state"], "lastUsefulUpdate": row["updated_at"], "focus": row["conversation_id"], "userActionRequired": row["observed_state"] == "needs_attention"})
    for row in store.conn.execute("SELECT * FROM project_messages WHERE project_id=? AND state IN ('pending','delivered') ORDER BY created_at DESC LIMIT 64", (project_id,)):
        item = {"id": row["message_id"], "title": row["kind"], "agentType": "message", "state": row["state"], "lastUsefulUpdate": row["created_at"], "focus": row["conversation_id"], "userActionRequired": row["kind"] == "needs-user"}
        rows["Needs attention" if row["kind"] in {"needs-user", "failure", "interrupted"} else "Working now"].append(item)
    for row in store.conn.execute("SELECT * FROM changes WHERE project_id=? ORDER BY updated_at DESC", (project_id,)):
        item = {"id": row["change_id"], "title": row["title"], "agentType": "change", "state": row["state"], "lastUsefulUpdate": row["updated_at"], "focus": row["change_id"], "userActionRequired": row["state"] == "open"}
        if row["state"] == "open":
            accepted = store.conn.execute("SELECT 1 FROM reviews WHERE change_id=? AND revision=? AND state='submitted' AND verdict='accept' LIMIT 1", (row["change_id"], row["current_revision"])).fetchone()
            rows["Changes ready to merge" if accepted else "Changes ready for review"].append(item)
        elif row["state"] == "merged":
            rows["Integrated recently"].append(item)
    return rows


__all__ = ["project_status", "register_project", "work_index"]
