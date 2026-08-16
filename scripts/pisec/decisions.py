"""Durable project decisions."""

from __future__ import annotations

from typing import Any

from .events import append_event_in_transaction
from .models import ConflictError, InvalidRequestError, NotFoundError, bounded_text, canonical_json, new_id, utc_now, validate_id
from .projects import get_project


def list_decisions(store: Any, project_id: str, *, state: str | None = None) -> list[dict[str, Any]]:
    get_project(store, project_id)
    if state not in (None, "open", "resolved"):
        raise InvalidRequestError("invalid decision state")
    sql = "SELECT * FROM decisions WHERE project_id=?"
    parameters: tuple[Any, ...] = (project_id,)
    if state is not None:
        sql += " AND state=?"
        parameters += (state,)
    return [dict(row) for row in store.conn.execute(sql + " ORDER BY created_at,decision_id", parameters)]


def record_decision(store: Any, *, project_id: str, summary: str, context: Any, workstream_id: str | None = None) -> dict[str, Any]:
    get_project(store, project_id)
    if workstream_id is not None:
        validate_id(workstream_id, prefix="ws")
        row = store.conn.execute("SELECT project_id FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()
        if row is None or row["project_id"] != project_id:
            raise NotFoundError("workstream was not found in the project")
    decision_id = new_id("dec")
    now = utc_now()
    context_json = canonical_json(context, max_bytes=64 * 1024)
    text = bounded_text(summary, name="summary", limit=512)
    with store.transaction():
        store.conn.execute("INSERT INTO decisions(decision_id,project_id,workstream_id,summary,context_json,state,created_at,updated_at) VALUES(?,?,?,?,?,'open',?,?)", (decision_id, project_id, workstream_id, text, context_json, now, now))
        append_event_in_transaction(store.conn, kind="decision.recorded", project_id=project_id, workstream_id=workstream_id, payload={"decisionId": decision_id, "summary": text})
    return dict(store.conn.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone())


def resolve_decision(store: Any, *, project_id: str, decision_id: str, resolution: str) -> dict[str, Any]:
    get_project(store, project_id)
    validate_id(decision_id, prefix="dec")
    text = bounded_text(resolution, name="resolution", limit=4096)
    row = store.conn.execute("SELECT * FROM decisions WHERE decision_id=? AND project_id=?", (decision_id, project_id)).fetchone()
    if row is None:
        raise NotFoundError("decision was not found")
    if row["state"] == "resolved":
        if row["resolution"] != text:
            raise ConflictError("decision is already resolved differently")
        return dict(row)
    now = utc_now()
    with store.transaction():
        store.conn.execute("UPDATE decisions SET state='resolved',resolution=?,resolved_at=?,updated_at=? WHERE decision_id=? AND state='open'", (text, now, now, decision_id))
        append_event_in_transaction(store.conn, kind="decision.resolved", project_id=project_id, workstream_id=row["workstream_id"], payload={"decisionId": decision_id, "resolution": text})
    return dict(store.conn.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone())
