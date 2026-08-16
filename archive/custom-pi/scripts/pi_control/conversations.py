"""Durable Pi conversation lifecycle operations."""

from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Any

from .events import append_event_in_transaction
from .models import bounded_text, json_digest, new_id, utc_now, validate_id
from .operations import update_operation_in_transaction
from .role_profiles import role_profile, validate_role_assignment


def conversation_session_binding(store: Any, project_id: str, conversation_id: str) -> tuple[str, str]:
    """Derive and confine a Pi session identity without creating its file."""

    validate_id(project_id, prefix="prj")
    validate_id(conversation_id, prefix="conv")
    sessions = Path(store.state_root) / "sessions"
    project_sessions = sessions / project_id
    for directory in (sessions, project_sessions):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            directory.mkdir(mode=0o700, parents=False)
            info = directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("session root is symlinked or unsafe")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise ValueError("session root is not user-owned")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("session root must be mode 0700")
    session_path = project_sessions / f"{conversation_id}.jsonl"
    if session_path.exists() or session_path.is_symlink():
        raise ValueError("derived session path already exists")
    if session_path.parent.resolve(strict=True) != project_sessions.resolve(strict=True):
        raise ValueError("derived session path escapes its project root")
    return f"pi-{conversation_id}", str(session_path.absolute())


def create_conversation(store: Any, *, project_id: str, role: str, display_name: str, working_copy_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    profile = role_profile(role)
    project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if project is None:
        raise KeyError("project not found")
    wc = None
    if role == "personal":
        primary = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary' AND purpose='personal' AND desired_state='present'", (project_id,)).fetchone()
        if primary is None:
            raise ValueError("personal conversation requires the controller-derived primary working copy")
        if working_copy_id is not None and working_copy_id != primary["working_copy_id"]:
            raise ValueError("personal conversation cannot select a non-primary working copy")
        working_copy_id = str(primary["working_copy_id"])
    if working_copy_id is not None:
        validate_id(working_copy_id, prefix="wc")
        wc = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (working_copy_id,)).fetchone()
        if wc is None or wc["project_id"] != project_id:
            raise ValueError("working copy does not belong to project")
    validate_role_assignment(profile, wc)
    request = {"projectId": project_id, "role": role, "displayName": display_name, "workingCopyId": working_copy_id}
    if idempotency_key is not None:
        existing = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing is not None:
            if existing["kind"] != "conversation.create" or existing["request_digest"] != json_digest(request):
                raise ValueError("conversation idempotency key is bound to another request")
            row = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (existing["resource_id"],)).fetchone()
            if row is None:
                raise ValueError("conversation creation operation is incomplete")
            return dict(row)
    conversation_id = new_id("conv")
    pi_session_id, session_file = conversation_session_binding(store, project_id, conversation_id)
    operation = store.create_operation(idempotency_key=idempotency_key, kind="conversation.create", resource_type="conversation", resource_id=conversation_id, actor_type="controller", request=request) if idempotency_key is not None else None
    now = utc_now()
    with store.transaction():
        store.conn.execute("INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,authority_profile,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (conversation_id, project_id, working_copy_id, role, profile.authority_profile, bounded_text(display_name, name="display_name", limit=512), pi_session_id, session_file, "active", "ready", 1, now, now, now, None, None))
        append_event_in_transaction(store.conn, event_kind="conversation.created", resource_type="conversation", resource_id=conversation_id, resource_version=1, payload={"projectId": project_id, "role": role, "workingCopyId": working_copy_id})
        if operation is not None:
            update_operation_in_transaction(store.conn, operation.operation_id, state="succeeded", step="conversation-created", result={"conversationId": conversation_id})
    if role == "personal" and working_copy_id is not None:
        environment_root = Path(store.state_root) / "environments" / working_copy_id
        environment_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(environment_root, 0o700)
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


__all__ = ["archive_conversation", "conversation_session_binding", "create_conversation", "focus_conversation"]
