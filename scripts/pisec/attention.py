"""Durable current delivery index for typed Pisec source records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .models import ConflictError, InvalidRequestError, NotFoundError, new_id, utc_now


SOURCE_KINDS = frozenset({"coordination", "research", "issue", "completion", "integration"})
ATTENTION_WAKE_PROMPT = "Pisec has pending attention. Review it with the Pisec attention tools before ending this turn."


def _priority(source_kind: str, source: Mapping[str, Any], event_kind: str) -> int:
    if source_kind == "coordination":
        return 0 if bool(source.get("blocking")) else (2 if source.get("kind") == "review_request" else 1)
    if source_kind == "research":
        return 1
    if source_kind == "issue":
        return 0 if source.get("severity") == "blocking" or event_kind in {"issue.verification_failed", "issue.remediation_failed"} else (2 if source.get("severity") == "improvement" else 1)
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


def _project_secretary(connection: Any, project_id: str) -> Iterable[str]:
    rows = connection.execute(
        "SELECT secretary_workstream_id FROM projects WHERE project_id=? AND secretary_workstream_id IS NOT NULL",
        (project_id,),
    ).fetchall()
    return (str(row["secretary_workstream_id"]) for row in rows)


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
        if kind == "issue.reported":
            if reporter_kind == "worker":
                recipients.extend((rid, str(project_id), None) for rid in _project_secretary(connection, str(project_id)))
            else:
                recipients.extend((rid, str(project_id), None) for rid in connection.execute("SELECT workstream_id FROM workstreams WHERE kind='first_mate' AND desired_state='active'"))
        elif kind == "issue.escalated":
            recipients.extend((rid, str(project_id), None) for rid in connection.execute("SELECT workstream_id FROM workstreams WHERE kind='first_mate' AND desired_state='active'"))
        elif kind == "issue.remediation_requested":
            recipients.extend((rid, str(project_id), 0) for rid in _project_secretary(connection, str(project_id)))
        elif kind == "issue.remediation_linked":
            row = connection.execute("SELECT workstream_id FROM issue_remediations WHERE issue_id=? ORDER BY created_at DESC LIMIT 1", (source_id,)).fetchone()
            if row is not None:
                recipients.append((str(row["workstream_id"]), str(project_id), 1))
        elif kind == "issue.verification_requested":
            recipients.append((str(source["reporter_workstream_id"]), str(project_id), 1))
        elif kind == "issue.verification_failed":
            if reporter_kind == "worker":
                recipients.extend((rid, str(project_id), 0) for rid in _project_secretary(connection, str(project_id)))
            else:
                recipients.extend((rid, str(project_id), 0) for rid in connection.execute("SELECT workstream_id FROM workstreams WHERE kind='first_mate' AND desired_state='active'"))
        elif kind == "issue.remediation_completed":
            if reporter_kind == "worker":
                recipients.extend((rid, str(project_id), 1) for rid in _project_secretary(connection, str(project_id)))
            else:
                recipients.extend((rid, str(project_id), 1) for rid in connection.execute("SELECT workstream_id FROM workstreams WHERE kind='first_mate' AND desired_state='active'"))
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


def list_open_attention(store: Any, *, recipient_workstream_id: str, limit: int = 32) -> list[dict[str, Any]]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 32:
        raise InvalidRequestError("attention limit is invalid")
    rows = store.conn.execute(
        """SELECT a.* FROM attention_items a
        JOIN workstreams recipient ON recipient.workstream_id=a.recipient_workstream_id
        LEFT JOIN coordination_requests c ON a.source_kind='coordination' AND c.request_id=a.source_id
        LEFT JOIN research_requests r ON a.source_kind='research' AND r.request_id=a.source_id
        LEFT JOIN issues i ON a.source_kind='issue' AND i.issue_id=a.source_id
        LEFT JOIN completion_packets cp ON a.source_kind='completion' AND cp.completion_packet_id=a.source_id
        LEFT JOIN integration_jobs j ON a.source_kind='integration' AND j.integration_id=a.source_id
        WHERE a.recipient_workstream_id=? AND recipient.desired_state='active'
          AND ((a.source_kind='coordination' AND ((recipient.kind='secretary' AND c.state='open') OR (recipient.kind='worker' AND c.state='answered')))
            OR (a.source_kind='research' AND ((recipient.kind='secretary' AND r.state='pending') OR (recipient.kind='worker' AND r.state IN ('needs_context','answered','declined'))))
            OR (a.source_kind='issue' AND (
                (recipient.kind='secretary' AND (
                    (i.reporter_kind='worker' AND i.state IN ('open','acknowledged'))
                    OR (i.reporter_kind='secretary' AND i.state='remediating'
                        AND EXISTS (SELECT 1 FROM issue_updates u WHERE u.issue_id=i.issue_id AND u.update_kind='remediation_requested')
                        AND NOT EXISTS (SELECT 1 FROM issue_updates u WHERE u.issue_id=i.issue_id AND u.update_kind IN ('remediation_linked','remediation_failed','resolved')))
                ))
                OR (recipient.kind='first_mate' AND i.reporter_kind='secretary' AND i.state IN ('open','acknowledged'))
                OR (recipient.kind='worker' AND i.reporter_workstream_id=recipient.workstream_id AND i.state='verifying')
                OR (recipient.kind='worker' AND i.state='remediating'
                    AND EXISTS (SELECT 1 FROM issue_remediations rm WHERE rm.issue_id=i.issue_id AND rm.workstream_id=recipient.workstream_id)
                    AND NOT EXISTS (SELECT 1 FROM issue_updates u WHERE u.issue_id=i.issue_id AND u.actor_id=recipient.workstream_id AND u.update_kind IN ('remediation_started','remediation_completed','remediation_failed')))
            ))
            OR (a.source_kind='completion' AND cp.sequence=(SELECT MAX(sequence) FROM completion_packets WHERE workstream_id=cp.workstream_id) AND cp.completion_packet_id NOT IN (SELECT completion_packet_id FROM workstream_acceptances))
            OR (a.source_kind='integration' AND ((recipient.kind='worker' AND j.state='awaiting_worker') OR (recipient.kind='secretary' AND j.state='needs_attention'))))
        ORDER BY a.priority,a.revision_at,a.recipient_workstream_id LIMIT ?""",
        (recipient_workstream_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


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


def backfill_attention(store: Any, *, after_sequence: int = 0, limit: int = 256) -> int:
    if after_sequence < 0 or not 1 <= limit <= 1000:
        raise InvalidRequestError("attention backfill bounds are invalid")
    rows = store.conn.execute("SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT ?", (after_sequence, limit)).fetchall()
    with store.transaction():
        for event in rows:
            index_event_in_transaction(store.conn, event)
    return len(rows)
