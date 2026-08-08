"""Durable Pi conversation lifecycle operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .events import append_event_in_transaction
from .models import bounded_text, new_id, utc_now, validate_id, validate_pi_session_id

_ROLES = {"secretary", "personal", "workstream", "review", "integration", "host"}


def create_conversation(store: Any, *, project_id: str, role: str, display_name: str, pi_session_id: str, session_file: str | None = None, working_copy_id: str | None = None) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    if role not in _ROLES:
        raise ValueError("conversation role is not supported")
    validate_pi_session_id(pi_session_id)
    project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if project is None:
        raise KeyError("project not found")
    if working_copy_id is not None:
        validate_id(working_copy_id, prefix="wc")
        wc = store.conn.execute("SELECT project_id FROM working_copies WHERE working_copy_id=?", (working_copy_id,)).fetchone()
        if wc is None or wc[0] != project_id:
            raise ValueError("working copy does not belong to project")
    conversation_id = new_id("conv")
    session_path = Path(session_file) if session_file is not None else Path(store.state_root) / "sessions" / project_id / f"{conversation_id}.jsonl"
    session_path = session_path.absolute()
    session_path.parent.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    with store.transaction():
        store.conn.execute("INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (conversation_id, project_id, working_copy_id, role, bounded_text(display_name, name="display_name", limit=512), pi_session_id, str(session_path), "active", "unknown", 1, now, now, None, None, None))
        append_event_in_transaction(store.conn, event_kind="conversation.created", resource_type="conversation", resource_id=conversation_id, resource_version=1, payload={"projectId": project_id, "role": role, "workingCopyId": working_copy_id})
    return dict(store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone())


def focus_conversation(store: Any, *, project_id: str, conversation_id: str) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(conversation_id, prefix="conv")
    row = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=? AND project_id=?", (conversation_id, project_id)).fetchone()
    if row is None:
        raise KeyError("conversation not found in project")
    return {"conversation": dict(row), "presentationOnly": True, "focus": conversation_id}


def archive_conversation(store: Any, *, project_id: str, conversation_id: str, expected_resource_version: int | None = None) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(conversation_id, prefix="conv")
    with store.transaction():
        row = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=? AND project_id=?", (conversation_id, project_id)).fetchone()
        if row is None:
            raise KeyError("conversation not found in project")
        if expected_resource_version is not None and int(row["resource_version"]) != expected_resource_version:
            raise ValueError("conversation resource version is stale")
        version = int(row["resource_version"]) + 1
        store.conn.execute("UPDATE conversations SET desired_state='archived',updated_at=?,resource_version=? WHERE conversation_id=?", (utc_now(), version, conversation_id))
        append_event_in_transaction(store.conn, event_kind="conversation.archived", resource_type="conversation", resource_id=conversation_id, resource_version=version, payload={"projectId": project_id})
        return dict(store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone())


__all__ = ["archive_conversation", "create_conversation", "focus_conversation"]
