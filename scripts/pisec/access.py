"""Project-wide read-only permission composition for runtime scopes."""

from __future__ import annotations

import json
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .fence import DOMAIN_RE
from .models import ConflictError, InvalidRequestError, NeedsAttentionError, NotFoundError, ScopeMismatchError, canonical_json, json_digest, new_id, utc_now
from .operations import authoritative_workstream_creation
from .runtime_surface import capture_runtime_surface, verify_surface
from .platform import runtime_root

_VIRTUAL_ROOTS = tuple(Path(value) for value in ("/proc", "/sys", "/dev", "/run"))
_CREDENTIAL_ROOTS = tuple(Path.home() / value for value in (".ssh", ".gnupg", ".aws", ".azure", ".config/gcloud", ".config/gh"))
_MAX_PERMISSION_ENTRIES = 64


def _project(store: Any, project_id: str) -> Mapping[str, Any]:
    row = store.conn.execute("SELECT * FROM projects WHERE project_id=? AND active=1", (project_id,)).fetchone()
    if row is None:
        raise NotFoundError("active project was not found")
    return row


def _canonical_project_paths(store: Any, project: Mapping[str, Any], values: Any) -> list[str]:
    if not isinstance(values, list) or len(values) > _MAX_PERMISSION_ENTRIES:
        raise InvalidRequestError("project data dirs must be a list of at most 64 entries")
    repository = Path(str(project["repository_path"])).resolve(strict=True)
    protected = [*(root.resolve(strict=False) for root in _VIRTUAL_ROOTS), *(root.resolve(strict=False) for root in _CREDENTIAL_ROOTS), runtime_root().resolve(strict=False)]
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
            raise InvalidRequestError("project data dir is invalid")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = repository / candidate
        try:
            resolved = candidate.resolve(strict=True)
            info = resolved.lstat()
        except OSError as error:
            raise InvalidRequestError("project data dir must exist") from error
        if resolved != candidate or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise InvalidRequestError("project data dir must use its canonical spelling and be a regular file or directory")
        if any(resolved == root or resolved.is_relative_to(root) or root.is_relative_to(resolved) for root in protected):
            raise InvalidRequestError("project data dir overlaps a protected root")
        result.append(str(resolved))
    if len(result) != len(set(result)):
        raise InvalidRequestError("project data dirs contain duplicates")
    return result


def _canonical_domains(values: Any) -> list[str]:
    if not isinstance(values, list) or len(values) > _MAX_PERMISSION_ENTRIES:
        raise InvalidRequestError("project external domains must be a list of at most 64 entries")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or DOMAIN_RE.fullmatch(value) is None:
            raise InvalidRequestError("project external domain is invalid")
        result.append(value)
    if len(result) != len(set(result)):
        raise InvalidRequestError("project external domains contain duplicates")
    return sorted(result)


def _current_permissions(store: Any, project: Mapping[str, Any]) -> dict[str, list[str]]:
    return {"dataDirs": _canonical_project_paths(store, project, json.loads(project["data_dirs"] or "[]")), "externalDomains": _canonical_domains(json.loads(project["external_domains"] or "[]"))}


def _operation(store: Any, *, project_id: str, request: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
    digest = json_digest(request)
    row = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    if row is not None:
        if row["request_sha256"] != digest or row["kind"] != "project.permissions.update":
            raise ConflictError("idempotency key is already used for another project permission request")
        return row
    operation_id = new_id("op")
    now = utc_now()
    with store.transaction():
        store.conn.execute("INSERT INTO operations(operation_id,kind,project_id,idempotency_key,request_json,request_sha256,state,step,created_at,updated_at) VALUES(?,?,?,?,?,?, 'planned','planned',?,?)", (operation_id, "project.permissions.update", project_id, idempotency_key, canonical_json(request), digest, now, now))
    return store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()


def prepare_project_permissions(store: Any, *, project_id: str, data_dirs: list[str], external_domains: list[str], issue_id: str | None, idempotency_key: str) -> dict[str, Any]:
    project = _project(store, project_id)
    paths = _canonical_project_paths(store, project, data_dirs)
    domains = _canonical_domains(external_domains)
    if issue_id is not None:
        issue = store.conn.execute("SELECT project_id,state FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
        if issue is None or issue["project_id"] != project_id or issue["state"] == "resolved":
            raise ConflictError("issue is not an unresolved issue in the project")
    previous = _current_permissions(store, project)
    operation = _operation(store, project_id=project_id, request={"projectId": project_id, "dataDirs": paths, "externalDomains": domains, "issueId": issue_id}, idempotency_key=idempotency_key)
    scope = {"kind": "project.permissions.update", "operationId": operation["operation_id"], "projectId": project_id, "issueId": issue_id, "previousPermissionsSha256": json_digest(previous), "dataDirs": paths, "externalDomains": domains, "effects": ["replace project-wide read-only filesystem paths", "replace project-wide network domains", "refresh idle runtimes"], "nonEffects": ["no filesystem write", "no runtime version selection", "no sibling-project access", "no secretary or First Mate authority"]}
    return {"operation": dict(operation), "approvalScope": scope, "reused": operation["state"] != "planned"}


def authorize_apply_project_permissions(store: Any, *, approval_scope: Mapping[str, Any], harness_resolver: Any, surface_resolver: Any, workspace: Any, actor: str) -> dict[str, Any]:
    if actor not in {"secretary", "first_mate"} or approval_scope.get("kind") != "project.permissions.update":
        raise ScopeMismatchError("project permission approval scope is invalid")
    project_id = str(approval_scope.get("projectId"))
    project = _project(store, project_id)
    if json_digest(_current_permissions(store, project)) != approval_scope.get("previousPermissionsSha256"):
        raise ScopeMismatchError("project permissions changed since approval")
    paths = _canonical_project_paths(store, project, approval_scope.get("dataDirs"))
    domains = _canonical_domains(approval_scope.get("externalDomains"))
    operation = store.conn.execute("SELECT * FROM operations WHERE operation_id=? AND kind='project.permissions.update' AND project_id=?", (approval_scope.get("operationId"), project_id)).fetchone()
    if operation is None:
        raise ScopeMismatchError("project permission operation was not found")
    bindings = [dict(row) for row in store.conn.execute(
        "SELECT r.*,w.kind,w.execution_profile,w.worktree_path,w.desired_state,w.provisioning_state FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE w.project_id=? AND w.desired_state='active' ORDER BY w.created_at,w.workstream_id",
        (project_id,),
    )]
    prepared: list[tuple[Mapping[str, Any], Any, Mapping[str, Any], Any, Path]] = []
    surfaces: dict[str, Any] = {}
    try:
        for binding in bindings:
            if int(binding.get("refresh_pending", 0)) or binding.get("observed_state") not in {"idle", "stopped", "done"}:
                raise ConflictError("project permission replacement requires every affected runtime to be idle or stopped", detail={"workstreamId": binding["workstream_id"]})
            workstream_id = str(binding["workstream_id"])
            selected_harness = harness_resolver(workstream_id) if callable(harness_resolver) else harness_resolver
            if selected_harness is None:
                raise NeedsAttentionError("affected workstream harness is unavailable", detail={"workstreamId": workstream_id})
            harness_id = str(selected_harness.manifest.adapter_id)
            surface = surfaces.get(harness_id)
            if surface is None:
                surface = surface_resolver(harness_id) if callable(surface_resolver) else capture_runtime_surface(selected_harness)
                surfaces[harness_id] = verify_surface(surface)
            scope = effective_runtime_scope(store, binding)
            scope["dataDirs"] = paths
            scope["externalDomains"] = domains
            desired = selected_harness.desired_generation({**scope, "runtimeSurfaceSha256": surface.content_sha256, "runtimeSurfaceRoot": surface.root_path, "runtimeSurfaceId": "surface_" + surface.content_sha256[:32]}, surface)
            stage_root = Path(tempfile.mkdtemp(prefix=f"pisec-permissions-{workstream_id}-"))
            staged = selected_harness.stage_profile({**scope, "operationId": str(operation["operation_id"]), "runtimeSurfaceSha256": surface.content_sha256, "runtimeSurfaceRoot": surface.root_path, "runtimeSurfaceId": "surface_" + surface.content_sha256[:32]}, surface, stage_root)
            prepared.append((binding, selected_harness, scope, staged, stage_root))
    except Exception:
        for _binding, selected_harness, _scope, staged, _stage_root in prepared:
            selected_harness.discard_staged_profile(staged)
        raise
    now = utc_now()
    try:
        with store.transaction():
            if store.conn.execute("SELECT 1 FROM authorizations WHERE operation_id=?", (operation["operation_id"],)).fetchone() is not None:
                raise ScopeMismatchError("project permission operation was already authorized")
            store.conn.execute("INSERT INTO authorizations(authorization_id,operation_id,scope_sha256,kind,scope_json,actor,consumed_at) VALUES(?,?,?,?,?,?,?)", (new_id("az"), operation["operation_id"], json_digest(approval_scope), "project.permissions.update", canonical_json(approval_scope), actor, now))
            store.conn.execute("UPDATE projects SET data_dirs=?,external_domains=?,updated_at=? WHERE project_id=?", (canonical_json(paths), canonical_json(domains), now, project_id))
            issue_id = approval_scope.get("issueId")
            if issue_id:
                actor_row = store.conn.execute("SELECT secretary_workstream_id FROM projects WHERE project_id=?", (project_id,)).fetchone()
                actor_id = actor_row["secretary_workstream_id"] if actor_row and actor_row["secretary_workstream_id"] else None
                if actor_id:
                    payload = {"operationId": operation["operation_id"], "kind": "project.permissions.update"}
                    store.conn.execute("INSERT OR IGNORE INTO issue_updates(update_id,issue_id,actor_kind,actor_id,update_kind,payload_json,payload_sha256,idempotency_key,created_at) VALUES(?,?, 'secretary',?, 'remediation_linked',?,?,?,?)", (new_id("upd"), issue_id, actor_id, canonical_json(payload), json_digest(payload), operation["operation_id"], now))
                store.conn.execute("UPDATE issues SET state='remediating',updated_at=? WHERE issue_id=? AND state IN ('open','acknowledged')", (now, issue_id))
            store.conn.execute("UPDATE operations SET state='succeeded',step='applied',result_json=?,updated_at=? WHERE operation_id=?", (canonical_json({"projectId": project_id, "dataDirs": paths, "externalDomains": domains}), now, operation["operation_id"]))
        from .refresh import refresh_runtimes
        selected = prepared[0][1] if prepared else (harness_resolver(project_id) if callable(harness_resolver) else harness_resolver)
        refresh = refresh_runtimes(store, selected, workspace, wait_seconds=0, harness_resolver=harness_resolver, surface_resolver=surface_resolver, project_ids=(project_id,))
        return {"operation": dict(store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation["operation_id"],)).fetchone()), "refresh": refresh}
    finally:
        for _binding, selected_harness, _scope, staged, _stage_root in prepared:
            selected_harness.discard_staged_profile(staged)


def effective_runtime_scope(store: Any, binding: Mapping[str, Any]) -> dict[str, Any]:
    """Re-read current project permissions; historical scopes are evidence only."""
    kind = str(binding["kind"])
    operation_kind = {"secretary": "secretary.ensure", "first_mate": "first_mate.ensure"}.get(kind, "workstream.create")
    if operation_kind == "workstream.create":
        row = authoritative_workstream_creation(store, str(binding["workstream_id"]))
    else:
        row = store.conn.execute("SELECT * FROM operations WHERE workstream_id=? AND kind=? AND state IN ('planned','applying','needs_attention','succeeded') ORDER BY created_at,operation_id", (binding["workstream_id"], operation_kind)).fetchone()
    if row is None or row["result_json"] is None:
        raise NeedsAttentionError("runtime generation scope is missing")
    scope = json.loads(str(row["result_json"]))
    project = _project(store, str(binding["project_id"]))
    permissions = _current_permissions(store, project)
    scope["dataDirs"] = permissions["dataDirs"]
    scope["externalDomains"] = permissions["externalDomains"]
    scope["readPathSources"] = {path: [{"kind": "project_data", "sourceId": str(binding["project_id"])}] for path in permissions["dataDirs"]}
    return scope
