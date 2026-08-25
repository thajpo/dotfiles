"""Dedicated per-project Pisec secretary lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .adapters import HarnessAdapter, WorkspaceAdapter, WorkspaceObservation, artifact_document
from .events import append_event_in_transaction
from .fence import resolve_data_dirs
from .models import ConflictError, NeedsAttentionError, NotFoundError, canonical_json, json_digest, new_id, utc_now
from .projects import resolve_project
from .project_workspaces import ensure_project_workspace
from .runtime_surface import capture_runtime_surface, materialize_current_surface
from .runtime import WORKSPACE_RUNTIME_MISSING, start_bound_agent
from .workstreams import APPLY_LOCK, _wait_for_agent


def _project_active(project: Mapping[str, Any]) -> bool:
    value = project.get("active")
    return True if value is None else bool(value)

SECRETARY_CHECKPOINTS = (
    "workspace_observed_or_created",
    "profile_materialized",
    "binding_committed",
    "map_committed",
    "agent_started",
    "brief_delivered",
    "observed",
    "committed",
)

SECRETARY_RESPONSE_CONTRACT = (
    "Default user-facing replies must fit a short screen and be action-oriented. "
    "Use only the headings Status, Needs attention, and Next action when applicable. "
    "Report material exceptions, active work, blockers, decisions needed, and next actions; "
    "suppress healthy or idle listings, raw metadata, timestamps, event history, and implementation narration. "
    "Include a projectId or workstreamId only when the user must approve, inspect, or act on that item. "
    "If nothing needs action, say so in one sentence. Provide detailed evidence only when the user explicitly asks for a drill-down."
)

_SCOPE_IDENTITY_FIELDS = frozenset(
    {
        "projectId",
        "workstreamId",
        "operationId",
        "harnessId",
        "workspaceAdapterId",
        "executionProfile",
        "targetRef",
        "baseCommitOid",
        "branchName",
        "worktreePath",
        "agentName",
    }
)


def _secretary(store: Any, project_id: str) -> dict[str, Any] | None:
    row = store.conn.execute("SELECT * FROM workstreams WHERE project_id=? AND kind='secretary' AND desired_state <> 'retired'", (project_id,)).fetchone()
    return None if row is None else dict(row)


def _binding(store: Any, workstream_id: str) -> dict[str, Any] | None:
    row = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()
    return None if row is None else dict(row)


def _operation(store: Any, workstream_id: str) -> dict[str, Any] | None:
    row = store.conn.execute("SELECT * FROM operations WHERE workstream_id=? AND kind='secretary.ensure' ORDER BY created_at DESC LIMIT 1", (workstream_id,)).fetchone()
    return None if row is None else dict(row)


def _validate_binding_identity(binding: Mapping[str, Any] | None, scope: Mapping[str, Any], harness: HarnessAdapter, workspace: WorkspaceAdapter) -> None:
    if binding is None:
        return
    if binding.get("workstream_id") != scope["workstreamId"]:
        raise NeedsAttentionError("secretary runtime binding does not match its workstream")
    if binding.get("harness_id") != harness.manifest.adapter_id or binding.get("workspace_adapter_id") != workspace.manifest.adapter_id:
        raise NeedsAttentionError("secretary runtime binding uses a different adapter")
    if binding.get("workspace_session_name") != workspace.manifest.session_name:
        raise NeedsAttentionError("secretary runtime binding uses a different workspace session")
    if binding.get("agent_name") != scope["agentName"]:
        raise NeedsAttentionError("secretary runtime binding agent identity does not match its scope")
    if not all(isinstance(binding.get(key), str) and binding[key] for key in ("workspace_id", "workspace_view_id", "workspace_surface_id")):
        raise NeedsAttentionError("secretary runtime binding has incomplete workspace identity")


def _load_or_repair_scope(
    store: Any,
    project: Mapping[str, Any],
    workstream: Mapping[str, Any],
    operation: Mapping[str, Any],
    binding: Mapping[str, Any] | None,
    harness: HarnessAdapter,
    workspace: WorkspaceAdapter,
    external_domains: tuple[str, ...],
) -> dict[str, Any]:
    if operation.get("project_id") != project["project_id"] or operation.get("workstream_id") != workstream["workstream_id"]:
        raise NeedsAttentionError("secretary ensure operation identity does not match its project")
    if workstream.get("project_id") != project["project_id"] or workstream.get("kind") != "secretary":
        raise NeedsAttentionError("secretary workstream identity does not match its project")

    canonical = _scope(project, workstream, operation["operation_id"], external_domains)
    _validate_binding_identity(binding, canonical, harness, workspace)
    raw = operation.get("result_json")
    try:
        stored = json.loads(raw) if raw is not None else None
    except (TypeError, json.JSONDecodeError):
        stored = None
    if stored is not None and not isinstance(stored, dict):
        stored = None
    if isinstance(stored, dict):
        unknown = set(stored) - set(canonical)
        if unknown:
            raise NeedsAttentionError("secretary ensure scope contains unknown fields")
        mismatched = sorted(field for field in _SCOPE_IDENTITY_FIELDS if field in stored and stored[field] != canonical[field])
        if mismatched:
            raise NeedsAttentionError(f"secretary ensure scope identity mismatch: {', '.join(mismatched)}")

    if stored == canonical:
        return canonical

    repaired_fields = sorted(set(canonical) - set(stored or {}))
    if isinstance(stored, dict):
        repaired_fields.extend(sorted(field for field in set(stored) & set(canonical) if stored[field] != canonical[field]))
    with store.transaction():
        store.conn.execute(
            "UPDATE operations SET result_json=?,updated_at=? WHERE operation_id=?",
            (canonical_json(canonical), utc_now(), operation["operation_id"]),
        )
        append_event_in_transaction(
            store.conn,
            kind="secretary.scope.repaired",
            project_id=project["project_id"],
            workstream_id=workstream["workstream_id"],
            operation_id=operation["operation_id"],
            payload={"repairedFields": sorted(set(repaired_fields)), "source": "registered project, secretary workstream, and runtime binding identity"},
        )
    return canonical


def _rank(step: str) -> int:
    return SECRETARY_CHECKPOINTS.index(step) if step in SECRETARY_CHECKPOINTS else -1


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
        "executionProfile": "secretary-project",
        "targetRef": workstream["target_ref"],
        "baseCommitOid": workstream["base_commit_oid"],
        "branchName": workstream["branch_name"],
        "worktreePath": workstream["worktree_path"],
        "agentName": f"pisec-{workstream['workstream_id'][-12:]}",
        "externalDomains": list(external_domains),
        "dataDirs": resolve_data_dirs(project.get("data_dirs"), Path(project["repository_path"])),
        "effects": ["create execution workspace", "start fenced harness agent", "read and write the registered project", "use the configured harness/plugin/MCP surface", "fast-forward existing non-default origin branches through the authenticated Pisec broker"],
        "nonEffects": ["no cross-project access", "no host-secret access", "no raw push or publish through normal command policy", "no force push, branch creation, branch deletion, or default-branch push", "no worker creation or workstream acceptance without exact approval"],
    }


def _checkpoint(store: Any, operation_id: str, step: str) -> None:
    store.conn.execute("UPDATE operations SET state='applying',step=?,error_code=NULL,error_message=NULL,updated_at=? WHERE operation_id=?", (step, utc_now(), operation_id))


def _mark_attention(store: Any, operation_id: str, workstream_id: str, reason: str) -> None:
    now = utc_now()
    with store.transaction():
        store.conn.execute("UPDATE operations SET state='needs_attention',error_code='effect_mismatch',error_message=?,updated_at=? WHERE operation_id=?", (reason[:512], now, operation_id))
        store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?", (reason[:512], now, workstream_id))


def _validate_workspace(observed: WorkspaceObservation, expected: Mapping[str, Any] | None = None) -> WorkspaceObservation:
    if not observed.workspace_id or not observed.view_id or not observed.surface_id:
        raise NeedsAttentionError("secretary workspace observation is incomplete")
    if expected is not None:
        if expected.get("workspace_id") not in {None, observed.workspace_id} or expected.get("workspace_view_id") not in {None, observed.view_id} or expected.get("workspace_surface_id") not in {None, observed.surface_id}:
            raise NeedsAttentionError("secretary workspace identity mismatch")
    return observed


def _observe(workspace: WorkspaceAdapter, project: Mapping[str, Any], scope: Mapping[str, Any], expected: Mapping[str, Any] | None = None) -> WorkspaceObservation | None:
    observed = workspace.observe_workstream(path=project["repository_path"], agent_name=scope["agentName"])
    if observed is None:
        return None
    observed = _validate_workspace(observed, expected)
    if observed.agent is not None and expected is not None and expected.get("workspace_surface_id") not in {None, observed.agent.surface_id}:
        raise NeedsAttentionError("secretary agent identity mismatch")
    return observed


def _observe_binding(workspace: WorkspaceAdapter, project: Mapping[str, Any], binding: Mapping[str, Any]) -> WorkspaceObservation | None:
    observed = workspace.observe_surface(
        workspace_id=str(binding["workspace_id"]),
        view_id=str(binding["workspace_view_id"]),
        surface_id=str(binding["workspace_surface_id"]),
        cwd=str(project["repository_path"]),
    )
    if observed is None:
        return None
    observed = _validate_workspace(observed, binding)
    if observed.agent is not None and observed.agent.surface_id != binding["workspace_surface_id"]:
        raise NeedsAttentionError("secretary agent identity mismatch")
    return observed


def _recover_workspace(
    store: Any,
    workspace: WorkspaceAdapter,
    project: Mapping[str, Any],
    scope: Mapping[str, Any],
    expected: Mapping[str, Any] | None,
) -> WorkspaceObservation:
    tab_label = f"Project: {project['display_name']}" if project.get("coordination_mode") == "fleet" else "Project chat"
    def normalize(observed: WorkspaceObservation) -> WorkspaceObservation:
        workspace.rename_tab(observed.view_id, tab_label)
        return observed

    try:
        result = ensure_project_workspace(
            store,
            project,
            workspace,
            label=f"Project: {project['display_name']}",
            create_tab=True,
        )
        observed = result.get("observation")
        if not isinstance(observed, WorkspaceObservation):
            raise NeedsAttentionError("project workspace tab is missing")
        return normalize(_validate_workspace(observed, expected))
    except Exception as error:
        try:
            observed = _observe(workspace, project, scope, expected)
        except Exception as observe_error:
            raise NeedsAttentionError("secretary workspace effect is ambiguous") from observe_error
        if observed is not None:
            return normalize(observed)
        raise RuntimeError("secretary project workspace creation failed") from error


def _recover_start(store: Any, workspace: WorkspaceAdapter, harness: HarnessAdapter, project: Mapping[str, Any], scope: Mapping[str, Any], binding: Mapping[str, Any]) -> None:
    observed = _observe_binding(workspace, project, binding)
    if observed is not None:
        workspace.rename_tab(observed.view_id, f"Project: {project['display_name']}" if project.get("coordination_mode") == "fleet" else "Project chat")
    agent = observed.agent if observed is not None else None
    ready = agent is not None and agent.surface_id == binding["workspace_surface_id"] and agent.identity_usable is True
    if not ready:
        with store.transaction():
            now = utc_now()
            store.conn.execute("UPDATE runtime_bindings SET runtime_instance_id=NULL,report_seq=0,launch_generation_sha256=IFNULL(applied_generation_sha256,launch_generation_sha256),observed_state='starting',updated_at=? WHERE workstream_id=?", (now, scope["workstreamId"]))
            store.conn.execute("UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=?", (now, scope["workstreamId"]))
        start_error: Exception | None = None
        try:
            start_bound_agent(
                store,
                workspace,
                harness,
                binding,
                workstream_id=str(scope["workstreamId"]),
                project_id=str(scope["projectId"]),
                cwd=str(project["repository_path"]),
            )
        except Exception as error:
            start_error = error
        try:
            _wait_for_agent(store, workspace, workstream_id=scope["workstreamId"], path=project["repository_path"], agent_name=scope["agentName"], workspace_id=binding["workspace_id"], view_id=binding["workspace_view_id"], surface_id=binding["workspace_surface_id"])
        except NeedsAttentionError as wait_error:
            if start_error is not None:
                raise wait_error from start_error
            raise
    else:
        _wait_for_agent(store, workspace, workstream_id=scope["workstreamId"], path=project["repository_path"], agent_name=scope["agentName"], workspace_id=binding["workspace_id"], view_id=binding["workspace_view_id"], surface_id=binding["workspace_surface_id"])


def _repair_launch_binding(
    store: Any,
    workspace: WorkspaceAdapter,
    harness: HarnessAdapter,
    project: Mapping[str, Any],
    scope: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Refresh a stopped retry binding after a deployed launcher change."""
    surface = capture_runtime_surface(harness)
    current_scope = {
        **scope,
        "runtimeSurfaceSha256": surface.content_sha256,
        "runtimeSurfaceRoot": surface.root_path,
        "runtimeSurfaceId": "surface_" + surface.content_sha256[:32],
    }
    desired = harness.desired_generation(current_scope, surface)
    if binding.get("desired_generation_sha256") == desired:
        return dict(binding)
    runtime = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
    if runtime.state == "unknown":
        raise NeedsAttentionError("secretary runtime identity is ambiguous before binding repair")
    if runtime.state == "live":
        raise NeedsAttentionError("secretary binding repair requires a stopped runtime")
    artifacts, _surface, materialized_scope = materialize_current_surface(store, harness, scope, surface=surface)
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
            "UPDATE runtime_bindings SET harness_home=?,adapter_artifacts_json=?,launch_secret_path=?,policy_path=?,policy_sha256=?,runtime_token_sha256=?,desired_generation_sha256=?,launch_generation_sha256=?,runtime_instance_id=NULL,report_seq=0,observed_state='starting',last_observed_at=NULL,updated_at=? WHERE workstream_id=?",
            (
                artifacts.harness_home,
                artifact_document(harness.manifest, artifacts),
                artifacts.launch_secret_path,
                artifacts.policy_path,
                artifacts.policy_sha256,
                artifacts.runtime_token_sha256,
                artifacts.generation_sha256,
                artifacts.generation_sha256,
                now,
                binding["workstream_id"],
            ),
        )
        append_event_in_transaction(
            store.conn,
            kind="secretary.binding.repaired",
            project_id=project["project_id"],
            workstream_id=scope["workstreamId"],
            operation_id=scope["operationId"],
            payload={"reason": "retry after deployed runtime binding change"},
        )
    repaired = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (binding["workstream_id"],)).fetchone()
    if repaired is None:
        raise NeedsAttentionError("secretary runtime binding disappeared during repair")
    return dict(repaired)

def _recover_prompt(workspace: WorkspaceAdapter, project: Mapping[str, Any], scope: Mapping[str, Any], binding: Mapping[str, Any]) -> None:
    try:
        workspace.prompt_agent_nowait(binding["workspace_surface_id"], scope["brief"])
    except Exception as error:
        try:
            _observe(workspace, project, scope, binding)
        except Exception as observe_error:
            raise RuntimeError("secretary brief delivery is ambiguous") from observe_error
        try:
            workspace.prompt_agent_nowait(binding["workspace_surface_id"], scope["brief"])
        except Exception:
            raise RuntimeError("secretary brief delivery failed after retry") from error




def _ensure_locked(store: Any, project_selector: str, harness: HarnessAdapter, workspace: WorkspaceAdapter, failpoint: Any = None) -> dict[str, Any]:
    project = resolve_project(store, project_selector)
    harness.validate_execution_profile("secretary-project", "secretary")
    external_domains = tuple(harness.profile_domains("secretary-project", ()))
    existing = _secretary(store, project["project_id"])
    if existing is None:
        workstream_id = new_id("ws")
        operation_id = new_id("op")
        now = utc_now()
        branch = f"secretary/{workstream_id}"
        with store.transaction():
            store.conn.execute(
                "INSERT INTO workstreams(workstream_id,project_id,kind,title,purpose,brief,harness_id,workspace_adapter_id,execution_profile,target_ref,base_commit_oid,branch_name,worktree_path,desired_state,provisioning_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (workstream_id, project["project_id"], "secretary", f"Project coordinator: {project['display_name']}", "Coordinate the registered project with durable Pisec workflows.", f"You are the project coordinator. You have project-scoped write access, normal local Git, broad public web access, the full configured harness/plugin/MCP surface, and Pisec coordination tools inside Fence. Publish existing non-default branches only through pisec_push_branch; raw git push remains denied. Delegate bounded implementation to approved worker workstreams; use exact approval for worker creation and workstream acceptance. After acceptance, own target refresh, bounded worker reconciliation, verification, fast-forward integration, completion, retirement, and cleanup without requesting a second merge approval. Answer worker research only through durable Pisec packets. {SECRETARY_RESPONSE_CONTRACT}", harness.manifest.adapter_id, workspace.manifest.adapter_id, "secretary-project", project["default_ref"], "0" * 40, branch, project["repository_path"], "active", "creating", now, now),
            )
            created = dict(store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone())
            scope = _scope(project, created, operation_id, external_domains)
            request = {"projectId": project["project_id"], "kind": "secretary.ensure"}
            store.conn.execute(
                "INSERT INTO operations(operation_id,kind,project_id,workstream_id,idempotency_key,request_json,request_sha256,state,step,result_json,created_at,updated_at) VALUES(?,'secretary.ensure',?,?,?,?,?,'applying','planned',?,?,?)",
                (operation_id, project["project_id"], workstream_id, f"secretary.ensure:{project['project_id']}", canonical_json(request), json_digest(request), canonical_json(scope), now, now),
            )
        _hit(failpoint, "after_secretary_proposal_commit", scope)
        existing = created
    operation = _operation(store, existing["workstream_id"])
    if operation is None:
        raise NeedsAttentionError("secretary ensure operation is missing")
    scope = _load_or_repair_scope(
        store,
        project,
        existing,
        operation,
        _binding(store, existing["workstream_id"]),
        harness,
        workspace,
        external_domains,
    )
    recoverable_missing = existing["provisioning_state"] == "needs_attention" and existing["attention_reason"] == WORKSPACE_RUNTIME_MISSING
    if operation["state"] == "succeeded" and (existing["provisioning_state"] == "bound" or recoverable_missing):
        binding = _binding(store, existing["workstream_id"])
        if binding is None:
            raise NeedsAttentionError("secretary runtime binding is missing")
        try:
            _recover_start(store, workspace, harness, project, scope, binding)
        except Exception as error:
            raise NeedsAttentionError("secretary runtime identity is missing or mismatched") from error
        if recoverable_missing or not project.get("active"):
            with store.transaction():
                now = utc_now()
                store.conn.execute("UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=?", (now, existing["workstream_id"]))
                store.conn.execute("UPDATE projects SET active=1,lifecycle_attention_reason=NULL,deactivated_at=NULL,secretary_workstream_id=?,updated_at=? WHERE project_id=?", (existing["workstream_id"], now, project["project_id"]))
        workspace.focus_pane(binding["workspace_surface_id"])
        return {"project": resolve_project(store, project["project_id"]), "workstream": dict(store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (existing["workstream_id"],)).fetchone()), "binding": _binding(store, existing["workstream_id"]), "reused": True}
    if not project.get("active") and (
        operation["state"] in {"needs_attention", "failed"}
        or existing["provisioning_state"] == "needs_attention"
    ):
        now = utc_now()
        with store.transaction():
            if operation["state"] in {"needs_attention", "failed"}:
                store.conn.execute("UPDATE operations SET state='applying',error_code=NULL,error_message=NULL,updated_at=? WHERE operation_id=?", (now, operation["operation_id"]))
            if existing["provisioning_state"] == "needs_attention":
                store.conn.execute("UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=?", (now, existing["workstream_id"]))
        operation = _operation(store, existing["workstream_id"])
        binding = _binding(store, existing["workstream_id"])
        if binding is not None and _rank(operation["step"]) >= _rank("map_committed"):
            binding = _repair_launch_binding(store, workspace, harness, project, scope, binding)
    elif operation["state"] == "needs_attention" or existing["provisioning_state"] == "needs_attention":
        raise NeedsAttentionError("secretary ensure requires attention")
    if operation["state"] == "failed":
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='applying',error_code=NULL,error_message=?,updated_at=? WHERE operation_id=? AND state='failed'", ("retrying durable secretary saga", utc_now(), operation["operation_id"]))
        operation = _operation(store, existing["workstream_id"])
    if _rank(operation["step"]) < _rank("workspace_observed_or_created"):
        observed = _recover_workspace(store, workspace, project, scope, None)
        _validate_workspace(observed)
        with store.transaction():
            _checkpoint(store, operation["operation_id"], "workspace_observed_or_created")
        _hit(failpoint, "after_secretary_workspace_creation", scope)
        operation = _operation(store, existing["workstream_id"])
    else:
        binding = _binding(store, existing["workstream_id"])
        observed = _observe_binding(workspace, project, binding) if binding is not None else _observe(workspace, project, scope, None)
        if observed is None:
            _mark_attention(store, operation["operation_id"], existing["workstream_id"], "secretary workspace is missing after checkpoint")
            raise NeedsAttentionError("secretary workspace is missing after checkpoint")
        _validate_workspace(observed)
    artifacts = None
    materialized_scope = scope
    if _rank(operation["step"]) < _rank("profile_materialized"):
        artifacts, _surface, materialized_scope = materialize_current_surface(store, harness, scope)
        with store.transaction():
            _checkpoint(store, operation["operation_id"], "profile_materialized")
        _hit(failpoint, "after_secretary_profile_materialization", scope)
        operation = _operation(store, existing["workstream_id"])
    binding = _binding(store, existing["workstream_id"])
    if artifacts is None and binding is None:
        artifacts, _surface, materialized_scope = materialize_current_surface(store, harness, scope)
    if artifacts is not None and binding is None and _rank(operation["step"]) < _rank("binding_committed"):
        now = utc_now()
        with store.transaction():
            store.conn.execute(
                "INSERT INTO runtime_bindings(workstream_id,workspace_adapter_id,workspace_session_name,workspace_id,workspace_view_id,workspace_surface_id,agent_name,harness_id,harness_home,adapter_artifacts_json,native_session_kind,native_session_value,launch_secret_path,policy_path,policy_sha256,runtime_token_sha256,desired_generation_sha256,applied_generation_sha256,launch_generation_sha256,runtime_instance_id,observed_state,report_seq,workspace_report_seq,last_observed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (existing["workstream_id"], workspace.manifest.adapter_id, workspace.manifest.session_name, observed.workspace_id, observed.view_id, observed.surface_id, scope["agentName"], harness.manifest.adapter_id, artifacts.harness_home, artifact_document(harness.manifest, artifacts), None, None, artifacts.launch_secret_path, artifacts.policy_path, artifacts.policy_sha256, artifacts.runtime_token_sha256, artifacts.generation_sha256, None, artifacts.generation_sha256, None, "starting", 0, 0, None, now),
            )
            _checkpoint(store, operation["operation_id"], "binding_committed")
        _hit(failpoint, "after_secretary_binding_commit", scope)
        operation = _operation(store, existing["workstream_id"])
    binding = _binding(store, existing["workstream_id"])
    if binding is None:
        raise NeedsAttentionError("secretary runtime binding was not persisted")
    if _rank(operation["step"]) < _rank("map_committed"):
        if artifacts is None:
            artifacts, _surface, materialized_scope = materialize_current_surface(store, harness, scope)
        harness.commit_launch_binding(
            materialized_scope,
            artifacts,
            workspace_session_name=workspace.manifest.session_name,
            workspace_id=observed.workspace_id,
            workspace_view_id=observed.view_id,
            workspace_surface_id=observed.surface_id,
        )
        with store.transaction():
            _checkpoint(store, operation["operation_id"], "map_committed")
        _hit(failpoint, "after_secretary_policy_map_materialization", scope)
        operation = _operation(store, existing["workstream_id"])
    _recover_start(store, workspace, harness, project, scope, binding)
    if _rank(operation["step"]) < _rank("agent_started"):
        with store.transaction():
            _checkpoint(store, operation["operation_id"], "agent_started")
        _hit(failpoint, "after_secretary_agent_start", scope)
        operation = _operation(store, existing["workstream_id"])
    if _rank(operation["step"]) < _rank("brief_delivered"):
        _recover_prompt(workspace, project, scope, binding)
        with store.transaction():
            _checkpoint(store, operation["operation_id"], "brief_delivered")
        _hit(failpoint, "after_secretary_brief_delivery", scope)
        operation = _operation(store, existing["workstream_id"])
    if _rank(operation["step"]) < _rank("observed"):
        exact = _observe_binding(workspace, project, binding)
        if exact is None:
            raise NeedsAttentionError("secretary workspace is missing")
        with store.transaction():
            _checkpoint(store, operation["operation_id"], "observed")
        operation = _operation(store, existing["workstream_id"])
    _hit(failpoint, "before_secretary_final_event_commit", scope)
    if _rank(operation["step"]) < _rank("committed"):
        now = utc_now()
        result = {"workstreamId": existing["workstream_id"], "projectId": project["project_id"], "workspaceId": binding["workspace_id"], "viewId": binding["workspace_view_id"], "surfaceId": binding["workspace_surface_id"], "agentName": binding["agent_name"]}
        with store.transaction():
            store.conn.execute("UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=?", (now, existing["workstream_id"]))
            store.conn.execute("UPDATE projects SET secretary_workstream_id=?,active=1,lifecycle_attention_reason=NULL,deactivated_at=NULL,updated_at=? WHERE project_id=?", (existing["workstream_id"], now, project["project_id"]))
            store.conn.execute("UPDATE operations SET state='succeeded',step='committed',result_json=?,updated_at=? WHERE operation_id=?", (canonical_json(scope), now, operation["operation_id"]))
            append_event_in_transaction(store.conn, kind="secretary.bound", project_id=project["project_id"], workstream_id=existing["workstream_id"], operation_id=operation["operation_id"], payload={"workspaceId": binding["workspace_id"], "surfaceId": binding["workspace_surface_id"]})
    _hit(failpoint, "after_secretary_final_event_commit", scope)
    return {"project": resolve_project(store, project["project_id"]), "workstream": dict(store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (existing["workstream_id"],)).fetchone()), "binding": dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (existing["workstream_id"],)).fetchone()), "reused": False}


def ensure_secretary(store: Any, project_selector: str, harness: HarnessAdapter, workspace: WorkspaceAdapter, failpoint: Any = None) -> dict[str, Any]:
    with APPLY_LOCK:
        try:
            result = _ensure_locked(store, project_selector, harness, workspace, failpoint)
            from .attention import backfill_attention
            backfill_attention(store, recipient_workstream_id=str(result["workstream"]["workstream_id"]), limit=128)
            return result
        except NeedsAttentionError:
            raise
        except Exception as error:
            project = resolve_project(store, project_selector)
            existing = _secretary(store, project["project_id"])
            if existing is not None:
                operation = _operation(store, existing["workstream_id"])
                if operation is not None and operation["state"] in {"planned", "applying"}:
                    with store.transaction():
                        now = utc_now()
                        store.conn.execute("UPDATE operations SET state='needs_attention',error_code='secretary_apply_failed',error_message=?,updated_at=? WHERE operation_id=?", (str(error)[:512], now, operation["operation_id"]))
                        store.conn.execute("UPDATE projects SET active=0,lifecycle_attention_reason=?,updated_at=? WHERE project_id=?", (f"project open requires repair: {error}"[:2048], now, project["project_id"]))
            else:
                with store.transaction():
                    store.conn.execute("UPDATE projects SET active=0,lifecycle_attention_reason=?,updated_at=? WHERE project_id=?", (f"project open requires repair: {error}"[:2048], utc_now(), project["project_id"]))
            raise


def focus_secretary(store: Any, project_selector: str, workspace: WorkspaceAdapter) -> dict[str, Any]:
    project = resolve_project(store, project_selector)
    secretary = _secretary(store, project["project_id"])
    if secretary is None:
        raise NotFoundError("project has no secretary; run secretary ensure first")
    binding = _binding(store, secretary["workstream_id"])
    if binding is None:
        raise NeedsAttentionError("secretary has no runtime binding")
    workspace.focus_pane(binding["workspace_surface_id"])
    return {"projectId": project["project_id"], "workstreamId": secretary["workstream_id"], "focused": True}
