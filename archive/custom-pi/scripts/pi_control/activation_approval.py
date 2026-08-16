"""One-use activation approvals bound to an exact build and cutover plan."""

from __future__ import annotations

import json
from typing import Any, Mapping

from .errors import ConstraintError, IdempotencyConflictError, NotFoundError
from .events import append_event_in_transaction
from .models import canonical_json, json_digest, new_id, utc_now, validate_id


class ActivationApprovalError(RuntimeError):
    pass


def _scope(store: Any, *, build_id: str, staged_root: str, data_root: str, rollback_plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "buildId": build_id,
        "stagedRoot": staged_root,
        "dataRoot": data_root,
        "rollbackPlan": dict(rollback_plan),
    }


def request_activation_approval(
    store: Any,
    *,
    build_id: str,
    staged_root: str,
    data_root: str,
    rollback_plan: Mapping[str, Any],
    actor_id: str,
    approval_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(build_id, str) or not build_id:
        raise ActivationApprovalError("activation build identity is invalid")
    if not isinstance(staged_root, str) or not staged_root or not isinstance(data_root, str) or not data_root:
        raise ActivationApprovalError("activation roots are invalid")
    scope = _scope(store, build_id=build_id, staged_root=staged_root, data_root=data_root, rollback_plan=rollback_plan)
    digest = json_digest(scope)
    approval_id = approval_id or new_id("act")
    validate_id(approval_id, prefix="act")
    now = utc_now()
    with store.transaction():
        existing = store.conn.execute(
            "SELECT * FROM activation_approvals WHERE build_id=? AND staged_root=? AND data_root=? AND scope_digest=?",
            (build_id, staged_root, data_root, digest),
        ).fetchone()
        if existing is not None:
            if existing["state"] != "active":
                raise IdempotencyConflictError(approval_id, existing_digest=str(existing["scope_digest"]), request_digest=digest)
            return dict(existing)
        store.conn.execute(
            "INSERT INTO activation_approvals(approval_id,build_id,staged_root,data_root,rollback_plan_json,scope_digest,actor_id,issued_at,state) VALUES(?,?,?,?,?,?,?,?,?)",
            (approval_id, build_id, staged_root, data_root, canonical_json(scope["rollbackPlan"]), digest, actor_id, now, "active"),
        )
        append_event_in_transaction(store.conn, event_kind="activation.requested", resource_type="activation", resource_id=approval_id, payload={"buildId": build_id, "stagedRoot": staged_root, "dataRoot": data_root, "scopeDigest": digest})
        return dict(store.conn.execute("SELECT * FROM activation_approvals WHERE approval_id=?", (approval_id,)).fetchone())


def consume_activation_approval(store: Any, *, approval_id: str, scope_digest: str) -> dict[str, Any]:
    validate_id(approval_id, prefix="act")
    with store.transaction():
        row = store.conn.execute("SELECT * FROM activation_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None:
            raise NotFoundError("activation approval was not found")
        if row["state"] != "active":
            raise ActivationApprovalError("activation approval is not active")
        if row["scope_digest"] != scope_digest:
            raise ActivationApprovalError("activation approval digest does not match the request")
        cursor = store.conn.execute("UPDATE activation_approvals SET state='consumed',consumed_at=? WHERE approval_id=? AND state='active'", (utc_now(), approval_id))
        if cursor.rowcount != 1:
            raise ActivationApprovalError("activation approval was already consumed")
        append_event_in_transaction(store.conn, event_kind="activation.approved", resource_type="activation", resource_id=approval_id, payload={"scopeDigest": scope_digest})
        return dict(store.conn.execute("SELECT * FROM activation_approvals WHERE approval_id=?", (approval_id,)).fetchone())


def cancel_activation_approval(store: Any, *, approval_id: str) -> dict[str, Any]:
    validate_id(approval_id, prefix="act")
    with store.transaction():
        row = store.conn.execute("SELECT * FROM activation_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None:
            raise NotFoundError("activation approval was not found")
        cursor = store.conn.execute("UPDATE activation_approvals SET state='cancelled' WHERE approval_id=? AND state='active'", (approval_id,))
        if cursor.rowcount != 1:
            raise ActivationApprovalError("activation approval is not active")
        append_event_in_transaction(store.conn, event_kind="activation.cancelled", resource_type="activation", resource_id=approval_id, payload={"scopeDigest": row["scope_digest"]})
        return dict(store.conn.execute("SELECT * FROM activation_approvals WHERE approval_id=?", (approval_id,)).fetchone())


__all__ = ["ActivationApprovalError", "cancel_activation_approval", "consume_activation_approval", "request_activation_approval"]
