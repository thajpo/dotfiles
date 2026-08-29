"""One selector-aware lifecycle engine for Pisec runtime convergence."""

from __future__ import annotations

import json
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adapters import HarnessAdapter, WorkspaceAdapter, RuntimeSurfaceArtifacts, artifact_document
from .worker_repo import validate_worker_resume_git
from .worker_repo import project_permissions_lock
from .models import ConflictError, NeedsAttentionError, NotFoundError, PisecError, canonical_json, json_digest, new_id, utc_now, validate_sha256
from .runtime import WORKSPACE_RUNTIME_MISSING, reset_codex_session_in_transaction, start_bound_agent, usable_runtime_binding
from .runtime_surface import capture_runtime_surface, verify_surface
from .events import append_event_in_transaction
from .operations import update_operation_in_transaction
from .control_plane import control_plane_mutation


_RECOVERABLE_STOPPED_REFRESH_ERRORS = frozenset(
    {
        "runtime process identity became ambiguous during refresh",
        "workstream changed before refresh reservation",
        "isolated OMP surface contains a symlink",
        "runtime binding is already reserved by another refresh",
        # Legacy OMP activation restored the old profile after rejecting an
        # owned runtime socket in its generated backup.  The exact message is
        # retained only to reconcile that stopped, still-owned reservation.
        "OMP generated backup contains an unsupported file",
    }
)


def _active_bindings(store: Any, *, project_ids: Sequence[str] = (), harness_ids: Sequence[str] = (), workstream_ids: Sequence[str] = ()) -> list[dict[str, Any]]:
    clauses = ["p.active=1", "w.desired_state='active'"]
    params: list[str] = []
    selectors = (("p.project_id", project_ids), ("r.harness_id", harness_ids), ("r.workstream_id", workstream_ids))
    for column, values in selectors:
        normalized = sorted({str(value) for value in values if str(value)})
        if normalized:
            clauses.append(f"{column} IN ({','.join('?' for _ in normalized)})")
            params.extend(normalized)
    rows = store.conn.execute(
        "SELECT r.*,w.project_id,w.kind,w.execution_profile,w.worktree_path,w.desired_state,w.provisioning_state,p.display_name "
        "FROM runtime_bindings r JOIN workstreams w USING(workstream_id) JOIN projects p USING(project_id) WHERE " + " AND ".join(clauses) + " ORDER BY p.display_name,w.kind,w.created_at,w.workstream_id",
        params,
    )
    return [dict(row) for row in rows]


def _binding(store: Any, workstream_id: str) -> dict[str, Any]:
    rows = _active_bindings(store, workstream_ids=(workstream_id,))
    if not rows:
        raise NotFoundError("active runtime binding was not found")
    return rows[0]


def _harness_for(binding: Mapping[str, Any], fallback: HarnessAdapter, resolver: Any | None) -> HarnessAdapter:
    return resolver(str(binding["workstream_id"])) if callable(resolver) else fallback


def _surface_for(binding: Mapping[str, Any], harness: HarnessAdapter, resolver: Any | None) -> RuntimeSurfaceArtifacts:
    if callable(resolver):
        value = resolver(str(binding["harness_id"]))
    else:
        value = capture_runtime_surface(harness)
    if not isinstance(value, RuntimeSurfaceArtifacts):
        raise NeedsAttentionError("current runtime surface is missing or corrupt; run pisec update")
    return verify_surface(value)


def _scope(store: Any, binding: Mapping[str, Any], harness: HarnessAdapter) -> dict[str, Any]:
    from .access import effective_runtime_scope
    return effective_runtime_scope(store, binding, harness=harness)


_binding_scope = _scope


def _desired(harness: HarnessAdapter, scope: Mapping[str, Any], surface: RuntimeSurfaceArtifacts) -> str:
    verify_surface(surface)
    value = harness.desired_generation({**scope, "runtimeSurfaceSha256": surface.content_sha256, "runtimeSurfaceRoot": surface.root_path, "runtimeSurfaceId": "surface_" + surface.content_sha256[:32]}, surface)
    try:
        validate_sha256(value, "runtime generation")
    except Exception as error:
        raise NeedsAttentionError("harness returned an invalid runtime generation") from error
    return value


def _state(store: Any, workstream_id: str) -> dict[str, Any]:
    return _binding(store, workstream_id)


def _item(binding: Mapping[str, Any], generation: str | None = None, **extra: Any) -> dict[str, Any]:
    result = {"project": str(binding["display_name"]), "workstreamId": str(binding["workstream_id"]), "harnessId": str(binding["harness_id"]), "generation": generation}
    result.update(extra)
    return result


def _mark_attention(store: Any, workstream_id: str, reason: str) -> None:
    now = utc_now()
    with store.transaction():
        operation = store.conn.execute(
            "SELECT * FROM operations WHERE workstream_id=? AND kind='runtime.refresh' AND state IN ('planned','applying','needs_attention') ORDER BY created_at DESC,operation_id DESC LIMIT 1",
            (workstream_id,),
        ).fetchone()
        # A staging failure occurs before the old runtime is stopped.  The
        # pre-stop helper records that truthful idle state so a later retry can
        # reserve and replace it; failures after that point remain visibly
        # blocked until a new authenticated attestation.
        if operation is not None and operation["step"] == "pre_stop_attention":
            store.conn.execute("UPDATE runtime_bindings SET last_observed_at=?,updated_at=? WHERE workstream_id=?", (now, now, workstream_id))
        else:
            # refresh_pending is intentionally retained: a failed restart must remain
            # visibly blocked until a later successful attestation.
            store.conn.execute("UPDATE runtime_bindings SET observed_state='error',last_observed_at=?,updated_at=? WHERE workstream_id=?", (now, now, workstream_id))
        store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?", (reason[:512], now, workstream_id))
        operation = store.conn.execute(
            "SELECT * FROM operations WHERE workstream_id=? AND kind='runtime.refresh' AND state IN ('planned','applying') ORDER BY created_at DESC,operation_id DESC LIMIT 1",
            (workstream_id,),
        ).fetchone()
        if operation is not None:
            store.conn.execute(
                "UPDATE operations SET state='needs_attention',step='attention',error_code='runtime_refresh_failed',error_message=?,updated_at=? WHERE operation_id=? AND state IN ('planned','applying')",
                (reason[:512], now, operation["operation_id"]),
            )
            append_event_in_transaction(
                store.conn,
                kind="runtime.refresh_failed",
                project_id=str(operation["project_id"]),
                workstream_id=workstream_id,
                operation_id=str(operation["operation_id"]),
                payload={"workstreamId": workstream_id, "reason": reason[:512]},
            )


def mark_stale_bindings(store: Any, harness: HarnessAdapter, *, harness_resolver: Any | None = None, surface_resolver: Any | None = None, project_ids: Sequence[str] = (), harness_ids: Sequence[str] = (), workstream_ids: Sequence[str] = ()) -> dict[str, Any]:
    stale: list[dict[str, str]] = []
    current: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    surfaces: dict[str, RuntimeSurfaceArtifacts] = {}
    for binding in _active_bindings(store, project_ids=project_ids, harness_ids=harness_ids, workstream_ids=workstream_ids):
        try:
            selected = _harness_for(binding, harness, harness_resolver)
            harness_id = str(binding["harness_id"])
            surface = surfaces.get(harness_id)
            if surface is None:
                surface = _surface_for(binding, selected, surface_resolver)
                surfaces[harness_id] = surface
            desired = _desired(selected, _scope(store, binding, selected), surface)
            with store.transaction():
                store.conn.execute("UPDATE runtime_bindings SET desired_generation_sha256=?,updated_at=? WHERE workstream_id=?", (desired, utc_now(), binding["workstream_id"]))
            if binding["applied_generation_sha256"] == desired and not binding["refresh_pending"]:
                current.append(_item(binding, desired))
            else:
                stale.append(_item(binding, desired))
        except Exception as error:
            failed.append(_item(binding, None, reason=str(error)[:512]))
    return {"stale": stale, "current": current, "failed": failed}


def _wait_for_exit(workspace: WorkspaceAdapter, binding: Mapping[str, Any], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        observation = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
        if observation.state == "stopped":
            return
        if time.monotonic() >= deadline:
            if observation.state == "unknown":
                raise NeedsAttentionError("runtime process identity became ambiguous during refresh")
            raise NeedsAttentionError("runtime did not stop gracefully")
        # Herdr reports a transitional process tree as unknown while the
        # launcher exits and the pane returns to its shell.  This is safe to
        # settle only after refresh has reserved the binding and explicitly
        # requested shutdown: never treat it as stopped, just re-observe until
        # the pane proves that only its shell remains or the bounded timeout
        # expires.
        time.sleep(0.05)


def _startup_attested(store: Any, workspace: WorkspaceAdapter, workstream_id: str, old_instance: str | None, generation: str) -> dict[str, Any] | None:
    binding = _state(store, workstream_id)
    if not (
        binding["runtime_instance_id"]
        and binding["runtime_instance_id"] != old_instance
        and int(binding["report_seq"]) >= 1
        and binding["applied_generation_sha256"] == generation
        and binding["launch_generation_sha256"] is None
        and not binding["refresh_pending"]
        and binding["session_start_report_seq"] == binding["report_seq"]
        and binding["session_start_event_sequence"] is not None
    ):
        return None
    event = store.conn.execute(
        "SELECT kind,payload_json FROM events WHERE sequence=? AND workstream_id=?",
        (binding["session_start_event_sequence"], workstream_id),
    ).fetchone()
    if event is None or event["kind"] != "runtime.session_started":
        return None
    try:
        payload = json.loads(str(event["payload_json"]))
    except (TypeError, ValueError):
        return None
    if payload != {
        "generationSha256": generation,
        "reportSeq": int(binding["report_seq"]),
        "runtimeInstanceId": str(binding["runtime_instance_id"]),
    }:
        return None
    if workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"])).state != "live":
        return None
    return binding


def _wait_for_start(store: Any, workspace: WorkspaceAdapter, workstream_id: str, old_instance: str | None, generation: str, timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while True:
        binding = _startup_attested(store, workspace, workstream_id, old_instance, generation)
        if binding is not None:
            return binding
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def _stage_profile(store: Any, harness: HarnessAdapter, binding: Mapping[str, Any], surface: RuntimeSurfaceArtifacts, operation_id: str) -> Any:
    validate_worker_resume_git(store, binding)
    scope = _scope(store, binding, harness)
    verify_surface(surface)
    stage_root = Path(str(scope.get("profileStagingRoot", Path(binding["harness_home"]).parent / ".staging" / str(binding["workstream_id"]))))
    staged = harness.stage_profile({**scope, "operationId": operation_id, "runtimeSurfaceSha256": surface.content_sha256, "runtimeSurfaceRoot": surface.root_path, "runtimeSurfaceId": "surface_" + surface.content_sha256[:32]}, surface, stage_root)
    verify_surface(surface)
    return staged


def _reset_stale_refresh_for_new_session(store: Any, binding: Mapping[str, Any]) -> None:
    if not int(binding.get("refresh_pending", 0)):
        return
    launch_generation = binding.get("launch_generation_sha256")
    if not isinstance(launch_generation, str) or not launch_generation:
        raise NeedsAttentionError("session reset requires a fully materialized current runtime")
    try:
        artifacts = json.loads(str(binding["adapter_artifacts_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise NeedsAttentionError("session reset requires a valid runtime artifact") from error
    if not isinstance(artifacts, dict) or artifacts.get("generationSha256") != binding.get("launch_generation_sha256"):
        raise NeedsAttentionError("session reset requires a matching runtime artifact")
    operation_id = binding.get("refresh_operation_id")
    operation = store.conn.execute("SELECT * FROM operations WHERE operation_id=? AND kind='runtime.refresh'", (operation_id,)).fetchone()
    recoverable_attention = bool(operation is not None and operation["state"] == "needs_attention" and operation["step"] == "attention")
    recoverable_reset_residue = bool(
        operation is not None
        and operation["state"] == "failed"
        and operation["step"] == "superseded"
        and operation["error_code"] == "runtime_session_reset"
    )
    if operation is None or not (recoverable_attention or recoverable_reset_residue):
        raise NeedsAttentionError("session reset cannot interrupt an active refresh")
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "UPDATE runtime_bindings SET refresh_pending=0,refresh_operation_id=NULL,refresh_started_at=NULL,launch_generation_sha256=NULL,runtime_instance_id=NULL,report_seq=0,session_start_event_sequence=NULL,session_start_report_seq=NULL,session_started_at=NULL,observed_state='stopped',last_observed_at=?,updated_at=? WHERE workstream_id=? AND refresh_pending=1 AND refresh_operation_id=?",
            (now, now, str(binding["workstream_id"]), str(operation_id)),
        )
        store.conn.execute(
            "UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=? AND provisioning_state='needs_attention'",
            (now, str(binding["workstream_id"])),
        )
        if recoverable_attention:
            update_operation_in_transaction(
                store.conn,
                str(operation_id),
                state="failed",
                step="superseded",
                expected_states=("needs_attention",),
                error_code="runtime_session_reset",
                error_message="refresh reservation was compensated by an explicit stopped-worker session reset",
            )
            append_event_in_transaction(
                store.conn,
                kind="runtime.refresh_compensated",
                project_id=str(operation["project_id"]),
                workstream_id=str(binding["workstream_id"]),
                operation_id=str(operation_id),
                payload={"workstreamId": str(binding["workstream_id"]), "reason": "explicit stopped-worker session reset"},
            )


def _materialize_and_launch(store: Any, harness: HarnessAdapter, workspace: WorkspaceAdapter, binding: Mapping[str, Any], surface: RuntimeSurfaceArtifacts, desired: str, *, staged: Any | None = None) -> dict[str, Any]:
    validate_worker_resume_git(store, binding)
    scope = _scope(store, binding, harness)
    verify_surface(surface)
    if staged is None:
        staged = _stage_profile(store, harness, binding, surface, str(scope.get("operationId", "op_refresh")))
    artifacts = harness.activate_profile(scope, staged)
    verify_surface(surface)
    if artifacts.generation_sha256 != desired:
        raise NeedsAttentionError("runtime generation changed while it was being materialized")
    harness.commit_launch_binding(scope, artifacts, workspace_session_name=str(binding["workspace_session_name"]), workspace_id=str(binding["workspace_id"]), workspace_view_id=str(binding["workspace_view_id"]), workspace_surface_id=str(binding["workspace_surface_id"]), replace=True)
    now = utc_now()
    with store.transaction():
        store.conn.execute("UPDATE runtime_bindings SET harness_home=?,adapter_artifacts_json=?,launch_secret_path=?,policy_path=?,policy_sha256=?,runtime_token_sha256=?,desired_generation_sha256=?,launch_generation_sha256=?,runtime_instance_id=NULL,report_seq=0,observed_state='starting',refresh_pending=1,last_observed_at=?,updated_at=? WHERE workstream_id=?", (artifacts.harness_home, artifact_document(harness.manifest, artifacts), artifacts.launch_secret_path, artifacts.policy_path, artifacts.policy_sha256, artifacts.runtime_token_sha256, desired, desired, now, now, binding["workstream_id"]))
        store.conn.execute("UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=?", (now, binding["workstream_id"]))
    current = _state(store, str(binding["workstream_id"]))
    start_bound_agent(store, workspace, harness, current, workstream_id=str(current["workstream_id"]), project_id=str(current["project_id"]), cwd=str(current["worktree_path"]))
    return _state(store, str(binding["workstream_id"]))


def _mark_pre_stop_attention(store: Any, workstream_id: str, operation_id: str, reason: str) -> None:
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "UPDATE runtime_bindings SET refresh_pending=0,refresh_operation_id=NULL,refresh_started_at=NULL,launch_generation_sha256=NULL,observed_state='idle',updated_at=? WHERE workstream_id=? AND refresh_operation_id=?",
            (now, workstream_id, operation_id),
        )
        store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?", (reason[:512], now, workstream_id))
        store.conn.execute(
            "UPDATE operations SET state='needs_attention',step='pre_stop_attention',error_code='runtime_refresh_staging_failed',error_message=?,updated_at=? WHERE operation_id=? AND state IN ('planned','applying')",
            (reason[:512], now, operation_id),
        )
        operation = store.conn.execute("SELECT project_id FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if operation is not None:
            append_event_in_transaction(
                store.conn,
                kind="runtime.refresh_failed",
                project_id=str(operation["project_id"]),
                workstream_id=workstream_id,
                operation_id=operation_id,
                payload={"workstreamId": workstream_id, "reason": reason[:512]},
            )


def _has_recorded_pre_stop_failure(store: Any, operation_id: str) -> bool:
    row = store.conn.execute(
        "SELECT payload_json FROM events WHERE operation_id=? AND kind='runtime.refresh_failed' ORDER BY sequence DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    if row is None:
        return False
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and isinstance(payload.get("reason"), str) and bool(payload["reason"])


def reconcile_superseded_pre_stop_refreshes(
    store: Any,
    workspace: WorkspaceAdapter,
    *,
    harness_resolver: Any,
) -> list[dict[str, Any]]:
    """Close historical staging failures after a newer attested refresh."""
    rows = store.conn.execute(
        "SELECT o.operation_id,o.project_id,o.workstream_id,r.applied_generation_sha256,"
        "(SELECT newer.operation_id FROM operations newer WHERE newer.kind='runtime.refresh' "
        "AND newer.workstream_id=o.workstream_id AND newer.state='succeeded' "
        "AND (newer.created_at>o.created_at OR (newer.created_at=o.created_at AND newer.operation_id>o.operation_id)) "
        "ORDER BY newer.created_at DESC,newer.operation_id DESC LIMIT 1) AS superseding_operation_id "
        "FROM operations o JOIN runtime_bindings r USING(workstream_id) "
        "WHERE o.kind='runtime.refresh' AND o.state='needs_attention' "
        "AND o.step='pre_stop_attention' AND o.error_code='runtime_refresh_staging_failed' "
        "ORDER BY o.created_at,o.operation_id"
    ).fetchall()
    recovered: list[dict[str, Any]] = []
    for row in rows:
        superseding_operation_id = row["superseding_operation_id"]
        if superseding_operation_id is None:
            continue
        workstream_id = str(row["workstream_id"])
        try:
            harness = harness_resolver(workstream_id)
            current_is_usable = usable_runtime_binding(
                store,
                workstream_id,
                workspace,
                harness,
                allowed_states={"idle", "working", "blocked"},
            )
        except Exception:
            current_is_usable = False
        if not current_is_usable:
            continue
        result = {
            "workstreamId": workstream_id,
            "generationSha256": str(row["applied_generation_sha256"]),
            "supersededByOperationId": str(superseding_operation_id),
            "recovered": True,
        }
        with store.transaction():
            update_operation_in_transaction(
                store.conn,
                str(row["operation_id"]),
                state="failed",
                step="superseded",
                expected_states=("needs_attention",),
                result=result,
                error_code="superseded_by_successful_refresh",
                error_message="earlier staging failure was superseded by a newer authenticated refresh",
            )
            append_event_in_transaction(
                store.conn,
                kind="runtime.refresh_superseded",
                project_id=str(row["project_id"]),
                workstream_id=workstream_id,
                operation_id=str(row["operation_id"]),
                payload=result,
            )
        recovered.append({"operationId": str(row["operation_id"]), "state": "failed", **result})
    return recovered


def _recover_stopped_refresh_attention(
    store: Any,
    current: Mapping[str, Any],
    operation: Mapping[str, Any],
    *,
    runtime_state: str,
) -> bool:
    """Reconcile a failed stop observation once the pane is truly stopped.

    The refresh reservation can survive a Herdr process-identity race even
    though the old profile was never activated.  Recovery is deliberately
    limited to that exact error family and verifies that the durable adapter
    artifact is still the applied generation before clearing the reservation.
    """
    current = dict(current)
    operation = dict(operation)
    if (
        runtime_state != "stopped"
        or operation.get("state") != "needs_attention"
        or operation.get("step") != "attention"
        or operation.get("error_message") not in _RECOVERABLE_STOPPED_REFRESH_ERRORS
        or int(current.get("refresh_pending", 0)) != 1
        or current.get("refresh_operation_id") != operation.get("operation_id")
    ):
        return False
    try:
        artifacts = json.loads(str(current["adapter_artifacts_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(artifacts, dict):
        return False
    artifact_generation = artifacts.get("generationSha256")
    applied_generation = current.get("applied_generation_sha256")
    launch_generation = current.get("launch_generation_sha256")
    if not applied_generation or artifact_generation not in {applied_generation, launch_generation}:
        return False
    # A refresh records the new launch artifact before startup attestation. If
    # the failure happened after that durable write, the applied generation is
    # intentionally still old. The artifact and launch generations are the
    # binding-owned proof of that attempt. A later deployment may supersede
    # the operation request before recovery runs, so the retry uses the
    # current desired generation after the stopped runtime is proved above.
    now = utc_now()
    workstream_id = str(current["workstream_id"])
    operation_id = str(operation["operation_id"])
    store.conn.execute(
        "UPDATE runtime_bindings SET refresh_pending=0,refresh_operation_id=NULL,refresh_started_at=NULL,launch_generation_sha256=NULL,runtime_instance_id=NULL,report_seq=0,session_start_event_sequence=NULL,session_start_report_seq=NULL,session_started_at=NULL,observed_state='stopped',last_observed_at=?,updated_at=? WHERE workstream_id=? AND refresh_pending=1 AND refresh_operation_id=?",
        (now, now, workstream_id, operation_id),
    )
    store.conn.execute(
        "UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=? AND provisioning_state='needs_attention'",
        (now, workstream_id),
    )
    update_operation_in_transaction(
        store.conn,
        operation_id,
        state="applying",
        step="reserved",
        expected_states=("needs_attention",),
    )
    append_event_in_transaction(
        store.conn,
        kind="runtime.refresh_compensated",
        project_id=str(operation["project_id"]),
        workstream_id=workstream_id,
        operation_id=operation_id,
        payload={"workstreamId": workstream_id, "reason": str(operation["error_message"])},
    )
    return True


def _reserve_refresh(store: Any, binding: Mapping[str, Any], desired: str, workspace: WorkspaceAdapter | None = None) -> str:
    """Reserve a binding before any stop or launch side effect."""
    workstream_id = str(binding["workstream_id"])
    key = f"runtime.refresh:{workstream_id}:{desired}"
    request = {"workstreamId": workstream_id, "desiredGenerationSha256": desired}
    now = utc_now()
    observation = None
    pre_stop_recovery = False
    if workspace is not None:
        observation = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
        if observation.state == "unknown":
            raise NeedsAttentionError("runtime process identity is ambiguous before refresh reservation")
        if observation.state == "live" and binding.get("observed_state") not in {"idle", "stopped"}:
            raise ConflictError("runtime became busy before refresh reservation")
    with store.transaction():
        operation = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (key,)).fetchone()
        pre_stop_retry = bool(
            operation is not None
            and operation["kind"] == "runtime.refresh"
            and operation["state"] == "needs_attention"
            and operation["step"] == "pre_stop_attention"
        )
        pre_stop_error_message = operation["error_message"] if pre_stop_retry else None
        if operation is None:
            operation_id = None
        else:
            operation_id = str(operation["operation_id"])
            if operation["state"] == "needs_attention" and operation["step"] == "pre_stop_attention":
                store.conn.execute(
                    "UPDATE operations SET state='applying',step='reserved',result_json=NULL,error_code=NULL,error_message=NULL,updated_at=? WHERE operation_id=?",
                    (now, operation_id),
                )
                operation = store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            elif operation["state"] == "needs_attention":
                # Defer the guard until the durable binding row is loaded so a
                # stopped pane can prove that the old profile is still active.
                pass
            elif operation["state"] == "failed" and operation["step"] == "superseded" and operation["error_code"] == "runtime_session_reset":
                store.conn.execute(
                    "UPDATE operations SET state='applying',step='reserved',result_json=NULL,error_code=NULL,error_message=NULL,updated_at=? WHERE operation_id=?",
                    (now, operation_id),
                )
                operation = store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        current = store.conn.execute(
            "SELECT r.*,w.desired_state,w.provisioning_state,w.attention_reason,p.active AS project_active,p.lifecycle_attention_reason "
            "FROM runtime_bindings r JOIN workstreams w USING(workstream_id) JOIN projects p USING(project_id) WHERE r.workstream_id=?",
            (workstream_id,),
        ).fetchone()
        if current is None:
            raise NotFoundError("runtime binding was removed")
        recovered = False
        expected_binding: Mapping[str, Any]
        if operation is None and current["refresh_pending"] and current["refresh_operation_id"]:
            pending_operation = store.conn.execute(
                "SELECT * FROM operations WHERE operation_id=? AND kind='runtime.refresh'",
                (current["refresh_operation_id"],),
            ).fetchone()
            if pending_operation is None:
                raise NeedsAttentionError("runtime binding has an unrecorded refresh operation")
            if _recover_stopped_refresh_attention(
                store,
                current,
                pending_operation,
                runtime_state=observation.state if observation is not None else "unknown",
            ):
                operation = store.conn.execute(
                    "SELECT * FROM operations WHERE operation_id=?",
                    (pending_operation["operation_id"],),
                ).fetchone()
                operation_id = str(pending_operation["operation_id"])
                current = store.conn.execute(
                    "SELECT r.*,w.desired_state,w.provisioning_state,w.attention_reason,p.active AS project_active,p.lifecycle_attention_reason "
                    "FROM runtime_bindings r JOIN workstreams w USING(workstream_id) JOIN projects p USING(project_id) WHERE r.workstream_id=?",
                    (workstream_id,),
                ).fetchone()
                expected_binding = dict(current)
                recovered = True
            else:
                raise NeedsAttentionError("runtime binding is already reserved by another refresh")
        elif operation is not None:
            recovered = bool(
                operation["state"] == "needs_attention"
                and _recover_stopped_refresh_attention(
                    store,
                    current,
                    operation,
                    runtime_state=observation.state if observation is not None else "unknown",
                )
            )
            if recovered:
                current = store.conn.execute(
                    "SELECT r.*,w.desired_state,w.provisioning_state,w.attention_reason,p.active AS project_active,p.lifecycle_attention_reason "
                    "FROM runtime_bindings r JOIN workstreams w USING(workstream_id) JOIN projects p USING(project_id) WHERE r.workstream_id=?",
                    (workstream_id,),
                ).fetchone()
                expected_binding = dict(current)
                operation = store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            else:
                expected_binding = binding
        else:
            operation_id = new_id("op")
            prior = store.conn.execute(
                "SELECT operation_id FROM operations WHERE workstream_id=? AND kind='runtime.refresh' AND state='needs_attention' AND step='pre_stop_attention' ORDER BY created_at DESC,operation_id DESC LIMIT 1",
                (workstream_id,),
            ).fetchone()
            pre_stop_recovery = prior is not None and _has_recorded_pre_stop_failure(store, str(prior["operation_id"]))
            store.conn.execute(
                "INSERT INTO operations(operation_id,kind,project_id,workstream_id,idempotency_key,request_json,request_sha256,state,step,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'applying','reserved',?,?)",
                (operation_id, "runtime.refresh", binding["project_id"], workstream_id, key, canonical_json(request), json_digest(request), now, now),
            )
            operation = store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            expected_binding = binding
        if operation is not None and operation["state"] == "needs_attention" and not recovered:
            raise NeedsAttentionError("runtime refresh requires recorded compensation or reconciliation before retry")
        for field in ("runtime_instance_id", "report_seq", "observed_state", "desired_generation_sha256", "applied_generation_sha256", "launch_generation_sha256"):
            if current[field] != expected_binding.get(field):
                raise ConflictError("runtime binding changed before refresh reservation", detail={"field": field})
        pre_stop_retry = pre_stop_recovery or (
            pre_stop_retry and (
                current["attention_reason"] == pre_stop_error_message
                or _has_recorded_pre_stop_failure(store, str(operation_id))
            )
        )
        lifecycle_ready = (
            int(current["project_active"]) == 1
            and current["lifecycle_attention_reason"] is None
            and current["desired_state"] == "active"
            and (
                (current["provisioning_state"] == "bound" and current["attention_reason"] is None)
                or (
                    current["provisioning_state"] == "needs_attention"
                    and (current["attention_reason"] == WORKSPACE_RUNTIME_MISSING or pre_stop_retry)
                )
            )
        )
        if not lifecycle_ready:
            raise ConflictError("workstream changed before refresh reservation", detail={"field": "lifecycle"})
        if int(current["refresh_pending"]) and current["refresh_operation_id"] != operation_id:
            raise NeedsAttentionError("runtime binding is already reserved by another refresh")
        if current["refresh_pending"] and current["refresh_operation_id"] is None:
            raise NeedsAttentionError("runtime binding has an ownerless refresh reservation")
        cursor = store.conn.execute(
            "UPDATE runtime_bindings SET refresh_pending=1,refresh_operation_id=?,refresh_started_at=?,launch_generation_sha256=?,updated_at=? WHERE workstream_id=? AND refresh_pending=0 AND observed_state IN ('idle','stopped') AND desired_generation_sha256=? AND runtime_instance_id IS ? AND report_seq=? AND applied_generation_sha256 IS ? AND launch_generation_sha256 IS ? AND EXISTS (SELECT 1 FROM workstreams w JOIN projects p USING(project_id) WHERE w.workstream_id=runtime_bindings.workstream_id AND p.active=1 AND p.lifecycle_attention_reason IS NULL AND w.desired_state='active' AND ((w.provisioning_state='bound' AND w.attention_reason IS NULL) OR (w.provisioning_state='needs_attention' AND w.attention_reason=?) OR (w.provisioning_state='needs_attention' AND EXISTS (SELECT 1 FROM operations o WHERE o.operation_id=? AND o.kind='runtime.refresh' AND o.state='applying' AND o.step='reserved'))) )",
            (operation_id, now, desired, now, workstream_id, desired, expected_binding.get("runtime_instance_id"), expected_binding.get("report_seq"), expected_binding.get("applied_generation_sha256"), expected_binding.get("launch_generation_sha256"), WORKSPACE_RUNTIME_MISSING, operation_id),
        )
        if cursor.rowcount != 1 and not (int(current["refresh_pending"]) and current["refresh_operation_id"] == operation_id):
            raise ConflictError("runtime binding changed before refresh reservation")
    return operation_id


def _refresh_one(store: Any, harness: HarnessAdapter, workspace: WorkspaceAdapter, binding: Mapping[str, Any], surface: RuntimeSurfaceArtifacts, desired: str, *, wait_seconds: float) -> dict[str, Any]:
    runtime = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
    if runtime.state == "unknown":
        raise NeedsAttentionError("runtime process identity is ambiguous")
    old_instance = str(binding["runtime_instance_id"]) if binding.get("runtime_instance_id") else None
    operation_id = _reserve_refresh(store, binding, desired, workspace)
    binding = _state(store, str(binding["workstream_id"]))
    try:
        staged = _stage_profile(store, harness, binding, surface, operation_id)
    except Exception as error:
        _mark_pre_stop_attention(store, str(binding["workstream_id"]), operation_id, str(error))
        raise
    if runtime.state == "live":
        if binding["observed_state"] not in {"idle", "stopped"}:
            return {"pending": True, "state": str(binding["observed_state"]), "reason": f"runtime is {binding['observed_state']}"}
        workspace.stop_runtime(str(binding["workspace_surface_id"]))
        _wait_for_exit(workspace, binding)
    current = _materialize_and_launch(store, harness, workspace, binding, surface, desired, staged=staged)
    attested = _startup_attested(store, workspace, str(binding["workstream_id"]), old_instance, desired)
    if attested is not None:
        return {"pending": False, "binding": attested}
    if wait_seconds > 0:
        attested = _wait_for_start(store, workspace, str(binding["workstream_id"]), old_instance, desired, wait_seconds)
        if attested is not None:
            return {"pending": False, "binding": attested}
    return {"pending": True, "binding": current, "state": "startup_in_progress", "reason": "startup attestation is still pending"}


def _refresh_runtimes_unlocked(store: Any, harness: HarnessAdapter, workspace: WorkspaceAdapter, *, wait_seconds: float = 300.0, harness_resolver: Any | None = None, surface_resolver: Any | None = None, project_ids: Sequence[str] = (), harness_ids: Sequence[str] = (), workstream_ids: Sequence[str] = ()) -> dict[str, Any]:
    if not isinstance(wait_seconds, (int, float)) or isinstance(wait_seconds, bool) or not 0 <= wait_seconds <= 3600:
        raise PisecError("refresh wait must be between 0 and 3600 seconds")
    selected = _active_bindings(store, project_ids=project_ids, harness_ids=harness_ids, workstream_ids=workstream_ids)
    result: dict[str, Any] = {"generation": None, "upgraded": [], "pending": [], "skipped": [], "failed": [], "ok": True}
    if not selected:
        return result
    surfaces: dict[str, RuntimeSurfaceArtifacts] = {}

    def operation_surface(harness_id: str) -> RuntimeSurfaceArtifacts:
        key = str(harness_id)
        if key not in surfaces:
            binding = next((row for row in selected if str(row["harness_id"]) == key), None)
            if binding is None:
                raise NeedsAttentionError("runtime surface was requested for an unselected harness")
            selected_harness = _harness_for(binding, harness, harness_resolver)
            surfaces[key] = _surface_for(binding, selected_harness, surface_resolver)
        return verify_surface(surfaces[key])

    marked = mark_stale_bindings(store, harness, harness_resolver=harness_resolver, surface_resolver=operation_surface, project_ids=project_ids, harness_ids=harness_ids, workstream_ids=workstream_ids)
    result["failed"].extend(marked["failed"])
    generations = {str(item["generation"]) for item in marked["stale"] if item.get("generation")}
    result["generation"] = next(iter(generations)) if len(generations) == 1 else ("per-binding" if generations else None)
    stale = {str(item["workstreamId"]): item for item in marked["stale"]}
    for item in marked["current"]:
        result["skipped"].append({**item, "reason": "current generation"})
    deadline = time.monotonic() + float(wait_seconds)
    remaining = list(stale)
    while remaining:
        next_remaining: list[str] = []
        for workstream_id in remaining:
            binding = _state(store, workstream_id)
            desired = str(binding["desired_generation_sha256"])
            selected_harness = _harness_for(binding, harness, harness_resolver)
            try:
                surface = operation_surface(str(binding["harness_id"]))
                runtime = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
                if runtime.state == "live" and binding["observed_state"] not in {"idle", "stopped"}:
                    result["pending"].append(_item(binding, desired, state=str(binding["observed_state"])))
                    next_remaining.append(workstream_id)
                    continue
                refreshed = _refresh_one(store, selected_harness, workspace, binding, surface, desired, wait_seconds=max(0.0, deadline - time.monotonic()))
                if refreshed.get("pending"):
                    result["pending"].append(
                        _item(
                            binding,
                            desired,
                            state=str(refreshed.get("state", binding["observed_state"])),
                            reason=str(refreshed.get("reason", "startup attestation is still pending")),
                        )
                    )
                    next_remaining.append(workstream_id)
                    continue
                current = _state(store, workstream_id)
                result["upgraded"].append(_item(current, str(current["desired_generation_sha256"])))
            except Exception as error:
                _mark_attention(store, workstream_id, str(error))
                result["failed"].append(_item(binding, desired, reason=str(error)[:512]))
        remaining = next_remaining
        if remaining and time.monotonic() < deadline:
            time.sleep(0.05)
        else:
            break
    result["ok"] = not result["failed"]
    return result


@control_plane_mutation
def refresh_runtimes(store: Any, harness: HarnessAdapter, workspace: WorkspaceAdapter, *, wait_seconds: float = 300.0, harness_resolver: Any | None = None, surface_resolver: Any | None = None, project_ids: Sequence[str] = (), harness_ids: Sequence[str] = (), workstream_ids: Sequence[str] = ()) -> dict[str, Any]:
    selected = _active_bindings(store, project_ids=project_ids, harness_ids=harness_ids, workstream_ids=workstream_ids)
    selected_projects = sorted({str(row["project_id"]) for row in selected})
    with ExitStack() as locks:
        for project_id in selected_projects:
            locks.enter_context(project_permissions_lock(store.state_root, project_id))
        return _refresh_runtimes_unlocked(
            store,
            harness,
            workspace,
            wait_seconds=wait_seconds,
            harness_resolver=harness_resolver,
            surface_resolver=surface_resolver,
            project_ids=project_ids,
            harness_ids=harness_ids,
            workstream_ids=workstream_ids,
        )


def refresh_projects(store: Any, harness: HarnessAdapter, workspace: WorkspaceAdapter, *, wait_seconds: float = 300.0, harness_resolver: Any | None = None, surface_resolver: Any | None = None) -> dict[str, Any]:
    return refresh_runtimes(store, harness, workspace, wait_seconds=wait_seconds, harness_resolver=harness_resolver, surface_resolver=surface_resolver)


def refresh_bindings(store: Any, harness: HarnessAdapter, workspace: WorkspaceAdapter, workstream_ids: Sequence[str], *, wait_seconds: float = 300.0, harness_resolver: Any | None = None, surface_resolver: Any | None = None) -> dict[str, Any]:
    return refresh_runtimes(store, harness, workspace, wait_seconds=wait_seconds, harness_resolver=harness_resolver, surface_resolver=surface_resolver, workstream_ids=workstream_ids)


@control_plane_mutation
def ensure_runtime(store: Any, harness: HarnessAdapter, workspace: WorkspaceAdapter, *, workstream_id: str, wait_seconds: float = 30.0, harness_resolver: Any | None = None, surface_resolver: Any | None = None, reset_session: bool = False) -> dict[str, Any]:
    if not isinstance(wait_seconds, (int, float)) or isinstance(wait_seconds, bool) or not 0 <= wait_seconds <= 3600:
        raise PisecError("ensure wait must be between 0 and 3600 seconds")
    binding = _binding(store, workstream_id)
    selected_harness = _harness_for(binding, harness, harness_resolver)
    try:
        surface = _surface_for(binding, selected_harness, surface_resolver)
        surface_cache = {str(selected_harness.manifest.adapter_id): surface}
        desired = _desired(selected_harness, _scope(store, binding, selected_harness), surface)
        if binding["desired_generation_sha256"] != desired:
            with store.transaction():
                store.conn.execute("UPDATE runtime_bindings SET desired_generation_sha256=?,updated_at=? WHERE workstream_id=?", (desired, utc_now(), workstream_id))
            binding = _state(store, workstream_id)
        runtime = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
        if reset_session:
            if runtime.state != "stopped":
                raise ConflictError("session reset requires a stopped runtime")
            _reset_stale_refresh_for_new_session(store, binding)
            binding = _state(store, workstream_id)
            reset_codex_session_in_transaction(store.conn, binding)
            binding = _state(store, workstream_id)
        if runtime.state == "unknown":
            raise NeedsAttentionError("runtime process identity is ambiguous")
        current = bool(
            binding["provisioning_state"] == "bound"
            and binding["applied_generation_sha256"] == desired
            and not binding["refresh_pending"]
            and binding["refresh_operation_id"] is None
            and binding["refresh_started_at"] is None
            and binding["launch_generation_sha256"] is None
        )
        if runtime.state == "live" and current and usable_runtime_binding(
            store,
            workstream_id,
            workspace,
            selected_harness,
            allowed_states=frozenset({"idle", "working", "blocked"}),
        ):
            return {"workstreamId": workstream_id, "action": "already_live", "state": "live", "generation": desired, "reason": None}
        if runtime.state == "live" and binding["observed_state"] == "starting":
            attested = _wait_for_start(store, workspace, workstream_id, str(binding["runtime_instance_id"]) if binding["runtime_instance_id"] else None, desired, float(wait_seconds)) if wait_seconds else None
            if attested:
                return {"workstreamId": workstream_id, "action": "already_live", "state": "live", "generation": desired, "reason": None}
            return {"workstreamId": workstream_id, "action": "startup_in_progress", "state": "starting", "generation": desired, "reason": "startup attestation is still pending"}
        if runtime.state == "live":
            if binding["observed_state"] not in {"idle", "stopped"}:
                return {"workstreamId": workstream_id, "action": "pending_refresh", "state": str(binding["observed_state"]), "generation": desired, "reason": "runtime is busy"}
            refreshed = refresh_runtimes(store, harness, workspace, wait_seconds=wait_seconds, harness_resolver=harness_resolver, surface_resolver=lambda harness_id: surface_cache.get(str(harness_id), surface), workstream_ids=(workstream_id,))
            if refreshed["upgraded"]:
                return {"workstreamId": workstream_id, "action": "refreshed_started" if wait_seconds else "startup_in_progress", "state": "live" if wait_seconds else "starting", "generation": desired, "reason": None}
            if refreshed["pending"]:
                pending = refreshed["pending"][0]
                if pending.get("state") == "startup_in_progress":
                    return {"workstreamId": workstream_id, "action": "startup_in_progress", "state": "starting", "generation": desired, "reason": pending.get("reason")}
                return {"workstreamId": workstream_id, "action": "pending_refresh", "state": str(pending.get("state", binding["observed_state"])), "generation": desired, "reason": pending.get("reason", "runtime is busy")}
            raise NeedsAttentionError((refreshed["failed"] or [{"reason": "targeted refresh failed"}])[0]["reason"])
        if runtime.state != "stopped":
            raise NeedsAttentionError(f"runtime state is {runtime.state}")
        if current:
            validate_worker_resume_git(store, binding)
            old_instance = str(binding["runtime_instance_id"]) if binding["runtime_instance_id"] else None
            now = utc_now()
            with store.transaction():
                cursor = store.conn.execute(
                    "UPDATE runtime_bindings SET runtime_instance_id=NULL,report_seq=0,session_start_event_sequence=NULL,session_start_report_seq=NULL,session_started_at=NULL,observed_state='starting',last_observed_at=?,updated_at=? WHERE workstream_id=? AND refresh_pending=0 AND refresh_operation_id IS NULL AND refresh_started_at IS NULL AND launch_generation_sha256 IS NULL AND desired_generation_sha256=? AND applied_generation_sha256=? AND observed_state=? AND runtime_instance_id IS ? AND report_seq=?",
                    (now, now, workstream_id, desired, desired, binding["observed_state"], binding["runtime_instance_id"], binding["report_seq"]),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("runtime binding changed before restart")
            start_bound_agent(store, workspace, selected_harness, _state(store, workstream_id), workstream_id=workstream_id, project_id=str(binding["project_id"]), cwd=str(binding["worktree_path"]))
            if wait_seconds:
                attested = _wait_for_start(store, workspace, workstream_id, old_instance, desired, float(wait_seconds))
                if attested:
                    return {"workstreamId": workstream_id, "action": "started", "state": "live", "generation": desired, "reason": None}
                return {"workstreamId": workstream_id, "action": "startup_in_progress", "state": "starting", "generation": desired, "reason": "startup attestation is still pending"}
            return {"workstreamId": workstream_id, "action": "startup_in_progress", "state": "starting", "generation": desired, "reason": "startup attestation is still pending"}
        refreshed = refresh_runtimes(store, harness, workspace, wait_seconds=wait_seconds, harness_resolver=harness_resolver, surface_resolver=lambda harness_id: surface_cache.get(str(harness_id), surface), workstream_ids=(workstream_id,))
        if refreshed["upgraded"]:
            return {"workstreamId": workstream_id, "action": "refreshed_started" if wait_seconds else "startup_in_progress", "state": "live" if wait_seconds else "starting", "generation": desired, "reason": None}
        if refreshed["pending"]:
            pending = refreshed["pending"][0]
            if pending.get("state") == "startup_in_progress":
                return {"workstreamId": workstream_id, "action": "startup_in_progress", "state": "starting", "generation": desired, "reason": pending.get("reason")}
            return {"workstreamId": workstream_id, "action": "pending_refresh", "state": str(pending.get("state", "starting")), "generation": desired, "reason": pending.get("reason", "startup attestation is pending")}
        raise NeedsAttentionError((refreshed["failed"] or [{"reason": "targeted refresh failed"}])[0]["reason"])
    except Exception as error:
        _mark_attention(store, workstream_id, str(error))
        return {"workstreamId": workstream_id, "action": "needs_attention", "state": "needs_attention", "generation": locals().get("desired"), "reason": str(error)[:512]}
