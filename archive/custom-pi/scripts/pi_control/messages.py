"""Durable project-scoped worker/secretary messaging."""

from __future__ import annotations

from typing import Any, Mapping

from .events import append_event_in_transaction
from .models import canonical_json, json_digest, new_id, utc_now, validate_id


class ProjectMessageError(ValueError):
    pass


_KINDS = {"progress", "needs-user", "decision-reply", "review-requested", "failure", "interrupted", "submitted-change", "package-review-required", "package-review-complete"}


def _binding(store: Any, *, project_id: str, conversation_id: str, run_id: str, writer_generation: int | None) -> tuple[Any, Any]:
    row = store.conn.execute(
        "SELECT p.desired_state AS project_desired,p.observed_state AS project_observed,c.*,"
        "r.authority AS run_authority,r.desired_state AS run_desired,r.observed_state AS run_observed,"
        "r.writer_epoch AS run_writer_epoch,r.project_id AS run_project_id,r.conversation_id AS run_conversation_id,"
        "r.working_copy_id AS run_working_copy_id,w.active_writer_run_id,w.writer_epoch AS current_writer_epoch "
        "FROM projects p JOIN conversations c ON c.project_id=p.project_id JOIN runs r ON r.conversation_id=c.conversation_id "
        "LEFT JOIN working_copies w ON w.working_copy_id=r.working_copy_id "
        "WHERE p.project_id=? AND c.conversation_id=? AND r.run_id=?",
        (project_id, conversation_id, run_id),
    ).fetchone()
    if row is None or row["run_project_id"] != project_id or row["run_conversation_id"] != conversation_id:
        raise ProjectMessageError("message binding crosses project or conversation boundary")
    if row["project_desired"] != "active" or row["project_observed"] != "ready" or row["desired_state"] != "active" or row["observed_state"] != "ready" or row["run_desired"] != "running" or row["run_observed"] not in {"ready", "running"}:
        raise ProjectMessageError("message run, conversation, or project is stale or terminal")
    writer_role = row["role"] in {"personal", "workstream", "integration"}
    if writer_role:
        if not isinstance(writer_generation, int) or isinstance(writer_generation, bool) or writer_generation < 1:
            raise ProjectMessageError("writer message requires the exact current writer epoch")
        if row["run_authority"] != "writer-container" or row["run_writer_epoch"] != writer_generation or row["active_writer_run_id"] != run_id or row["current_writer_epoch"] != writer_generation:
            raise ProjectMessageError("writer message epoch or working-copy claim is stale")
    elif writer_generation is not None:
        raise ProjectMessageError("host role message cannot claim a writer epoch")
    return row, row


def post_message(store: Any, *, project_id: str, conversation_id: str, run_id: str, kind: str, payload: Mapping[str, Any], idempotency_key: str, workstream_id: str | None = None, writer_generation: int | None = None, request_id: str | None = None, reply_to_message_id: str | None = None) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(conversation_id, prefix="conv")
    validate_id(run_id, prefix="run")
    if kind not in _KINDS:
        raise ProjectMessageError("unsupported project message kind")
    if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 256 or "\x00" in idempotency_key:
        raise ProjectMessageError("idempotency key is invalid")
    payload_json = canonical_json(dict(payload), max_bytes=32 * 1024)
    digest = json_digest({"projectId": project_id, "conversationId": conversation_id, "runId": run_id, "kind": kind, "payload": dict(payload), "writerGeneration": writer_generation, "replyTo": reply_to_message_id})
    with store.transaction():
        _binding(store, project_id=project_id, conversation_id=conversation_id, run_id=run_id, writer_generation=writer_generation)
        if workstream_id is not None:
            validate_id(workstream_id, prefix="ws")
            ws = store.conn.execute("SELECT project_id,conversation_id FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()
            if ws is None or ws["project_id"] != project_id or ws["conversation_id"] != conversation_id:
                raise ProjectMessageError("workstream does not match the authenticated conversation")
        existing = store.conn.execute("SELECT * FROM project_messages WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing is not None:
            existing_digest = json_digest({"projectId": existing["project_id"], "conversationId": existing["conversation_id"], "runId": existing["run_id"], "kind": existing["kind"], "payload": __import__('json').loads(existing["payload_json"]), "writerGeneration": existing["writer_generation"], "replyTo": existing["reply_to_message_id"]})
            if existing_digest != digest:
                raise ProjectMessageError("idempotency key was reused with different content")
            return dict(existing)
        if reply_to_message_id is not None:
            validate_id(reply_to_message_id, prefix="msg")
            target = store.conn.execute("SELECT project_id FROM project_messages WHERE message_id=?", (reply_to_message_id,)).fetchone()
            if target is None or target[0] != project_id:
                raise ProjectMessageError("reply target does not belong to project")
        message_id = new_id("msg")
        now = utc_now()
        store.conn.execute("INSERT INTO project_messages(message_id,project_id,workstream_id,conversation_id,run_id,writer_generation,kind,request_id,idempotency_key,payload_json,state,created_at,delivered_at,acknowledged_at,resolved_at,reply_to_message_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (message_id, project_id, workstream_id, conversation_id, run_id, writer_generation, kind, request_id, idempotency_key, payload_json, "pending", now, None, None, None, reply_to_message_id))
        append_event_in_transaction(store.conn, event_kind="project-message.posted", resource_type="project-message", resource_id=message_id, resource_version=None, payload={"projectId": project_id, "conversationId": conversation_id, "runId": run_id, "kind": kind})
        return dict(store.conn.execute("SELECT * FROM project_messages WHERE message_id=?", (message_id,)).fetchone())


def list_messages(store: Any, *, project_id: str, conversation_id: str | None = None, states: set[str] | None = None, limit: int = 256) -> list[dict[str, Any]]:
    validate_id(project_id, prefix="prj")
    if not isinstance(limit, int) or limit < 1 or limit > 1000:
        raise ProjectMessageError("message limit is invalid")
    clauses = ["project_id=?"]
    values: list[Any] = [project_id]
    if conversation_id is not None:
        validate_id(conversation_id, prefix="conv")
        clauses.append("conversation_id=?")
        values.append(conversation_id)
    if states:
        if not states.issubset({"pending", "delivered", "acknowledged", "resolved"}):
            raise ProjectMessageError("message state is invalid")
        clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
        values.extend(sorted(states))
    values.append(limit)
    return [dict(row) for row in store.conn.execute("SELECT * FROM project_messages WHERE " + " AND ".join(clauses) + " ORDER BY created_at LIMIT ?", values)]


def mark_delivered(store: Any, *, project_id: str, message_id: str, conversation_id: str | None = None, run_id: str | None = None, writer_generation: int | None = None) -> dict[str, Any]:
    return _transition(store, project_id=project_id, message_id=message_id, state="delivered", field="delivered_at", conversation_id=conversation_id, run_id=run_id, writer_generation=writer_generation)


def acknowledge_message(store: Any, *, project_id: str, message_id: str, resolve: bool = False, conversation_id: str | None = None, run_id: str | None = None, writer_generation: int | None = None) -> dict[str, Any]:
    return _transition(store, project_id=project_id, message_id=message_id, state="resolved" if resolve else "acknowledged", field="resolved_at" if resolve else "acknowledged_at", conversation_id=conversation_id, run_id=run_id, writer_generation=writer_generation)


def _transition(store: Any, *, project_id: str, message_id: str, state: str, field: str, conversation_id: str | None, run_id: str | None, writer_generation: int | None) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(message_id, prefix="msg")
    with store.transaction():
        if (conversation_id is None) != (run_id is None):
            raise ProjectMessageError("message transition binding is incomplete")
        if conversation_id is not None and run_id is not None:
            _binding(store, project_id=project_id, conversation_id=conversation_id, run_id=run_id, writer_generation=writer_generation)
        row = store.conn.execute("SELECT * FROM project_messages WHERE message_id=? AND project_id=?", (message_id, project_id)).fetchone()
        if row is None:
            raise ProjectMessageError("message not found in project")
        allowed = {
            "delivered": {"pending", "delivered"},
            "acknowledged": {"pending", "delivered", "acknowledged"},
            "resolved": {"pending", "delivered", "acknowledged", "resolved"},
        }
        if row["state"] not in allowed[state]:
            raise ProjectMessageError("message transition would move a terminal state backward")
        if row["state"] == state:
            return dict(row)
        now = utc_now()
        store.conn.execute(f"UPDATE project_messages SET state=?,{field}=? WHERE message_id=?", (state, now, message_id))
        return dict(store.conn.execute("SELECT * FROM project_messages WHERE message_id=?", (message_id,)).fetchone())


def reply_message(store: Any, *, project_id: str, target_message_id: str, conversation_id: str, run_id: str, payload: Mapping[str, Any], idempotency_key: str, workstream_id: str | None = None, writer_generation: int | None = None) -> dict[str, Any]:
    return post_message(store, project_id=project_id, conversation_id=conversation_id, run_id=run_id, kind="decision-reply", payload=payload, idempotency_key=idempotency_key, workstream_id=workstream_id, writer_generation=writer_generation, reply_to_message_id=target_message_id)


__all__ = ["ProjectMessageError", "acknowledge_message", "list_messages", "mark_delivered", "post_message", "reply_message"]
