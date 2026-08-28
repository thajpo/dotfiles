"""Durable project-owned workspace identity and tab recovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .adapters import WorkspaceAdapter, WorkspaceObservation
from .models import ConflictError, NeedsAttentionError, PisecError, utc_now
from .control_plane import control_plane_mutation


def _validate_observation(observed: WorkspaceObservation, workspace_id: str | None = None) -> WorkspaceObservation:
    if not observed.workspace_id or not observed.view_id or not observed.surface_id:
        raise NeedsAttentionError("project workspace observation is incomplete")
    if workspace_id is not None and observed.workspace_id != workspace_id:
        raise NeedsAttentionError("workspace response escaped the project workspace")
    return observed


def _record(store: Any, project: Mapping[str, Any], workspace: WorkspaceAdapter) -> dict[str, Any] | None:
    row = store.conn.execute("SELECT * FROM project_workspaces WHERE project_id=?", (project["project_id"],)).fetchone()
    if row is None:
        return None
    value = dict(row)
    if value["workspace_adapter_id"] != workspace.manifest.adapter_id or value["workspace_session_name"] != workspace.manifest.session_name:
        raise NeedsAttentionError("project workspace uses a different adapter or session")
    if str(Path(value["repository_path"]).resolve(strict=False)) != str(Path(str(project["repository_path"])).resolve(strict=False)):
        raise NeedsAttentionError("project workspace repository identity drifted")
    return value


def _first_mate_workspace(store: Any, workspace: WorkspaceAdapter) -> str:
    rows = store.conn.execute(
        "SELECT r.workspace_id FROM runtime_bindings r JOIN workstreams w USING(workstream_id) "
        "WHERE w.kind='first_mate' AND w.desired_state='active' AND w.provisioning_state='bound' "
        "AND r.workspace_adapter_id=? AND r.workspace_session_name=?",
        (workspace.manifest.adapter_id, workspace.manifest.session_name),
    ).fetchall()
    if len(rows) != 1 or not rows[0]["workspace_id"]:
        raise NeedsAttentionError("fleet project requires one bound First Mate workspace")
    return str(rows[0]["workspace_id"])


@control_plane_mutation
def ensure_project_workspace(
    store: Any,
    project: Mapping[str, Any],
    workspace: WorkspaceAdapter,
    *,
    label: str,
    create_tab: bool = True,
) -> dict[str, Any]:
    """Return the project workspace, creating only its first workspace once.

    A project workspace is intentionally independent from any secretary binding.
    Its first tab may be a secretary tab, a worker tab, or another project-owned
    surface, but every later tab must use the recorded workspace identity.
    """
    record = _record(store, project, workspace)
    repository = str(project["repository_path"])
    observed: WorkspaceObservation | None = None
    fleet_workspace_id = _first_mate_workspace(store, workspace) if project.get("coordination_mode") == "fleet" else None
    if record is None:
        if fleet_workspace_id is None:
            observed = _validate_observation(workspace.create_workspace(repository, label, focus=False))
            workspace_id = observed.workspace_id
        else:
            workspace_id = fleet_workspace_id
        now = utc_now()
        with store.transaction():
            existing = store.conn.execute("SELECT * FROM project_workspaces WHERE project_id=?", (project["project_id"],)).fetchone()
            if existing is None:
                store.conn.execute(
                    "INSERT INTO project_workspaces(project_id,workspace_adapter_id,workspace_session_name,workspace_id,repository_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (project["project_id"], workspace.manifest.adapter_id, workspace.manifest.session_name, workspace_id, repository, now, now),
                )
            elif existing["workspace_id"] != workspace_id:
                raise ConflictError("project workspace was created concurrently with a different identity")
        record = _record(store, project, workspace)
        if record is None:
            raise NeedsAttentionError("project workspace was not persisted")
    workspace_id = str(record["workspace_id"])
    if fleet_workspace_id is not None and workspace_id != fleet_workspace_id:
        raise NeedsAttentionError("fleet project workspace requires the bound First Mate workspace identity")
    if observed is None and create_tab:
        observed = workspace.observe_tab(workspace_id=workspace_id, cwd=repository)
        if observed is None:
            try:
                observed = _validate_observation(workspace.create_tab(workspace_id=workspace_id, cwd=repository, label=label, focus=False), workspace_id)
            except PisecError as error:
                if str(error) != f"workspace {workspace_id} not found":
                    raise
                observed = _validate_observation(workspace.create_workspace(repository, label, focus=False))
                now = utc_now()
                with store.transaction():
                    store.conn.execute(
                        "UPDATE project_workspaces SET workspace_id=?,updated_at=? WHERE project_id=? AND workspace_id=?",
                        (observed.workspace_id, now, project["project_id"], workspace_id),
                    )
                record = _record(store, project, workspace)
                if record is None or record["workspace_id"] != observed.workspace_id:
                    raise NeedsAttentionError("project workspace identity was not repaired")
        else:
            observed = _validate_observation(observed, workspace_id)
    return {**record, "observation": observed}


def project_workspace(store: Any, project_id: str) -> dict[str, Any] | None:
    row = store.conn.execute("SELECT * FROM project_workspaces WHERE project_id=?", (project_id,)).fetchone()
    return None if row is None else dict(row)
