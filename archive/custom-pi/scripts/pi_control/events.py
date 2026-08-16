"""Transactional outbox events and consumer cursors."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from .errors import ConstraintError, ResourceStaleError, error_from_exception
from .models import EventRecord, canonical_json, new_id, utc_now


def append_event_in_transaction(
    connection: sqlite3.Connection,
    *,
    event_kind: str,
    resource_type: str,
    resource_id: str,
    payload: Any,
    resource_version: int | None = None,
    operation_id: str | None = None,
    event_id: str | None = None,
) -> EventRecord:
    event_json = canonical_json(payload)
    created_at = utc_now()
    eid = event_id or new_id("evt")
    try:
        cursor = connection.execute(
            "INSERT INTO control_events(event_id,event_kind,resource_type,resource_id,resource_version,operation_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (eid, event_kind, resource_type, resource_id, resource_version, operation_id, event_json, created_at),
        )
    except sqlite3.Error as error:
        raise error_from_exception(error) from error
    row = connection.execute("SELECT * FROM control_events WHERE sequence=?", (cursor.lastrowid,)).fetchone()
    return EventRecord.from_row(dict(row))


def append_event(store: Any, **kwargs: Any) -> EventRecord:
    with store.transaction():
        return append_event_in_transaction(store.conn, **kwargs)


def get_events(store: Any, *, after: int = 0, limit: int = 256) -> list[EventRecord]:
    if not isinstance(after, int) or after < 0:
        raise ValueError("after must be non-negative")
    if not isinstance(limit, int) or limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    rows = store.conn.execute("SELECT * FROM control_events WHERE sequence>? ORDER BY sequence LIMIT ?", (after, limit))
    return [EventRecord.from_row(dict(row)) for row in rows]


def _validate_consumer_id(consumer_id: str) -> None:
    if not isinstance(consumer_id, str) or not consumer_id or len(consumer_id) > 256 or "\x00" in consumer_id:
        raise ValueError("consumer ID is invalid")


def ensure_consumer_in_transaction(connection: sqlite3.Connection, consumer_id: str) -> None:
    _validate_consumer_id(consumer_id)
    connection.execute(
        "INSERT OR IGNORE INTO event_consumers(consumer_id,last_sequence,updated_at) VALUES(?,?,?)",
        (consumer_id, 0, utc_now()),
    )


def acknowledge(store: Any, consumer_id: str, sequence: int) -> None:
    _validate_consumer_id(consumer_id)
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise ValueError("sequence must be non-negative")
    with store.transaction():
        ensure_consumer_in_transaction(store.conn, consumer_id)
        current = int(store.conn.execute("SELECT last_sequence FROM event_consumers WHERE consumer_id=?", (consumer_id,)).fetchone()[0])
        if sequence < current:
            raise ResourceStaleError(consumer_id, current, sequence)
        if sequence == current:
            return
        if store.conn.execute("SELECT 1 FROM control_events WHERE sequence=?", (sequence,)).fetchone() is None:
            raise ConstraintError("consumer cannot acknowledge an event sequence that was not emitted", detail={"consumer_id": consumer_id, "sequence": sequence})
        cursor = store.conn.execute("UPDATE event_consumers SET last_sequence=?,updated_at=? WHERE consumer_id=? AND last_sequence=?", (sequence, utc_now(), consumer_id, current))
        if cursor.rowcount != 1:
            actual = store.conn.execute("SELECT last_sequence FROM event_consumers WHERE consumer_id=?", (consumer_id,)).fetchone()
            raise ResourceStaleError(consumer_id, current, int(actual[0]) if actual is not None else None)


def consume_once(store: Any, consumer_id: str, *, limit: int = 256) -> list[EventRecord]:
    _validate_consumer_id(consumer_id)
    row = store.conn.execute("SELECT last_sequence FROM event_consumers WHERE consumer_id=?", (consumer_id,)).fetchone()
    after = int(row[0]) if row is not None else 0
    return get_events(store, after=after, limit=limit)


__all__ = [
    "acknowledge",
    "append_event",
    "append_event_in_transaction",
    "consume_once",
    "ensure_consumer_in_transaction",
    "get_events",
]
