"""Durable journal for external provisioning effects.

The journal is represented by immutable events so it works with the existing
Pisec v1 database without an unsafe live schema migration.  A step is only
recoverable after its confirmed event records the exact observed identity.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .events import append_event_in_transaction
from .models import ConflictError, NeedsAttentionError, json_digest, utc_now


EFFECT_INTENDED = "provisioning.effect.intended"
EFFECT_CONFIRMED = "provisioning.effect.confirmed"
EFFECT_COMPENSATED = "provisioning.effect.compensated"
EFFECT_STEPS = (
    "worker_repository",
    "herdr_tab",
    "runtime_surface",
    "runtime_profile_staged",
    "runtime_profile_activated",
    "runtime_binding",
    "agent_started",
    "bootstrap_delivered",
    "final_identity",
)


def _events(store: Any, operation_id: str, *, step: str | None = None) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT sequence,kind,payload_json,created_at FROM events WHERE operation_id=? AND kind IN (?,?,?) ORDER BY sequence",
        (operation_id, EFFECT_INTENDED, EFFECT_CONFIRMED, EFFECT_COMPENSATED),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise NeedsAttentionError("provisioning effect journal contains invalid JSON") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("step"), str) or not isinstance(payload.get("identity"), dict):
            raise NeedsAttentionError("provisioning effect journal entry is incomplete")
        if step is None or payload["step"] == step:
            result.append({"sequence": int(row["sequence"]), "kind": str(row["kind"]), "createdAt": row["created_at"], **payload})
    return result


def _events_in_connection(connection: Any, operation_id: str, *, step: str | None = None) -> list[dict[str, Any]]:
    class StoreView:
        conn = connection

    return _events(StoreView(), operation_id, step=step)


def _latest(store: Any, operation_id: str, step: str) -> dict[str, Any] | None:
    entries = _events(store, operation_id, step=step)
    return entries[-1] if entries else None


def effect_state(store: Any, operation_id: str, step: str) -> dict[str, Any] | None:
    """Return the latest journal state for one operation/effect step."""
    latest = _latest(store, operation_id, step)
    if latest is None:
        return None
    if latest["kind"] == EFFECT_INTENDED:
        return {"state": "intended", **latest}
    if latest["kind"] == EFFECT_CONFIRMED:
        return {"state": "confirmed", **latest}
    return {"state": "compensated", **latest}


def journal_entries(store: Any, operation_id: str) -> list[dict[str, Any]]:
    """Return one current state per journal step, in provisioning order."""
    entries: list[dict[str, Any]] = []
    for step in EFFECT_STEPS:
        current = effect_state(store, operation_id, step)
        if current is not None:
            entries.append(current)
    return entries


def _check_identity(current: Mapping[str, Any] | None, identity: Mapping[str, Any], *, step: str) -> None:
    if current is None:
        return
    expected = current.get("identity")
    if not isinstance(expected, Mapping) or any(key in expected and expected[key] != value for key, value in identity.items()):
        raise NeedsAttentionError(f"provisioning effect identity changed for {step}")


def _intent_in_transaction(
    connection: Any,
    *,
    operation_id: str,
    project_id: str,
    workstream_id: str,
    step: str,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    if step not in EFFECT_STEPS:
        raise ValueError("unknown provisioning effect step")
    normalized = dict(identity)
    entries = _events_in_connection(connection, operation_id, step=step)
    current = None if not entries else effect_state_from_entries(entries)
    _check_identity(current, normalized, step=step)
    if current is not None:
        return current
    payload = {
        "operationId": operation_id,
        "step": step,
        "identity": normalized,
        "identitySha256": json_digest(normalized),
        "state": "intended",
        "recordedAt": utc_now(),
    }
    append_event_in_transaction(
        connection,
        kind=EFFECT_INTENDED,
        project_id=project_id,
        workstream_id=workstream_id,
        operation_id=operation_id,
        payload=payload,
    )
    return {"state": "intended", **payload}


def effect_state_from_entries(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not entries:
        return None
    latest = entries[-1]
    if latest["kind"] == EFFECT_INTENDED:
        state = "intended"
    elif latest["kind"] == EFFECT_CONFIRMED:
        state = "confirmed"
    else:
        state = "compensated"
    return {"state": state, **latest}


def journal_intent(
    store: Any,
    *,
    operation_id: str,
    project_id: str,
    workstream_id: str,
    step: str,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist the deterministic identity before an external effect starts."""
    with store.transaction():
        return _intent_in_transaction(
            store.conn,
            operation_id=operation_id,
            project_id=project_id,
            workstream_id=workstream_id,
            step=step,
            identity=identity,
        )


def journal_intent_in_transaction(
    connection: Any,
    *,
    operation_id: str,
    project_id: str,
    workstream_id: str,
    step: str,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    return _intent_in_transaction(
        connection,
        operation_id=operation_id,
        project_id=project_id,
        workstream_id=workstream_id,
        step=step,
        identity=identity,
    )


def journal_confirm(
    store: Any,
    *,
    operation_id: str,
    project_id: str,
    workstream_id: str,
    step: str,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Record an observed identity only after the owning adapter corroborates it."""
    with store.transaction():
        return _confirm_in_transaction(
            store.conn,
            operation_id=operation_id,
            project_id=project_id,
            workstream_id=workstream_id,
            step=step,
            identity=identity,
        )


def _confirm_in_transaction(
    connection: Any,
    *,
    operation_id: str,
    project_id: str,
    workstream_id: str,
    step: str,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    if step not in EFFECT_STEPS:
        raise ValueError("unknown provisioning effect step")
    normalized = dict(identity)
    entries = _events_in_connection(connection, operation_id, step=step)
    current = None if not entries else effect_state_from_entries(entries)
    if current is None:
        raise ConflictError(f"provisioning effect {step} was confirmed without intent")
    _check_identity(current, normalized, step=step)
    if current["state"] == "confirmed":
        return current
    if current["state"] == "compensated":
        raise NeedsAttentionError(f"provisioning effect {step} was already compensated")
    payload = {
        "operationId": operation_id,
        "step": step,
        "identity": normalized,
        "identitySha256": json_digest(normalized),
        "state": "confirmed",
        "recordedAt": utc_now(),
    }
    append_event_in_transaction(
        connection,
        kind=EFFECT_CONFIRMED,
        project_id=project_id,
        workstream_id=workstream_id,
        operation_id=operation_id,
        payload=payload,
    )
    return {"state": "confirmed", **payload}


def journal_confirm_in_transaction(
    connection: Any,
    *,
    operation_id: str,
    project_id: str,
    workstream_id: str,
    step: str,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    return _confirm_in_transaction(
        connection,
        operation_id=operation_id,
        project_id=project_id,
        workstream_id=workstream_id,
        step=step,
        identity=identity,
    )


def journal_compensate(
    store: Any,
    *,
    operation_id: str,
    project_id: str,
    workstream_id: str,
    step: str,
    identity: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Record a successful compensation of a confirmed owned effect."""
    current = effect_state(store, operation_id, step)
    if current is None:
        return {"state": "absent", "step": step, "identity": dict(identity)}
    _check_identity(current, dict(identity), step=step)
    if current["state"] == "compensated":
        return current
    if current["state"] != "confirmed":
        raise NeedsAttentionError(f"provisioning effect {step} is not confirmed for compensation")
    payload = {
        "operationId": operation_id,
        "step": step,
        "identity": dict(identity),
        "identitySha256": json_digest(dict(identity)),
        "state": "compensated",
        "reason": str(reason)[:1024],
        "recordedAt": utc_now(),
    }
    with store.transaction():
        append_event_in_transaction(
            store.conn,
            kind=EFFECT_COMPENSATED,
            project_id=project_id,
            workstream_id=workstream_id,
            operation_id=operation_id,
            payload=payload,
        )
    return {"state": "compensated", **payload}
