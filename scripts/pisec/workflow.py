"""Durable semantic workflow records authenticated by the broker."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Mapping

from .events import append_event_in_transaction
from .models import ConflictError, IdempotencyConflictError, InvalidRequestError, NotFoundError, bounded_text, canonical_json, json_digest, new_id, utc_now, validate_git_oid, validate_id, validate_sha256
from .projects import _git, assert_project_writable, get_project
from .worker_repo import validate_worker_repository

PHASES = frozenset({"investigating", "implementing", "verifying"})
COORDINATION_KINDS = frozenset({"clarification", "blocker", "review_request"})

ISSUE_CATEGORIES = frozenset({"permission", "access", "lifecycle", "tooling", "other"})
ISSUE_SEVERITIES = frozenset({"blocking", "degraded", "improvement"})
HELP_KINDS = frozenset({"clarification", "blocker", "review", "access", "permission", "tooling", "lifecycle"})


def _issue_row(store: Any, issue_id: str, project_id: str | None = None) -> dict[str, Any]:
    query = "SELECT * FROM issues WHERE issue_id=?"
    params: list[Any] = [issue_id]
    if project_id is not None:
        query += " AND project_id=?"
        params.append(project_id)
    row = store.conn.execute(query, params).fetchone()
    if row is None:
        raise NotFoundError("issue was not found")
    return dict(row)


def _active_reporter(store: Any, project_id: str, workstream_id: str) -> dict[str, Any]:
    row = store.conn.execute(
        "SELECT w.workstream_id,w.project_id,w.kind,w.desired_state,w.provisioning_state FROM workstreams w JOIN runtime_bindings r USING(workstream_id) WHERE w.workstream_id=? AND w.project_id=?",
        (workstream_id, project_id),
    ).fetchone()
    if row is None or row["kind"] not in {"worker", "secretary"} or row["desired_state"] != "active" or row["provisioning_state"] != "bound":
        raise ConflictError("issue reporter is not an active bound project worker or secretary")
    return dict(row)


def _report_digest(category: str, severity: str, summary: str, details: str, requested_action: str, evidence: Any) -> str:
    return json_digest({"category": category, "severity": severity, "summary": summary, "details": details, "requestedAction": requested_action, "evidence": evidence})


def report_issue(store: Any, *, project_id: str, reporter_workstream_id: str, category: str, severity: str, summary: str, details: str, requested_action: str, evidence: Any, idempotency_key: str) -> dict[str, Any]:
    assert_project_writable(store, project_id)
    reporter = _active_reporter(store, project_id, reporter_workstream_id)
    if category not in ISSUE_CATEGORIES or severity not in ISSUE_SEVERITIES:
        raise InvalidRequestError("issue category or severity is invalid")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise InvalidRequestError("issue idempotency key is required")
    if not isinstance(evidence, list):
        raise InvalidRequestError("issue evidence must be a list")
    text = (
        bounded_text(summary, name="summary", limit=1024),
        bounded_text(details, name="details", limit=4096),
        bounded_text(requested_action, name="requested_action", limit=4096),
    )
    evidence_json = canonical_json(evidence, max_bytes=32768, max_text=4096)
    digest = _report_digest(category, severity, text[0], text[1], text[2], evidence)
    existing = store.conn.execute("SELECT * FROM issues WHERE reporter_workstream_id=? AND idempotency_key=?", (reporter_workstream_id, idempotency_key)).fetchone()
    if existing is not None:
        if existing["report_sha256"] != digest:
            raise IdempotencyConflictError("issue report differs for the idempotency key")
        return inspect_issue(store, issue_id=existing["issue_id"], project_id=project_id)
    issue_id = new_id("iss")
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "INSERT INTO issues(issue_id,project_id,reporter_workstream_id,reporter_kind,category,severity,summary,details,requested_action,evidence_json,idempotency_key,report_sha256,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (issue_id, project_id, reporter_workstream_id, reporter["kind"], category, severity, text[0], text[1], text[2], evidence_json, idempotency_key, digest, "open", now, now),
        )
        append_event_in_transaction(store.conn, kind="issue.reported", project_id=project_id, workstream_id=reporter_workstream_id, payload={"issueId": issue_id, "reporterKind": reporter["kind"], "severity": severity})
    return inspect_issue(store, issue_id=issue_id, project_id=project_id)


def list_issues(store: Any, *, project_id: str | None = None, project_ids: Sequence[str] | None = None, state: str | None = None, limit: int = 100, reporter_workstream_id: str | None = None) -> list[dict[str, Any]]:
    if limit < 1 or limit > 1000:
        raise InvalidRequestError("issue limit is invalid")
    params: list[Any] = []
    where: list[str] = []
    if project_id is not None:
        where.append("project_id=?")
        params.append(project_id)
    if project_ids is not None:
        ids = list(dict.fromkeys(project_ids))
        if not ids:
            return []
        where.append("project_id IN (" + ",".join("?" for _ in ids) + ")")
        params.extend(ids)
    if reporter_workstream_id is not None:
        where.append("(reporter_workstream_id=? OR EXISTS (SELECT 1 FROM issue_remediations r WHERE r.issue_id=issues.issue_id AND r.workstream_id=?))")
        params.extend((reporter_workstream_id, reporter_workstream_id))
    if state is not None:
        if state not in {"open", "acknowledged", "remediating", "verifying", "resolved"}:
            raise InvalidRequestError("issue state is invalid")
        where.append("state=?")
        params.append(state)
    clause = " WHERE " + " AND ".join(where) if where else ""
    params.append(limit)
    rows = store.conn.execute(f"SELECT * FROM issues{clause} ORDER BY CASE severity WHEN 'blocking' THEN 0 WHEN 'degraded' THEN 1 ELSE 2 END, created_at, issue_id LIMIT ?", params)
    return [dict(row) for row in rows]


def inspect_issue(store: Any, *, issue_id: str, project_id: str | None = None, reporter_workstream_id: str | None = None) -> dict[str, Any]:
    issue = _issue_row(store, issue_id, project_id)
    if reporter_workstream_id is not None and issue["reporter_workstream_id"] != reporter_workstream_id:
        linked = store.conn.execute("SELECT 1 FROM issue_remediations WHERE issue_id=? AND workstream_id=?", (issue_id, reporter_workstream_id)).fetchone()
        if linked is None:
            raise NotFoundError("issue was not found")
    issue["updates"] = [dict(row) for row in store.conn.execute("SELECT * FROM issue_updates WHERE issue_id=? ORDER BY created_at,update_id", (issue_id,))]
    issue["remediations"] = [dict(row) for row in store.conn.execute("SELECT * FROM issue_remediations WHERE issue_id=? ORDER BY created_at,remediation_id", (issue_id,))]
    return issue


def _append_issue_update(store: Any, *, issue: Mapping[str, Any], actor_kind: str, actor_id: str, update_kind: str, payload: Any, idempotency_key: str) -> bool:
    payload_json = canonical_json(payload, max_bytes=32768, max_text=4096)
    payload_sha = json_digest(payload)
    existing = store.conn.execute(
        "SELECT update_kind,payload_sha256 FROM issue_updates WHERE issue_id=? AND actor_id=? AND idempotency_key=?",
        (issue["issue_id"], actor_id, idempotency_key),
    ).fetchone()
    if existing is not None:
        if existing["update_kind"] != update_kind or existing["payload_sha256"] != payload_sha:
            raise IdempotencyConflictError("issue update differs for the idempotency key")
        return False
    store.conn.execute(
        "INSERT INTO issue_updates(update_id,issue_id,actor_kind,actor_id,update_kind,payload_json,payload_sha256,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (new_id("iup"), issue["issue_id"], actor_kind, actor_id, update_kind, payload_json, payload_sha, idempotency_key, utc_now()),
    )
    return True


def _issue_actor(store: Any, project_id: str, actor_id: str, *, kinds: set[str]) -> dict[str, Any]:
    row = store.conn.execute(
        "SELECT w.workstream_id,w.project_id,w.kind,w.desired_state,w.provisioning_state FROM workstreams w JOIN runtime_bindings r USING(workstream_id) WHERE w.workstream_id=? AND w.project_id=? AND w.desired_state='active' AND w.provisioning_state='bound'",
        (actor_id, project_id),
    ).fetchone()
    if row is None or row["kind"] not in kinds:
        raise ConflictError("issue actor is not authorized")
    return dict(row)


def add_issue_context(store: Any, *, project_id: str, issue_id: str, actor_id: str, context: Any, idempotency_key: str) -> dict[str, Any]:
    issue = _issue_row(store, issue_id, project_id)
    actor = _issue_actor(store, project_id, actor_id, kinds={"worker", "secretary", "first_mate"})
    with store.transaction():
        _append_issue_update(store, issue=issue, actor_kind=actor["kind"], actor_id=actor_id, update_kind="context", payload=context, idempotency_key=idempotency_key)
        store.conn.execute("UPDATE issues SET updated_at=? WHERE issue_id=?", (utc_now(), issue_id))
    return inspect_issue(store, issue_id=issue_id, project_id=project_id)


def acknowledge_issue(store: Any, *, project_id: str, issue_id: str, actor_id: str) -> dict[str, Any]:
    issue = _issue_row(store, issue_id, project_id)
    actor = _issue_actor(store, project_id, actor_id, kinds={"first_mate"})
    if issue["state"] not in {"open", "acknowledged"}:
        raise ConflictError("issue cannot be acknowledged in its current state")
    now = utc_now()
    with store.transaction():
        if issue["state"] == "open":
            store.conn.execute("UPDATE issues SET state='acknowledged',acknowledged_at=?,updated_at=? WHERE issue_id=?", (now, now, issue_id))
            _append_issue_update(store, issue=issue, actor_kind=actor["kind"], actor_id=actor_id, update_kind="acknowledged", payload={}, idempotency_key=f"ack:{issue_id}")
    return inspect_issue(store, issue_id=issue_id, project_id=project_id)


def _task_covers_remediation(store: Any, *, workstream_id: str, issue: Mapping[str, Any]) -> bool:
    row = store.conn.execute("SELECT packet_json FROM task_packets WHERE workstream_id=?", (workstream_id,)).fetchone()
    if row is None:
        return False
    packet = str(row["packet_json"])
    # The worker's immutable packet is the authority.  The issue identity and
    # bounded requested action are the durable anchors; free-form summaries are
    # accepted only as an additional, human-readable scope marker.
    return str(issue["issue_id"]) in packet or str(issue["requested_action"]) in packet or str(issue["summary"]) in packet


def link_issue_remediation(store: Any, *, project_id: str, issue_id: str, actor_id: str, target_id: str, idempotency_key: str) -> dict[str, Any]:
    issue = _issue_row(store, issue_id, project_id)
    actor = _issue_actor(store, project_id, actor_id, kinds={"secretary"})
    target = store.conn.execute(
        "SELECT w.* FROM workstreams w JOIN runtime_bindings r USING(workstream_id) "
        "WHERE w.workstream_id=? AND w.project_id=? AND w.kind='worker' "
        "AND w.desired_state='active' AND w.provisioning_state='bound'",
        (target_id, project_id),
    ).fetchone()
    if target is None:
        raise ConflictError("remediation target is not an active bound project worker")
    if not _task_covers_remediation(store, workstream_id=target_id, issue=issue):
        raise ConflictError("worker task packet does not cover the requested remediation")
    for table in ("completion_packets", "workstream_acceptances", "integration_jobs", "merge_receipts"):
        if store.conn.execute(f"SELECT 1 FROM {table} WHERE workstream_id=? LIMIT 1", (target_id,)).fetchone() is not None:
            raise ConflictError("remediation target already has completion or integration history")
    if issue["state"] == "resolved":
        raise ConflictError("resolved issue cannot be remediated")
    linked_issues = [issue]
    if issue["escalated_from_issue_id"] is not None:
        underlying = _issue_row(store, str(issue["escalated_from_issue_id"]), project_id)
        if underlying["reporter_kind"] != "worker":
            raise ConflictError("escalation does not point to a worker issue")
        linked_issues.insert(0, underlying)
    with store.transaction():
        for linked in linked_issues:
            existing = store.conn.execute("SELECT * FROM issue_remediations WHERE issue_id=? AND workstream_id=?", (linked["issue_id"], target_id)).fetchone()
            if existing is None:
                store.conn.execute(
                    "INSERT INTO issue_remediations(remediation_id,issue_id,workstream_id,linked_by_workstream_id,created_at) VALUES(?,?,?,?,?)",
                    (new_id("rem"), linked["issue_id"], target_id, actor_id, utc_now()),
                )
            changed = _append_issue_update(
                store,
                issue=linked,
                actor_kind=actor["kind"],
                actor_id=actor_id,
                update_kind="remediation_linked",
                payload={"workstreamId": target_id},
                idempotency_key=idempotency_key if linked["issue_id"] == issue_id else f"{idempotency_key}:underlying",
            )
            if changed:
                store.conn.execute("UPDATE issues SET state='remediating',updated_at=? WHERE issue_id=?", (utc_now(), linked["issue_id"]))
                append_event_in_transaction(store.conn, kind="issue.remediation_linked", project_id=project_id, workstream_id=actor_id, payload={"issueId": linked["issue_id"], "workstreamId": target_id})
    return inspect_issue(store, issue_id=issue_id, project_id=project_id)


def request_issue_verification(store: Any, *, project_id: str, issue_id: str, actor_id: str, evidence: Any, idempotency_key: str) -> dict[str, Any]:
    issue = _issue_row(store, issue_id, project_id)
    actor = _issue_actor(store, project_id, actor_id, kinds={"first_mate"})
    if issue["state"] != "remediating":
        raise ConflictError("issue is not ready for verification")
    now = utc_now()
    with store.transaction():
        store.conn.execute("UPDATE issues SET state='verifying',updated_at=? WHERE issue_id=?", (now, issue_id))
        _append_issue_update(store, issue=issue, actor_kind=actor["kind"], actor_id=actor_id, update_kind="verification_requested", payload={"evidence": evidence}, idempotency_key=idempotency_key)
    return inspect_issue(store, issue_id=issue_id, project_id=project_id)


def verify_issue(store: Any, *, project_id: str, issue_id: str, actor_id: str, status: str, evidence: Any, idempotency_key: str) -> dict[str, Any]:
    issue = _issue_row(store, issue_id, project_id)
    actor = _issue_actor(store, project_id, actor_id, kinds={"worker", "secretary"})
    if actor_id != issue["reporter_workstream_id"] and actor["kind"] != "secretary":
        raise ConflictError("only the reporter or project secretary may verify an issue")
    if issue["state"] != "verifying":
        raise ConflictError("issue is not awaiting verification")
    if status not in {"fixed", "still_blocked"}:
        raise InvalidRequestError("invalid issue verification status")
    now = utc_now()
    with store.transaction():
        update_kind = "verification_passed" if status == "fixed" else "verification_failed"
        state = "resolved" if status == "fixed" else "acknowledged"
        disposition = "fixed" if status == "fixed" else None
        store.conn.execute("UPDATE issues SET state=?,disposition=?,resolution=?,resolved_at=?,updated_at=? WHERE issue_id=?", (state, disposition, None if status != "fixed" else "verified by reporter", now if status == "fixed" else None, now, issue_id))
        _append_issue_update(store, issue=issue, actor_kind=actor["kind"], actor_id=actor_id, update_kind=update_kind, payload={"status": status, "evidence": evidence}, idempotency_key=idempotency_key)
    return inspect_issue(store, issue_id=issue_id, project_id=project_id)


def resolve_issue(store: Any, *, project_id: str, issue_id: str, actor_id: str, disposition: str, reason: str, decision_id: str) -> dict[str, Any]:
    issue = _issue_row(store, issue_id, project_id)
    actor = _issue_actor(store, project_id, actor_id, kinds={"first_mate"})
    if disposition not in {"declined", "duplicate", "not_reproducible"} or not reason.strip():
        raise InvalidRequestError("non-fix issue resolution requires a disposition and reason")
    decision = store.conn.execute("SELECT * FROM decisions WHERE decision_id=? AND project_id=? AND state='resolved'", (decision_id, project_id)).fetchone()
    if decision is None:
        raise ConflictError("non-fix issue resolution requires a resolved decision in the same project")
    if issue["state"] == "resolved":
        if issue["disposition"] != disposition or issue["resolved_decision_id"] != decision_id or issue["resolution"] != reason:
            raise ConflictError("issue is already resolved differently")
        return inspect_issue(store, issue_id=issue_id, project_id=project_id)
    now = utc_now()
    with store.transaction():
        text = bounded_text(reason, name="reason", limit=4096)
        store.conn.execute("UPDATE issues SET state='resolved',disposition=?,resolution=?,resolved_decision_id=?,resolved_at=?,updated_at=? WHERE issue_id=?", (disposition, text, decision_id, now, now, issue_id))
        _append_issue_update(store, issue=issue, actor_kind=actor["kind"], actor_id=actor_id, update_kind="resolved", payload={"disposition": disposition, "reason": reason, "decisionId": decision_id}, idempotency_key=f"resolve:{decision_id}")
    return inspect_issue(store, issue_id=issue_id, project_id=project_id)


def report_secretary_issue(store: Any, **kwargs: Any) -> dict[str, Any]:
    return report_issue(store, reporter_workstream_id=kwargs.pop("secretary_workstream_id"), **kwargs)


def list_secretary_issues(store: Any, **kwargs: Any) -> list[dict[str, Any]]:
    return list_issues(store, **kwargs)


def inspect_secretary_issue(store: Any, **kwargs: Any) -> dict[str, Any]:
    return inspect_issue(store, **kwargs)


def _runtime_binding(store: Any, workstream_id: str, runtime_instance_id: str) -> dict[str, Any]:
    row = store.conn.execute(
        "SELECT r.*,w.project_id,w.kind,w.desired_state,w.provisioning_state FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=?",
        (workstream_id,),
    ).fetchone()
    if row is None or row["runtime_instance_id"] != runtime_instance_id:
        raise ConflictError("runtime binding is stale")
    return dict(row)


def checkpoint(store: Any, *, workstream_id: str, runtime_instance_id: str, phase: str, summary: str, next_action: str, evidence: Any, idempotency_key: str, remediation_issue_id: str | None = None) -> dict[str, Any]:
    binding = _runtime_binding(store, workstream_id, runtime_instance_id)
    if binding["desired_state"] != "active" or binding["provisioning_state"] != "bound":
        raise ConflictError("workstream is not active and bound")
    assert_project_writable(store, str(binding["project_id"]))
    if phase not in PHASES:
        raise InvalidRequestError("checkpoint phase is invalid")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise InvalidRequestError("checkpoint idempotency key is required")
    if not isinstance(evidence, list) or any(not isinstance(item, (str, dict)) for item in evidence):
        raise InvalidRequestError("checkpoint evidence must be a list of references")
    if remediation_issue_id is not None:
        validate_id(remediation_issue_id, prefix="iss")
        if store.conn.execute("SELECT 1 FROM issue_remediations WHERE issue_id=? AND workstream_id=?", (remediation_issue_id, workstream_id)).fetchone() is None:
            raise ConflictError("checkpoint remediation issue is not linked to this worker")
    summary = bounded_text(summary, name="summary", limit=1024)
    next_action = bounded_text(next_action, name="next_action", limit=1024)
    evidence_json = canonical_json(evidence, max_bytes=32768, max_text=4096)
    now = utc_now()
    checkpoint_id = new_id("cp")
    with store.transaction():
        existing = store.conn.execute("SELECT * FROM workstream_checkpoints WHERE workstream_id=? AND idempotency_key=?", (workstream_id, idempotency_key)).fetchone()
        if existing is not None:
            if remediation_issue_id is not None and existing["remediation_issue_id"] != remediation_issue_id:
                raise IdempotencyConflictError("checkpoint idempotency key was reused with different remediation issue")
            checkpoint_id = str(existing["checkpoint_id"])
        else:
            sequence = int(store.conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM workstream_checkpoints WHERE workstream_id=?", (workstream_id,)).fetchone()[0])
            store.conn.execute(
                "INSERT INTO workstream_checkpoints(checkpoint_id,workstream_id,runtime_instance_id,sequence,idempotency_key,phase,summary,next_action,remediation_issue_id,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (checkpoint_id, workstream_id, runtime_instance_id, sequence, idempotency_key, phase, summary, next_action, remediation_issue_id, evidence_json, now),
            )
        if existing is None:
            append_event_in_transaction(store.conn, kind="workstream.checkpointed", project_id=binding["project_id"], workstream_id=workstream_id, payload={"checkpointId": checkpoint_id, "sequence": sequence, "phase": phase, "remediationIssueId": remediation_issue_id})
    return dict(store.conn.execute("SELECT * FROM workstream_checkpoints WHERE checkpoint_id=?", (checkpoint_id,)).fetchone())


def _completion_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(packet, Mapping):
        raise InvalidRequestError("completion packet must be an object")
    required = {"acceptance", "verification", "sourceCommit", "taskPacketSha256", "changedSurfaces", "residualRisk"}
    if set(packet) != required:
        raise InvalidRequestError("completion packet fields do not match the contract")
    acceptance = packet["acceptance"]
    verification = packet["verification"]
    if not isinstance(acceptance, list) or not acceptance:
        raise InvalidRequestError("completion packet must list acceptance criteria")
    for item in acceptance:
        if not isinstance(item, Mapping) or not isinstance(item.get("criterion"), str) or item.get("status") != "passed" or not isinstance(item.get("evidence"), list) or not item["evidence"]:
            raise InvalidRequestError("every acceptance criterion requires passed evidence")
    if not isinstance(verification, list) or not verification or any(not isinstance(item, Mapping) or not item.get("command") or not item.get("result") for item in verification):
        raise InvalidRequestError("verification must list exact commands and observed results")
    source = packet["sourceCommit"]
    digest = packet["taskPacketSha256"]
    validate_git_oid(source, "completion source commit")
    validate_sha256(digest, "completion task packet digest")
    return json.loads(canonical_json(dict(packet), max_bytes=65536, max_text=8192))


def _submit_completion_in_transaction(store: Any, *, workstream_id: str, runtime_instance_id: str, packet: Mapping[str, Any]) -> dict[str, Any]:
    binding = _runtime_binding(store, workstream_id, runtime_instance_id)
    project_id = str(binding["project_id"])
    assert_project_writable(store, project_id)
    normalized = _completion_packet(packet)
    workstream = store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()
    task = store.conn.execute("SELECT * FROM task_packets WHERE workstream_id=?", (workstream_id,)).fetchone()
    if workstream is None or workstream["kind"] != "worker":
        raise ConflictError("only worker workstreams may submit completion")
    if task is None or normalized["taskPacketSha256"] != task["packet_sha256"]:
        raise ConflictError("completion packet does not match the immutable task packet")
    integration = store.conn.execute(
        "SELECT integration_id,state,target_oid FROM integration_jobs WHERE workstream_id=? ORDER BY created_at DESC LIMIT 1",
        (workstream_id,),
    ).fetchone()
    private_ref = None
    history_base_oid = None
    if integration is not None and integration["state"] in {"awaiting_worker", "queued", "refreshing", "verifying", "applying"}:
        private_ref = f"refs/pisec/target/{integration['integration_id']}"
        history_base_oid = str(integration["target_oid"]) if integration["target_oid"] else None
    observed_source = validate_worker_repository(
        Path(str(workstream["worktree_path"])),
        branch_name=str(workstream["branch_name"]),
        base_oid=str(workstream["base_commit_oid"]),
        target_branch=str(workstream["target_ref"]).removeprefix("refs/heads/"),
        allowed_private_ref=private_ref,
        history_base_oid=history_base_oid,
        review_base_oid=history_base_oid,
    )
    if normalized["sourceCommit"].lower() != observed_source:
        raise ConflictError("completion source commit is stale")
    packet_sha = hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()
    existing = store.conn.execute("SELECT * FROM completion_packets WHERE packet_sha256=?", (packet_sha,)).fetchone()
    if existing is not None:
        return dict(existing)
    now = utc_now()
    packet_id = new_id("cmp")
    sequence = int(store.conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM completion_packets WHERE workstream_id=?", (workstream_id,)).fetchone()[0])
    store.conn.execute(
        "INSERT INTO completion_packets(completion_packet_id,workstream_id,sequence,source_commit_oid,task_packet_sha256,packet_sha256,packet_json,submitted_at,accepted_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (packet_id, workstream_id, sequence, observed_source, task["packet_sha256"], packet_sha, canonical_json(normalized, max_bytes=65536, max_text=8192), now, None),
    )
    append_event_in_transaction(store.conn, kind="workstream.completion_submitted", project_id=project_id, workstream_id=workstream_id, payload={"completionPacketId": packet_id, "completionPacketSha256": packet_sha, "sourceCommit": observed_source})
    return dict(store.conn.execute("SELECT * FROM completion_packets WHERE completion_packet_id=?", (packet_id,)).fetchone())


def submit_completion(store: Any, *, workstream_id: str, runtime_instance_id: str, packet: Mapping[str, Any]) -> dict[str, Any]:
    with store.transaction():
        result = _submit_completion_in_transaction(store, workstream_id=workstream_id, runtime_instance_id=runtime_instance_id, packet=packet)
        existing_checkpoint = store.conn.execute("SELECT * FROM workstream_checkpoints WHERE workstream_id=? AND phase='ready_review' AND remediation_issue_id IS NULL ORDER BY sequence DESC LIMIT 1", (workstream_id,)).fetchone()
        if existing_checkpoint is None:
            checkpoint_id = new_id("cp")
            checkpoint_sequence = int(store.conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM workstream_checkpoints WHERE workstream_id=?", (workstream_id,)).fetchone()[0])
            store.conn.execute(
                "INSERT INTO workstream_checkpoints(checkpoint_id,workstream_id,runtime_instance_id,sequence,idempotency_key,phase,summary,next_action,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (checkpoint_id, workstream_id, runtime_instance_id, checkpoint_sequence, f"completion:{result['packet_sha256']}", "ready_review", "Completion packet submitted for review", "Secretary must review the immutable completion packet", "[]", result["submitted_at"]),
            )
            binding = _runtime_binding(store, workstream_id, runtime_instance_id)
            append_event_in_transaction(store.conn, kind="workstream.checkpointed", project_id=binding["project_id"], workstream_id=workstream_id, payload={"checkpointId": checkpoint_id, "sequence": checkpoint_sequence, "phase": "ready_review", "completionPacketId": result["completion_packet_id"]})
        return result




def list_coordination(store: Any, *, project_id: str, workstream_id: str | None = None, include_resolved: bool = False) -> list[dict[str, Any]]:
    params: list[Any] = [project_id]
    where = "r.project_id=?"
    if workstream_id is not None:
        where += " AND r.workstream_id=?"
        params.append(workstream_id)
    if not include_resolved:
        where += " AND r.state <> 'acknowledged'"
    rows = store.conn.execute(f"SELECT r.*, (SELECT response FROM coordination_packets p WHERE p.request_id=r.request_id ORDER BY p.sequence DESC LIMIT 1) AS response, (SELECT decision_id FROM coordination_packets p WHERE p.request_id=r.request_id ORDER BY p.sequence DESC LIMIT 1) AS decision_id FROM coordination_requests r WHERE {where} ORDER BY r.created_at,r.request_id", params)
    return [dict(row) for row in rows]

def request_coordination(store: Any, *, project_id: str, workstream_id: str, kind: str, summary: str, question: str, blocking: bool, idempotency_key: str, request_payload: Any = None) -> dict[str, Any]:
    assert_project_writable(store, project_id)
    binding = store.conn.execute("SELECT r.observed_state,w.desired_state,w.provisioning_state FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=? AND w.project_id=?", (workstream_id, project_id)).fetchone()
    if binding is None or binding["desired_state"] != "active" or binding["provisioning_state"] != "bound":
        raise ConflictError("workstream is not active and bound")
    if kind not in COORDINATION_KINDS:
        raise InvalidRequestError("coordination request kind is invalid")
    if kind == "review_request" and blocking:
        raise InvalidRequestError("review requests cannot be blocking")
    packet = store.conn.execute("SELECT task_packet_id FROM task_packets WHERE workstream_id=?", (workstream_id,)).fetchone()
    if packet is None:
        raise ConflictError("workstream has no immutable task packet")
    existing = store.conn.execute("SELECT * FROM coordination_requests WHERE workstream_id=? AND idempotency_key=?", (workstream_id, idempotency_key)).fetchone()
    request_document = request_payload if request_payload is not None else {"kind": kind, "summary": summary, "question": question, "blocking": bool(blocking)}
    request_json = canonical_json(request_document, max_bytes=32768, max_text=4096)
    request_sha = json_digest(request_document)
    if existing is not None:
        if existing["request_sha256"] != request_sha:
            raise IdempotencyConflictError("coordination request differs for the idempotency key")
        return dict(existing)
    request_id = new_id("cr")
    now = utc_now()
    with store.transaction():
        store.conn.execute("INSERT INTO coordination_requests(request_id,project_id,workstream_id,task_packet_id,kind,summary,question,blocking,state,idempotency_key,request_json,request_sha256,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (request_id, project_id, workstream_id, packet["task_packet_id"], kind, bounded_text(summary, name="summary", limit=1024), bounded_text(question, name="question", limit=4096), 1 if blocking else 0, "open", idempotency_key, request_json, request_sha, now, now))
        append_event_in_transaction(store.conn, kind="coordination.requested", project_id=project_id, workstream_id=workstream_id, payload={"requestId": request_id, "kind": kind, "blocking": bool(blocking)})
    return dict(store.conn.execute("SELECT * FROM coordination_requests WHERE request_id=?", (request_id,)).fetchone())


def request_help(
    store: Any,
    *,
    project_id: str,
    workstream_id: str,
    kind: str,
    summary: str,
    details: str,
    requested_action: str,
    blocking: bool,
    evidence: Any,
    idempotency_key: str,
) -> dict[str, Any]:
    """Route one worker help request to the existing durable record type."""
    if kind not in HELP_KINDS:
        raise InvalidRequestError("help request kind is invalid")
    if kind in {"clarification", "blocker", "review"}:
        coordination_kind = "review_request" if kind == "review" else kind
        request = request_coordination(
            store,
            project_id=project_id,
            workstream_id=workstream_id,
            kind=coordination_kind,
            summary=summary,
            question=details,
            blocking=False if kind == "review" else bool(blocking),
            idempotency_key=idempotency_key,
            request_payload={"kind": kind, "summary": summary, "details": details, "requestedAction": requested_action, "blocking": bool(blocking), "evidence": evidence},
        )
        return {"kind": kind, "recordType": "coordination", "request": request}
    issue = report_issue(
        store,
        project_id=project_id,
        reporter_workstream_id=workstream_id,
        category=kind,
        severity="blocking" if blocking else "degraded",
        summary=summary,
        details=details,
        requested_action=requested_action,
        evidence=evidence,
        idempotency_key=idempotency_key,
    )
    return {"kind": kind, "recordType": "issue", "request": issue}


def answer_coordination(store: Any, *, project_id: str, secretary_workstream_id: str, request_id: str, response: str, idempotency_key: str, decision_id: str | None = None) -> dict[str, Any]:
    assert_project_writable(store, project_id)
    row = store.conn.execute("SELECT * FROM coordination_requests WHERE request_id=? AND project_id=?", (request_id, project_id)).fetchone()
    if row is None:
        raise NotFoundError("coordination request was not found")
    if row["state"] == "acknowledged":
        raise ConflictError("coordination request is already acknowledged")
    existing = store.conn.execute("SELECT * FROM coordination_packets WHERE request_id=? AND actor='secretary' AND idempotency_key=?", (request_id, idempotency_key)).fetchone()
    if existing is not None:
        return dict(existing)
    now = utc_now()
    payload = {"requestId": request_id, "response": bounded_text(response, name="response", limit=4096), "decisionId": decision_id}
    packet_id = new_id("cop")
    with store.transaction():
        store.conn.execute("INSERT INTO coordination_packets(packet_id,request_id,actor,idempotency_key,response,decision_id,payload_sha256,created_at) VALUES(?,?,?,?,?,?,?,?)", (packet_id, request_id, "secretary", idempotency_key, payload["response"], decision_id, json_digest(payload), now))
        store.conn.execute("UPDATE coordination_requests SET state='answered',updated_at=?,answered_at=? WHERE request_id=?", (now, now, request_id))
        append_event_in_transaction(store.conn, kind="coordination.answered", project_id=project_id, workstream_id=row["workstream_id"], payload={"requestId": request_id, "decisionId": decision_id})
    return dict(store.conn.execute("SELECT * FROM coordination_packets WHERE packet_id=?", (packet_id,)).fetchone())


def acknowledge_coordination(store: Any, *, project_id: str, workstream_id: str, request_id: str) -> dict[str, Any]:
    assert_project_writable(store, project_id)
    row = store.conn.execute("SELECT * FROM coordination_requests WHERE request_id=? AND project_id=? AND workstream_id=?", (request_id, project_id, workstream_id)).fetchone()
    if row is None:
        raise NotFoundError("coordination request was not found")
    if row["state"] == "open":
        raise ConflictError("coordination request has no answer")
    now = utc_now()
    with store.transaction():
        store.conn.execute("UPDATE coordination_requests SET state='acknowledged',acknowledged_at=?,updated_at=? WHERE request_id=?", (now, now, request_id))
    return dict(store.conn.execute("SELECT * FROM coordination_requests WHERE request_id=?", (request_id,)).fetchone())
