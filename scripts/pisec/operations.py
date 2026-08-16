"""Idempotent durable operation records."""

from __future__ import annotations

from typing import Any, Iterable

from .models import ConflictError, IdempotencyConflictError, NotFoundError, OperationRecord, canonical_json, json_digest, new_id, utc_now

_OPERATION_STATES = frozenset({"planned", "applying", "succeeded", "failed", "needs_attention", "cancelled"})


def _record(store: Any, operation_id: str) -> OperationRecord:
    row = store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row is None:
        raise NotFoundError("operation was not found", detail={"operation_id": operation_id})
    return OperationRecord.from_row(dict(row))


def create_operation(
    store: Any,
    *,
    kind: str,
    idempotency_key: str,
    request: Any,
    project_id: str | None = None,
    workstream_id: str | None = None,
    operation_id: str | None = None,
    state: str = "planned",
    step: str = "planned",
) -> tuple[OperationRecord, bool]:
    if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 256 or "\x00" in idempotency_key:
        raise ValueError("idempotency key is invalid")
    request_json = canonical_json(request)
    digest = json_digest(request)
    existing = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    if existing is not None:
        record = OperationRecord.from_row(dict(existing))
        if record.request_sha256 != digest or record.kind != kind:
            raise IdempotencyConflictError("idempotency key is already bound to another request")
        return record, False
    op_id = operation_id or new_id("op")
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "INSERT INTO operations(operation_id,kind,project_id,workstream_id,idempotency_key,request_json,request_sha256,state,step,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (op_id, kind, project_id, workstream_id, idempotency_key, request_json, digest, state, step, now, now),
        )
    return _record(store, op_id), True


def update_operation_in_transaction(
    connection: Any,
    operation_id: str,
    *,
    state: str,
    step: str,
    expected_states: Iterable[str],
    result: Any | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    if state not in _OPERATION_STATES:
        raise ValueError("invalid operation state")
    expected = tuple(expected_states)
    row = connection.execute("SELECT state FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row is None:
        raise NotFoundError("operation was not found")
    if row["state"] not in expected:
        raise ConflictError("operation state changed", detail={"state": row["state"], "expected": expected})
    result_json = None if result is None else canonical_json(result)
    cursor = connection.execute(
        "UPDATE operations SET state=?,step=?,result_json=?,error_code=?,error_message=?,updated_at=? WHERE operation_id=? AND state=?",
        (state, step, result_json, error_code, error_message, utc_now(), operation_id, row["state"]),
    )
    if cursor.rowcount != 1:
        raise ConflictError("operation state changed during update")


def update_operation(store: Any, operation_id: str, **kwargs: Any) -> OperationRecord:
    with store.transaction():
        update_operation_in_transaction(store.conn, operation_id, **kwargs)
    return _record(store, operation_id)
