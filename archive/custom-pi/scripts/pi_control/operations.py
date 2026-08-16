"""Transactional operation, CAS, and run helpers for the Phase 2 store."""

from __future__ import annotations

from typing import Any, Mapping

from .errors import ConstraintError, IdempotencyConflictError, NotFoundError, error_from_exception
from .models import OperationRecord, canonical_json, json_digest, new_id, utc_now


def _record(store: Any, operation_id: str) -> OperationRecord:
    row = store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row is None:
        raise NotFoundError("operation was not found", detail={"operation_id": operation_id})
    return OperationRecord.from_row(dict(row))


def create_operation(
    store: Any,
    *,
    idempotency_key: str,
    kind: str,
    resource_type: str,
    resource_id: str,
    actor_type: str,
    request: Any,
    actor_id: str | None = None,
    authorization_id: str | None = None,
    expected_resource_version: int | None = None,
    writer_epoch: int | None = None,
    state: str = "planned",
    step: str = "intent-recorded",
    operation_id: str | None = None,
) -> OperationRecord:
    if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 256 or "\x00" in idempotency_key:
        raise ValueError("idempotency key is invalid")
    request_json = canonical_json(request)
    request_digest = json_digest(request)
    with store.transaction():
        existing = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing is not None:
            existing_digest = str(existing["request_digest"])
            same_binding = (
                existing["kind"] == kind
                and existing["resource_type"] == resource_type
                and existing["resource_id"] == resource_id
                and existing["actor_type"] == actor_type
                and existing["actor_id"] == actor_id
                and existing["authorization_id"] == authorization_id
                and existing["expected_resource_version"] == expected_resource_version
                and existing["writer_epoch"] == writer_epoch
            )
            if existing_digest != request_digest or not same_binding:
                raise IdempotencyConflictError(idempotency_key, existing_digest=existing_digest, request_digest=request_digest)
            return OperationRecord.from_row(dict(existing))
        op_id = operation_id or new_id("op")
        now = utc_now()
        try:
            store.conn.execute(
                "INSERT INTO operations(operation_id,idempotency_key,kind,resource_type,resource_id,actor_type,actor_id,authorization_id,request_digest,expected_resource_version,writer_epoch,state,step,request_json,result_json,created_at,updated_at,completed_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (op_id, idempotency_key, kind, resource_type, resource_id, actor_type, actor_id, authorization_id, request_digest, expected_resource_version, writer_epoch, state, step, request_json, None, now, now, None, None, None),
            )
        except Exception as error:
            raise error_from_exception(error) from error
        return _record(store, op_id)


def update_operation_in_transaction(
    connection: Any,
    operation_id: str,
    *,
    state: str,
    step: str,
    result: Any = None,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> None:
    """Advance an operation monotonically without a nested transaction."""

    terminal = {"succeeded", "failed", "needs_attention", "cancelled"}
    allowed = {"planned", "applying", *terminal}
    if state not in allowed:
        raise ValueError("operation state is invalid")
    current = connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if current is None:
        raise NotFoundError("operation was not found", detail={"operation_id": operation_id})
    current_state = str(current["state"])
    result_json = None if result is None else canonical_json(result)
    if current_state in terminal:
        identical = (
            current_state == state
            and current["step"] == step
            and current["result_json"] == result_json
            and current["error_code"] == error_code
            and current["error_detail"] == error_detail
        )
        if identical:
            return
        raise ConstraintError("terminal operation outcome is immutable", detail={"operation_id": operation_id, "state": current_state})
    if current_state == "applying" and state == "planned":
        raise ConstraintError("operation state cannot move backward", detail={"operation_id": operation_id, "state": current_state})
    completed_at = utc_now() if state in terminal else None
    cursor = connection.execute(
        "UPDATE operations SET state=?,step=?,result_json=?,updated_at=?,completed_at=?,error_code=?,error_detail=? WHERE operation_id=? AND state=?",
        (state, step, result_json, utc_now(), completed_at, error_code, error_detail, operation_id, current_state),
    )
    if cursor.rowcount != 1:
        raise ConstraintError("operation state changed during update", detail={"operation_id": operation_id, "state": current_state})


def update_operation(
    store: Any,
    operation_id: str,
    *,
    state: str,
    step: str,
    result: Any = None,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> OperationRecord:
    with store.transaction():
        update_operation_in_transaction(
            store.conn, operation_id, state=state, step=step, result=result,
            error_code=error_code, error_detail=error_detail,
        )
    return _record(store, operation_id)


def complete_operation(store: Any, operation_id: str, *, result: Any = None, step: str = "completed") -> OperationRecord:
    return update_operation(store, operation_id, state="succeeded", step=step, result=result)


def fail_operation(store: Any, operation_id: str, *, code: str, detail: str | None = None, step: str = "failed") -> OperationRecord:
    return update_operation(store, operation_id, state="failed", step=step, error_code=code, error_detail=detail)


def mutate_with_event(
    store: Any,
    mutation: Any,
    *,
    event_kind: str,
    resource_type: str,
    resource_id: str,
    payload: Any,
    resource_version: int | None = None,
    operation_id: str | None = None,
) -> Any:
    """Run a SQLite-only mutation and outbox insert in one transaction."""

    with store.transaction():
        result = mutation(store.conn)
        from .events import append_event_in_transaction
        append_event_in_transaction(
            store.conn,
            event_kind=event_kind,
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload,
            resource_version=resource_version,
            operation_id=operation_id,
        )
        return result


__all__ = [
    "complete_operation",
    "create_operation",
    "fail_operation",
    "mutate_with_event",
    "update_operation",
    "update_operation_in_transaction",
]
