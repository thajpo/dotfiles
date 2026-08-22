"""Transactional migration of legacy Herdr binding surfaces into main."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .adapters import HarnessAdapter, WorkspaceAdapter, WorkspaceObservation, artifact_document
from .models import NeedsAttentionError, canonical_json, utc_now
from .runtime import start_bound_agent


def _scope(operation: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(str(operation["result_json"]))
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise NeedsAttentionError("legacy binding scope is missing or invalid") from error
    if not isinstance(value, dict):
        raise NeedsAttentionError("legacy binding scope is invalid")
    return value


def _rename_project_tab(workspace: WorkspaceAdapter, observed: WorkspaceObservation) -> WorkspaceObservation:
    workspace.rename_tab(observed.view_id, "Project chat")
    return observed


def _secretary_surface(
    workspace: WorkspaceAdapter,
    project: Mapping[str, Any],
    scope: Mapping[str, Any],
) -> WorkspaceObservation:
    observed = workspace.observe_workstream(path=str(project["repository_path"]), agent_name=str(scope["agentName"]))
    if observed is None:
        observed = workspace.create_workspace(str(project["repository_path"]), f"Project: {project['display_name']}", focus=False)
    return _rename_project_tab(workspace, observed)


def _worker_surface(
    store: Any,
    workspace: WorkspaceAdapter,
    project: Mapping[str, Any],
    workstream: Mapping[str, Any],
) -> WorkspaceObservation:
    coordinator = store.conn.execute(
        "SELECT r.* FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE w.project_id=? AND w.kind='secretary' AND w.desired_state='active'",
        (project["project_id"],),
    ).fetchone()
    if coordinator is None or not coordinator["workspace_id"]:
        raise NeedsAttentionError("project coordinator binding is not migrated")
    coordinator_observation = workspace.observe_surface(
        workspace_id=str(coordinator["workspace_id"]),
        view_id=str(coordinator["workspace_view_id"]),
        surface_id=str(coordinator["workspace_surface_id"]),
        cwd=str(project["repository_path"]),
    )
    if coordinator_observation is None:
        raise NeedsAttentionError("project coordinator workspace is missing")
    observed = workspace.observe_tab(
        workspace_id=str(coordinator["workspace_id"]),
        cwd=str(workstream["worktree_path"]),
    )
    if observed is None:
        observed = workspace.create_tab(
            workspace_id=str(coordinator["workspace_id"]),
            cwd=str(workstream["worktree_path"]),
            label=f"Task: {workstream['title']}",
            focus=False,
        )
    if observed.workspace_id != coordinator["workspace_id"]:
        raise NeedsAttentionError("migrated worker tab escaped its project workspace")
    workspace.rename_tab(observed.view_id, f"Task: {workstream['title']}")
    return observed


def _migrate_one(
    store: Any,
    harness: HarnessAdapter,
    workspace: WorkspaceAdapter,
    *,
    project: Mapping[str, Any],
    workstream: Mapping[str, Any],
    binding: Mapping[str, Any],
    operation: Mapping[str, Any],
) -> dict[str, Any]:
    scope = _scope(operation)
    expected_workstream_id = str(workstream["workstream_id"])
    if str(scope.get("workstreamId")) != expected_workstream_id or str(scope.get("projectId")) != str(project["project_id"]):
        raise NeedsAttentionError("legacy binding scope does not match durable workstream")
    if str(scope.get("agentName")) != str(binding["agent_name"]):
        raise NeedsAttentionError("legacy binding agent identity does not match durable scope")
    if workstream["kind"] == "secretary":
        observed = _secretary_surface(workspace, project, scope)
    elif workstream["kind"] == "first_mate":
        observed = workspace.observe_workstream(path=str(workstream["worktree_path"]), agent_name=str(scope["agentName"]))
        if observed is None:
            raise NeedsAttentionError("First Mate workspace surface is missing")
    else:
        observed = _worker_surface(store, workspace, project, workstream)
    artifacts = harness.materialize_profile(scope)
    harness.commit_launch_binding(
        scope,
        artifacts,
        workspace_session_name=workspace.manifest.session_name,
        workspace_id=observed.workspace_id,
        workspace_view_id=observed.view_id,
        workspace_surface_id=observed.surface_id,
        replace=True,
    )
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "UPDATE runtime_bindings SET workspace_session_name=?,workspace_id=?,workspace_view_id=?,workspace_surface_id=?,harness_home=?,adapter_artifacts_json=?,launch_secret_path=?,policy_path=?,policy_sha256=?,runtime_token_sha256=?,desired_generation_sha256=?,applied_generation_sha256=NULL,launch_generation_sha256=?,runtime_instance_id=NULL,observed_state='starting',report_seq=0,workspace_report_seq=0,last_observed_at=NULL,updated_at=? WHERE workstream_id=?",
            (workspace.manifest.session_name, observed.workspace_id, observed.view_id, observed.surface_id, artifacts.harness_home, artifact_document(harness.manifest, artifacts), artifacts.launch_secret_path, artifacts.policy_path, artifacts.policy_sha256, artifacts.runtime_token_sha256, artifacts.generation_sha256, artifacts.generation_sha256, now, expected_workstream_id),
        )
        store.conn.execute(
            "UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=?",
            (now, expected_workstream_id),
        )
    refreshed = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (expected_workstream_id,)).fetchone())
    try:
        start_bound_agent(
            store,
            workspace,
            harness,
            refreshed,
            workstream_id=expected_workstream_id,
            project_id=str(project["project_id"]),
            cwd=str(workstream["worktree_path"]),
        )
    except Exception as error:
        with store.transaction():
            store.conn.execute(
                "UPDATE runtime_bindings SET observed_state='error',updated_at=? WHERE workstream_id=?",
                (utc_now(), expected_workstream_id),
            )
            store.conn.execute(
                "UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?",
                (f"migrated agent start failed: {error}"[:512], utc_now(), expected_workstream_id),
            )
        raise NeedsAttentionError("migrated agent could not be started") from error
    return {
        "workstreamId": expected_workstream_id,
        "projectId": str(project["project_id"]),
        "workspaceId": observed.workspace_id,
        "viewId": observed.view_id,
        "surfaceId": observed.surface_id,
    }


def migrate_legacy_bindings(
    store: Any,
    harness: HarnessAdapter,
    workspace: WorkspaceAdapter,
) -> dict[str, Any]:
    """Re-home active legacy bindings while preserving native session artifacts."""
    rows = store.conn.execute(
        "SELECT w.*,p.display_name,p.repository_path,p.git_common_dir,r.*,o.kind AS operation_kind,o.result_json AS operation_result_json "
        "FROM workstreams w JOIN projects p USING(project_id) JOIN runtime_bindings r USING(workstream_id) "
        "JOIN operations o ON o.workstream_id=w.workstream_id "
        "WHERE w.desired_state='active' AND o.state='succeeded' "
        "AND o.created_at=(SELECT MAX(o2.created_at) FROM operations o2 WHERE o2.workstream_id=w.workstream_id) "
        "ORDER BY w.kind DESC,w.workstream_id"
    ).fetchall()
    migrated: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for row in rows:
        if row["workspace_session_name"] == workspace.manifest.session_name:
            continue
        project = {key: row[key] for key in ("project_id", "display_name", "repository_path", "git_common_dir")}
        workstream = {key: row[key] for key in row.keys() if key in {
            "workstream_id", "kind", "title", "worktree_path", "desired_state", "execution_profile", "harness_id", "workspace_adapter_id"
        }}
        binding = {key: row[key] for key in row.keys() if key in {
            "workstream_id", "workspace_session_name", "workspace_id", "workspace_view_id", "workspace_surface_id", "agent_name",
            "harness_home", "adapter_artifacts_json", "launch_secret_path", "policy_path", "policy_sha256", "runtime_token_sha256",
        }}
        operation = {"kind": row["operation_kind"], "result_json": row["operation_result_json"]}
        try:
            migrated.append(_migrate_one(store, harness, workspace, project=project, workstream=workstream, binding=binding, operation=operation))
        except Exception as error:
            errors.append({"workstreamId": str(row["workstream_id"]), "error": str(error)[:256]})
    return {"migrated": migrated, "errors": errors}
