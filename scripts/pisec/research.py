"""Bounded immutable task packets and durable worker research threads."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .events import append_event_in_transaction
from .models import (
    ConflictError,
    IdempotencyConflictError,
    InvalidRequestError,
    NotFoundError,
    bounded_text,
    canonical_json,
    json_digest,
    new_id,
    utc_now,
    validate_git_oid,
    validate_id,
    validate_sha256,
)

PACKET_MAX_BYTES = 32 * 1024
PACKET_MAX_ITEMS = 16
PACKET_TEXT_LIMIT = 4096
URL_MAX = 2048
TASK_PACKET_KEYS = frozenset({"schemaVersion", "outcome", "boundaries", "acceptance", "openQuestions", "evidence"})
EXECUTION_PACKET_KEYS = frozenset({
    "projectId", "workstreamId", "title", "purpose", "brief", "targetRef", "baseCommitOid",
    "branchName", "executionProfile", "harnessId", "workspaceAdapterId", "implementationModel", "harnessModel", "reasoningEffort", "nonEffects", "approvalScopeSha256",
})
RESEARCH_REQUEST_KEYS = frozenset({"kind", "summary", "question", "context", "attempted", "candidateSources", "blocking"})
RESEARCH_RESULT_KEYS = frozenset({"schemaVersion", "findings", "sources", "uncertainties"})
RESEARCH_CONTEXT_KEYS = frozenset({"context", "attempted", "candidateSources"})
RESEARCH_NEEDS_CONTEXT_KEYS = frozenset({"schemaVersion", "message", "missing"})
RESEARCH_DECLINE_KEYS = frozenset({"reason"})
RESEARCH_STATES = frozenset({"pending", "researching", "needs_context", "answered", "declined", "acknowledged"})


def _require_object(value: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{name} must be an object")
    return dict(value)


def _bounded_list(value: Any, *, name: str, limit: int = PACKET_MAX_ITEMS) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise InvalidRequestError(f"{name} must be a list of at most {limit} items")
    return [bounded_text(item, name=f"{name}[]", limit=PACKET_TEXT_LIMIT) for item in value]


def _url(value: Any, *, name: str) -> str:
    text = bounded_text(value, name=name, limit=URL_MAX)
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise InvalidRequestError(f"{name} must be an http or https URL")
    if any(ord(char) < 0x21 for char in text):
        raise InvalidRequestError(f"{name} contains unsafe whitespace")
    return text


def _url_list(value: Any, *, name: str) -> list[str]:
    if not isinstance(value, list) or len(value) > PACKET_MAX_ITEMS:
        raise InvalidRequestError(f"{name} must contain at most {PACKET_MAX_ITEMS} URLs")
    result = [_url(item, name=f"{name}[]") for item in value]
    if len(result) != len(set(result)):
        raise InvalidRequestError(f"{name} must not contain duplicates")
    return result


def _packet_json(value: Mapping[str, Any]) -> str:
    return canonical_json(value, max_bytes=PACKET_MAX_BYTES, max_text=PACKET_TEXT_LIMIT)


def validate_task_packet(value: Any) -> dict[str, Any]:
    packet = _require_object(value, name="taskPacket")
    if set(packet) != TASK_PACKET_KEYS:
        raise InvalidRequestError("taskPacket fields do not match the exact contract")
    version = packet["schemaVersion"]
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise InvalidRequestError("taskPacket schemaVersion must be 1")
    normalized = {
        "schemaVersion": 1,
        "outcome": bounded_text(packet["outcome"], name="taskPacket.outcome", limit=PACKET_TEXT_LIMIT),
        "boundaries": _bounded_list(packet["boundaries"], name="taskPacket.boundaries"),
        "acceptance": _bounded_list(packet["acceptance"], name="taskPacket.acceptance"),
        "openQuestions": _bounded_list(packet["openQuestions"], name="taskPacket.openQuestions"),
        "evidence": _bounded_list(packet["evidence"], name="taskPacket.evidence"),
    }
    _packet_json(normalized)
    return normalized


def _execution_packet(value: Any) -> dict[str, Any]:
    execution = _require_object(value, name="taskPacket.execution")
    if set(execution) != EXECUTION_PACKET_KEYS:
        raise InvalidRequestError("broker execution identity fields are invalid")
    for key, prefix in (("projectId", "prj"), ("workstreamId", "ws")):
        validate_id(execution[key], prefix=prefix)
    for key, limit in (("title", 512), ("purpose", PACKET_TEXT_LIMIT), ("brief", PACKET_TEXT_LIMIT), ("targetRef", 512), ("branchName", 512), ("executionProfile", 128), ("harnessId", 64), ("workspaceAdapterId", 64)):
        bounded_text(execution[key], name=f"taskPacket.execution.{key}", limit=limit)
    for key in ("implementationModel", "harnessModel"):
        if execution[key] is not None:
            bounded_text(execution[key], name=f"taskPacket.execution.{key}", limit=256)
    if execution["reasoningEffort"] is not None and execution["reasoningEffort"] not in {"low", "medium", "high", "xhigh"}:
        raise InvalidRequestError("taskPacket.execution.reasoningEffort is invalid")
    validate_git_oid(execution["baseCommitOid"], "taskPacket.execution.baseCommitOid")
    normalized = dict(execution)
    normalized["nonEffects"] = _bounded_list(execution["nonEffects"], name="taskPacket.execution.nonEffects")
    validate_sha256(execution["approvalScopeSha256"], "taskPacket.execution.approvalScopeSha256")
    return normalized


def build_committed_task_packet(task_packet: Any, execution: Mapping[str, Any]) -> dict[str, Any]:
    """Build the broker-owned packet persisted before worker runtime starts."""
    result = {"schemaVersion": 1, "taskPacket": validate_task_packet(task_packet), "execution": _execution_packet(execution)}
    _packet_json(result)
    return result


def validate_research_request(value: Any) -> dict[str, Any]:
    request = _require_object(value, name="research request")
    if set(request) != RESEARCH_REQUEST_KEYS:
        raise InvalidRequestError("research request fields do not match the exact contract")
    if request["kind"] != "research":
        raise InvalidRequestError("research request kind must be research")
    if not isinstance(request["blocking"], bool):
        raise InvalidRequestError("research request blocking must be boolean")
    normalized = {
        "kind": "research",
        "summary": bounded_text(request["summary"], name="research.summary", limit=1024),
        "question": bounded_text(request["question"], name="research.question", limit=PACKET_TEXT_LIMIT),
        "context": bounded_text(request["context"], name="research.context", limit=PACKET_TEXT_LIMIT, allow_empty=True),
        "attempted": _bounded_list(request["attempted"], name="research.attempted"),
        "candidateSources": _url_list(request["candidateSources"], name="research.candidateSources"),
        "blocking": request["blocking"],
    }
    _packet_json(normalized)
    return normalized


def validate_research_context(value: Any) -> dict[str, Any]:
    context = _require_object(value, name="research context")
    if set(context) != RESEARCH_CONTEXT_KEYS:
        raise InvalidRequestError("research context fields do not match the exact contract")
    normalized = {
        "context": bounded_text(context["context"], name="research.context", limit=PACKET_TEXT_LIMIT),
        "attempted": _bounded_list(context["attempted"], name="research.attempted"),
        "candidateSources": _url_list(context["candidateSources"], name="research.candidateSources"),
    }
    _packet_json(normalized)
    return normalized


def validate_research_needs_context(value: Any) -> dict[str, Any]:
    context = _require_object(value, name="research context request")
    if set(context) != RESEARCH_NEEDS_CONTEXT_KEYS:
        raise InvalidRequestError("research context request fields do not match the exact contract")
    version = context["schemaVersion"]
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise InvalidRequestError("research context request schemaVersion must be 1")
    result = {"schemaVersion": 1, "message": bounded_text(context["message"], name="research context message", limit=PACKET_TEXT_LIMIT), "missing": _bounded_list(context["missing"], name="research context missing")}
    _packet_json(result)
    return result


def validate_research_result(value: Any) -> dict[str, Any]:
    result = _require_object(value, name="research result")
    if set(result) != RESEARCH_RESULT_KEYS:
        raise InvalidRequestError("research result fields do not match the exact contract")
    version = result["schemaVersion"]
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise InvalidRequestError("research result schemaVersion must be 1")
    sources_value = result["sources"]
    if not isinstance(sources_value, list) or len(sources_value) > PACKET_MAX_ITEMS:
        raise InvalidRequestError("research result sources must contain at most 16 entries")
    sources: list[dict[str, str]] = []
    for source in sources_value:
        item = _require_object(source, name="research result source")
        if set(item) != {"url", "title", "excerpt"}:
            raise InvalidRequestError("research result source fields are invalid")
        sources.append({
            "url": _url(item["url"], name="research source URL"),
            "title": bounded_text(item["title"], name="research source title", limit=1024),
            "excerpt": bounded_text(item["excerpt"], name="research source excerpt", limit=PACKET_TEXT_LIMIT),
        })
    result_value = {
        "schemaVersion": 1,
        "findings": _bounded_list(result["findings"], name="research.findings"),
        "sources": sources,
        "uncertainties": _bounded_list(result["uncertainties"], name="research.uncertainties"),
    }
    _packet_json(result_value)
    return result_value


def validate_research_decline(value: Any) -> dict[str, str]:
    decline = _require_object(value, name="research decline")
    if set(decline) != RESEARCH_DECLINE_KEYS:
        raise InvalidRequestError("research decline fields do not match the exact contract")
    result = {"reason": bounded_text(decline["reason"], name="research decline reason", limit=PACKET_TEXT_LIMIT)}
    _packet_json(result)
    return result


def _workstream(store: Any, project_id: str, workstream_id: str, *, worker_only: bool = False) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(workstream_id, prefix="ws")
    row = store.conn.execute("SELECT * FROM workstreams WHERE project_id=? AND workstream_id=?", (project_id, workstream_id)).fetchone()
    if row is None or (worker_only and row["kind"] != "worker"):
        raise NotFoundError("workstream was not found in the project")
    if row["desired_state"] == "retired":
        raise ConflictError("workstream is retired")
    return dict(row)


def _task_packet_row(store: Any, project_id: str, workstream_id: str) -> dict[str, Any]:
    row = store.conn.execute("SELECT t.*,w.kind,w.desired_state FROM task_packets t JOIN workstreams w USING(workstream_id) WHERE t.project_id=? AND t.workstream_id=?", (project_id, workstream_id)).fetchone()
    if row is None:
        raise NotFoundError("workstream task packet was not found")
    return dict(row)


def _packet_row(connection: Any, request_id: str, actor: str, idempotency_key: str, payload: Mapping[str, Any], kind: str) -> dict[str, Any]:
    payload_json = _packet_json(payload)
    payload_sha = json_digest(payload)
    existing = connection.execute("SELECT * FROM research_packets WHERE request_id=? AND actor=? AND idempotency_key=?", (request_id, actor, idempotency_key)).fetchone()
    if existing is not None:
        if existing["payload_sha256"] != payload_sha or existing["kind"] != kind:
            raise IdempotencyConflictError("research packet idempotency key is already bound to another payload")
        return dict(existing)
    packet_id = new_id("rpk")
    created = utc_now()
    connection.execute(
        "INSERT INTO research_packets(packet_id,request_id,actor,kind,idempotency_key,payload_json,payload_sha256,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (packet_id, request_id, actor, kind, idempotency_key, payload_json, payload_sha, created),
    )
    return dict(connection.execute("SELECT * FROM research_packets WHERE packet_id=?", (packet_id,)).fetchone())

def _single_packet(
    connection: Any,
    request_id: str,
    actor: str,
    kind: str,
    idempotency_key: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one semantic packet, replaying an equivalent terminal packet."""
    payload_sha = json_digest(payload)
    existing = connection.execute(
        "SELECT * FROM research_packets WHERE request_id=? AND actor=? AND kind=? ORDER BY sequence DESC LIMIT 1",
        (request_id, actor, kind),
    ).fetchone()
    if existing is not None:
        if existing["payload_sha256"] != payload_sha:
            raise IdempotencyConflictError("research packet already has a different payload")
        return dict(existing)
    return _packet_row(connection, request_id, actor, idempotency_key, payload, kind)

def _increment_inbox(connection: Any, project_id: str) -> int:
    now = utc_now()
    row = connection.execute("SELECT generation FROM research_inbox WHERE project_id=?", (project_id,)).fetchone()
    if row is None:
        connection.execute("INSERT INTO research_inbox(project_id,generation,notified_generation,updated_at) VALUES(?,?,?,?)", (project_id, 1, 0, now))
        return 1
    generation = int(row["generation"]) + 1
    connection.execute("UPDATE research_inbox SET generation=?,updated_at=? WHERE project_id=?", (generation, now, project_id))
    return generation


def _request_projection(store: Any, row: Mapping[str, Any], *, include_packets: bool = True) -> dict[str, Any]:
    result = {key: row[key] for key in ("request_id", "project_id", "workstream_id", "task_packet_id", "idempotency_key", "request_sha256", "state", "claimed_by_secretary_workstream_id", "created_at", "updated_at", "answered_at", "acknowledged_at")}
    packets = []
    for packet in store.conn.execute("SELECT packet_id,request_id,actor,kind,idempotency_key,payload_json,payload_sha256,created_at FROM research_packets WHERE request_id=? ORDER BY sequence", (row["request_id"],)):
        item = dict(packet)
        if include_packets:
            item["payload"] = json.loads(item.pop("payload_json"))
        else:
            item.pop("payload_json", None)
        packets.append(item)
    result["packets"] = packets
    result["packetCount"] = len(packets)
    return result


def get_task_packet(store: Any, project_id: str, workstream_id: str) -> dict[str, Any]:
    _workstream(store, project_id, workstream_id, worker_only=True)
    row = _task_packet_row(store, project_id, workstream_id)
    return {"taskPacketId": row["task_packet_id"], "projectId": row["project_id"], "workstreamId": row["workstream_id"], "scopeSha256": row["scope_sha256"], "packetSha256": row["packet_sha256"], "issuedAt": row["issued_at"], "packet": json.loads(row["packet_json"])}


def issue_task_packet_in_transaction(connection: Any, *, scope: Mapping[str, Any]) -> dict[str, Any]:
    packet = build_committed_task_packet(scope["taskPacket"], {
        "projectId": scope["projectId"],
        "workstreamId": scope["workstreamId"],
        "title": scope["title"],
        "purpose": scope["purpose"],
        "brief": scope["brief"],
        "targetRef": scope["targetRef"],
        "baseCommitOid": scope["baseCommitOid"],
        "branchName": scope["branchName"],
        "executionProfile": scope["executionProfile"],
        "harnessId": scope["harnessId"],
        "workspaceAdapterId": scope["workspaceAdapterId"],
        "implementationModel": scope.get("implementationModel"),
        "harnessModel": scope.get("harnessModel"),
        "reasoningEffort": scope.get("reasoningEffort"),
        "nonEffects": scope["nonEffects"],
        "approvalScopeSha256": json_digest(scope),
    })
    packet_json = _packet_json(packet)
    packet_sha = json_digest(packet)
    existing = connection.execute("SELECT * FROM task_packets WHERE workstream_id=?", (scope["workstreamId"],)).fetchone()
    if existing is not None:
        if existing["scope_sha256"] != json_digest(scope) or existing["packet_sha256"] != packet_sha or existing["packet_json"] != packet_json:
            raise ConflictError("immutable task packet differs from the approved scope")
        return dict(existing)
    packet_id = new_id("tp")
    issued = utc_now()
    connection.execute(
        "INSERT INTO task_packets(task_packet_id,project_id,workstream_id,scope_sha256,packet_json,packet_sha256,issued_at) VALUES(?,?,?,?,?,?,?)",
        (packet_id, scope["projectId"], scope["workstreamId"], json_digest(scope), packet_json, packet_sha, issued),
    )
    return dict(connection.execute("SELECT * FROM task_packets WHERE task_packet_id=?", (packet_id,)).fetchone())


def request_research(store: Any, *, project_id: str, workstream_id: str, idempotency_key: str, request: Mapping[str, Any]) -> dict[str, Any]:
    worker = _workstream(store, project_id, workstream_id, worker_only=True)
    task = _task_packet_row(store, project_id, workstream_id)
    key = bounded_text(idempotency_key, name="research idempotency key", limit=256)
    normalized = validate_research_request(request)
    digest = json_digest(normalized)
    existing = store.conn.execute("SELECT * FROM research_requests WHERE workstream_id=? AND idempotency_key=?", (workstream_id, key)).fetchone()
    if existing is not None:
        if existing["request_sha256"] != digest:
            raise IdempotencyConflictError("research idempotency key is already bound to another request")
        return _request_projection(store, existing)
    now = utc_now()
    request_id = new_id("wrq")
    with store.transaction():
        store.conn.execute(
            "INSERT INTO research_requests(request_id,project_id,workstream_id,task_packet_id,idempotency_key,request_sha256,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?, ?,?)",
            (request_id, project_id, workstream_id, task["task_packet_id"], key, digest, "pending", now, now),
        )
        _packet_row(store.conn, request_id, "worker", key, normalized, "request")
        generation = _increment_inbox(store.conn, project_id)
        append_event_in_transaction(store.conn, kind="research.requested", project_id=project_id, workstream_id=workstream_id, payload={"requestId": request_id, "taskPacketId": task["task_packet_id"], "generation": generation})
    row = store.conn.execute("SELECT * FROM research_requests WHERE request_id=?", (request_id,)).fetchone()
    return _request_projection(store, row)


def list_research_requests(store: Any, *, project_id: str, workstream_id: str | None = None, states: set[str] | None = None, limit: int = 32) -> list[dict[str, Any]]:
    if limit < 1 or limit > 100:
        raise InvalidRequestError("research list limit is invalid")
    if workstream_id is not None:
        _workstream(store, project_id, workstream_id, worker_only=True)
    if states is not None and not states <= RESEARCH_STATES:
        raise InvalidRequestError("research state filter is invalid")
    clauses = ["project_id=?"]
    args: list[Any] = [project_id]
    if workstream_id is not None:
        clauses.append("workstream_id=?")
        args.append(workstream_id)
    if states:
        clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
        args.extend(sorted(states))
    rows = store.conn.execute("SELECT * FROM research_requests WHERE " + " AND ".join(clauses) + " ORDER BY created_at,request_id LIMIT ?", (*args, limit)).fetchall()
    return [_request_projection(store, row, include_packets=False) for row in rows]


def list_unacknowledged_research(store: Any, *, project_id: str, workstream_id: str) -> list[dict[str, Any]]:
    _workstream(store, project_id, workstream_id, worker_only=True)
    rows = store.conn.execute("SELECT * FROM research_requests WHERE project_id=? AND workstream_id=? AND state IN ('needs_context','answered','declined') ORDER BY created_at,request_id", (project_id, workstream_id)).fetchall()
    return [_request_projection(store, row) for row in rows]


def inspect_research(store: Any, *, project_id: str, request_id: str, workstream_id: str | None = None) -> dict[str, Any]:
    validate_id(request_id, prefix="wrq")
    clauses = ["project_id=?"]
    args: list[Any] = [project_id]
    if workstream_id is not None:
        _workstream(store, project_id, workstream_id, worker_only=True)
        clauses.append("workstream_id=?")
        args.append(workstream_id)
    row = store.conn.execute("SELECT * FROM research_requests WHERE " + " AND ".join(clauses) + " AND request_id=?", (*args, request_id)).fetchone()
    if row is None:
        raise NotFoundError("research request was not found")
    return _request_projection(store, row)


def claim_research(store: Any, *, project_id: str, secretary_workstream_id: str, request_id: str) -> dict[str, Any]:
    secretary = _workstream(store, project_id, secretary_workstream_id)
    if secretary["kind"] != "secretary" or secretary["execution_profile"] != "secretary-project":
        raise InvalidRequestError("research claim requires the project secretary")
    validate_id(request_id, prefix="wrq")
    with store.transaction():
        row = store.conn.execute("SELECT * FROM research_requests WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
        if row is None:
            raise NotFoundError("research request was not found")
        if row["state"] == "researching" and row["claimed_by_secretary_workstream_id"] == secretary_workstream_id:
            return _request_projection(store, row)
        if row["state"] != "pending":
            raise ConflictError("research request is not pending")
        now = utc_now()
        store.conn.execute("UPDATE research_requests SET state='researching',claimed_by_secretary_workstream_id=?,updated_at=? WHERE request_id=?", (secretary_workstream_id, now, request_id))
        append_event_in_transaction(store.conn, kind="research.claimed", project_id=project_id, workstream_id=row["workstream_id"], payload={"requestId": request_id, "secretaryWorkstreamId": secretary_workstream_id})
        row = store.conn.execute("SELECT * FROM research_requests WHERE request_id=?", (request_id,)).fetchone()
    return _request_projection(store, row)


def request_research_context(store: Any, *, project_id: str, secretary_workstream_id: str, request_id: str, idempotency_key: str, context_request: Mapping[str, Any]) -> dict[str, Any]:
    secretary = _workstream(store, project_id, secretary_workstream_id)
    if secretary["kind"] != "secretary":
        raise InvalidRequestError("research context requests require the project secretary")
    validate_id(request_id, prefix="wrq")
    key = bounded_text(idempotency_key, name="research context idempotency key", limit=256)
    normalized = validate_research_needs_context(context_request)
    with store.transaction():
        row = store.conn.execute("SELECT * FROM research_requests WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
        if row is None:
            raise NotFoundError("research request was not found")
        if row["state"] not in {"pending", "researching", "needs_context"}:
            raise ConflictError("research request is terminal")
        packet = _single_packet(store.conn, request_id, "secretary", "needs_context", key, normalized)
        if row["state"] != "needs_context":
            now = utc_now()
            store.conn.execute("UPDATE research_requests SET state='needs_context',claimed_by_secretary_workstream_id=?,updated_at=? WHERE request_id=?", (secretary_workstream_id, now, request_id))
            generation = _increment_inbox(store.conn, row["project_id"])
            append_event_in_transaction(store.conn, kind="research.context_requested", project_id=project_id, workstream_id=row["workstream_id"], payload={"requestId": request_id, "packetId": packet["packet_id"], "generation": generation})
        row = store.conn.execute("SELECT * FROM research_requests WHERE request_id=?", (request_id,)).fetchone()
    return _request_projection(store, row)


def add_research_context(store: Any, *, project_id: str, workstream_id: str, request_id: str, idempotency_key: str, context: Mapping[str, Any]) -> dict[str, Any]:
    _workstream(store, project_id, workstream_id, worker_only=True)
    validate_id(request_id, prefix="wrq")
    key = bounded_text(idempotency_key, name="research context idempotency key", limit=256)
    normalized = validate_research_context(context)
    with store.transaction():
        row = store.conn.execute("SELECT * FROM research_requests WHERE project_id=? AND workstream_id=? AND request_id=?", (project_id, workstream_id, request_id)).fetchone()
        if row is None:
            raise NotFoundError("research request was not found")
        if row["state"] not in {"needs_context", "pending"}:
            if row["state"] in {"answered", "declined", "acknowledged"}:
                raise ConflictError("research request is terminal")
            raise ConflictError("research request is not awaiting context")
        packet = _packet_row(store.conn, request_id, "worker", key, normalized, "context")
        now = utc_now()
        store.conn.execute("UPDATE research_requests SET state='pending',claimed_by_secretary_workstream_id=NULL,updated_at=? WHERE request_id=?", (now, request_id))
        generation = _increment_inbox(store.conn, project_id)
        append_event_in_transaction(store.conn, kind="research.context_added", project_id=project_id, workstream_id=workstream_id, payload={"requestId": request_id, "packetId": packet["packet_id"], "generation": generation})
        row = store.conn.execute("SELECT * FROM research_requests WHERE request_id=?", (request_id,)).fetchone()
    return _request_projection(store, row)

def answer_research(store: Any, *, project_id: str, secretary_workstream_id: str, request_id: str, idempotency_key: str, result: Mapping[str, Any]) -> dict[str, Any]:
    secretary = _workstream(store, project_id, secretary_workstream_id)
    if secretary["kind"] != "secretary":
        raise InvalidRequestError("research answers require the project secretary")
    validate_id(request_id, prefix="wrq")
    key = bounded_text(idempotency_key, name="research result idempotency key", limit=256)
    normalized = validate_research_result(result)
    with store.transaction():
        row = store.conn.execute("SELECT * FROM research_requests WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
        if row is None:
            raise NotFoundError("research request was not found")
        if row["state"] == "answered":
            packet = _single_packet(store.conn, request_id, "secretary", "result", key, normalized)
            return _request_projection(store, row)
        if row["state"] not in {"pending", "researching"}:
            raise ConflictError("research request cannot be answered in its current state")
        packet = _single_packet(store.conn, request_id, "secretary", "result", key, normalized)
        now = utc_now()
        store.conn.execute("UPDATE research_requests SET state='answered',answered_at=?,updated_at=? WHERE request_id=?", (now, now, request_id))
        append_event_in_transaction(store.conn, kind="research.answered", project_id=project_id, workstream_id=row["workstream_id"], payload={"requestId": request_id, "packetId": packet["packet_id"]})
        row = store.conn.execute("SELECT * FROM research_requests WHERE request_id=?", (request_id,)).fetchone()
    return _request_projection(store, row)


def decline_research(store: Any, *, project_id: str, secretary_workstream_id: str, request_id: str, idempotency_key: str, decline: Mapping[str, Any]) -> dict[str, Any]:
    secretary = _workstream(store, project_id, secretary_workstream_id)
    if secretary["kind"] != "secretary":
        raise InvalidRequestError("research declines require the project secretary")
    validate_id(request_id, prefix="wrq")
    key = bounded_text(idempotency_key, name="research decline idempotency key", limit=256)
    normalized = validate_research_decline(decline)
    with store.transaction():
        row = store.conn.execute("SELECT * FROM research_requests WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
        if row is None:
            raise NotFoundError("research request was not found")
        if row["state"] == "declined":
            packet = _single_packet(store.conn, request_id, "secretary", "declined", key, normalized)
            return _request_projection(store, row)
        if row["state"] not in {"pending", "researching"}:
            raise ConflictError("research request cannot be declined in its current state")
        packet = _single_packet(store.conn, request_id, "secretary", "declined", key, normalized)
        now = utc_now()
        store.conn.execute("UPDATE research_requests SET state='declined',answered_at=?,updated_at=? WHERE request_id=?", (now, now, request_id))
        append_event_in_transaction(store.conn, kind="research.declined", project_id=project_id, workstream_id=row["workstream_id"], payload={"requestId": request_id, "packetId": packet["packet_id"]})
        row = store.conn.execute("SELECT * FROM research_requests WHERE request_id=?", (request_id,)).fetchone()
    return _request_projection(store, row)


def acknowledge_research(store: Any, *, project_id: str, workstream_id: str, request_id: str) -> dict[str, Any]:
    _workstream(store, project_id, workstream_id, worker_only=True)
    validate_id(request_id, prefix="wrq")
    with store.transaction():
        row = store.conn.execute("SELECT * FROM research_requests WHERE project_id=? AND workstream_id=? AND request_id=?", (project_id, workstream_id, request_id)).fetchone()
        if row is None:
            raise NotFoundError("research request was not found")
        if row["state"] == "acknowledged":
            return _request_projection(store, row)
        if row["state"] not in {"answered", "declined"}:
            raise ConflictError("only answered or declined research can be acknowledged")
        now = utc_now()
        store.conn.execute("UPDATE research_requests SET state='acknowledged',acknowledged_at=?,updated_at=? WHERE request_id=?", (now, now, request_id))
        append_event_in_transaction(store.conn, kind="research.acknowledged", project_id=project_id, workstream_id=workstream_id, payload={"requestId": request_id})
        row = store.conn.execute("SELECT * FROM research_requests WHERE request_id=?", (request_id,)).fetchone()
    return _request_projection(store, row)


def pending_research_wakes(store: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in store.conn.execute("SELECT project_id,generation,notified_generation,updated_at FROM research_inbox WHERE generation>notified_generation ORDER BY project_id")]


def mark_research_wake_notified(store: Any, project_id: str, generation: int) -> bool:
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise InvalidRequestError("research wake generation is invalid")
    with store.transaction():
        cursor = store.conn.execute("UPDATE research_inbox SET notified_generation=? WHERE project_id=? AND notified_generation<? AND generation>=?", (generation, project_id, generation, generation))
    return cursor.rowcount == 1


def research_counts(store: Any, project_id: str, workstream_id: str | None = None) -> dict[str, int]:
    clauses = ["project_id=?"]
    args: list[Any] = [project_id]
    if workstream_id is not None:
        clauses.append("workstream_id=?")
        args.append(workstream_id)
    rows = store.conn.execute("SELECT state,count(*) AS count FROM research_requests WHERE " + " AND ".join(clauses) + " GROUP BY state", args)
    result = {state: 0 for state in RESEARCH_STATES}
    for row in rows:
        result[row["state"]] = int(row["count"])
    result["unacknowledged"] = result["answered"] + result["declined"] + result["needs_context"]
    return result
