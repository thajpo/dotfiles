"""One selector-aware lifecycle engine for Pisec runtime convergence."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adapters import HarnessAdapter, WorkspaceAdapter, RuntimeSurfaceArtifacts, artifact_document
from .worker_repo import validate_worker_resume_git
from .models import ConflictError, NeedsAttentionError, NotFoundError, PisecError, canonical_json, json_digest, new_id, utc_now, validate_sha256
from .runtime import start_bound_agent, usable_runtime_binding
from .runtime_surface import capture_runtime_surface, verify_surface
from .events import append_event_in_transaction


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


def _scope(store: Any, binding: Mapping[str, Any]) -> dict[str, Any]:
    from .access import effective_runtime_scope
    return effective_runtime_scope(store, binding)


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
            desired = _desired(selected, _scope(store, binding), surface)
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
        if observation.state == "unknown":
            raise NeedsAttentionError("runtime process identity became ambiguous during refresh")
        if time.monotonic() >= deadline:
            raise NeedsAttentionError("runtime did not stop gracefully")
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
    scope = _scope(store, binding)
    verify_surface(surface)
    stage_root = Path(str(scope.get("profileStagingRoot", Path(binding["harness_home"]).parent / ".staging" / str(binding["workstream_id"]))))
    staged = harness.stage_profile({**scope, "operationId": operation_id, "runtimeSurfaceSha256": surface.content_sha256, "runtimeSurfaceRoot": surface.root_path, "runtimeSurfaceId": "surface_" + surface.content_sha256[:32]}, surface, stage_root)
    verify_surface(surface)
    return staged


def _materialize_and_launch(store: Any, harness: HarnessAdapter, workspace: WorkspaceAdapter, binding: Mapping[str, Any], surface: RuntimeSurfaceArtifacts, desired: str, *, staged: Any | None = None) -> dict[str, Any]:
    validate_worker_resume_git(store, binding)
    scope = _scope(store, binding)
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


def _reserve_refresh(store: Any, binding: Mapping[str, Any], desired: str, workspace: WorkspaceAdapter | None = None) -> str:
    """Reserve a binding before any stop or launch side effect."""
    workstream_id = str(binding["workstream_id"])
    key = f"runtime.refresh:{workstream_id}:{desired}"
    request = {"workstreamId": workstream_id, "desiredGenerationSha256": desired}
    now = utc_now()
    if workspace is not None:
        observation = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
        if observation.state == "unknown":
            raise NeedsAttentionError("runtime process identity is ambiguous before refresh reservation")
        if observation.state == "live" and binding.get("observed_state") not in {"idle", "stopped"}:
            raise ConflictError("runtime became busy before refresh reservation")
    with store.transaction():
        operation = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (key,)).fetchone()
        if operation is None:
            operation_id = new_id("op")
            store.conn.execute(
                "INSERT INTO operations(operation_id,kind,project_id,workstream_id,idempotency_key,request_json,request_sha256,state,step,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'applying','reserved',?,?)",
                (operation_id, "runtime.refresh", binding["project_id"], workstream_id, key, canonical_json(request), json_digest(request), now, now),
            )
        else:
            operation_id = str(operation["operation_id"])
            if operation["state"] == "needs_attention" and operation["step"] == "pre_stop_attention":
                store.conn.execute(
                    "UPDATE operations SET state='applying',step='reserved',result_json=NULL,error_code=NULL,error_message=NULL,updated_at=? WHERE operation_id=?",
                    (now, operation_id),
                )
            elif operation["state"] == "needs_attention":
                raise NeedsAttentionError("runtime refresh requires recorded compensation or reconciliation before retry")
        current = store.conn.execute("SELECT refresh_pending,refresh_operation_id,refresh_started_at,runtime_instance_id,report_seq,observed_state,desired_generation_sha256,applied_generation_sha256,launch_generation_sha256 FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()
        if current is None:
            raise NotFoundError("runtime binding was removed")
        for field in ("runtime_instance_id", "report_seq", "observed_state", "desired_generation_sha256", "applied_generation_sha256", "launch_generation_sha256"):
            if current[field] != binding.get(field):
                raise ConflictError("runtime binding changed before refresh reservation", detail={"field": field})
        if int(current["refresh_pending"]) and current["refresh_operation_id"] != operation_id:
            raise NeedsAttentionError("runtime binding is already reserved by another refresh")
        if current["refresh_pending"] and current["refresh_operation_id"] is None:
            raise NeedsAttentionError("runtime binding has an ownerless refresh reservation")
        cursor = store.conn.execute(
            "UPDATE runtime_bindings SET refresh_pending=1,refresh_operation_id=?,refresh_started_at=?,launch_generation_sha256=?,updated_at=? WHERE workstream_id=? AND refresh_pending=0 AND observed_state IN ('idle','stopped') AND desired_generation_sha256=? AND runtime_instance_id IS ? AND report_seq=? AND applied_generation_sha256 IS ? AND launch_generation_sha256 IS ?",
            (operation_id, now, desired, now, workstream_id, desired, binding.get("runtime_instance_id"), binding.get("report_seq"), binding.get("applied_generation_sha256"), binding.get("launch_generation_sha256")),
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


def refresh_runtimes(store: Any, harness: HarnessAdapter, workspace: WorkspaceAdapter, *, wait_seconds: float = 300.0, harness_resolver: Any | None = None, surface_resolver: Any | None = None, project_ids: Sequence[str] = (), harness_ids: Sequence[str] = (), workstream_ids: Sequence[str] = ()) -> dict[str, Any]:
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


def refresh_projects(store: Any, harness: HarnessAdapter, workspace: WorkspaceAdapter, *, wait_seconds: float = 300.0, harness_resolver: Any | None = None, surface_resolver: Any | None = None) -> dict[str, Any]:
    return refresh_runtimes(store, harness, workspace, wait_seconds=wait_seconds, harness_resolver=harness_resolver, surface_resolver=surface_resolver)


def refresh_bindings(store: Any, harness: HarnessAdapter, workspace: WorkspaceAdapter, workstream_ids: Sequence[str], *, wait_seconds: float = 300.0, harness_resolver: Any | None = None, surface_resolver: Any | None = None) -> dict[str, Any]:
    return refresh_runtimes(store, harness, workspace, wait_seconds=wait_seconds, harness_resolver=harness_resolver, surface_resolver=surface_resolver, workstream_ids=workstream_ids)


def ensure_runtime(store: Any, harness: HarnessAdapter, workspace: WorkspaceAdapter, *, workstream_id: str, wait_seconds: float = 30.0, harness_resolver: Any | None = None, surface_resolver: Any | None = None) -> dict[str, Any]:
    if not isinstance(wait_seconds, (int, float)) or isinstance(wait_seconds, bool) or not 0 <= wait_seconds <= 3600:
        raise PisecError("ensure wait must be between 0 and 3600 seconds")
    binding = _binding(store, workstream_id)
    selected_harness = _harness_for(binding, harness, harness_resolver)
    try:
        surface = _surface_for(binding, selected_harness, surface_resolver)
        surface_cache = {str(selected_harness.manifest.adapter_id): surface}
        desired = _desired(selected_harness, _scope(store, binding), surface)
        if binding["desired_generation_sha256"] != desired:
            with store.transaction():
                store.conn.execute("UPDATE runtime_bindings SET desired_generation_sha256=?,updated_at=? WHERE workstream_id=?", (desired, utc_now(), workstream_id))
            binding = _state(store, workstream_id)
        runtime = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
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
