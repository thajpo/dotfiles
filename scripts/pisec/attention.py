"""Durable current delivery index for typed Pisec source records."""

from __future__ import annotations

import json
import threading
from contextlib import nullcontext
from collections.abc import Mapping
from typing import Any
from types import SimpleNamespace

from .models import ConflictError, InvalidRequestError, NotFoundError, new_id, utc_now
from .projects import first_mate_issue_project_ids
from .runtime_eligibility import runtime_eligible_sql


SOURCE_KINDS = frozenset({"coordination", "research", "issue", "completion", "integration"})
ATTENTION_WAKE_PROMPT = "Pisec has pending attention. Review it with the Pisec attention tools before ending this turn."
_ATTENTION_HINT = threading.Event()


def signal_attention_hint() -> None:
    """Set the lossy process-local hint; SQLite remains authoritative."""
    _ATTENTION_HINT.set()


def wait_for_attention_hint(timeout: float) -> bool:
    signaled = _ATTENTION_HINT.wait(timeout)
    _ATTENTION_HINT.clear()
    return signaled


def _priority(source_kind: str, source: Mapping[str, Any], event_kind: str) -> int:
    if source_kind == "coordination":
        return 0 if bool(source.get("blocking")) else (2 if source.get("kind") == "review_request" else 1)
    if source_kind == "research":
        return 1
    if source_kind == "issue":
        if event_kind in {"issue.verification_failed", "issue.remediation_failed"}:
            return 0
        if event_kind in {"issue.remediation_completed", "issue.verification_requested"}:
            return 1
        return 0 if source.get("severity") == "blocking" else (2 if source.get("severity") == "improvement" else 1)
    if source_kind == "completion":
        return 1
    if source_kind == "integration":
        return 0
    raise InvalidRequestError("attention source kind is invalid")


def _source(connection: Any, source_kind: str, source_id: str) -> dict[str, Any]:
    tables = {
        "coordination": ("coordination_requests", "request_id"),
        "research": ("research_requests", "request_id"),
        "issue": ("issues", "issue_id"),
        "completion": ("completion_packets", "completion_packet_id"),
        "integration": ("integration_jobs", "integration_id"),
    }
    table, key = tables[source_kind]
    row = connection.execute(f"SELECT * FROM {table} WHERE {key}=?", (source_id,)).fetchone()
    if row is None:
        raise NotFoundError("attention source reference is not present")
    return dict(row)


def _upsert(connection: Any, *, recipient_workstream_id: str, project_id: str, source_kind: str, source_id: str, event: Mapping[str, Any], priority: int | None = None) -> None:
    if source_kind not in SOURCE_KINDS or not isinstance(source_id, str) or not source_id:
        raise InvalidRequestError("attention source reference is invalid")
    source = _source(connection, source_kind, source_id)
    event_sequence = int(event["sequence"])
    now = str(event["created_at"])
    effective_priority = _priority(source_kind, source, str(event["kind"])) if priority is None else priority
    connection.execute(
        """INSERT INTO attention_items(
            attention_id,recipient_workstream_id,project_id,source_kind,source_id,
            source_event_sequence,priority,created_at,revision_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(recipient_workstream_id,source_kind,source_id) DO UPDATE SET
            source_event_sequence=excluded.source_event_sequence,
            priority=excluded.priority,
            revision_at=excluded.revision_at,
            updated_at=excluded.updated_at
        WHERE excluded.source_event_sequence > attention_items.source_event_sequence""",
        (new_id("att"), recipient_workstream_id, project_id, source_kind, source_id, event_sequence, effective_priority, now, now, now),
    )


def _project_secretary(connection: Any, project_id: str) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT p.secretary_workstream_id FROM projects p JOIN workstreams w ON w.workstream_id=p.secretary_workstream_id WHERE p.project_id=? AND p.active=1 AND w.desired_state='active' AND w.provisioning_state='bound' AND p.secretary_workstream_id IS NOT NULL",
        (project_id,),
    ).fetchall()
    return tuple(str(row["secretary_workstream_id"]) for row in rows)


def _fleet_first_mates(connection: Any, project_id: str) -> tuple[str, ...]:
    if project_id not in first_mate_issue_project_ids(SimpleNamespace(conn=connection)):
        return ()
    rows = connection.execute(
        "SELECT workstream_id FROM workstreams WHERE kind='first_mate' AND desired_state='active' AND provisioning_state='bound'"
    ).fetchall()
    return tuple(str(row["workstream_id"]) for row in rows)


def index_event_in_transaction(connection: Any, event: Mapping[str, Any]) -> None:
    """Apply the event-to-current-index transition after the event is inserted."""
    kind = str(event["kind"])
    payload = json.loads(str(event["payload_json"]))
    project_id = event.get("project_id") or payload.get("projectId")
    source_kind: str | None = None
    source_id: str | None = None
    recipients: list[tuple[str, str, int | None]] = []

    if kind.startswith("coordination."):
        source_kind, source_id = "coordination", payload.get("requestId")
        source = _source(connection, source_kind, str(source_id))
        if kind == "coordination.requested":
            recipients.extend((rid, str(project_id), None) for rid in _project_secretary(connection, str(project_id)))
        elif kind == "coordination.answered":
            recipients.append((str(source["workstream_id"]), str(project_id), 1))
    elif kind.startswith("research."):
        source_kind, source_id = "research", payload.get("requestId")
        source = _source(connection, source_kind, str(source_id))
        if kind in {"research.requested", "research.context_added"}:
            recipients.extend((rid, str(project_id), 1) for rid in _project_secretary(connection, str(project_id)))
        elif kind in {"research.context_requested", "research.answered", "research.declined"}:
            recipients.append((str(source["workstream_id"]), str(project_id), 1))
    elif kind.startswith("issue."):
        source_kind, source_id = "issue", payload.get("issueId")
        if source_id is None:
            source_id = payload.get("escalationIssueId")
        source = _source(connection, source_kind, str(source_id))
        reporter_kind = str(source["reporter_kind"])
        supervisor_recipients = _fleet_first_mates(connection, str(project_id)) if reporter_kind == "secretary" and source.get("escalated_from_issue_id") is not None else _project_secretary(connection, str(project_id))
        if kind == "issue.reported" and reporter_kind == "worker":
            recipients.extend((rid, str(project_id), None) for rid in _project_secretary(connection, str(project_id)))
        elif kind == "issue.escalated" and source.get("escalated_from_issue_id") is not None:
            recipients.extend((rid, str(project_id), None) for rid in _fleet_first_mates(connection, str(project_id)))
        elif kind in {"issue.acknowledged", "issue.context_added"}:
            recipients.extend((rid, str(project_id), None) for rid in supervisor_recipients)
        elif kind == "issue.remediation_requested":
            recipients.extend((rid, str(project_id), 0) for rid in _project_secretary(connection, str(project_id)))
        elif kind == "issue.remediation_linked":
            row = connection.execute("SELECT workstream_id FROM issue_remediations WHERE issue_id=? ORDER BY created_at DESC LIMIT 1", (source_id,)).fetchone()
            if row is not None:
                recipients.append((str(row["workstream_id"]), str(project_id), 1))
        elif kind == "issue.verification_requested":
            if source.get("escalated_from_issue_id") is not None:
                underlying = _source(connection, "issue", str(source["escalated_from_issue_id"]))
                recipients.append((str(underlying["reporter_workstream_id"]), str(underlying["project_id"]), 1))
            else:
                recipients.append((str(source["reporter_workstream_id"]), str(project_id), 1))
        elif kind in {"issue.verification_failed", "issue.remediation_failed"}:
            recipients.extend((rid, str(project_id), 0) for rid in supervisor_recipients)
        elif kind == "issue.remediation_completed":
            recipients.extend((rid, str(project_id), 1) for rid in supervisor_recipients)
    elif kind == "workstream.completion_submitted":
        source_kind, source_id = "completion", payload.get("completionPacketId")
        if source_id is None:
            source_id = connection.execute("SELECT completion_packet_id FROM completion_packets WHERE packet_sha256=?", (payload.get("completionPacketSha256"),)).fetchone()
            source_id = None if source_id is None else source_id[0]
        if source_id is not None:
            source = _source(connection, source_kind, str(source_id))
            accepted = connection.execute("SELECT 1 FROM workstream_acceptances WHERE completion_packet_id=?", (source_id,)).fetchone()
            if accepted is None:
                recipients.extend((rid, str(project_id), 1) for rid in _project_secretary(connection, str(project_id)))
    elif kind.startswith("integration."):
        source_kind, source_id = "integration", payload.get("integrationId")
        if source_id is not None:
            source = _source(connection, source_kind, str(source_id))
            if kind == "integration.awaiting_worker":
                recipients.append((str(source["workstream_id"]), str(project_id), 0))
            elif kind == "integration.needs_attention":
                recipients.extend((rid, str(project_id), 0) for rid in _project_secretary(connection, str(project_id)))

    if source_kind is None or source_id is None:
        return
    for recipient, recipient_project, priority in recipients:
        _upsert(connection, recipient_workstream_id=recipient, project_id=recipient_project, source_kind=source_kind, source_id=str(source_id), event=event, priority=priority)


def list_open_attention(store: Any, *, recipient_workstream_id: str, limit: int = 32, due_only: bool = False) -> list[dict[str, Any]]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 32:
        raise InvalidRequestError("attention limit is invalid")
    due_clause = "" if not due_only else """
          AND (a.source_event_sequence > a.last_presented_revision
            OR a.last_presented_at IS NULL
            OR (julianday('now') - julianday(a.last_presented_at)) * 86400 >=
              CASE a.priority WHEN 0 THEN 300 WHEN 1 THEN 1800 ELSE 3600 END)
    """
    rows = store.conn.execute(
        f"""SELECT a.* FROM attention_items a
        JOIN workstreams recipient ON recipient.workstream_id=a.recipient_workstream_id
        JOIN runtime_bindings rb ON rb.workstream_id=recipient.workstream_id
        JOIN projects p ON p.project_id=a.project_id
        LEFT JOIN coordination_requests c ON a.source_kind='coordination' AND c.request_id=a.source_id
        LEFT JOIN research_requests r ON a.source_kind='research' AND r.request_id=a.source_id
        LEFT JOIN issues i ON a.source_kind='issue' AND i.issue_id=a.source_id
        LEFT JOIN completion_packets cp ON a.source_kind='completion' AND cp.completion_packet_id=a.source_id
        LEFT JOIN integration_jobs j ON a.source_kind='integration' AND j.integration_id=a.source_id
        WHERE a.recipient_workstream_id=? AND {runtime_eligible_sql("recipient")} AND recipient.provisioning_state='bound'
          AND ((recipient.kind='secretary' AND p.active=1 AND p.secretary_workstream_id=recipient.workstream_id)
            OR (recipient.kind='first_mate' AND p.active=1 AND (p.coordination_mode='fleet' OR (i.reporter_kind='secretary' AND i.escalated_from_issue_id IS NOT NULL)))
            OR recipient.kind='worker')
          AND ((a.source_kind='coordination' AND ((recipient.kind='secretary' AND c.state='open') OR (recipient.kind='worker' AND c.state='answered')))
            OR (a.source_kind='research' AND ((recipient.kind='secretary' AND r.state='pending') OR (recipient.kind='worker' AND r.state IN ('needs_context','answered','declined'))))
            OR (a.source_kind='issue' AND (
                (recipient.kind='secretary' AND (
                    (i.reporter_kind='worker' AND (i.state='open' OR (i.state='acknowledged' AND EXISTS (SELECT 1 FROM issue_updates u WHERE u.issue_id=i.issue_id AND u.update_kind='remediation_completed'))))
                    OR (i.reporter_kind='secretary' AND i.state='remediating'
                        AND EXISTS (SELECT 1 FROM issue_updates u WHERE u.issue_id=i.issue_id AND u.update_kind='remediation_requested')
                        AND NOT EXISTS (SELECT 1 FROM issue_updates u WHERE u.issue_id=i.issue_id AND u.update_kind IN ('remediation_linked','remediation_failed','resolved')))
                ))
                OR (recipient.kind='first_mate' AND i.reporter_kind='secretary' AND (i.state='open' OR (i.state='acknowledged' AND EXISTS (SELECT 1 FROM issue_updates u WHERE u.issue_id=i.issue_id AND u.update_kind='remediation_completed'))))
                OR (recipient.kind='secretary' AND i.reporter_kind='secretary' AND i.reporter_workstream_id=recipient.workstream_id AND i.state='verifying')
                OR (recipient.kind='worker' AND i.reporter_workstream_id=recipient.workstream_id AND i.state='verifying')
                OR (recipient.kind='worker' AND i.state='remediating'
                    AND EXISTS (SELECT 1 FROM issue_remediations rm WHERE rm.issue_id=i.issue_id AND rm.workstream_id=recipient.workstream_id)
                    AND NOT EXISTS (SELECT 1 FROM issue_updates u WHERE u.issue_id=i.issue_id AND u.actor_id=recipient.workstream_id AND u.update_kind IN ('remediation_started','remediation_completed','remediation_failed')))
            ))
            OR (a.source_kind='completion' AND cp.sequence=(SELECT MAX(sequence) FROM completion_packets WHERE workstream_id=cp.workstream_id) AND cp.completion_packet_id NOT IN (SELECT completion_packet_id FROM workstream_acceptances))
            OR (a.source_kind='integration' AND ((recipient.kind='worker' AND j.state='awaiting_worker') OR (recipient.kind='secretary' AND j.state='needs_attention')))){due_clause}
        ORDER BY a.priority,a.revision_at,a.recipient_workstream_id LIMIT ?""",
        (recipient_workstream_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def compact_attention(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "attentionId": str(row["attention_id"]),
        "sourceKind": str(row["source_kind"]),
        "sourceId": str(row["source_id"]),
        "priority": int(row["priority"]),
        "revision": int(row["source_event_sequence"]),
        "revisionAt": str(row["revision_at"]),
    }


def present_attention_in_transaction(connection: Any, *, recipient_workstream_id: str, attention_id: str, revision: int) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM attention_items WHERE attention_id=? AND recipient_workstream_id=?", (attention_id, recipient_workstream_id)).fetchone()
    if row is None:
        raise NotFoundError("attention item was not found")
    if int(row["source_event_sequence"]) != revision:
        raise ConflictError("attention item revision is stale")
    now = utc_now()
    connection.execute(
        "UPDATE attention_items SET last_presented_revision=?,first_presented_at=COALESCE(first_presented_at,?),last_presented_at=?,presentation_count=presentation_count+1,updated_at=? WHERE attention_id=? AND source_event_sequence=?",
        (revision, now, now, now, attention_id, revision),
    )
    return dict(connection.execute("SELECT * FROM attention_items WHERE attention_id=?", (attention_id,)).fetchone())


def backfill_attention(store: Any, *, recipient_workstream_id: str | None = None, after_sequence: int = 0, limit: int = 128) -> int:
    if after_sequence < 0 or not 1 <= limit <= 128:
        raise InvalidRequestError("attention backfill bounds are invalid")
    from .events import append_event_in_transaction

    recipient = None if recipient_workstream_id is None else store.conn.execute(
        "SELECT w.*,p.active AS project_active,p.coordination_mode FROM workstreams w LEFT JOIN projects p ON p.project_id=w.project_id WHERE w.workstream_id=? AND " + runtime_eligible_sql("w") + " AND w.provisioning_state='bound'",
        (recipient_workstream_id,),
    ).fetchone()
    if recipient_workstream_id is not None and recipient is None:
        return 0
    sources: list[tuple[str, str, str]] = []

    def add_rows(rows: Any, kind: str, project_column: str = "project_id") -> None:
        for row in rows:
            sources.append((kind, str(row["source_id"]), str(row[project_column])))

    if recipient is None or recipient["kind"] == "secretary":
        project_id = None if recipient is None else str(recipient["project_id"])
        project_filter = "" if project_id is None else " AND project_id=?"
        params: tuple[Any, ...] = () if project_id is None else (project_id,)
        add_rows(store.conn.execute(f"SELECT request_id AS source_id,project_id FROM coordination_requests WHERE state='open'{project_filter}", params), "coordination")
        add_rows(store.conn.execute(f"SELECT request_id AS source_id,project_id FROM research_requests WHERE state='pending'{project_filter}", params), "research")
        add_rows(store.conn.execute(f"SELECT issue_id AS source_id,project_id FROM issues WHERE ((reporter_kind='worker' AND (state='open' OR (state='acknowledged' AND EXISTS (SELECT 1 FROM issue_updates u WHERE u.issue_id=issues.issue_id AND u.update_kind='remediation_completed')))) OR (reporter_kind='secretary' AND state='remediating' AND EXISTS (SELECT 1 FROM issue_updates u WHERE u.issue_id=issues.issue_id AND u.update_kind='remediation_requested') AND NOT EXISTS (SELECT 1 FROM issue_updates u WHERE u.issue_id=issues.issue_id AND u.update_kind IN ('remediation_linked','remediation_failed','resolved'))) OR (reporter_kind='secretary' AND reporter_workstream_id=? AND state='verifying')){project_filter}", ((str(recipient["workstream_id"]),) + params) if project_id is not None else (str(recipient["workstream_id"]),)), "issue")
        add_rows(store.conn.execute(f"SELECT cp.completion_packet_id AS source_id,ws.project_id FROM completion_packets cp JOIN workstreams ws USING(workstream_id) WHERE cp.sequence=(SELECT MAX(sequence) FROM completion_packets WHERE workstream_id=cp.workstream_id) AND cp.completion_packet_id NOT IN (SELECT completion_packet_id FROM workstream_acceptances){'' if project_id is None else ' AND ws.project_id=?'}", (() if project_id is None else (project_id,))), "completion")
        add_rows(store.conn.execute(f"SELECT integration_id AS source_id,project_id FROM integration_jobs WHERE state='needs_attention'{project_filter}", params), "integration")
    elif recipient["kind"] == "first_mate":
        add_rows(store.conn.execute("SELECT i.issue_id AS source_id,i.project_id FROM issues i WHERE i.project_id IN (" + ",".join("?" for _ in first_mate_issue_project_ids(store)) + ") AND i.reporter_kind='secretary' AND i.escalated_from_issue_id IS NOT NULL AND (i.state='open' OR (i.state='acknowledged' AND EXISTS (SELECT 1 FROM issue_updates u WHERE u.issue_id=i.issue_id AND u.update_kind='remediation_completed')))" , first_mate_issue_project_ids(store)), "issue")
    else:
        workstream_id = str(recipient["workstream_id"])
        add_rows(store.conn.execute("SELECT request_id AS source_id,project_id FROM coordination_requests WHERE workstream_id=? AND state='answered'", (workstream_id,)), "coordination")
        add_rows(store.conn.execute("SELECT request_id AS source_id,project_id FROM research_requests WHERE workstream_id=? AND state IN ('needs_context','answered','declined')", (workstream_id,)), "research")
        add_rows(store.conn.execute("SELECT issue_id AS source_id,project_id FROM issues WHERE reporter_workstream_id=? AND state='verifying' UNION SELECT i.issue_id AS source_id,i.project_id FROM issues i JOIN issue_remediations rm ON rm.issue_id=i.issue_id WHERE rm.workstream_id=? AND i.state='remediating' AND NOT EXISTS (SELECT 1 FROM issue_updates u WHERE u.issue_id=i.issue_id AND u.actor_id=? AND u.update_kind IN ('remediation_started','remediation_completed','remediation_failed'))", (workstream_id, workstream_id, workstream_id)), "issue")
        add_rows(store.conn.execute("SELECT integration_id AS source_id,project_id FROM integration_jobs WHERE workstream_id=? AND state='awaiting_worker'", (workstream_id,)), "integration")

    inserted = 0
    transaction = store.transaction() if not store.conn.in_transaction else nullcontext(store.conn)
    with transaction:
        for source_kind, source_id, project_id in sources:
            if inserted >= limit:
                break
            if recipient_workstream_id is not None and store.conn.execute("SELECT 1 FROM attention_items WHERE recipient_workstream_id=? AND source_kind=? AND source_id=?", (recipient_workstream_id, source_kind, source_id)).fetchone() is not None:
                continue
            recipients = [recipient_workstream_id] if recipient_workstream_id is not None else []
            if recipient_workstream_id is None:
                if source_kind == "issue":
                    source = _source(store.conn, source_kind, source_id)
                    recipients = list(_fleet_first_mates(store.conn, project_id)) if source.get("reporter_kind") == "secretary" and source.get("escalated_from_issue_id") is not None else list(_project_secretary(store.conn, project_id))
                else:
                    recipients = list(_project_secretary(store.conn, project_id))
            for target in recipients:
                if target is None or store.conn.execute("SELECT 1 FROM attention_items WHERE recipient_workstream_id=? AND source_kind=? AND source_id=?", (target, source_kind, source_id)).fetchone() is not None:
                    continue
                event = append_event_in_transaction(store.conn, kind="attention.backfilled", project_id=project_id, workstream_id=target, payload={"projectId": project_id, "recipientWorkstreamId": target, "sourceKind": source_kind, "sourceId": source_id, "reason": "supervisor_bound"})
                _upsert(store.conn, recipient_workstream_id=target, project_id=project_id, source_kind=source_kind, source_id=source_id, event=event)
                inserted += 1
                if inserted >= limit:
                    break
    return inserted
