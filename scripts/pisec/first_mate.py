"""Global Pisec First Mate lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .adapters import HarnessAdapter, RuntimeSurfaceArtifacts, WorkspaceAdapter, WorkspaceObservation, artifact_document
from .access import compose_runtime_domains
from .events import append_event_in_transaction
from .fence import resolve_data_dirs
from .models import ConflictError, NeedsAttentionError, NotFoundError, canonical_json, json_digest, new_id, utc_now, validate_sha256
from .prompt_contract import FIRST_MATE_BRIEF, FIRST_MATE_RESPONSE_CONTRACT
from .projects import observe_project, resolve_project
from .runtime_surface import capture_runtime_surface, materialize_current_surface
from .runtime import WORKSPACE_RUNTIME_MISSING, start_bound_agent, usable_runtime_binding
from .workstreams import APPLY_LOCK, _session_attestation_matches, _wait_for_agent
from .worker_repo import project_permissions_lock


def _project_active(project: Mapping[str, Any]) -> bool:
    value = project.get("active")
    return True if value is None else bool(value)

FIRST_MATE_CHECKPOINTS = (
    "workspace_observed_or_created",
    "profile_materialized",
    "binding_committed",
    "map_committed",
    "agent_started",
    "brief_delivered",
    "observed",
    "committed",
)
_SURFACE_SCOPE_FIELDS = frozenset({"runtimeSurfaceSha256", "runtimeSurfaceRoot", "runtimeSurfaceId", "runtimeSurfaceManifest"})

def _first_mate(store: Any) -> dict[str, Any] | None:
    row = store.conn.execute(
        "SELECT * FROM workstreams WHERE kind='first_mate' AND desired_state <> 'retired'"
    ).fetchone()
    return None if row is None else dict(row)


def _binding(store: Any, workstream_id: str) -> dict[str, Any] | None:
    row = store.conn.execute(
        "SELECT * FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)
    ).fetchone()
    return None if row is None else dict(row)


def _operation(store: Any, workstream_id: str) -> dict[str, Any] | None:
    row = store.conn.execute(
        "SELECT * FROM operations WHERE workstream_id=? AND kind='first_mate.ensure' ORDER BY created_at DESC LIMIT 1",
        (workstream_id,),
    ).fetchone()
    return None if row is None else dict(row)


def _rank(step: str) -> int:
    return FIRST_MATE_CHECKPOINTS.index(step) if step in FIRST_MATE_CHECKPOINTS else -1


def _hit(failpoint: Any, name: str, scope: Mapping[str, Any]) -> None:
    if failpoint is not None:
        failpoint.hit(name, {"operation_id": str(scope["operationId"]), "workstream_id": str(scope["workstreamId"])})


def _scope(project: Mapping[str, Any], workstream: Mapping[str, Any], operation_id: str, external_domains: tuple[str, ...] = ("*",)) -> dict[str, Any]:
    return {
        "projectId": project["project_id"],
        "workstreamId": workstream["workstream_id"],
        "operationId": operation_id,
        "title": workstream["title"],
        "purpose": workstream["purpose"],
        "brief": workstream["brief"],
        "harnessId": workstream["harness_id"],
        "workspaceAdapterId": workstream["workspace_adapter_id"],
        "executionProfile": "first-mate",
        "targetRef": workstream["target_ref"],
        "baseCommitOid": workstream["base_commit_oid"],
        "branchName": workstream["branch_name"],
        "worktreePath": workstream["worktree_path"],
        "agentName": f"pisec-{workstream['workstream_id'][-12:]}",
        "externalDomains": list(external_domains),
        "dataDirs": resolve_data_dirs(project.get("data_dirs"), Path(project["repository_path"])),
        "effects": ["create execution workspace", "start fenced global coordinator", "read brokered project worker state and diffs", "route typed attention and workflow records to registered project secretaries"],
        "nonEffects": ["no host-secret access", "no project checkout or worker-worktree writes", "no raw push or publish", "no project registration or runtime administration", "no worker creation or workstream acceptance without exact user approval"],
    }


def _surface_scope(scope: Mapping[str, Any], surface: RuntimeSurfaceArtifacts) -> dict[str, Any]:
    return {
        **scope,
        "runtimeSurfaceSha256": surface.content_sha256,
        "runtimeSurfaceRoot": surface.root_path,
        "runtimeSurfaceId": "surface_" + surface.content_sha256[:32],
        "runtimeSurfaceManifest": surface.manifest_json,
    }


def _surface_from_scope(scope: Mapping[str, Any]) -> RuntimeSurfaceArtifacts | None:
    if not (_SURFACE_SCOPE_FIELDS & set(scope)):
        return None
    try:
        surface = RuntimeSurfaceArtifacts(str(scope["runtimeSurfaceSha256"]), str(scope["runtimeSurfaceManifest"]), str(scope["runtimeSurfaceRoot"]))
        if scope["runtimeSurfaceId"] != "surface_" + surface.content_sha256[:32]:
            raise NeedsAttentionError("First Mate runtime surface identity is invalid")
        return surface
    except Exception as error:
        raise NeedsAttentionError("First Mate runtime surface snapshot is invalid") from error


def _ensure_surface_snapshot(store: Any, operation_id: str, scope: Mapping[str, Any], harness: HarnessAdapter) -> tuple[dict[str, Any], RuntimeSurfaceArtifacts]:
    surface = _surface_from_scope(scope)
    if surface is None:
        surface = capture_runtime_surface(harness)
        scope = _surface_scope(scope, surface)
        with store.transaction():
            store.conn.execute("UPDATE operations SET result_json=?,updated_at=? WHERE operation_id=?", (canonical_json(scope, max_bytes=256 * 1024, max_text=64 * 1024), utc_now(), operation_id))
    return dict(scope), surface


def _mark_attention(store: Any, operation_id: str, workstream_id: str, reason: str) -> None:
    now = utc_now()
    with store.transaction():
        store.conn.execute("UPDATE operations SET state='needs_attention',error_code='effect_mismatch',error_message=?,updated_at=? WHERE operation_id=?", (reason[:512], now, operation_id))
        store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?", (reason[:512], now, workstream_id))


def _validate_workspace(observed: WorkspaceObservation, expected: Mapping[str, Any] | None = None) -> WorkspaceObservation:
    if not observed.workspace_id or not observed.view_id or not observed.surface_id:
        raise NeedsAttentionError("First Mate workspace observation is incomplete")
    if expected is not None:
        if expected.get("workspace_id") not in {None, observed.workspace_id} or expected.get("workspace_view_id") not in {None, observed.view_id} or expected.get("workspace_surface_id") not in {None, observed.surface_id}:
            raise NeedsAttentionError("First Mate workspace identity mismatch")
    return observed


def _observe(workspace: WorkspaceAdapter, scope: Mapping[str, Any], expected: Mapping[str, Any] | None = None) -> WorkspaceObservation | None:
    observed = workspace.observe_workstream(path=str(scope["worktreePath"]), agent_name=str(scope["agentName"]))
    if observed is None:
        return None
    observed = _validate_workspace(observed, expected)
    if observed.agent is not None and expected is not None and expected.get("workspace_surface_id") not in {None, observed.agent.surface_id}:
        raise NeedsAttentionError("First Mate agent identity mismatch")
    return observed


def _observe_binding(workspace: WorkspaceAdapter, scope: Mapping[str, Any], binding: Mapping[str, Any]) -> WorkspaceObservation | None:
    observed = workspace.observe_surface(
        workspace_id=str(binding["workspace_id"]),
        view_id=str(binding["workspace_view_id"]),
        surface_id=str(binding["workspace_surface_id"]),
        cwd=str(scope["worktreePath"]),
    )
    if observed is None:
        return None
    return _validate_workspace(observed, binding)


def _recover_workspace(workspace: WorkspaceAdapter, scope: Mapping[str, Any], expected: Mapping[str, Any] | None = None) -> WorkspaceObservation:
    scratch = Path(str(scope["worktreePath"]))
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    observed = _observe(workspace, scope, expected)
    if observed is not None:
        workspace.rename_tab(observed.view_id, "First Mate")
        return observed
    try:
        observed = workspace.create_workspace(str(scratch), "Pisec First Mate", focus=False)
    except Exception as error:
        try:
            observed = _observe(workspace, scope, expected)
        except Exception as observe_error:
            raise NeedsAttentionError("First Mate workspace effect is ambiguous") from observe_error
        if observed is None:
            raise RuntimeError("First Mate workspace creation failed after retry") from error
    observed = _validate_workspace(observed, expected)
    workspace.rename_tab(observed.view_id, "First Mate")
    return observed


def _recover_start(store: Any, workspace: WorkspaceAdapter, harness: HarnessAdapter, scope: Mapping[str, Any], binding: Mapping[str, Any]) -> None:
    desired_generation = binding.get("desired_generation_sha256")
    validate_sha256(desired_generation, "First Mate desired runtime generation")
    if binding.get("launch_generation_sha256") not in {None, desired_generation}:
        raise NeedsAttentionError("First Mate launch generation is stale")
    if binding.get("applied_generation_sha256") not in {None, desired_generation} and binding.get("launch_generation_sha256") != desired_generation:
        raise NeedsAttentionError("First Mate applied runtime generation is stale")
    observed = _observe_binding(workspace, scope, binding)
    agent = observed.agent if observed is not None else None
    runtime = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
    if runtime.state == "unknown":
        raise NeedsAttentionError("First Mate runtime identity is ambiguous before start")
    ready = runtime.state == "live" and agent is not None and agent.surface_id == binding["workspace_surface_id"] and agent.identity_usable is True
    if not ready:
        with store.transaction():
            now = utc_now()
            store.conn.execute("UPDATE runtime_bindings SET runtime_instance_id=NULL,report_seq=0,launch_generation_sha256=?,refresh_pending=1,refresh_operation_id=?,refresh_started_at=?,observed_state='starting',updated_at=? WHERE workstream_id=?", (desired_generation, scope["operationId"], now, now, scope["workstreamId"]))
            store.conn.execute("UPDATE workstreams SET provisioning_state='bound',updated_at=? WHERE workstream_id=?", (now, scope["workstreamId"]))
        start_error: Exception | None = None
        try:
            start_bound_agent(store, workspace, harness, binding, workstream_id=str(scope["workstreamId"]), project_id=str(scope["projectId"]), cwd=str(scope["worktreePath"]))
        except Exception as error:
            start_error = error
        try:
            _wait_for_agent(store, workspace, workstream_id=str(scope["workstreamId"]), path=str(scope["worktreePath"]), agent_name=str(scope["agentName"]), workspace_id=str(binding["workspace_id"]), view_id=str(binding["workspace_view_id"]), surface_id=str(binding["workspace_surface_id"]))
        except NeedsAttentionError as wait_error:
            if start_error is not None:
                raise wait_error from start_error
            raise
    else:
        _wait_for_agent(store, workspace, workstream_id=str(scope["workstreamId"]), path=str(scope["worktreePath"]), agent_name=str(scope["agentName"]), workspace_id=str(binding["workspace_id"]), view_id=str(binding["workspace_view_id"]), surface_id=str(binding["workspace_surface_id"]))
    with store.transaction():
        store.conn.execute("UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=?", (utc_now(), scope["workstreamId"]))


def _repair_launch_binding(
    store: Any,
    workspace: WorkspaceAdapter,
    harness: HarnessAdapter,
    project: Mapping[str, Any],
    scope: Mapping[str, Any],
    binding: Mapping[str, Any],
    *,
    surface: RuntimeSurfaceArtifacts,
) -> dict[str, Any]:
    current_scope = _surface_scope(scope, surface)
    desired = harness.desired_generation(current_scope, surface)
    if (
        binding.get("desired_generation_sha256") == desired
        and binding.get("applied_generation_sha256") == desired
        and binding.get("launch_generation_sha256") is None
        and not binding.get("refresh_pending")
    ):
        return dict(binding)
    runtime = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
    if runtime.state == "unknown":
        raise NeedsAttentionError("First Mate runtime identity is ambiguous before binding repair")
    if runtime.state == "live":
        raise NeedsAttentionError("First Mate binding repair requires a stopped runtime")
    artifacts, _surface, materialized_scope = materialize_current_surface(store, harness, current_scope, surface=surface)
    if artifacts.generation_sha256 != desired:
        raise NeedsAttentionError("activated First Mate runtime generation does not match the captured surface")
    harness.commit_launch_binding(
        materialized_scope,
        artifacts,
        workspace_session_name=str(binding["workspace_session_name"]),
        workspace_id=str(binding["workspace_id"]),
        workspace_view_id=str(binding["workspace_view_id"]),
        workspace_surface_id=str(binding["workspace_surface_id"]),
        replace=True,
    )
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "UPDATE runtime_bindings SET harness_home=?,adapter_artifacts_json=?,launch_secret_path=?,policy_path=?,policy_sha256=?,runtime_token_sha256=?,desired_generation_sha256=?,applied_generation_sha256=NULL,launch_generation_sha256=?,refresh_pending=1,refresh_operation_id=?,refresh_started_at=?,runtime_instance_id=NULL,report_seq=0,session_start_event_sequence=NULL,session_start_report_seq=NULL,session_started_at=NULL,observed_state='starting',last_observed_at=NULL,updated_at=? WHERE workstream_id=?",
            (artifacts.harness_home, artifact_document(harness.manifest, artifacts), artifacts.launch_secret_path, artifacts.policy_path, artifacts.policy_sha256, artifacts.runtime_token_sha256, desired, desired, scope["operationId"], now, now, binding["workstream_id"]),
        )
        append_event_in_transaction(store.conn, kind="first_mate.binding.repaired", project_id=project["project_id"], workstream_id=scope["workstreamId"], operation_id=scope["operationId"], payload={"reason": "reopen after deployed runtime binding change"})
    repaired = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (binding["workstream_id"],)).fetchone()
    if repaired is None:
        raise NeedsAttentionError("First Mate runtime binding disappeared during repair")
    return dict(repaired)


def _ensure_locked(store: Any, control_project_selector: str, harness: HarnessAdapter, workspace: WorkspaceAdapter, failpoint: Any = None) -> dict[str, Any]:
    project = resolve_project(store, control_project_selector)
    if not _project_active(project):
        raise ConflictError("control project is inactive; choose an active project for the First Mate")
    harness.validate_execution_profile("first-mate", "first_mate")
    project_domains = json.loads(project["external_domains"] or "[]")
    external_domains = compose_runtime_domains(harness, "first-mate", project_domains)
    existing = _first_mate(store)
    if existing is None:
        workstream_id = new_id("ws")
        operation_id = new_id("op")
        now = utc_now()
        base_oid = observe_project(project["repository_path"], project["default_ref"])["default_oid"]
        scratch = Path(store.state_root) / "first-mate" / workstream_id
        branch = f"first-mate/{workstream_id}"
        with store.transaction():
            store.conn.execute(
                "INSERT INTO workstreams(workstream_id,project_id,kind,title,purpose,brief,harness_id,workspace_adapter_id,execution_profile,target_ref,base_commit_oid,branch_name,worktree_path,desired_state,provisioning_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (workstream_id, project["project_id"], "first_mate", "Global Pisec First Mate", "Manage all registered Pisec project secretaries.", FIRST_MATE_BRIEF, harness.manifest.adapter_id, workspace.manifest.adapter_id, "first-mate", project["default_ref"], base_oid, branch, str(scratch), "active", "proposed", now, now),
            )
            created = dict(store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone())
            scope = _scope(project, created, operation_id, external_domains)
            request = {"projectId": project["project_id"], "kind": "first_mate.ensure"}
            store.conn.execute(
                "INSERT INTO operations(operation_id,kind,project_id,workstream_id,idempotency_key,request_json,request_sha256,state,step,result_json,created_at,updated_at) VALUES(?,'first_mate.ensure',?,?,?,?,?,'applying','planned',?,?,?)",
                (operation_id, project["project_id"], workstream_id, "first_mate.ensure", canonical_json(request), json_digest(request), canonical_json(scope, max_bytes=256 * 1024, max_text=64 * 1024), now, now),
            )
        _hit(failpoint, "after_first_mate_proposal_commit", scope)
        existing = created
    operation = _operation(store, existing["workstream_id"])
    if operation is None:
        raise NeedsAttentionError("First Mate ensure operation is missing")
    try:
        scope = json.loads(operation["result_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise NeedsAttentionError("First Mate ensure scope is missing or invalid") from error
    fresh = _scope(project, existing, operation["operation_id"], external_domains)
    if not isinstance(scope, dict) or {key: value for key, value in scope.items() if key not in _SURFACE_SCOPE_FIELDS} != fresh:
        raise NeedsAttentionError("First Mate ensure scope is missing or invalid")
    recoverable_missing = (
        existing["provisioning_state"] == "needs_attention"
        and existing["attention_reason"] == WORKSPACE_RUNTIME_MISSING
        and operation["state"] in {"applying", "succeeded"}
    )
    if operation["state"] == "succeeded" and (existing["provisioning_state"] == "bound" or recoverable_missing):
        binding = _binding(store, existing["workstream_id"])
        if binding is None:
            raise NeedsAttentionError("First Mate runtime binding is missing")
        surface = capture_runtime_surface(harness)
        current_scope = _surface_scope(scope, surface)
        try:
            desired = harness.desired_generation(current_scope, surface)
            if not (
                binding.get("desired_generation_sha256") == desired
                and (binding.get("applied_generation_sha256") == desired or binding.get("launch_generation_sha256") == desired)
                and binding.get("launch_generation_sha256") in {None, desired}
                and not binding.get("refresh_pending")
            ):
                binding = _repair_launch_binding(store, workspace, harness, project, current_scope, binding, surface=surface)
            _recover_start(store, workspace, harness, current_scope, binding)
        except NeedsAttentionError:
            raise
        except Exception as error:
            _mark_attention(store, operation["operation_id"], existing["workstream_id"], str(error))
            raise NeedsAttentionError("First Mate runtime identity is missing or mismatched") from error
        workspace.focus_pane(binding["workspace_surface_id"])
        return {"project": resolve_project(store, project["project_id"]), "workstream": dict(store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (existing["workstream_id"],)).fetchone()), "binding": binding, "reused": True}
    if operation["state"] == "needs_attention":
        with store.transaction():
            store.conn.execute(
                "UPDATE operations SET state='applying',step='map_committed',error_code=NULL,error_message=NULL,updated_at=? WHERE operation_id=? AND state='needs_attention'",
                (utc_now(), operation["operation_id"]),
            )
        operation = _operation(store, existing["workstream_id"])
    elif existing["provisioning_state"] == "needs_attention" and not recoverable_missing:
        raise NeedsAttentionError("First Mate ensure requires attention")
    if operation["state"] == "failed":
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='applying',error_code=NULL,error_message=?,updated_at=? WHERE operation_id=? AND state='failed'", ("retrying durable First Mate saga", utc_now(), operation["operation_id"]))
        operation = _operation(store, existing["workstream_id"])
    scope, surface = _ensure_surface_snapshot(store, operation["operation_id"], scope, harness)
    if _rank(operation["step"]) < _rank("workspace_observed_or_created"):
        observed = _recover_workspace(workspace, scope)
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='applying',step=?,updated_at=? WHERE operation_id=?", ("workspace_observed_or_created", utc_now(), operation["operation_id"]))
        _hit(failpoint, "after_first_mate_workspace_creation", scope)
        operation = _operation(store, existing["workstream_id"])
    else:
        binding = _binding(store, existing["workstream_id"])
        observed = _observe_binding(workspace, scope, binding) if binding is not None else _observe(workspace, scope)
        if observed is None:
            raise NeedsAttentionError("First Mate workspace is missing after checkpoint")
    artifacts = None
    materialized_scope = scope
    if _rank(operation["step"]) < _rank("profile_materialized"):
        artifacts, _surface, materialized_scope = materialize_current_surface(store, harness, scope, surface=surface)
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='applying',step=?,result_json=?,updated_at=? WHERE operation_id=?", ("profile_materialized", canonical_json(scope, max_bytes=256 * 1024, max_text=64 * 1024), utc_now(), operation["operation_id"]))
        _hit(failpoint, "after_first_mate_profile_materialization", scope)
        operation = _operation(store, existing["workstream_id"])
    binding = _binding(store, existing["workstream_id"])
    if artifacts is None and binding is None:
        artifacts, _surface, materialized_scope = materialize_current_surface(store, harness, scope, surface=surface)
    if artifacts is not None and binding is None:
        now = utc_now()
        with store.transaction():
            store.conn.execute(
                "INSERT INTO runtime_bindings(workstream_id,workspace_adapter_id,workspace_session_name,workspace_id,workspace_view_id,workspace_surface_id,agent_name,harness_id,harness_home,adapter_artifacts_json,native_session_kind,native_session_value,launch_secret_path,policy_path,policy_sha256,runtime_token_sha256,desired_generation_sha256,applied_generation_sha256,launch_generation_sha256,runtime_instance_id,observed_state,report_seq,workspace_report_seq,last_observed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (existing["workstream_id"], workspace.manifest.adapter_id, workspace.manifest.session_name, observed.workspace_id, observed.view_id, observed.surface_id, scope["agentName"], harness.manifest.adapter_id, artifacts.harness_home, artifact_document(harness.manifest, artifacts), None, None, artifacts.launch_secret_path, artifacts.policy_path, artifacts.policy_sha256, artifacts.runtime_token_sha256, artifacts.generation_sha256, artifacts.generation_sha256, None, None, "starting", 0, 0, None, now),
            )
            store.conn.execute("UPDATE operations SET state='applying',step=?,updated_at=? WHERE operation_id=?", ("binding_committed", now, operation["operation_id"]))
        _hit(failpoint, "after_first_mate_binding_commit", scope)
        operation = _operation(store, existing["workstream_id"])
    binding = _binding(store, existing["workstream_id"])
    if binding is None:
        raise NeedsAttentionError("First Mate runtime binding was not persisted")
    if _rank(operation["step"]) < _rank("map_committed"):
        if artifacts is None:
            scope, surface = _ensure_surface_snapshot(store, operation["operation_id"], scope, harness)
            artifacts, _surface, materialized_scope = materialize_current_surface(store, harness, scope, surface=surface)
        harness.commit_launch_binding(materialized_scope, artifacts, workspace_session_name=workspace.manifest.session_name, workspace_id=observed.workspace_id, workspace_view_id=observed.view_id, workspace_surface_id=observed.surface_id)
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='applying',step=?,updated_at=? WHERE operation_id=?", ("map_committed", utc_now(), operation["operation_id"]))
        _hit(failpoint, "after_first_mate_policy_map_materialization", scope)
        operation = _operation(store, existing["workstream_id"])
    try:
        _recover_start(store, workspace, harness, scope, binding)
    except NeedsAttentionError:
        raise
    except Exception as error:
        _mark_attention(store, operation["operation_id"], existing["workstream_id"], str(error))
        raise NeedsAttentionError("First Mate runtime identity is missing or mismatched") from error
    if _rank(operation["step"]) < _rank("agent_started"):
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='applying',step=?,updated_at=? WHERE operation_id=?", ("agent_started", utc_now(), operation["operation_id"]))
        _hit(failpoint, "after_first_mate_agent_start", scope)
        operation = _operation(store, existing["workstream_id"])
    if _rank(operation["step"]) < _rank("brief_delivered"):
        workspace.prompt_agent_nowait(binding["workspace_surface_id"], scope["brief"])
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='applying',step=?,updated_at=? WHERE operation_id=?", ("brief_delivered", utc_now(), operation["operation_id"]))
        _hit(failpoint, "after_first_mate_brief_delivery", scope)
        operation = _operation(store, existing["workstream_id"])
    if _rank(operation["step"]) < _rank("observed"):
        if _observe_binding(workspace, scope, binding) is None:
            raise NeedsAttentionError("First Mate workspace is missing")
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='applying',step=?,updated_at=? WHERE operation_id=?", ("observed", utc_now(), operation["operation_id"]))
        operation = _operation(store, existing["workstream_id"])
    _hit(failpoint, "before_first_mate_final_event_commit", scope)
    if _rank(operation["step"]) < _rank("committed"):
        expected_generation = str(binding["desired_generation_sha256"])
        now = utc_now()
        with store.transaction():
            if not usable_runtime_binding(
                store,
                str(existing["workstream_id"]),
                workspace,
                harness,
                allowed_states=frozenset({"idle", "working", "blocked"}),
            ):
                raise NeedsAttentionError("First Mate runtime binding is not usable at finalization")
            if not usable_runtime_binding(
                store,
                str(existing["workstream_id"]),
                workspace,
                harness,
                allowed_states=frozenset({"idle", "working", "blocked"}),
            ):
                raise NeedsAttentionError("First Mate runtime identity changed before finalization")
            cursor = store.conn.execute(
                "UPDATE runtime_bindings SET updated_at=updated_at WHERE workstream_id=? AND desired_generation_sha256=? AND applied_generation_sha256=? AND launch_generation_sha256 IS NULL AND refresh_pending=0 AND refresh_operation_id IS NULL AND refresh_started_at IS NULL AND observed_state IN ('idle','working','blocked')",
                (existing["workstream_id"], expected_generation, expected_generation),
            )
            if cursor.rowcount != 1:
                raise NeedsAttentionError("First Mate runtime binding changed before finalization")
            current = store.conn.execute(
                "SELECT r.*,w.provisioning_state FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=?",
                (existing["workstream_id"],),
            ).fetchone()
            if current is None or not _session_attestation_matches(store, dict(current)):
                raise NeedsAttentionError("First Mate runtime attestation changed before finalization")
            try:
                artifacts = json.loads(str(current["adapter_artifacts_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise NeedsAttentionError("First Mate runtime artifact record is invalid") from error
            if not isinstance(artifacts, dict) or artifacts.get("generationSha256") != expected_generation:
                raise NeedsAttentionError("First Mate runtime artifact generation is stale")
            cursor = store.conn.execute("UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=? AND provisioning_state='bound'", (now, existing["workstream_id"]))
            if cursor.rowcount != 1:
                raise NeedsAttentionError("First Mate workstream changed before finalization")
            cursor = store.conn.execute("UPDATE operations SET state='succeeded',step='committed',result_json=?,updated_at=? WHERE operation_id=? AND state='applying' AND step='observed'", (canonical_json(scope, max_bytes=256 * 1024, max_text=64 * 1024), now, operation["operation_id"]))
            if cursor.rowcount != 1:
                raise NeedsAttentionError("First Mate ensure operation changed before finalization")
            append_event_in_transaction(store.conn, kind="first_mate.bound", project_id=project["project_id"], workstream_id=existing["workstream_id"], operation_id=operation["operation_id"], payload={"workspaceId": binding["workspace_id"], "surfaceId": binding["workspace_surface_id"]})
    _hit(failpoint, "after_first_mate_final_event_commit", scope)
    return {"project": resolve_project(store, project["project_id"]), "workstream": dict(store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (existing["workstream_id"],)).fetchone()), "binding": _binding(store, existing["workstream_id"]), "reused": False}


def ensure_first_mate(store: Any, control_project_selector: str, harness: HarnessAdapter, workspace: WorkspaceAdapter, failpoint: Any = None) -> dict[str, Any]:
    project = resolve_project(store, control_project_selector)
    with project_permissions_lock(store.state_root, str(project["project_id"])), APPLY_LOCK:
        try:
            result = _ensure_locked(store, control_project_selector, harness, workspace, failpoint)
            from .attention import backfill_attention
            backfill_attention(store, recipient_workstream_id=str(result["workstream"]["workstream_id"]), limit=128)
            return result
        except NeedsAttentionError as error:
            try:
                existing = _first_mate(store)
                if existing is not None:
                    operation = _operation(store, existing["workstream_id"])
                    if operation is not None:
                        _mark_attention(store, operation["operation_id"], existing["workstream_id"], str(error))
            except Exception:
                pass
            raise
        except Exception as error:
            existing = _first_mate(store)
            if existing is not None:
                operation = _operation(store, existing["workstream_id"])
                if operation is not None and operation["state"] in {"planned", "applying"}:
                    with store.transaction():
                        store.conn.execute("UPDATE operations SET state='failed',error_code='first_mate_apply_failed',error_message=?,updated_at=? WHERE operation_id=?", (str(error)[:512], utc_now(), operation["operation_id"]))
            raise


def focus_first_mate(store: Any, workspace: WorkspaceAdapter) -> dict[str, Any]:
    existing = _first_mate(store)
    if existing is None:
        raise NotFoundError("First Mate is not provisioned; run first_mate.ensure first")
    binding = _binding(store, existing["workstream_id"])
    if binding is None:
        raise NeedsAttentionError("First Mate has no runtime binding")
    workspace.focus_pane(binding["workspace_surface_id"])
    return {"workstreamId": existing["workstream_id"], "focused": True}
