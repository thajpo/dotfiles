"""Transactional migration of legacy Herdr binding surfaces into main."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any, Mapping

from .adapters import HarnessAdapter, WorkspaceAdapter, WorkspaceObservation, artifact_document
from .models import NeedsAttentionError, canonical_json, utc_now
from .runtime import start_bound_agent
from .releases import materialize_active_release


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


def _first_mate_binding(store: Any, workspace: WorkspaceAdapter) -> dict[str, Any]:
    rows = store.conn.execute(
        "SELECT r.* FROM runtime_bindings r JOIN workstreams w USING(workstream_id) "
        "WHERE w.kind='first_mate' AND w.desired_state='active' AND w.provisioning_state='bound' "
        "AND r.workspace_adapter_id=? AND r.workspace_session_name=?",
        (workspace.manifest.adapter_id, workspace.manifest.session_name),
    ).fetchall()
    if len(rows) != 1 or not rows[0]["workspace_id"]:
        raise NeedsAttentionError("fleet topology requires one bound First Mate workspace")
    return dict(rows[0])


def _fleet_tab_label(project: Mapping[str, Any], workstream: Mapping[str, Any]) -> str:
    if workstream["kind"] == "secretary":
        return f"Project: {project['display_name']}"
    return f"{project['display_name']}: {workstream['title']}"


def _wait_for_exit(workspace: WorkspaceAdapter, binding: Mapping[str, Any], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        state = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"])).state
        if state == "stopped":
            return
        if state == "unknown":
            raise NeedsAttentionError("runtime process identity became ambiguous during topology migration")
        if time.monotonic() >= deadline:
            raise NeedsAttentionError("runtime did not stop for topology migration")
        time.sleep(0.05)


def _wait_for_start(store: Any, workspace: WorkspaceAdapter, workstream_id: str, release_id: str, generation: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        row = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()
        if row is not None and row["runtime_instance_id"] and int(row["report_seq"]) >= 1 and row["applied_release_id"] == release_id and row["applied_generation_sha256"] == generation:
            if workspace.observe_runtime(str(row["workspace_surface_id"]), str(row["policy_path"])).state == "live":
                return dict(row)
        if time.monotonic() >= deadline:
            raise NeedsAttentionError("migrated runtime did not attest its launch identity")
        time.sleep(0.05)


def _secretary_surface(
    store: Any,
    workspace: WorkspaceAdapter,
    project: Mapping[str, Any],
    scope: Mapping[str, Any],
) -> WorkspaceObservation:
    if project.get("coordination_mode") == "fleet":
        target_workspace_id = str(_first_mate_binding(store, workspace)["workspace_id"])
        observed = workspace.observe_tab(workspace_id=target_workspace_id, cwd=str(project["repository_path"]))
        if observed is None:
            observed = workspace.create_tab(workspace_id=target_workspace_id, cwd=str(project["repository_path"]), label=f"Project: {project['display_name']}", focus=False)
        workspace.rename_tab(observed.view_id, f"Project: {project['display_name']}")
        return observed
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
        observed = _secretary_surface(store, workspace, project, scope)
    elif workstream["kind"] == "first_mate":
        observed = workspace.observe_workstream(path=str(workstream["worktree_path"]), agent_name=str(scope["agentName"]))
        if observed is None:
            raise NeedsAttentionError("First Mate workspace surface is missing")
    else:
        observed = _worker_surface(store, workspace, project, workstream)
    artifacts, release, materialized_scope = materialize_active_release(store, harness, scope)
    harness.commit_launch_binding(
        materialized_scope,
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
            "UPDATE runtime_bindings SET workspace_session_name=?,workspace_id=?,workspace_view_id=?,workspace_surface_id=?,harness_home=?,adapter_artifacts_json=?,launch_secret_path=?,policy_path=?,policy_sha256=?,runtime_token_sha256=?,desired_release_id=?,applied_release_id=NULL,launch_release_id=?,desired_generation_sha256=?,applied_generation_sha256=NULL,launch_generation_sha256=?,runtime_instance_id=NULL,observed_state='starting',report_seq=0,workspace_report_seq=0,last_observed_at=NULL,updated_at=? WHERE workstream_id=?",
            (workspace.manifest.session_name, observed.workspace_id, observed.view_id, observed.surface_id, artifacts.harness_home, artifact_document(harness.manifest, artifacts), artifacts.launch_secret_path, artifacts.policy_path, artifacts.policy_sha256, artifacts.runtime_token_sha256, release["release_id"], release["release_id"], artifacts.generation_sha256, artifacts.generation_sha256, now, expected_workstream_id),
        )
        store.conn.execute(
            "UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=?",
            (now, expected_workstream_id),
        )
        if project.get("coordination_mode") == "fleet":
            store.conn.execute(
                "INSERT INTO project_workspaces(project_id,workspace_adapter_id,workspace_session_name,workspace_id,repository_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(project_id) DO UPDATE SET workspace_adapter_id=excluded.workspace_adapter_id,workspace_session_name=excluded.workspace_session_name,workspace_id=excluded.workspace_id,repository_path=excluded.repository_path,updated_at=excluded.updated_at",
                (project["project_id"], workspace.manifest.adapter_id, workspace.manifest.session_name, observed.workspace_id, project["repository_path"], now, now),
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


def _rehome_fleet_binding(
    store: Any,
    harness: HarnessAdapter,
    workspace: WorkspaceAdapter,
    *,
    project: Mapping[str, Any],
    workstream: Mapping[str, Any],
    binding: Mapping[str, Any],
    operation: Mapping[str, Any],
    target_workspace_id: str,
) -> dict[str, Any]:
    workstream_id = str(workstream["workstream_id"])
    current = store.conn.execute(
        "SELECT r.*,w.desired_state,w.provisioning_state,p.active,p.coordination_mode FROM runtime_bindings r "
        "JOIN workstreams w USING(workstream_id) JOIN projects p USING(project_id) WHERE r.workstream_id=?",
        (workstream_id,),
    ).fetchone()
    if current is None or current["desired_state"] != "active" or not current["active"] or current["coordination_mode"] != "fleet":
        return {"workstreamId": workstream_id, "state": "inactive", "deferred": True}
    binding = dict(current)
    reserved = bool(binding["refresh_pending"])
    if not reserved and binding["observed_state"] not in {"idle", "done", "stopped"}:
        return {"workstreamId": workstream_id, "state": str(binding["observed_state"]), "deferred": True}
    if not reserved:
        expected_instance = binding["runtime_instance_id"]
        expected_seq = int(binding["report_seq"])
        expected_state = str(binding["observed_state"])
        with store.transaction():
            changed = store.conn.execute(
                "UPDATE runtime_bindings SET refresh_pending=1,updated_at=? WHERE workstream_id=? AND refresh_pending=0 AND observed_state=? AND report_seq=? AND runtime_instance_id IS ?",
                (utc_now(), workstream_id, expected_state, expected_seq, expected_instance),
            ).rowcount
        if changed != 1:
            latest = store.conn.execute("SELECT observed_state FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()
            return {"workstreamId": workstream_id, "state": "missing" if latest is None else str(latest["observed_state"]), "deferred": True}
        latest = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()
        if latest is None or latest["observed_state"] != expected_state or int(latest["report_seq"]) != expected_seq or latest["runtime_instance_id"] != expected_instance:
            with store.transaction():
                store.conn.execute("UPDATE runtime_bindings SET refresh_pending=0,updated_at=? WHERE workstream_id=?", (utc_now(), workstream_id))
            return {"workstreamId": workstream_id, "state": "changed", "deferred": True}
        binding = dict(latest)
    scope = _scope(operation)
    if str(scope.get("workstreamId")) != workstream_id or str(scope.get("projectId")) != str(project["project_id"]):
        raise NeedsAttentionError("fleet topology scope does not match durable workstream")
    label = _fleet_tab_label(project, workstream)
    observed = workspace.observe_tab(workspace_id=target_workspace_id, cwd=str(workstream["worktree_path"]))
    if observed is not None and reserved:
        identity_matches = (
            binding["workspace_id"] == observed.workspace_id
            and binding["workspace_view_id"] == observed.view_id
            and binding["workspace_surface_id"] == observed.surface_id
        )
        runtime = workspace.observe_runtime(str(observed.surface_id), str(binding["policy_path"]))
        attested = (
            identity_matches
            and binding["runtime_instance_id"]
            and int(binding["report_seq"]) >= 1
            and binding["applied_release_id"] == binding["desired_release_id"]
            and binding["applied_generation_sha256"] == binding["desired_generation_sha256"]
        )
        if runtime.state == "live" and attested:
            with store.transaction():
                store.conn.execute("UPDATE runtime_bindings SET refresh_pending=0,updated_at=? WHERE workstream_id=?", (utc_now(), workstream_id))
            return {
                "workstreamId": workstream_id,
                "projectId": str(project["project_id"]),
                "workspaceId": binding["workspace_id"],
                "viewId": binding["workspace_view_id"],
                "surfaceId": binding["workspace_surface_id"],
                "recovered": True,
            }
        if runtime.state == "unknown":
            raise NeedsAttentionError("reserved fleet topology runtime is ambiguous")
        if runtime.state == "live" and not identity_matches:
            raise NeedsAttentionError("reserved fleet topology pane identity is ambiguous")
        if runtime.state == "live" and identity_matches:
            try:
                resumed = _wait_for_start(store, workspace, workstream_id, str(binding["desired_release_id"]), str(binding["desired_generation_sha256"]))
            except NeedsAttentionError:
                pass
            else:
                with store.transaction():
                    store.conn.execute("UPDATE runtime_bindings SET refresh_pending=0,updated_at=? WHERE workstream_id=?", (utc_now(), workstream_id))
                return {
                    "workstreamId": workstream_id,
                    "projectId": str(project["project_id"]),
                    "workspaceId": resumed["workspace_id"],
                    "viewId": resumed["workspace_view_id"],
                    "surfaceId": resumed["workspace_surface_id"],
                    "recovered": True,
                }
        if runtime.state == "live":
            workspace.stop_runtime(str(observed.surface_id))
            moved_binding = {**binding, "workspace_surface_id": observed.surface_id}
            _wait_for_exit(workspace, moved_binding)
    if observed is None:
        old_surface = workspace.observe_surface(
            workspace_id=str(binding["workspace_id"]),
            view_id=str(binding["workspace_view_id"]),
            surface_id=str(binding["workspace_surface_id"]),
            cwd=str(workstream["worktree_path"]),
        )
        if old_surface is None:
            raise NeedsAttentionError("fleet topology source pane is missing")
        latest = store.conn.execute("SELECT runtime_instance_id,report_seq,observed_state FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()
        if not reserved and latest is not None and (latest["runtime_instance_id"] != binding["runtime_instance_id"] or int(latest["report_seq"]) != int(binding["report_seq"]) or latest["observed_state"] != binding["observed_state"]):
            with store.transaction():
                store.conn.execute("UPDATE runtime_bindings SET refresh_pending=0,updated_at=? WHERE workstream_id=?", (utc_now(), workstream_id))
            return {"workstreamId": workstream_id, "state": str(latest["observed_state"]), "deferred": True}
        runtime = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
        if runtime.state == "unknown":
            raise NeedsAttentionError("fleet topology source runtime is ambiguous")
        if runtime.state == "live":
            workspace.stop_runtime(str(binding["workspace_surface_id"]))
            _wait_for_exit(workspace, binding)
        observed = workspace.move_surface_to_tab(
            surface_id=str(binding["workspace_surface_id"]),
            workspace_id=target_workspace_id,
            label=label,
            focus=False,
        )
    if observed.workspace_id != target_workspace_id:
        raise NeedsAttentionError("fleet topology move escaped First Mate workspace")
    if observed.worktree_path is not None and str(Path(observed.worktree_path).resolve(strict=False)) != str(Path(str(workstream["worktree_path"])).resolve(strict=False)):
        raise NeedsAttentionError("fleet topology moved pane has the wrong cwd")
    workspace.rename_tab(observed.view_id, label)
    artifacts, release, materialized_scope = materialize_active_release(store, harness, scope)
    harness.commit_launch_binding(
        materialized_scope,
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
            "UPDATE runtime_bindings SET workspace_session_name=?,workspace_id=?,workspace_view_id=?,workspace_surface_id=?,harness_home=?,adapter_artifacts_json=?,launch_secret_path=?,policy_path=?,policy_sha256=?,runtime_token_sha256=?,desired_release_id=?,launch_release_id=?,desired_generation_sha256=?,launch_generation_sha256=?,runtime_instance_id=NULL,observed_state='starting',report_seq=0,refresh_pending=1,last_observed_at=?,updated_at=? WHERE workstream_id=?",
            (workspace.manifest.session_name, observed.workspace_id, observed.view_id, observed.surface_id, artifacts.harness_home, artifact_document(harness.manifest, artifacts), artifacts.launch_secret_path, artifacts.policy_path, artifacts.policy_sha256, artifacts.runtime_token_sha256, release["release_id"], release["release_id"], artifacts.generation_sha256, artifacts.generation_sha256, now, now, workstream_id),
        )
        store.conn.execute(
            "INSERT INTO project_workspaces(project_id,workspace_adapter_id,workspace_session_name,workspace_id,repository_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET workspace_adapter_id=excluded.workspace_adapter_id,workspace_session_name=excluded.workspace_session_name,workspace_id=excluded.workspace_id,repository_path=excluded.repository_path,updated_at=excluded.updated_at",
            (project["project_id"], workspace.manifest.adapter_id, workspace.manifest.session_name, target_workspace_id, project["repository_path"], now, now),
        )
        store.conn.execute("UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=?", (now, workstream_id))
    refreshed = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone())
    start_bound_agent(store, workspace, harness, refreshed, workstream_id=workstream_id, project_id=str(project["project_id"]), cwd=str(workstream["worktree_path"]))
    attested = _wait_for_start(store, workspace, workstream_id, str(release["release_id"]), artifacts.generation_sha256)
    with store.transaction():
        store.conn.execute("UPDATE runtime_bindings SET refresh_pending=0,updated_at=? WHERE workstream_id=?", (utc_now(), workstream_id))
    return {
        "workstreamId": workstream_id,
        "projectId": str(project["project_id"]),
        "workspaceId": attested["workspace_id"],
        "viewId": attested["workspace_view_id"],
        "surfaceId": attested["workspace_surface_id"],
    }


def migrate_legacy_bindings(
    store: Any,
    harness: HarnessAdapter,
    workspace: WorkspaceAdapter,
) -> dict[str, Any]:
    """Re-home active legacy bindings while preserving native session artifacts."""
    rows = store.conn.execute(
        "SELECT w.*,p.display_name,p.repository_path,p.git_common_dir,p.coordination_mode,r.*,o.kind AS operation_kind,o.result_json AS operation_result_json "
        "FROM workstreams w JOIN projects p USING(project_id) JOIN runtime_bindings r USING(workstream_id) "
        "JOIN operations o ON o.workstream_id=w.workstream_id AND o.kind=CASE w.kind WHEN 'first_mate' THEN 'first_mate.ensure' WHEN 'secretary' THEN 'secretary.ensure' ELSE 'workstream.create' END "
        "WHERE w.desired_state='active' AND o.state='succeeded' "
        "AND o.created_at=(SELECT MAX(o2.created_at) FROM operations o2 WHERE o2.workstream_id=w.workstream_id AND o2.kind=o.kind) "
        "ORDER BY CASE w.kind WHEN 'first_mate' THEN 0 WHEN 'secretary' THEN 1 ELSE 2 END,p.display_name,w.created_at,w.workstream_id"
    ).fetchall()
    migrated: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    target_workspace_id: str | None = None
    for row in rows:
        fleet_move = row["coordination_mode"] == "fleet" and row["kind"] != "first_mate"
        if fleet_move and target_workspace_id is None:
            try:
                target_workspace_id = str(_first_mate_binding(store, workspace)["workspace_id"])
            except Exception as error:
                errors.append({"workstreamId": str(row["workstream_id"]), "error": str(error)[:256]})
                continue
        if row["workspace_session_name"] == workspace.manifest.session_name and (not fleet_move or (row["workspace_id"] == target_workspace_id and not row["refresh_pending"])):
            continue
        project = {key: row[key] for key in ("project_id", "display_name", "repository_path", "git_common_dir", "coordination_mode")}
        workstream = {key: row[key] for key in row.keys() if key in {
            "workstream_id", "kind", "title", "worktree_path", "desired_state", "execution_profile", "harness_id", "workspace_adapter_id"
        }}
        binding = {key: row[key] for key in row.keys() if key in {
            "workstream_id", "workspace_session_name", "workspace_id", "workspace_view_id", "workspace_surface_id", "agent_name",
            "harness_home", "adapter_artifacts_json", "launch_secret_path", "policy_path", "policy_sha256", "runtime_token_sha256",
        }}
        operation = {"kind": row["operation_kind"], "result_json": row["operation_result_json"]}
        try:
            if fleet_move and row["workspace_session_name"] == workspace.manifest.session_name:
                result = _rehome_fleet_binding(store, harness, workspace, project=project, workstream=workstream, binding=dict(row), operation=operation, target_workspace_id=str(target_workspace_id))
                (deferred if result.get("deferred") else migrated).append(result)
            else:
                migrated.append(_migrate_one(store, harness, workspace, project=project, workstream=workstream, binding=binding, operation=operation))
        except Exception as error:
            errors.append({"workstreamId": str(row["workstream_id"]), "error": str(error)[:256]})
    return {"migrated": migrated, "deferred": deferred, "errors": errors}
