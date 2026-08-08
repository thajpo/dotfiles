"""Exact, one-use host and container-network command requests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .events import append_event_in_transaction
from .models import canonical_json, json_digest, new_id, utc_now, validate_id


class CommandRequestError(ValueError):
    pass


def _expires(ms: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(milliseconds=ms)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _binding(store: Any, project_id: str, conversation_id: str, run_id: str) -> Any:
    row = store.conn.execute("SELECT c.*,r.writer_epoch AS run_writer_epoch,r.project_id AS run_project_id,r.conversation_id AS run_conversation_id FROM conversations c JOIN runs r ON r.conversation_id=c.conversation_id WHERE c.conversation_id=? AND r.run_id=?", (conversation_id, run_id)).fetchone()
    if row is None or row["project_id"] != project_id or row["run_project_id"] != project_id:
        raise CommandRequestError("command binding crosses project boundary")
    if row["role"] not in {"personal", "workstream", "integration"} or row["run_writer_epoch"] is None:
        raise CommandRequestError("only active coding runs may request commands")
    return row


def request_command(store: Any, *, project_id: str, conversation_id: str, run_id: str, execution_place: str, command: Sequence[str] | str, working_directory: str, required_resource: str, purpose: str, expected_effect: str, change_scope: Mapping[str, Any], expected_output: str = "", sensitive_output: bool = False, expected_duration_ms: int = 30000, workstream_id: str | None = None) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(conversation_id, prefix="conv")
    validate_id(run_id, prefix="run")
    if execution_place not in {"container-network", "host"}:
        raise CommandRequestError("execution place is invalid")
    if isinstance(command, str):
        command_value: list[str] = [command]
    elif isinstance(command, Sequence) and not isinstance(command, (bytes, bytearray)) and command and all(isinstance(item, str) and item and "\x00" not in item for item in command):
        command_value = list(command)
    else:
        raise CommandRequestError("command must be an exact non-empty argv")
    if not isinstance(working_directory, str) or not working_directory.startswith("/") or "\x00" in working_directory:
        raise CommandRequestError("working directory must be absolute")
    if not isinstance(expected_duration_ms, int) or not 1 <= expected_duration_ms <= 3600000:
        raise CommandRequestError("expected duration is outside its bound")
    binding = _binding(store, project_id, conversation_id, run_id)
    if workstream_id is not None:
        validate_id(workstream_id, prefix="ws")
        ws = store.conn.execute("SELECT project_id FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()
        if ws is None or ws[0] != project_id:
            raise CommandRequestError("workstream does not belong to project")
    body = {"projectId": project_id, "conversationId": conversation_id, "runId": run_id, "writerGeneration": int(binding["run_writer_epoch"]), "executionPlace": execution_place, "command": command_value, "workingDirectory": working_directory, "requiredResource": required_resource, "purpose": purpose, "expectedEffect": expected_effect, "changeScope": dict(change_scope), "expectedOutput": expected_output, "sensitiveOutput": bool(sensitive_output), "expectedDurationMs": expected_duration_ms}
    digest = json_digest(body)
    request_id = new_id("cmd")
    now = utc_now()
    with store.transaction():
        store.conn.execute("INSERT INTO command_requests(command_request_id,project_id,workstream_id,conversation_id,run_id,writer_generation,execution_place,command_json,working_directory,required_resource,purpose,expected_effect,change_scope_json,expected_output,sensitive_output,expected_duration_ms,request_digest,state,authorization_id,result_json,created_at,expires_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (request_id, project_id, workstream_id, conversation_id, run_id, int(binding["run_writer_epoch"]), execution_place, canonical_json(command_value), working_directory, required_resource, purpose, expected_effect, canonical_json(dict(change_scope)), expected_output, int(bool(sensitive_output)), expected_duration_ms, digest, "requested", None, None, now, _expires(expected_duration_ms), None))
        append_event_in_transaction(store.conn, event_kind="command.requested", resource_type="command-request", resource_id=request_id, payload={"projectId": project_id, "executionPlace": execution_place, "requestDigest": digest})
        return dict(store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=?", (request_id,)).fetchone())


def authorize_command(store: Any, *, project_id: str, command_request_id: str, request_digest: str, actor_id: str, scope: Mapping[str, Any] | None = None, authorization_id: str | None = None) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(command_request_id, prefix="cmd")
    with store.transaction():
        request = store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=? AND project_id=?", (command_request_id, project_id)).fetchone()
        if request is None:
            raise CommandRequestError("command request not found")
        if request["request_digest"] != request_digest or request["state"] != "requested":
            raise CommandRequestError("command request digest or state is stale")
        kind = "host-command" if request["execution_place"] == "host" else "container-network-command"
        auth_id = authorization_id or new_id("auth")
        scope_value = dict(scope or {"requestDigest": request_digest, "commandRequestId": command_request_id})
        scope_json = canonical_json(scope_value)
        now = utc_now()
        store.conn.execute("INSERT INTO authorizations(authorization_id,kind,actor_type,actor_id,project_id,resource_type,resource_id,request_context_id,scope_json,scope_digest,issued_at,expires_at,consumed_at,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (auth_id, kind, "user", actor_id, project_id, "command-request", command_request_id, command_request_id, scope_json, json_digest(scope_value), now, request["expires_at"], None, "active"))
        store.conn.execute("UPDATE command_requests SET state='approved',authorization_id=? WHERE command_request_id=?", (auth_id, command_request_id))
        append_event_in_transaction(store.conn, event_kind="command.authorized", resource_type="command-request", resource_id=command_request_id, payload={"projectId": project_id, "authorizationId": auth_id, "requestDigest": request_digest})
        return dict(store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=?", (command_request_id,)).fetchone())


def reject_command(store: Any, *, project_id: str, command_request_id: str, reason: str) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(command_request_id, prefix="cmd")
    with store.transaction():
        row = store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=? AND project_id=?", (command_request_id, project_id)).fetchone()
        if row is None:
            raise CommandRequestError("command request not found")
        if row["state"] != "requested":
            raise CommandRequestError("command request is not pending")
        store.conn.execute("UPDATE command_requests SET state='rejected',result_json=?,completed_at=? WHERE command_request_id=?", (canonical_json({"reason": reason[:1024]}), utc_now(), command_request_id))
        return dict(store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=?", (command_request_id,)).fetchone())


def consume_authorization(store: Any, *, project_id: str, command_request_id: str, request_digest: str) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    with store.transaction():
        row = store.conn.execute("SELECT r.*,a.state AS authorization_state,a.kind AS authorization_kind FROM command_requests r JOIN authorizations a ON a.authorization_id=r.authorization_id WHERE r.command_request_id=? AND r.project_id=?", (command_request_id, project_id)).fetchone()
        if row is None or row["request_digest"] != request_digest or row["state"] != "approved" or row["authorization_state"] != "active":
            raise CommandRequestError("command approval is missing, stale, or already used")
        store.conn.execute("UPDATE authorizations SET state='consumed',consumed_at=? WHERE authorization_id=? AND state='active'", (utc_now(), row["authorization_id"]))
        store.conn.execute("UPDATE command_requests SET state='running' WHERE command_request_id=?", (command_request_id,))
        return dict(store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=?", (command_request_id,)).fetchone())


__all__ = ["CommandRequestError", "authorize_command", "consume_authorization", "reject_command", "request_command"]
