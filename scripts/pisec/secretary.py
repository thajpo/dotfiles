"""Dedicated per-project Pisec secretary lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .adapters import HarnessAdapter, WorkspaceAdapter, WorkspaceObservation, artifact_document
from .events import append_event_in_transaction
from .models import NeedsAttentionError, NotFoundError, canonical_json, json_digest, new_id, utc_now
from .projects import resolve_project
from .runtime import WORKSPACE_RUNTIME_MISSING
from .workstreams import APPLY_LOCK, _wait_for_agent

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


def _secretary(store: Any, project_id: str) -> dict[str, Any] | None:
    row = store.conn.execute("SELECT * FROM workstreams WHERE project_id=? AND kind='secretary' AND desired_state <> 'retired'", (project_id,)).fetchone()
    return None if row is None else dict(row)


def _binding(store: Any, workstream_id: str) -> dict[str, Any] | None:
    row = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()
    return None if row is None else dict(row)


def _operation(store: Any, workstream_id: str) -> dict[str, Any] | None:
    row = store.conn.execute("SELECT * FROM operations WHERE workstream_id=? AND kind='secretary.ensure' ORDER BY created_at DESC LIMIT 1", (workstream_id,)).fetchone()
    return None if row is None else dict(row)


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
        "privateGitObjectDir": None,
        "gitCommonObjectDir": str((Path(project["git_common_dir"]) / "objects").absolute()),
        "agentName": f"pisec-{workstream['workstream_id'][-12:]}",
        "externalDomains": list(external_domains),
        "effects": ["create execution workspace", "start fenced harness agent", "read and write the registered project", "use the configured harness/plugin/MCP surface"],
        "nonEffects": ["no cross-project access", "no host-secret access", "no push or publish through normal command policy", "no worker creation without exact approval"],
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
    if observed.agent is not None and (observed.agent.name != scope["agentName"] or (expected is not None and expected.get("workspace_surface_id") not in {None, observed.agent.surface_id})):
        raise NeedsAttentionError("secretary agent identity mismatch")
    return observed


def _recover_workspace(workspace: WorkspaceAdapter, project: Mapping[str, Any], scope: Mapping[str, Any], expected: Mapping[str, Any] | None) -> WorkspaceObservation:
    observed = _observe(workspace, project, scope, expected)
    if observed is not None:
        return observed
    try:
        return workspace.create_workspace(project["repository_path"], scope["title"], focus=False)
    except Exception as error:
        try:
            observed = _observe(workspace, project, scope, expected)
        except Exception as observe_error:
            raise NeedsAttentionError("secretary workspace effect is ambiguous") from observe_error
        if observed is not None:
            return observed
        try:
            return workspace.create_workspace(project["repository_path"], scope["title"], focus=False)
        except Exception:
            raise RuntimeError("secretary workspace creation failed after retry") from error


def _recover_start(store: Any, workspace: WorkspaceAdapter, harness: HarnessAdapter, project: Mapping[str, Any], scope: Mapping[str, Any], binding: Mapping[str, Any]) -> None:
    observed = _observe(workspace, project, scope, binding)
    agent = observed.agent if observed is not None else None
    ready = agent is not None and agent.name == scope["agentName"] and agent.surface_id == binding["workspace_surface_id"] and agent.interactive_ready is True
    if not ready:
        with store.transaction():
            store.conn.execute("UPDATE runtime_bindings SET runtime_instance_id=NULL,report_seq=0,observed_state='starting',updated_at=? WHERE workstream_id=?", (utc_now(), scope["workstreamId"]))
        start_error: Exception | None = None
        try:
            workspace.start_agent(binding["workspace_surface_id"], scope["agentName"], harness.manifest.agent_kind)
        except Exception as error:
            start_error = error
        try:
            _wait_for_agent(store, workspace, workstream_id=scope["workstreamId"], path=project["repository_path"], agent_name=scope["agentName"], workspace_id=binding["workspace_id"], surface_id=binding["workspace_surface_id"])
        except NeedsAttentionError as wait_error:
            if start_error is not None:
                raise wait_error from start_error
            raise
    else:
        _wait_for_agent(store, workspace, workstream_id=scope["workstreamId"], path=project["repository_path"], agent_name=scope["agentName"], workspace_id=binding["workspace_id"], surface_id=binding["workspace_surface_id"])


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
                (workstream_id, project["project_id"], "secretary", f"Pisec secretary: {project['display_name']}", "Coordinate the registered project with durable Pisec workflows.", "You are the project secretary. You have project-scoped write access, normal local Git, broad public web access, the full configured harness/plugin/MCP surface, and Pisec coordination tools inside Fence. Use exact approval for worker creation and merge application; answer worker research only through durable Pisec packets.", harness.manifest.adapter_id, workspace.manifest.adapter_id, "secretary-project", project["default_ref"], "0" * 40, branch, project["repository_path"], "active", "creating", now, now),
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
    try:
        scope = json.loads(operation["result_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise NeedsAttentionError("secretary ensure scope is missing or invalid") from error
    if not isinstance(scope, dict) or set(scope) != set(_scope(project, existing, operation["operation_id"], external_domains)):
        raise NeedsAttentionError("secretary ensure scope is missing or invalid")
    if scope["harnessId"] != harness.manifest.adapter_id or scope["workspaceAdapterId"] != workspace.manifest.adapter_id:
        raise NeedsAttentionError("configured adapter does not match the approved secretary scope")
    recoverable_missing = existing["provisioning_state"] == "needs_attention" and existing["attention_reason"] == WORKSPACE_RUNTIME_MISSING
    if operation["state"] == "succeeded" and (existing["provisioning_state"] == "bound" or recoverable_missing):
        binding = _binding(store, existing["workstream_id"])
        if binding is None:
            raise NeedsAttentionError("secretary runtime binding is missing")
        try:
            _recover_start(store, workspace, harness, project, scope, binding)
        except Exception as error:
            _mark_attention(store, operation["operation_id"], existing["workstream_id"], "secretary runtime identity is missing or mismatched")
            raise NeedsAttentionError("secretary runtime identity is missing or mismatched") from error
        if recoverable_missing:
            with store.transaction():
                store.conn.execute("UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=?", (utc_now(), existing["workstream_id"]))
        workspace.focus_agent(binding["workspace_surface_id"])
        return {"project": resolve_project(store, project["project_id"]), "workstream": dict(store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (existing["workstream_id"],)).fetchone()), "binding": _binding(store, existing["workstream_id"]), "reused": True}
    if operation["state"] == "needs_attention" or existing["provisioning_state"] == "needs_attention":
        raise NeedsAttentionError("secretary ensure requires attention")
    if operation["state"] == "failed":
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='applying',error_code=NULL,error_message=?,updated_at=? WHERE operation_id=? AND state='failed'", ("retrying durable secretary saga", utc_now(), operation["operation_id"]))
        operation = _operation(store, existing["workstream_id"])
    if _rank(operation["step"]) < _rank("workspace_observed_or_created"):
        observed = _recover_workspace(workspace, project, scope, None)
        _validate_workspace(observed)
        with store.transaction():
            _checkpoint(store, operation["operation_id"], "workspace_observed_or_created")
        _hit(failpoint, "after_secretary_workspace_creation", scope)
        operation = _operation(store, existing["workstream_id"])
    else:
        observed = _observe(workspace, project, scope, None)
        if observed is None:
            _mark_attention(store, operation["operation_id"], existing["workstream_id"], "secretary workspace is missing after checkpoint")
            raise NeedsAttentionError("secretary workspace is missing after checkpoint")
        _validate_workspace(observed)
    artifacts = None
    if _rank(operation["step"]) < _rank("profile_materialized"):
        artifacts = harness.materialize_profile(scope)
        with store.transaction():
            _checkpoint(store, operation["operation_id"], "profile_materialized")
        _hit(failpoint, "after_secretary_profile_materialization", scope)
        operation = _operation(store, existing["workstream_id"])
    binding = _binding(store, existing["workstream_id"])
    if artifacts is None and binding is None:
        artifacts = harness.materialize_profile(scope)
    if artifacts is not None and binding is None and _rank(operation["step"]) < _rank("binding_committed"):
        now = utc_now()
        with store.transaction():
            store.conn.execute(
                "INSERT INTO runtime_bindings(workstream_id,workspace_adapter_id,workspace_session_name,workspace_id,workspace_view_id,workspace_surface_id,agent_name,harness_id,harness_home,adapter_artifacts_json,native_session_kind,native_session_value,launch_secret_path,private_git_object_dir,policy_path,policy_sha256,runtime_token_sha256,runtime_instance_id,observed_state,report_seq,workspace_report_seq,last_observed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (existing["workstream_id"], workspace.manifest.adapter_id, workspace.manifest.session_name, observed.workspace_id, observed.view_id, observed.surface_id, scope["agentName"], harness.manifest.adapter_id, artifacts.harness_home, artifact_document(harness.manifest, artifacts), None, None, artifacts.launch_secret_path, None, artifacts.policy_path, artifacts.policy_sha256, artifacts.runtime_token_sha256, None, "starting", 0, 0, None, now),
            )
            _checkpoint(store, operation["operation_id"], "binding_committed")
        _hit(failpoint, "after_secretary_binding_commit", scope)
        operation = _operation(store, existing["workstream_id"])
    binding = _binding(store, existing["workstream_id"])
    if binding is None:
        raise NeedsAttentionError("secretary runtime binding was not persisted")
    if _rank(operation["step"]) < _rank("map_committed"):
        if artifacts is None:
            artifacts = harness.materialize_profile(scope)
        harness.commit_launch_binding(scope, artifacts)
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
        exact = _observe(workspace, project, scope, binding)
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
            store.conn.execute("UPDATE projects SET secretary_workstream_id=?,updated_at=? WHERE project_id=?", (existing["workstream_id"], now, project["project_id"]))
            store.conn.execute("UPDATE operations SET state='succeeded',step='committed',result_json=?,updated_at=? WHERE operation_id=?", (canonical_json(scope), now, operation["operation_id"]))
            append_event_in_transaction(store.conn, kind="secretary.bound", project_id=project["project_id"], workstream_id=existing["workstream_id"], operation_id=operation["operation_id"], payload={"workspaceId": binding["workspace_id"], "surfaceId": binding["workspace_surface_id"]})
    _hit(failpoint, "after_secretary_final_event_commit", scope)
    return {"project": resolve_project(store, project["project_id"]), "workstream": dict(store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (existing["workstream_id"],)).fetchone()), "binding": dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (existing["workstream_id"],)).fetchone()), "reused": False}


def ensure_secretary(store: Any, project_selector: str, harness: HarnessAdapter, workspace: WorkspaceAdapter, failpoint: Any = None) -> dict[str, Any]:
    with APPLY_LOCK:
        try:
            return _ensure_locked(store, project_selector, harness, workspace, failpoint)
        except NeedsAttentionError:
            raise
        except Exception as error:
            project = resolve_project(store, project_selector)
            existing = _secretary(store, project["project_id"])
            if existing is not None:
                operation = _operation(store, existing["workstream_id"])
                if operation is not None and operation["state"] in {"planned", "applying"}:
                    with store.transaction():
                        store.conn.execute("UPDATE operations SET state='failed',error_code='secretary_apply_failed',error_message=?,updated_at=? WHERE operation_id=?", (str(error)[:512], utc_now(), operation["operation_id"]))
            raise


def focus_secretary(store: Any, project_selector: str, workspace: WorkspaceAdapter) -> dict[str, Any]:
    project = resolve_project(store, project_selector)
    secretary = _secretary(store, project["project_id"])
    if secretary is None:
        raise NotFoundError("project has no secretary; run secretary ensure first")
    binding = _binding(store, secretary["workstream_id"])
    if binding is None:
        raise NeedsAttentionError("secretary has no runtime binding")
    workspace.focus_agent(binding["workspace_surface_id"])
    return {"projectId": project["project_id"], "workstreamId": secretary["workstream_id"], "focused": True}
