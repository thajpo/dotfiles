"""Canonical Git project registration and status."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .events import append_event_in_transaction
from .fence import resolve_data_dirs
from .models import AuthorizationError, ConflictError, InvalidRequestError, NeedsAttentionError, NotFoundError, PisecError, bounded_text, canonical_json, new_id, utc_now, validate_git_oid, validate_id, validate_remote_url
from .research import research_counts
from .git_runner import git_text
from .control_plane import control_plane_mutation

COORDINATION_MODES = frozenset({"fleet", "project"})
FLEET_COORDINATION_MODE = "fleet"


def _git(path: Path, *args: str) -> str:
    try:
        return git_text(path, *args, timeout=10, max_bytes=64 * 1024)
    except InvalidRequestError:
        raise


def _origin_url(path: Path) -> str | None:
    try:
        value = _git(path, "config", "--local", "--get", "remote.origin.url")
    except InvalidRequestError:
        return None
    return validate_remote_url(value)


def observe_project(path: str | Path, default_ref: str | None = None) -> dict[str, Any]:
    requested = Path(path).expanduser().resolve(strict=True)
    if _git(requested, "rev-parse", "--is-bare-repository") != "false":
        raise InvalidRequestError("bare repositories are not supported")
    top = Path(_git(requested, "rev-parse", "--show-toplevel")).resolve(strict=True)
    common_text = _git(requested, "rev-parse", "--git-common-dir")
    common = Path(common_text)
    if not common.is_absolute():
        common = requested / common
    common = common.resolve(strict=True)
    ref = bounded_text(default_ref or "HEAD", name="default_ref", limit=512)
    if ref.startswith("-") or any(ord(char) < 0x20 for char in ref):
        raise InvalidRequestError("default_ref contains unsafe characters")
    oid = _git(top, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    validate_git_oid(oid, "Git commit object id")
    return {"repository_path": str(top), "git_common_dir": str(common), "default_ref": ref, "default_oid": oid, "remote_url": _origin_url(top)}


def get_project(store: Any, project_id: str) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    row = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if row is None:
        raise NotFoundError("project was not found")
    value = dict(row)
    raw_data = value.get("data_dirs")
    value["data_dirs"] = json.loads(raw_data) if raw_data else []
    raw_domains = value.get("external_domains")
    value["external_domains"] = json.loads(raw_domains) if raw_domains else []
    return value


def resolve_project(store: Any, selector: str) -> dict[str, Any]:
    try:
        if selector.startswith("prj_"):
            return get_project(store, selector)
        observation = observe_project(selector)
        row = store.conn.execute("SELECT * FROM projects WHERE git_common_dir=?", (observation["git_common_dir"],)).fetchone()
    except (OSError, InvalidRequestError):
        rows = list(store.conn.execute("SELECT * FROM projects WHERE display_name=?", (selector,)))
        if len(rows) > 1:
            raise InvalidRequestError("project name is ambiguous")
        row = rows[0] if rows else None
    if row is None:
        raise NotFoundError("project was not found")
    return get_project(store, row["project_id"])


def _bound_runtime_is_usable(store: Any, workstream_id: str, workspace: Any, harness: Any | None = None) -> bool:
    try:
        from .runtime import usable_runtime_binding
        if workspace is None or harness is None:
            return False
        return usable_runtime_binding(store, workstream_id, workspace, harness, allowed_states={"idle", "working", "blocked"})
    except Exception:
        return False


def _project_mode_target(store: Any, project: Mapping[str, Any], workspace: Any, *, first_mate_workspace_id: str) -> tuple[str, bool, str | None]:
    """Select or create a project-owned destination for a project-mode Secretary."""
    from .project_workspaces import _record, _validate_observation

    repository = str(project["repository_path"])
    record = _record(store, project, workspace)
    if record is not None and str(record["workspace_id"]) != first_mate_workspace_id:
        target_id = str(record["workspace_id"])
        observed = workspace.observe_tab(workspace_id=target_id, cwd=repository)
        if observed is not None:
            return target_id, False, None
        try:
            observed = _validate_observation(workspace.create_tab(workspace_id=target_id, cwd=repository, label=f"Project: {project['display_name']}", focus=False), target_id)
        except PisecError as error:
            if str(error) != f"workspace {target_id} not found":
                raise
        else:
            return target_id, False, observed.view_id

    observed = _validate_observation(workspace.create_workspace(repository, f"Project: {project['display_name']}", focus=False))
    if observed.workspace_id == first_mate_workspace_id:
        raise NeedsAttentionError("project mode workspace creation returned the First Mate workspace")
    return observed.workspace_id, True, observed.view_id


def _move_secretary_surface(
    store: Any,
    project: Mapping[str, Any],
    binding: Mapping[str, Any],
    *,
    target_workspace_id: str,
    created_workspace: bool,
    created_view_id: str | None,
    workspace: Any,
    harness: Any,
    coordination_mode: str,
    first_mate_id: str | None,
) -> dict[str, Any]:
    """Move a live Secretary through the workspace adapter before changing mode."""
    from .project_workspaces import _record, _validate_observation

    project_id = str(project["project_id"])
    workstream_id = str(binding["workstream_id"])
    repository = str(project["repository_path"])
    old_workspace_id = str(binding["workspace_id"])
    moved = False
    try:
        moved_observation = _validate_observation(
            workspace.move_surface_to_tab(
                surface_id=str(binding["workspace_surface_id"]),
                workspace_id=target_workspace_id,
                label=f"Secretary: {project['display_name']}",
                focus=False,
            ),
            target_workspace_id,
        )
        moved = True
        observed = workspace.observe_surface(
            workspace_id=moved_observation.workspace_id,
            view_id=moved_observation.view_id,
            surface_id=moved_observation.surface_id,
            cwd=repository,
        )
        observed = _validate_observation(observed, target_workspace_id)
        if observed.worktree_path is not None and str(Path(observed.worktree_path).resolve(strict=False)) != str(Path(repository).resolve(strict=False)):
            raise NeedsAttentionError("moved Secretary surface escaped the approved repository")
        agent = observed.agent
        expected_names = {str(binding["agent_name"]), str(harness.manifest.agent_kind)}
        if agent is None or agent.name not in expected_names or agent.surface_id != observed.surface_id or not agent.identity_usable:
            raise NeedsAttentionError("moved Secretary surface lost its durable agent identity")
        if workspace.observe_runtime(observed.surface_id, str(binding["policy_path"])).state != "live":
            raise NeedsAttentionError("moved Secretary surface is not live")
    except Exception:
        if not moved:
            if created_workspace:
                workspace.close_workspace(target_workspace_id)
            elif created_view_id is not None:
                workspace.close_tab(created_view_id)
        raise

    record = _record(store, project, workspace)
    now = utc_now()
    with store.transaction():
        if record is None:
            store.conn.execute(
                "INSERT INTO project_workspaces(project_id,workspace_adapter_id,workspace_session_name,workspace_id,repository_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (project_id, workspace.manifest.adapter_id, workspace.manifest.session_name, target_workspace_id, repository, now, now),
            )
        else:
            store.conn.execute(
                "UPDATE project_workspaces SET workspace_id=?,updated_at=? WHERE project_id=?",
                (target_workspace_id, now, project_id),
            )
        cursor = store.conn.execute(
            "UPDATE runtime_bindings SET workspace_id=?,workspace_view_id=?,workspace_surface_id=?,updated_at=? WHERE workstream_id=? AND workspace_id=? AND workspace_view_id=? AND workspace_surface_id=?",
            (observed.workspace_id, observed.view_id, observed.surface_id, now, workstream_id, old_workspace_id, binding["workspace_view_id"], binding["workspace_surface_id"]),
        )
        if cursor.rowcount != 1:
            raise NeedsAttentionError("Secretary binding changed while its workspace surface was moving")
        cursor = store.conn.execute(
            "UPDATE projects SET coordination_mode=?,updated_at=? WHERE project_id=? AND active=1 AND lifecycle_attention_reason IS NULL AND coordination_mode=?",
            (coordination_mode, now, project_id, project["coordination_mode"]),
        )
        if cursor.rowcount != 1:
            raise NeedsAttentionError("project mode changed while its Secretary workspace surface was moving")
        append_event_in_transaction(
            store.conn,
            kind="project.mode.changed",
            project_id=project_id,
            workstream_id=workstream_id,
            payload={
                "from": project["coordination_mode"],
                "to": coordination_mode,
                "workspaceId": observed.workspace_id,
                "workspaceViewId": observed.view_id,
                "workspaceSurfaceId": observed.surface_id,
            },
        )
        if first_mate_id is not None:
            from .attention import backfill_attention
            backfill_attention(store, recipient_workstream_id=first_mate_id, limit=128)
    return {"workspace_id": observed.workspace_id, "workspace_view_id": observed.view_id, "workspace_surface_id": observed.surface_id}


@control_plane_mutation
def change_project_mode(store: Any, selector: str, coordination_mode: str, *, workspace: Any, harness: Any | None = None) -> dict[str, Any]:
    if coordination_mode not in COORDINATION_MODES:
        raise InvalidRequestError("coordination_mode must be project or fleet")
    project = resolve_project(store, selector)
    if not project.get("active"):
        raise ConflictError("project mode changes require an active project")
    current = str(project.get("coordination_mode") or "project")
    if current == coordination_mode:
        return project
    assert_project_writable(store, str(project["project_id"]))
    secretary_id = project.get("secretary_workstream_id")
    if not isinstance(secretary_id, str) or not _bound_runtime_is_usable(store, secretary_id, workspace, harness):
        raise NeedsAttentionError("project mode changes require a usable bound Secretary")
    first_mate_id: str | None = None
    first_mate_workspace_id: str | None = None
    if coordination_mode == FLEET_COORDINATION_MODE:
        from .project_workspaces import _first_mate_workspace
        first_mate_workspace_id = _first_mate_workspace(store, workspace)
        first_mate = store.conn.execute(
            "SELECT w.workstream_id,r.workspace_id FROM workstreams w JOIN runtime_bindings r USING(workstream_id) WHERE w.workstream_id IN (SELECT workstream_id FROM workstreams WHERE kind='first_mate' AND desired_state='active' AND provisioning_state='bound') AND r.workspace_id=?",
            (first_mate_workspace_id,),
        ).fetchone()
        if first_mate is None or not _bound_runtime_is_usable(store, str(first_mate["workstream_id"]), workspace, harness):
            raise NeedsAttentionError("entering fleet mode requires a usable bound First Mate")
        first_mate_id = str(first_mate["workstream_id"])
    else:
        open_escalation = store.conn.execute(
            "SELECT 1 FROM issues WHERE project_id=? AND reporter_kind='secretary' AND escalated_from_issue_id IS NOT NULL AND state IN ('open','acknowledged','remediating','verifying') LIMIT 1",
            (project["project_id"],),
        ).fetchone()
        if open_escalation is not None:
            raise ConflictError("leaving fleet mode requires all Secretary escalation issues to be closed")
    binding = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (secretary_id,)).fetchone()
    if binding is None:
        raise NeedsAttentionError("project Secretary binding is missing")
    if coordination_mode == FLEET_COORDINATION_MODE:
        target_workspace_id = str(first_mate_workspace_id)
        created_workspace = False
        created_view_id = None
    else:
        target_workspace_id, created_workspace, created_view_id = _project_mode_target(
            store,
            project,
            workspace,
            first_mate_workspace_id=str(binding["workspace_id"]),
        )
    _move_secretary_surface(
        store,
        project,
        dict(binding),
        target_workspace_id=target_workspace_id,
        created_workspace=created_workspace,
        created_view_id=created_view_id,
        workspace=workspace,
        harness=harness,
        coordination_mode=coordination_mode,
        first_mate_id=first_mate_id,
    )
    return get_project(store, project["project_id"])


@control_plane_mutation
def register_project(store: Any, path: str | Path, *, display_name: str | None = None, default_ref: str | None = None, data_dirs: Any = None, external_domains: Any = None, coordination_mode: str | None = None, workspace: Any = None, harness: Any | None = None) -> dict[str, Any]:
    if coordination_mode is not None and coordination_mode not in COORDINATION_MODES:
        raise InvalidRequestError("coordination_mode must be project or fleet")
    observed = observe_project(path, default_ref)
    resolved_data = resolve_data_dirs(data_dirs, Path(observed["repository_path"]))
    if external_domains is None:
        resolved_domains: list[str] = []
    elif not isinstance(external_domains, list) or any(not isinstance(item, str) or not item.strip() for item in external_domains):
        raise InvalidRequestError("external_domains must be a list of non-empty strings")
    else:
        resolved_domains = sorted(set(external_domains))
    data_json = json.dumps(sorted(set(resolved_data)), separators=(",", ":"))
    domains_json = json.dumps(resolved_domains, separators=(",", ":"))
    existing = store.conn.execute("SELECT * FROM projects WHERE git_common_dir=?", (observed["git_common_dir"],)).fetchone()
    if existing is not None:
        value = dict(existing)
        if coordination_mode is not None and coordination_mode != value.get("coordination_mode", "project"):
            value = change_project_mode(store, str(value["project_id"]), coordination_mode, workspace=workspace, harness=harness)
        registered_remote = value.get("remote_url")
        observed_remote = observed["remote_url"]
        if registered_remote is None and observed_remote is not None:
            with store.transaction():
                store.conn.execute(
                    "UPDATE projects SET remote_url=?,updated_at=? WHERE project_id=? AND remote_url IS NULL",
                    (observed_remote, utc_now(), value["project_id"]),
                )
            return get_project(store, value["project_id"])
        if registered_remote != observed_remote:
            raise InvalidRequestError("registered project origin remote drifted")
        if data_dirs is not None or external_domains is not None:
            old_domains = value.get("external_domains")
            if value.get("data_dirs") == data_json and old_domains == domains_json:
                return get_project(store, value["project_id"])
            raise ConflictError("project permissions must be replaced through the protected permission operation")
        return value
    if coordination_mode not in {None, "project"}:
        raise InvalidRequestError("new projects must be registered in project mode")
    project_id = new_id("prj")
    name = bounded_text(display_name or Path(observed["repository_path"]).name, name="display_name", limit=512)
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "INSERT INTO projects(project_id,display_name,repository_path,git_common_dir,default_ref,remote_url,data_dirs,external_domains,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (project_id, name, observed["repository_path"], observed["git_common_dir"], observed["default_ref"], observed["remote_url"], data_json, domains_json, now, now),
        )
        append_event_in_transaction(store.conn, kind="project.registered", project_id=project_id, payload={"displayName": name, "repositoryPath": observed["repository_path"], "gitCommonDir": observed["git_common_dir"], "defaultRef": observed["default_ref"]})
    return get_project(store, project_id)


def list_projects(store: Any, include_inactive: bool = False) -> list[dict[str, Any]]:
    if include_inactive:
        rows = store.conn.execute("SELECT * FROM projects ORDER BY display_name,project_id")
    else:
        rows = store.conn.execute("SELECT * FROM projects WHERE active=1 ORDER BY display_name,project_id")
    return [get_project(store, row["project_id"]) for row in rows]


def list_fleet_projects(store: Any) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in store.conn.execute(
            "SELECT * FROM projects WHERE active=1 AND coordination_mode=? ORDER BY display_name,project_id",
            (FLEET_COORDINATION_MODE,),
        )
    ]


def fleet_project_ids(store: Any) -> list[str]:
    return [str(row["project_id"]) for row in store.conn.execute("SELECT project_id FROM projects WHERE active=1 AND coordination_mode=? ORDER BY display_name,project_id", (FLEET_COORDINATION_MODE,))]


def platform_project_id(store: Any) -> str | None:
    """Return the active registered project that owns Pisec platform remediation.

    Platform issues are intentionally routed to the repository that contains
    this broker.  The display-name fallback keeps test and development
    checkouts usable when the source tree is imported through a symlink or
    packaged path; callers still require the project to be explicitly
    registered and active.
    """
    source_root = Path(__file__).resolve().parents[2]
    rows = store.conn.execute("SELECT * FROM projects WHERE active=1 ORDER BY project_id").fetchall()
    for row in rows:
        if Path(str(row["repository_path"])).resolve(strict=False) == source_root:
            return str(row["project_id"])
    for row in rows:
        display_name = str(row["display_name"]).strip().lower()
        repository_name = Path(str(row["repository_path"])).name.lower()
        if display_name in {"dotfiles", "pisec"} or repository_name == "dotfiles":
            return str(row["project_id"])
    return None


def first_mate_issue_project_ids(store: Any) -> list[str]:
    """Return project IDs whose issue records are visible to the First Mate."""
    ids = fleet_project_ids(store)
    platform_id = platform_project_id(store)
    if platform_id is not None and platform_id not in ids:
        ids.append(platform_id)
    return ids


def is_first_mate_issue_project(store: Any, project_id: str) -> bool:
    return project_id in first_mate_issue_project_ids(store)


def require_fleet_project(store: Any, project_id: str) -> dict[str, Any]:
    project = get_project(store, project_id)
    if not project.get("active") or project.get("coordination_mode") != FLEET_COORDINATION_MODE:
        raise AuthorizationError("project is outside the First Mate fleet scope")
    return project


def _project_lifecycle_state(store: Any, project: Mapping[str, Any]) -> bool:
    value = project.get("active")
    return True if value is None else bool(value)


def assert_project_writable(store: Any, project_id: str) -> None:
    project = get_project(store, project_id)
    if not _project_lifecycle_state(store, project):
        raise ConflictError("project is inactive")
    if project.get("lifecycle_attention_reason"):
        raise NeedsAttentionError("project lifecycle requires repair")
    operation = store.conn.execute(
        "SELECT state FROM operations WHERE project_id=? AND kind='project.deactivate' ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if operation is not None and operation["state"] in {"planned", "applying", "needs_attention"}:
        raise ConflictError("project deactivation is in progress")

@control_plane_mutation
def deactivate_project(store: Any, selector: str, workspace: Any, harness: Any) -> dict[str, Any]:
    from .cleanup import _validate_retained_session_root

    project = get_project(store, resolve_project(store, selector)["project_id"])
    project_id = project["project_id"]
    if not _project_lifecycle_state(store, project):
        return {"projectId": project_id, "displayName": project["display_name"], "workstreamId": None, "retainedSessionRoot": None, "reused": True}

    live_rows = [dict(row) for row in store.conn.execute("SELECT * FROM workstreams WHERE project_id=? AND desired_state <> 'retired'", (project_id,))]
    workers = [row for row in live_rows if row["kind"] == "worker"]
    if workers:
        raise ConflictError("project has active worker workstreams; complete or retire them before deactivation")
    first_mates = [row for row in live_rows if row["kind"] == "first_mate"]
    if first_mates:
        raise ConflictError("global First Mate workstreams are managed separately from projects")
    secretaries = [row for row in live_rows if row["kind"] == "secretary"]
    secretary = secretaries[0] if secretaries else None
    binding = None
    if secretary is not None:
        row = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (secretary["workstream_id"],)).fetchone()
        binding = None if row is None else dict(row)
    if binding is not None and binding["observed_state"] in {"starting", "working", "blocked"}:
        raise ConflictError("project coordinator runtime is still active; wait for it to become idle")

    lifecycle_index = int(store.conn.execute(
        "SELECT COUNT(*) FROM events WHERE project_id=? AND kind='project.deactivated'",
        (project_id,),
    ).fetchone()[0])
    key = f"project.deactivate:{project_id}:{lifecycle_index}"
    operation = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (key,)).fetchone()
    if operation is not None and operation["state"] == "succeeded":
        result = json.loads(operation["result_json"] or "{}")
        return {"projectId": project_id, "displayName": project["display_name"], **result, "reused": True}
    if operation is None:
        operation_id = new_id("op")
        now = utc_now()
        request = {"kind": "project.deactivate", "projectId": project_id}
        with store.transaction():
            store.conn.execute(
                "INSERT INTO operations(operation_id,kind,project_id,idempotency_key,request_json,request_sha256,state,step,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'planned',?,?)",
                (operation_id, "project.deactivate", project_id, key, canonical_json(request), __import__("hashlib").sha256(canonical_json(request).encode()).hexdigest(), "applying", now, now),
            )
        operation = store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    operation_id = str(operation["operation_id"])
    step = str(operation["step"])
    stages = ("planned", "workspace_close_intent", "workspace_closed", "retained_root_committed", "binding_cleanup_intent", "binding_cleaned", "committed")
    rank = lambda value: stages.index(value) if value in stages else -1

    if binding is not None and (
        workspace is None or harness is None
        or binding["workspace_adapter_id"] != workspace.manifest.adapter_id
        or binding["harness_id"] != harness.manifest.adapter_id
    ):
        now = utc_now()
        reason = "configured adapter does not match the durable coordinator binding"
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='needs_attention',error_code='deactivation_binding_mismatch',error_message=?,updated_at=? WHERE operation_id=?", (reason, now, operation_id))
            store.conn.execute("UPDATE projects SET active=1,lifecycle_attention_reason=?,updated_at=? WHERE project_id=?", (reason, now, project_id))
        raise NeedsAttentionError("configured adapter does not match the durable coordinator binding")

    retained_root = None
    try:
        if binding is not None:
            if rank(step) < rank("workspace_close_intent"):
                with store.transaction():
                    store.conn.execute("UPDATE operations SET state='applying',step='workspace_close_intent',error_code=NULL,error_message=NULL,updated_at=? WHERE operation_id=?", (utc_now(), operation_id))
                step = "workspace_close_intent"
            if rank(step) < rank("workspace_closed"):
                shared = store.conn.execute(
                    "SELECT 1 FROM runtime_bindings r JOIN workstreams w USING(workstream_id) "
                    "WHERE r.workspace_id=? AND r.workstream_id<>? AND w.desired_state='active' LIMIT 1",
                    (binding["workspace_id"], binding["workstream_id"]),
                ).fetchone()
                if shared is None:
                    workspace.close_workspace(str(binding["workspace_id"]))
                else:
                    workspace.close_tab(str(binding["workspace_view_id"]))
                with store.transaction():
                    store.conn.execute("UPDATE operations SET state='applying',step='workspace_closed',updated_at=? WHERE operation_id=?", (utc_now(), operation_id))
                step = "workspace_closed"
            retained_root_path = _validate_retained_session_root(binding, harness)
            retained_root = None if retained_root_path is None else str(retained_root_path)
            if retained_root_path is not None and rank(step) < rank("retained_root_committed"):
                with store.transaction():
                    existing_root = store.conn.execute("SELECT 1 FROM retained_session_roots WHERE workstream_id=?", (binding["workstream_id"],)).fetchone()
                    if existing_root is None:
                        store.conn.execute(
                            "INSERT INTO retained_session_roots(workstream_id,harness_id,harness_home,native_session_kind,native_session_value,retained_at) VALUES(?,?,?,?,?,?)",
                            (binding["workstream_id"], binding["harness_id"], binding["harness_home"], binding["native_session_kind"], binding["native_session_value"], utc_now()),
                        )
                    store.conn.execute("UPDATE operations SET state='applying',step='retained_root_committed',updated_at=? WHERE operation_id=?", (utc_now(), operation_id))
                step = "retained_root_committed"
            if rank(step) < rank("binding_cleanup_intent"):
                with store.transaction():
                    store.conn.execute("UPDATE operations SET state='applying',step='binding_cleanup_intent',updated_at=? WHERE operation_id=?", (utc_now(), operation_id))
                step = "binding_cleanup_intent"
            if rank(step) < rank("binding_cleaned"):
                harness.cleanup_binding(binding)
                with store.transaction():
                    store.conn.execute("UPDATE operations SET state='applying',step='binding_cleaned',updated_at=? WHERE operation_id=?", (utc_now(), operation_id))
                step = "binding_cleaned"
        now = utc_now()
        result = {"projectId": project_id, "workstreamId": None if secretary is None else secretary["workstream_id"], "retainedSessionRoot": retained_root}
        with store.transaction():
            if binding is not None:
                store.conn.execute("DELETE FROM runtime_bindings WHERE workstream_id=?", (binding["workstream_id"],))
            if secretary is not None:
                store.conn.execute("UPDATE workstreams SET desired_state='retired',retired_at=?,attention_reason=NULL,updated_at=? WHERE workstream_id=?", (now, now, secretary["workstream_id"]))
            store.conn.execute("DELETE FROM project_workspaces WHERE project_id=?", (project_id,))
            store.conn.execute("UPDATE projects SET active=0,deactivated_at=?,secretary_workstream_id=NULL,updated_at=? WHERE project_id=?", (now, now, project_id))
            store.conn.execute("UPDATE operations SET state='succeeded',step='committed',result_json=?,updated_at=? WHERE operation_id=?", (canonical_json(result), now, operation_id))
            append_event_in_transaction(store.conn, kind="project.deactivated", project_id=project_id, operation_id=operation_id, payload=result)
        return {"projectId": project_id, "displayName": project["display_name"], **result, "reused": False}
    except (ConflictError, NeedsAttentionError):
        raise
    except Exception as error:
        with store.transaction():
            now = utc_now()
            store.conn.execute("UPDATE operations SET state='needs_attention',error_code='deactivation_step_failed',error_message=?,updated_at=? WHERE operation_id=?", (str(error)[:512], now, operation_id))
            store.conn.execute("UPDATE projects SET active=1,lifecycle_attention_reason=?,updated_at=? WHERE project_id=?", (f"project deactivation requires repair: {error}"[:2048], now, project_id))
        raise NeedsAttentionError("project deactivation requires retry") from error


def project_activity(store: Any, project_id: str, after: int = 0) -> dict[str, Any]:
    project = get_project(store, project_id)
    after = max(0, int(after))
    current = int(store.conn.execute("SELECT COALESCE(MAX(sequence),0) FROM events").fetchone()[0])
    rows = store.conn.execute(
        """
        SELECT w.workstream_id,w.title,w.kind,w.desired_state,w.completed_at,
               cp.phase,cp.summary,cp.next_action,cp.sequence AS checkpoint_sequence,
               (SELECT COUNT(*) FROM research_requests rr WHERE rr.workstream_id=w.workstream_id AND rr.state NOT IN ('answered','declined','acknowledged')) AS research_open,
               (SELECT COUNT(*) FROM coordination_requests cr WHERE cr.workstream_id=w.workstream_id AND cr.state <> 'acknowledged') AS coordination_open,
               (SELECT COUNT(*) FROM decisions d WHERE d.workstream_id=w.workstream_id AND d.state='open') AS decisions_open,
               (SELECT MAX(e.sequence) FROM events e WHERE e.workstream_id=w.workstream_id) AS changed_sequence
        FROM workstreams w
        LEFT JOIN workstream_checkpoints cp ON cp.workstream_id=w.workstream_id
          AND cp.sequence=(SELECT MAX(sequence) FROM workstream_checkpoints WHERE workstream_id=w.workstream_id)
        WHERE w.project_id=? AND w.kind='worker'
        ORDER BY w.created_at,w.workstream_id
        """,
        (project_id,),
    )
    cards = []
    for row in rows:
        if after and (row["changed_sequence"] is None or int(row["changed_sequence"]) <= after):
            continue
        attention = "review requested" if row["phase"] == "ready_review" else None
        cards.append({
            "workstreamId": row["workstream_id"],
            "outcome": row["title"],
            "owner": "Worker",
            "state": "completed" if row["desired_state"] == "completed" else (row["phase"] or "implementing"),
            "checkpoint": None if row["phase"] is None else {"phase": row["phase"], "summary": row["summary"], "sequence": row["checkpoint_sequence"]},
            "nextAction": row["next_action"] if row["next_action"] else ("Review completion packet" if row["desired_state"] == "completed" else "Continue task"),
            "attention": attention,
            "completed": row["desired_state"] == "completed",
            "needsUser": bool(attention or row["coordination_open"] or row["decisions_open"]),
            "open": {"coordination": row["coordination_open"], "research": row["research_open"], "decisions": row["decisions_open"]},
        })
    issue_rows = store.conn.execute(
        "SELECT i.*,w.kind AS reporter_kind,w.title AS reporter_title,(SELECT MAX(e.sequence) FROM events e WHERE e.payload_json LIKE '%' || i.issue_id || '%') AS changed_sequence FROM issues i JOIN workstreams w ON w.workstream_id=i.reporter_workstream_id WHERE i.project_id=? AND i.state <> 'resolved' ORDER BY CASE i.severity WHEN 'blocking' THEN 0 WHEN 'degraded' THEN 1 ELSE 2 END,i.created_at,i.issue_id",
        (project_id,),
    )
    for row in issue_rows:
        if after and (row["changed_sequence"] is None or int(row["changed_sequence"]) <= after):
            continue
        cards.append({
            "issueId": row["issue_id"],
            "projectId": row["project_id"],
            "reporterKind": row["reporter_kind"],
            "reporterWorkstreamId": row["reporter_workstream_id"],
            "severity": row["severity"],
            "state": row["state"],
            "summary": row["summary"],
            "nextAction": "Acknowledge and inspect issue" if row["state"] == "open" else ("Obtain reporter verification" if row["state"] == "verifying" else "Choose approved remediation"),
            "needsUser": True,
        })
    return {"projectId": project_id, "cards": cards, "after": after, "cursor": current}


def fleet_activity(store: Any, after: int = 0) -> dict[str, Any]:
    after = max(0, int(after))
    projects = [
        {
            "projectId": row["project_id"],
            "displayName": row["display_name"],
            "cards": project_activity(store, row["project_id"], after)["cards"],
        }
        for row in list_fleet_projects(store)
    ]
    return {"projects": projects, "after": after, "cursor": int(store.conn.execute("SELECT COALESCE(MAX(sequence),0) FROM events").fetchone()[0])}


def project_status(store: Any, project_id: str) -> dict[str, Any]:
    project = get_project(store, project_id)
    workstreams = []
    for row in store.conn.execute(
        "SELECT w.*,r.observed_state,r.last_observed_at FROM workstreams w LEFT JOIN runtime_bindings r USING(workstream_id) WHERE w.project_id=? ORDER BY w.created_at",
        (project_id,),
    ):
        workstreams.append(_semantic_workstream_status(store, dict(row)))
    decisions = [dict(row) for row in store.conn.execute("SELECT * FROM decisions WHERE project_id=? ORDER BY created_at", (project_id,))]
    secretary = next((row for row in workstreams if row["kind"] == "secretary" and row["desired_state"] != "retired"), None)
    project_view = dict(project)
    if secretary is not None:
        project_view["taskState"] = secretary["taskState"]
        project_view["runtimeState"] = secretary["runtimeState"]
        project_view["attentionCount"] = secretary["attentionCount"]
        project_view["attentionPriority"] = secretary["attentionPriority"]
        project_view["nextAction"] = secretary["nextAction"]
    else:
        project_view["taskState"] = "retired" if not project.get("active") else "setting_up"
        project_view["runtimeState"] = "not_bound"
        project_view["attentionCount"] = 0
        project_view["attentionPriority"] = None
        project_view["nextAction"] = "Open project coordinator" if project.get("active") else "Reopen project"
    return {"project": project_view, "workstreams": workstreams, "decisions": decisions, "researchCounts": research_counts(store, project_id), "source": "pisec-sqlite"}


def _semantic_workstream_status(store: Any, row: dict[str, Any]) -> dict[str, Any]:
    workstream_id = str(row["workstream_id"])
    binding = store.conn.execute(
        "SELECT * FROM runtime_bindings WHERE workstream_id=?",
        (workstream_id,),
    ).fetchone()
    runtime_state = "not_bound" if binding is None else str(binding["observed_state"] or "unknown")
    integration_rows = list(store.conn.execute(
        "SELECT * FROM integration_jobs WHERE workstream_id=? ORDER BY created_at DESC",
        (workstream_id,),
    ))
    integration = integration_rows[0] if integration_rows else None
    task_state_error = None
    acceptance_count = int(store.conn.execute("SELECT COUNT(*) FROM workstream_acceptances WHERE workstream_id=?", (workstream_id,)).fetchone()[0])
    if len(integration_rows) > 1:
        task_state_error = "workstream has multiple integration jobs"
    elif acceptance_count and len(integration_rows) != 1:
        task_state_error = "accepted workstream must have exactly one integration job"
    packet = store.conn.execute(
        "SELECT * FROM completion_packets WHERE workstream_id=? ORDER BY sequence DESC LIMIT 1",
        (workstream_id,),
    ).fetchone()
    checkpoint = store.conn.execute(
        "SELECT * FROM workstream_checkpoints WHERE workstream_id=? ORDER BY sequence DESC LIMIT 1",
        (workstream_id,),
    ).fetchone()
    if packet is not None and (checkpoint is None or checkpoint["phase"] != "ready_review" or checkpoint["idempotency_key"] != f"completion:{packet['packet_sha256']}"):
        task_state_error = "completion packet lacks its matching ready_review checkpoint"
    if task_state_error is not None:
        task_state = "needs_attention"
    elif row["desired_state"] == "retired":
        task_state = "retired"
    elif row["desired_state"] == "completed":
        task_state = "completed"
    elif row["provisioning_state"] == "needs_attention" or (integration is not None and integration["state"] == "needs_attention"):
        task_state = "needs_attention"
    elif row["provisioning_state"] in {"proposed", "creating"}:
        task_state = "setting_up"
    elif row["kind"] in {"secretary", "first_mate"} and row["provisioning_state"] == "bound":
        task_state = "supervising"
    elif integration is not None and integration["state"] == "awaiting_worker":
        task_state = "reconciling"
    elif integration is not None and integration["state"] == "queued":
        task_state = "accepted"
    elif integration is not None and integration["state"] in {"refreshing", "verifying", "applying", "integrated"}:
        task_state = "integrating"
    elif checkpoint is not None and checkpoint["phase"] == "ready_review" and packet is not None and acceptance_count == 0:
        task_state = "ready_review"
    else:
        task_state = "active"
    attention_count = 0
    attention_priority = None
    if binding is not None and row["desired_state"] == "active" and row["provisioning_state"] == "bound":
        try:
            from .attention import list_open_attention
            attention = list_open_attention(store, recipient_workstream_id=workstream_id, limit=32)
            attention_count = len(attention)
            attention_priority = min((int(item["priority"]) for item in attention), default=None)
        except Exception:
            task_state_error = task_state_error or "attention projection is unavailable"
            task_state = "needs_attention"
    next_action = None
    if integration is not None and integration["next_action"]:
        next_action = integration["next_action"]
    elif row.get("attention_reason"):
        next_action = row["attention_reason"]
    else:
        next_action = {
            "setting_up": "Complete runtime setup",
            "supervising": "Review current attention",
            "reconciling": "Review bounded target reconciliation",
            "accepted": "Begin accepted integration",
            "integrating": "Complete integration verification",
            "ready_review": "Review completion candidate",
            "needs_attention": "Inspect and repair the reported invariant",
            "completed": "No action required",
            "retired": "No action required",
        }.get(task_state, "Continue task")
    result = dict(row)
    result.update({
        "taskState": task_state,
        "runtimeState": runtime_state,
        "attentionCount": attention_count,
        "attentionPriority": attention_priority,
        "nextAction": next_action,
    })
    if task_state_error is not None:
        result["taskStateError"] = task_state_error
    return result
