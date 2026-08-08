"""State-only completion-resource operations.

These helpers mutate only controller SQLite state and transactional outbox
records.  External Git, session, runtime, tmux, and Herdr effects belong to
later adapters and are intentionally absent here.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Mapping

from .errors import (
    ActivationMismatchError,
    ConstraintError,
    MigrationUnresolvedError,
    NotFoundError,
    ResourceStaleError,
    WorkstreamConflictError,
    error_from_exception,
)
from .events import append_event_in_transaction
from .models import canonical_json, new_id, parse_canonical_json, row_to_dict, utc_now, validate_id
from .operations import create_operation, update_operation_in_transaction

_OID_RE = re.compile(r"^[0-9a-f]{40,64}$")
_DISPOSITIONS = {"import", "observe", "unmigrated", "exclude", "requires-decision", "contradiction"}


def _check_id(value: str, prefix: str) -> str:
    try:
        return validate_id(value, prefix=prefix)
    except ValueError as error:
        raise ConstraintError(f"{prefix} identifier is invalid") from error


def _json(value: Any, *, limit: int, name: str) -> str:
    try:
        return canonical_json(value, max_bytes=limit)
    except Exception as error:
        raise ConstraintError(f"{name} is not bounded canonical JSON") from error


def _project(store: Any, project_id: str) -> Any:
    row = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if row is None:
        raise NotFoundError("project was not found", detail={"project_id": project_id})
    return row


def _replayed_result(store: Any, operation_id: str | None) -> dict[str, Any] | None:
    if operation_id is None:
        return None
    row = store.conn.execute("SELECT state,result_json FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row is not None and row["state"] == "succeeded" and row["result_json"] is not None:
        value = parse_canonical_json(row["result_json"])
        return dict(value) if isinstance(value, Mapping) else {"value": value}
    return None


def _operation(store: Any, *, operation_id: str | None, idempotency_key: str | None,
               kind: str, resource_type: str, resource_id: str, request: Mapping[str, Any],
               actor_type: str, actor_id: str | None, authorization_id: str | None,
               expected_resource_version: int | None = None, writer_epoch: int | None = None) -> str | None:
    if operation_id is not None:
        row = store.conn.execute("SELECT operation_id FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise NotFoundError("operation was not found", detail={"operation_id": operation_id})
        return operation_id
    if idempotency_key is None:
        return None
    return create_operation(
        store,
        idempotency_key=idempotency_key,
        kind=kind,
        resource_type=resource_type,
        resource_id=resource_id,
        actor_type=actor_type,
        actor_id=actor_id,
        authorization_id=authorization_id,
        expected_resource_version=expected_resource_version,
        writer_epoch=writer_epoch,
        request=request,
    ).operation_id


def _finish(connection: Any, operation_id: str | None, *, result: Any) -> None:
    if operation_id is not None:
        update_operation_in_transaction(connection, operation_id, state="succeeded", step="state-committed", result=result)


def _event(connection: Any, *, kind: str, resource_type: str, resource_id: str,
           version: int, payload: Mapping[str, Any], operation_id: str | None,
           failpoint: Any | None = None) -> None:
    if failpoint is not None:
        failpoint.hit("completion.event.before", {"resource_id": resource_id, "event_kind": kind})
    append_event_in_transaction(
        connection,
        event_kind=kind,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_version=version,
        operation_id=operation_id,
        payload=payload,
    )
    if failpoint is not None:
        failpoint.hit("completion.event.after", {"resource_id": resource_id, "event_kind": kind})


def _workstream_links(store: Any, project_id: str, working_copy_id: str, conversation_id: str) -> None:
    row = store.conn.execute(
        """SELECT wc.project_id AS wc_project, wc.kind, wc.purpose, wc.controller_owned,
                  c.project_id AS c_project, c.working_copy_id AS c_working_copy, c.role
             FROM working_copies wc JOIN conversations c ON c.conversation_id=?
            WHERE wc.working_copy_id=?""",
        (conversation_id, working_copy_id),
    ).fetchone()
    if row is None:
        raise NotFoundError("workstream link resource was not found")
    if (
        row["wc_project"] != project_id or row["c_project"] != project_id
        or row["c_working_copy"] != working_copy_id or row["role"] != "workstream"
        or row["kind"] not in {"worktree", "isolated"}
        or row["purpose"] not in {"workstream", "integration"}
        or not bool(row["controller_owned"])
    ):
        raise WorkstreamConflictError("workstream resources are not an exact separate controller-owned link")


def create_workstream(
    store: Any, *, project_id: str, working_copy_id: str, conversation_id: str,
    title: str, brief: Any, target_ref: str, starting_oid: str,
    workstream_id: str | None = None, actor_type: str = "controller",
    actor_id: str | None = None, authorization_id: str | None = None,
    operation_id: str | None = None, idempotency_key: str | None = None,
    failpoint: Any | None = None,
) -> dict[str, Any]:
    _check_id(project_id, "prj")
    _check_id(working_copy_id, "wc")
    _check_id(conversation_id, "conv")
    workstream_id = _check_id(workstream_id or new_id("ws"), "ws")
    if not isinstance(title, str) or not title or len(title) > 512 or "\x00" in title:
        raise ConstraintError("workstream title is invalid")
    if not isinstance(target_ref, str) or not target_ref or len(target_ref) > 512 or "\x00" in target_ref:
        raise ConstraintError("workstream target ref is invalid")
    if not isinstance(starting_oid, str) or _OID_RE.fullmatch(starting_oid) is None:
        raise ConstraintError("workstream starting object ID is invalid")
    brief_json = _json(brief, limit=64 * 1024, name="workstream brief")
    request = {"projectId": project_id, "workingCopyId": working_copy_id, "conversationId": conversation_id, "title": title, "brief": brief, "targetRef": target_ref, "startingOid": starting_oid}
    operation_id = _operation(store, operation_id=operation_id, idempotency_key=idempotency_key, kind="create-workstream", resource_type="workstream", resource_id=workstream_id, request=request, actor_type=actor_type, actor_id=actor_id, authorization_id=authorization_id)
    replay = _replayed_result(store, operation_id)
    if replay is not None:
        return replay
    now = utc_now()
    try:
        with store.transaction():
            _project(store, project_id)
            _workstream_links(store, project_id, working_copy_id, conversation_id)
            if store.conn.execute("SELECT 1 FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone() is not None:
                raise WorkstreamConflictError("workstream already exists", detail={"workstream_id": workstream_id})
            store.conn.execute(
                """INSERT INTO workstreams(workstream_id,project_id,working_copy_id,conversation_id,title,brief_json,target_ref,starting_oid,desired_state,observed_state,controller_owned,resource_version,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (workstream_id, project_id, working_copy_id, conversation_id, title, brief_json, target_ref, starting_oid, "active", "planned", 1, 1, now, now),
            )
            _event(store.conn, kind="workstream.created", resource_type="workstream", resource_id=workstream_id, version=1, operation_id=operation_id, payload={"workstreamId": workstream_id, "projectId": project_id, "workingCopyId": working_copy_id, "conversationId": conversation_id}, failpoint=failpoint)
            result = row_to_dict(store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone())
            _finish(store.conn, operation_id, result=result)
            return result
    except sqlite3.Error as error:
        raise error_from_exception(error) from error


def get_workstream(store: Any, workstream_id: str) -> dict[str, Any]:
    _check_id(workstream_id, "ws")
    row = store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()
    if row is None:
        raise NotFoundError("workstream was not found", detail={"workstream_id": workstream_id})
    return row_to_dict(row)


def list_workstreams(store: Any, project_id: str | None = None) -> list[dict[str, Any]]:
    if project_id is not None:
        _check_id(project_id, "prj")
        rows = store.conn.execute("SELECT * FROM workstreams WHERE project_id=? ORDER BY created_at,workstream_id", (project_id,))
    else:
        rows = store.conn.execute("SELECT * FROM workstreams ORDER BY created_at,workstream_id")
    return [row_to_dict(row) for row in rows]


def update_workstream(store: Any, workstream_id: str, *, expected_resource_version: int,
                      updates: Mapping[str, Any], operation_id: str | None = None,
                      idempotency_key: str | None = None, actor_type: str = "controller",
                      actor_id: str | None = None, authorization_id: str | None = None,
                      failpoint: Any | None = None) -> dict[str, Any]:
    _check_id(workstream_id, "ws")
    if not isinstance(expected_resource_version, int) or expected_resource_version < 1:
        raise ConstraintError("expected workstream version is invalid")
    allowed = {"title", "brief", "targetRef", "observedState", "desiredState", "errorCode", "errorDetail", "lastReconciledAt"}
    if not isinstance(updates, Mapping) or not updates or not set(updates).issubset(allowed):
        raise ConstraintError("workstream update fields are not allowlisted")
    row = store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()
    if row is None:
        raise NotFoundError("workstream was not found", detail={"workstream_id": workstream_id})
    db_updates: dict[str, Any] = {}
    for key, value in updates.items():
        column = {"targetRef": "target_ref", "observedState": "observed_state", "desiredState": "desired_state", "errorCode": "error_code", "errorDetail": "error_detail", "lastReconciledAt": "last_reconciled_at", "brief": "brief_json"}.get(key, key)
        if key == "brief": value = _json(value, limit=64 * 1024, name="workstream brief")
        if key == "title" and (not isinstance(value, str) or not value or len(value) > 512): raise ConstraintError("workstream title is invalid")
        db_updates[column] = value
    request = {"workstreamId": workstream_id, "expectedResourceVersion": expected_resource_version, "updates": updates}
    operation_id = _operation(store, operation_id=operation_id, idempotency_key=idempotency_key, kind="workstream-update", resource_type="workstream", resource_id=workstream_id, request=request, actor_type=actor_type, actor_id=actor_id, authorization_id=authorization_id, expected_resource_version=expected_resource_version)
    replay = _replayed_result(store, operation_id)
    if replay is not None:
        return replay
    now = utc_now()
    with store.transaction():
        assignments = ", ".join(f"{key}=?" for key in sorted(db_updates)) + ", resource_version=resource_version+1, updated_at=?"
        values = [db_updates[key] for key in sorted(db_updates)] + [now, workstream_id, expected_resource_version]
        cursor = store.conn.execute(f"UPDATE workstreams SET {assignments} WHERE workstream_id=? AND resource_version=?", values)
        if cursor.rowcount != 1:
            actual = store.conn.execute("SELECT resource_version FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()
            if actual is None: raise NotFoundError("workstream was not found")
            raise ResourceStaleError(workstream_id, expected_resource_version, int(actual[0]))
        result = row_to_dict(store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone())
        _event(store.conn, kind="workstream.updated", resource_type="workstream", resource_id=workstream_id, version=int(result["resource_version"]), operation_id=operation_id, payload={"workstreamId": workstream_id, "updates": sorted(updates)}, failpoint=failpoint)
        _finish(store.conn, operation_id, result=result)
        return result


def create_presentation_assignment(store: Any, *, conversation_id: str, backend: str,
                                   desired_state: str = "present", locator: Any = None,
                                   presentation_assignment_id: str | None = None) -> dict[str, Any]:
    _check_id(conversation_id, "conv")
    assignment_id = _check_id(presentation_assignment_id or new_id("pa"), "pa")
    if backend not in {"tmux", "herdr"} or desired_state not in {"present", "absent"}:
        raise ConstraintError("presentation assignment state is invalid")
    locator_json = _json({} if locator is None else locator, limit=16 * 1024, name="presentation locator")
    now = utc_now()
    with store.transaction():
        if store.conn.execute("SELECT 1 FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone() is None:
            raise NotFoundError("conversation was not found")
        store.conn.execute(
            "INSERT INTO presentation_assignments(presentation_assignment_id,conversation_id,backend,desired_state,observed_state,locator_json,resource_version,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (assignment_id, conversation_id, backend, desired_state, "unknown", locator_json, 1, now),
        )
        _event(store.conn, kind="presentation.created", resource_type="presentation_assignment", resource_id=assignment_id, version=1, operation_id=None, payload={"conversationId": conversation_id, "backend": backend})
        return row_to_dict(store.conn.execute("SELECT * FROM presentation_assignments WHERE presentation_assignment_id=?", (assignment_id,)).fetchone())


def update_presentation_assignment(store: Any, presentation_assignment_id: str, *, expected_resource_version: int,
                                   updates: Mapping[str, Any]) -> dict[str, Any]:
    _check_id(presentation_assignment_id, "pa")
    row = store.conn.execute("SELECT * FROM presentation_assignments WHERE presentation_assignment_id=?", (presentation_assignment_id,)).fetchone()
    if row is None: raise NotFoundError("presentation assignment was not found")
    allowed = {"desiredState", "observedState", "locator", "observedAt", "errorCode", "errorDetail"}
    if not isinstance(updates, Mapping) or not updates or not set(updates).issubset(allowed): raise ConstraintError("presentation update fields are not allowlisted")
    mapped = {"desiredState":"desired_state", "observedState":"observed_state", "locator":"locator_json", "observedAt":"observed_at", "errorCode":"error_code", "errorDetail":"error_detail"}
    db: dict[str, Any] = {}
    for key, value in updates.items(): db[mapped.get(key, key)] = _json(value, limit=16*1024, name="presentation locator") if key == "locator" else value
    with store.transaction():
        assignments = ", ".join(f"{key}=?" for key in sorted(db)) + ", resource_version=resource_version+1, updated_at=?"
        cursor = store.conn.execute(f"UPDATE presentation_assignments SET {assignments} WHERE presentation_assignment_id=? AND resource_version=?", [db[k] for k in sorted(db)] + [utc_now(), presentation_assignment_id, expected_resource_version])
        if cursor.rowcount != 1:
            current = store.conn.execute("SELECT resource_version FROM presentation_assignments WHERE presentation_assignment_id=?", (presentation_assignment_id,)).fetchone()
            if current is None: raise NotFoundError("presentation assignment was not found")
            raise ResourceStaleError(presentation_assignment_id, expected_resource_version, int(current[0]))
        result = row_to_dict(store.conn.execute("SELECT * FROM presentation_assignments WHERE presentation_assignment_id=?", (presentation_assignment_id,)).fetchone())
        _event(store.conn, kind="presentation.updated", resource_type="presentation_assignment", resource_id=presentation_assignment_id, version=int(result["resource_version"]), operation_id=None, payload={"updates": sorted(updates)})
        return result


def ensure_project_activation(store: Any, *, project_id: str, expected_project_version: int | None = None) -> dict[str, Any]:
    _check_id(project_id, "prj")
    project = _project(store, project_id)
    existing = store.conn.execute("SELECT * FROM project_activations WHERE project_id=?", (project_id,)).fetchone()
    if existing is not None: return row_to_dict(existing)
    version = int(project["resource_version"] if expected_project_version is None else expected_project_version)
    if version != int(project["resource_version"]): raise ActivationMismatchError("project version does not match activation request")
    now = utc_now()
    with store.transaction():
        store.conn.execute("INSERT INTO project_activations(project_id,mode,expected_project_version,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?)", (project_id, "legacy", version, 1, now, now))
        _event(store.conn, kind="activation.created", resource_type="project_activation", resource_id=project_id, version=1, operation_id=None, payload={"projectId": project_id, "mode": "legacy"})
        return row_to_dict(store.conn.execute("SELECT * FROM project_activations WHERE project_id=?", (project_id,)).fetchone())


def transition_activation(store: Any, *, project_id: str, mode: str, expected_resource_version: int,
                          controller_build_id: str | None = None, migration_id: str | None = None,
                          expected_project_version: int | None = None, operation_id: str | None = None,
                          idempotency_key: str | None = None, actor_type: str = "controller",
                          actor_id: str | None = None, authorization_id: str | None = None,
                          rollback: bool = False) -> dict[str, Any]:
    _check_id(project_id, "prj")
    if mode not in {"legacy", "shadow", "controller"}: raise ActivationMismatchError("activation mode is invalid")
    row = store.conn.execute("SELECT * FROM project_activations WHERE project_id=?", (project_id,)).fetchone()
    if row is None: row = ensure_project_activation(store, project_id=project_id)
    current = str(row["mode"])
    legal = (current, mode) in {("legacy", "shadow"), ("shadow", "controller"), ("controller", "legacy")}
    if not legal or ((current, mode) == ("controller", "legacy") and not rollback): raise ActivationMismatchError("activation transition is not legal")
    if mode == "legacy": controller_build_id = migration_id = None
    else:
        if not controller_build_id or not migration_id: raise ActivationMismatchError("non-legacy activation requires build and migration")
        build = store.conn.execute("SELECT status FROM installed_builds WHERE build_id=?", (controller_build_id,)).fetchone()
        migration = store.conn.execute("SELECT state FROM migration_runs WHERE migration_id=?", (migration_id,)).fetchone()
        if build is None or migration is None or (mode == "controller" and (build[0] != "active" or migration[0] != "succeeded")) or (mode == "shadow" and build[0] not in {"staged", "active"}):
            raise ActivationMismatchError("activation build or migration predicate is not satisfied")
    project = _project(store, project_id)
    target_project_version = int(project["resource_version"] if expected_project_version is None else expected_project_version)
    if target_project_version != int(project["resource_version"]): raise ActivationMismatchError("activation project version is stale")
    request = {"projectId": project_id, "mode": mode, "buildId": controller_build_id, "migrationId": migration_id, "expectedProjectVersion": target_project_version, "rollback": rollback}
    operation_id = _operation(store, operation_id=operation_id, idempotency_key=idempotency_key, kind="activation-change", resource_type="project_activation", resource_id=project_id, request=request, actor_type=actor_type, actor_id=actor_id, authorization_id=authorization_id, expected_resource_version=expected_resource_version)
    replay = _replayed_result(store, operation_id)
    if replay is not None:
        return replay
    with store.transaction():
        now = utc_now()
        cursor = store.conn.execute("UPDATE project_activations SET mode=?,controller_build_id=?,migration_id=?,expected_project_version=?,resource_version=resource_version+1,updated_at=?,activated_at=? WHERE project_id=? AND resource_version=?", (mode, controller_build_id, migration_id, target_project_version, now, now if mode == "controller" else None, project_id, expected_resource_version))
        if cursor.rowcount != 1: raise ResourceStaleError(project_id, expected_resource_version, int(store.conn.execute("SELECT resource_version FROM project_activations WHERE project_id=?", (project_id,)).fetchone()[0]))
        result = row_to_dict(store.conn.execute("SELECT * FROM project_activations WHERE project_id=?", (project_id,)).fetchone())
        _event(store.conn, kind="activation.changed", resource_type="project_activation", resource_id=project_id, version=int(result["resource_version"]), operation_id=operation_id, payload={"projectId": project_id, "mode": mode})
        _finish(store.conn, operation_id, result=result)
        return result


def create_migration_mapping(store: Any, *, migration_id: str, record_id: str, adapter_kind: str,
                             source_kind: str, source_digest: str, resource_type: str,
                             disposition: str, reason_code: str, detail: Any,
                             resource_id: str | None = None) -> dict[str, Any]:
    _check_id(migration_id, "mig")
    if not isinstance(record_id, str) or not record_id or len(record_id) > 256: raise MigrationUnresolvedError("migration record ID is invalid")
    if disposition not in _DISPOSITIONS: raise MigrationUnresolvedError("migration disposition is invalid")
    if disposition == "import" and not resource_id: raise MigrationUnresolvedError("import mapping requires a target resource")
    detail_json = _json(detail, limit=16 * 1024, name="mapping detail")
    now = utc_now()
    with store.transaction():
        try:
            store.conn.execute("INSERT INTO migration_resource_mappings(migration_id,record_id,adapter_kind,source_kind,source_digest,resource_type,resource_id,disposition,reason_code,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (migration_id, record_id, adapter_kind, source_kind, source_digest, resource_type, resource_id, disposition, reason_code, detail_json, now))
        except sqlite3.Error as error: raise error_from_exception(error) from error
        return row_to_dict(store.conn.execute("SELECT * FROM migration_resource_mappings WHERE migration_id=? AND record_id=?", (migration_id, record_id)).fetchone())


def focus_workstream(store: Any, workstream_id: str) -> dict[str, Any]:
    row = get_workstream(store, workstream_id)
    return {"workstreamId": row["workstream_id"], "projectId": row["project_id"], "workingCopyId": row["working_copy_id"], "conversationId": row["conversation_id"], "desiredState": row["desired_state"], "observedState": row["observed_state"], "presentationOnly": True}


def retire_workstream(store: Any, workstream_id: str, *, expected_resource_version: int, operation_id: str | None = None, failpoint: Any | None = None) -> dict[str, Any]:
    row = get_workstream(store, workstream_id)
    active = store.conn.execute("SELECT run_id,observed_state FROM runs WHERE working_copy_id=? AND observed_state NOT IN ('stopped','failed','lost')", (row["working_copy_id"],)).fetchone()
    if active is not None:
        raise WorkstreamConflictError("workstream has a live or unknown run; retirement refused", detail={"run_id": active["run_id"], "state": active["observed_state"]})
    return update_workstream(store, workstream_id, expected_resource_version=expected_resource_version, updates={"desiredState": "retired", "observedState": "stopped"}, operation_id=operation_id, failpoint=failpoint)


def relaunch_workstream(store: Any, workstream_id: str, *, expected_resource_version: int, operation_id: str | None = None, failpoint: Any | None = None) -> dict[str, Any]:
    row = get_workstream(store, workstream_id)
    if row["desired_state"] != "retired":
        raise WorkstreamConflictError("only a retired workstream can be relaunched")
    return update_workstream(store, workstream_id, expected_resource_version=expected_resource_version, updates={"desiredState": "active", "observedState": "creating"}, operation_id=operation_id, failpoint=failpoint)


def plan_workstream(store: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(request, Mapping) or not {"projectId", "workingCopyId", "conversationId"}.issubset(request):
        raise ConstraintError("workstream plan requires exact project, working-copy, and conversation IDs")
    _check_id(request["projectId"], "prj"); _check_id(request["workingCopyId"], "wc"); _check_id(request["conversationId"], "conv")
    _workstream_links(store, request["projectId"], request["workingCopyId"], request["conversationId"])
    return {"planned": True, "effects": [], "projectId": request["projectId"], "workingCopyId": request["workingCopyId"], "conversationId": request["conversationId"]}


# Stable aliases used by callers that prefer resource-oriented names.
create_presentation = create_presentation_assignment
update_presentation = update_presentation_assignment
create_migration_resource_mapping = create_migration_mapping
activate_project = transition_activation


__all__ = [
    "activate_project", "create_migration_mapping", "create_migration_resource_mapping",
    "create_presentation", "create_presentation_assignment", "create_workstream",
    "ensure_project_activation", "focus_workstream", "get_workstream", "list_workstreams",
    "plan_workstream", "relaunch_workstream", "retire_workstream", "transition_activation",
    "update_presentation", "update_presentation_assignment", "update_workstream",
]
