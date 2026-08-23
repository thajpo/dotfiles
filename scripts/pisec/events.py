"""Transactional immutable Pisec audit events."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from .models import canonical_json, new_id, utc_now


def append_event_in_transaction(
    connection: sqlite3.Connection,
    *,
    kind: str,
    payload: Any,
    project_id: str | None = None,
    workstream_id: str | None = None,
    operation_id: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    eid = event_id or new_id("evt")
    created = utc_now()
    cursor = connection.execute(
        "INSERT INTO events(event_id,kind,project_id,workstream_id,operation_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
        (eid, kind, project_id, workstream_id, operation_id, canonical_json(payload), created),
    )
    row = connection.execute("SELECT * FROM events WHERE sequence=?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def append_event(store: Any, **kwargs: Any) -> dict[str, Any]:
    with store.transaction():
        return append_event_in_transaction(store.conn, **kwargs)


def list_events(store: Any, *, after: int = 0, limit: int = 256, project_ids: Sequence[str] | None = None) -> list[dict[str, Any]]:
    if not isinstance(after, int) or isinstance(after, bool) or after < 0:
        raise ValueError("after must be a non-negative integer")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    params: list[Any] = [after]
    where = ["sequence>?"]
    if project_ids is not None:
        ids = list(dict.fromkeys(project_ids))
        if not ids:
            return []
        where.append("project_id IN (" + ",".join("?" for _ in ids) + ")")
        params.extend(ids)
    params.append(limit)
    return [dict(row) for row in store.conn.execute("SELECT * FROM events WHERE " + " AND ".join(where) + " ORDER BY sequence LIMIT ?", params)]
