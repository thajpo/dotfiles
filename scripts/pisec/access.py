"""Project-wide read-only permission composition for runtime scopes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .fence import DOMAIN_RE, resolve_data_dirs
from .models import ConflictError, InvalidRequestError, NeedsAttentionError, NotFoundError, ScopeMismatchError, canonical_json, json_digest, new_id, utc_now
from .platform import runtime_root

_VIRTUAL_ROOTS = tuple(Path(value) for value in ("/proc", "/sys", "/dev", "/run"))
_CREDENTIAL_ROOTS = tuple(Path.home() / value for value in (".ssh", ".gnupg", ".aws", ".azure", ".config/gcloud", ".config/gh"))
_MAX_PERMISSION_ENTRIES = 64

def _safe_external_path(path: str) -> str:
    if not isinstance(path, str) or not Path(path).is_absolute() or len(path) > 4096 or "\x00" in path:
        raise InvalidRequestError("grant path must be an absolute path")
    original = Path(path)
    try:
        resolved = original.resolve(strict=True)
    except OSError as error:
        raise InvalidRequestError("grant path must exist") from error
    if resolved != original or not (resolved.is_file() or resolved.is_dir()):
        raise InvalidRequestError("grant path must use its canonical spelling and be a regular file or directory")
    protected = list(_VIRTUAL_ROOTS) + list(_CREDENTIAL_ROOTS) + [runtime_root().resolve(strict=False)]
    for root in protected:
        root = root.resolve(strict=False)
        if resolved == root or resolved.is_relative_to(root) or root.is_relative_to(resolved):
            raise InvalidRequestError("grant path overlaps a protected root")
    return str(resolved)


def _project(store: Any, project_id: str) -> Mapping[str, Any]:
    row = store.conn.execute("SELECT * FROM projects WHERE project_id=? AND active=1", (project_id,)).fetchone()
    if row is None:
        raise NotFoundError("active project was not found")
    return row


def _check_subject(store: Any, project_id: str, subject_kind: str, workstream_id: str | None) -> None:
    if subject_kind not in {"workstream", "project_workers"}:
        raise InvalidRequestError("grant subject kind is invalid")
    if subject_kind == "workstream":
        if not isinstance(workstream_id, str):
            raise InvalidRequestError("workstream grant requires a workstream")
        row = store.conn.execute("SELECT kind,desired_state,provisioning_state,project_id FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()
        if row is None or row["project_id"] != project_id or row["kind"] != "worker" or row["desired_state"] != "active" or row["provisioning_state"] != "bound":
            raise ConflictError("workstream grant requires an active bound worker")
    elif workstream_id is not None:
        raise InvalidRequestError("project_workers grant cannot name a workstream")


def _grant_row(store: Any, grant_id: str) -> Mapping[str, Any]:
    row = store.conn.execute("SELECT * FROM access_grants WHERE grant_id=?", (grant_id,)).fetchone()
    if row is None:
        raise NotFoundError("access grant was not found")
    return row


def _grant_targets(store: Any, grant: Mapping[str, Any]) -> list[str]:
    if grant["subject_kind"] == "workstream":
        return [str(grant["workstream_id"])]
    return [
        str(row["workstream_id"])
        for row in store.conn.execute(
            "SELECT w.workstream_id FROM workstreams w JOIN runtime_bindings r USING(workstream_id) WHERE w.project_id=? AND w.kind='worker' AND w.desired_state='active' AND w.provisioning_state='bound'",
            (grant["project_id"],),
        )
    ]


def _operation(store: Any, *, kind: str, project_id: str, request: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
    digest = json_digest(request)
    row = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    if row is not None:
        if row["request_sha256"] != digest or row["kind"] != kind:
            raise ConflictError("idempotency key is already used for another access request")
        return row
    operation_id = new_id("op")
    now = utc_now()
    with store.transaction():
        store.conn.execute("INSERT INTO operations(operation_id,kind,project_id,idempotency_key,request_json,request_sha256,state,step,created_at,updated_at) VALUES(?,?,?,?,?,?, 'planned','planned',?,?)", (operation_id, kind, project_id, idempotency_key, canonical_json(request), digest, now, now))
    return store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()


def _scope(*, operation: Mapping[str, Any], project_id: str, subject_kind: str, workstream_id: str | None, path: str, issue_id: str | None, kind: str) -> dict[str, Any]:
    return {"kind": kind, "operationId": operation["operation_id"], "projectId": project_id, "issueId": issue_id, "subjectKind": subject_kind, "workstreamId": workstream_id, "path": path, "mode": "read", "effects": ["Fence filesystem.allowRead only"], "nonEffects": ["no filesystem write", "no network", "no secretary or First Mate access", "no sibling-project access", "no protected-root access", "no direct executable policy"]}


def prepare_access_grant(store: Any, *, project_id: str, subject_kind: str, workstream_id: str | None, path: str, issue_id: str | None, idempotency_key: str) -> dict[str, Any]:
    _project(store, project_id)
    _check_subject(store, project_id, subject_kind, workstream_id)
    canonical_path = _safe_external_path(path)
    if issue_id is not None:
        issue = store.conn.execute("SELECT project_id,state FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
        if issue is None or issue["project_id"] != project_id or issue["state"] == "resolved":
            raise ConflictError("issue is not an unresolved issue in the project")
    for row in store.conn.execute("SELECT * FROM access_grants WHERE project_id=? AND subject_kind=? AND state IN ('activating','active','revoking')", (project_id, subject_kind)):
        if subject_kind == "workstream" and row["workstream_id"] != workstream_id:
            continue
        existing_path = Path(row["path"])
        requested = Path(canonical_path)
        if requested == existing_path or requested.is_relative_to(existing_path):
            if row["state"] == "revoking":
                raise ConflictError("a revoking grant overlaps the requested path")
            return {"grant": dict(row), "approvalScope": None, "reused": True}
    request = {"projectId": project_id, "subjectKind": subject_kind, "workstreamId": workstream_id, "path": canonical_path, "issueId": issue_id}
    operation = _operation(store, kind="access.grant", project_id=project_id, request=request, idempotency_key=idempotency_key)
    existing = store.conn.execute("SELECT * FROM access_grants WHERE proposal_operation_id=?", (operation["operation_id"],)).fetchone()
    if existing is None:
        now = utc_now()
        with store.transaction():
            store.conn.execute("INSERT INTO access_grants(grant_id,project_id,subject_kind,workstream_id,path,access_mode,state,proposal_operation_id,issue_id,created_at,updated_at) VALUES(?,?,?,?,?,'read','proposed',?,?,?,?)", (new_id("agr"), project_id, subject_kind, workstream_id, canonical_path, operation["operation_id"], issue_id, now, now))
        existing = store.conn.execute("SELECT * FROM access_grants WHERE proposal_operation_id=?", (operation["operation_id"],)).fetchone()
    scope = {**_scope(operation=operation, project_id=project_id, subject_kind=subject_kind, workstream_id=workstream_id, path=canonical_path, issue_id=issue_id, kind="access.read.grant"), "grantId": existing["grant_id"]}
    return {"grant": dict(existing), "approvalScope": scope, "reused": False}


def authorize_apply_access_grant(store: Any, *, scope: Mapping[str, Any], harness: Any, workspace: Any, actor: str = "first_mate") -> dict[str, Any]:
    if actor != "first_mate" or scope.get("kind") != "access.read.grant":
        raise ScopeMismatchError("access grant approval scope is invalid")
    operation = store.conn.execute("SELECT * FROM operations WHERE operation_id=? AND kind='access.grant'", (scope.get("operationId"),)).fetchone()
    grant = _grant_row(store, str(scope.get("grantId"))) if scope.get("grantId") else None
    if operation is None or grant is None or grant["proposal_operation_id"] != operation["operation_id"] or grant["path"] != scope.get("path") or grant["project_id"] != scope.get("projectId"):
        raise ScopeMismatchError("access grant scope does not match its proposal")
    now = utc_now()
    target_ids = _grant_targets(store, grant)
    with store.transaction():
        if store.conn.execute("SELECT 1 FROM authorizations WHERE operation_id=?", (operation["operation_id"],)).fetchone() is None:
            store.conn.execute("INSERT INTO authorizations(authorization_id,operation_id,scope_sha256,kind,scope_json,actor,consumed_at) VALUES(?,?,?,'access.grant',?,?,?)", (new_id("az"), operation["operation_id"], json_digest(scope), canonical_json(scope), actor, now))
        store.conn.execute("UPDATE access_grants SET state='active',approved_at=COALESCE(approved_at,?),updated_at=? WHERE grant_id=?", (now, now, grant["grant_id"]))
        if grant["issue_id"] is not None:
            if store.conn.execute("SELECT 1 FROM issue_remediations WHERE issue_id=? AND access_grant_id=?", (grant["issue_id"], grant["grant_id"])).fetchone() is None:
                store.conn.execute("INSERT INTO issue_remediations(remediation_id,issue_id,kind,access_grant_id,created_at) VALUES(?,?, 'access_grant',?,?)", (new_id("rem"), grant["issue_id"], grant["grant_id"], now))
            store.conn.execute("UPDATE issues SET state='remediating',updated_at=? WHERE issue_id=? AND state IN ('open','acknowledged')", (now, grant["issue_id"]))
        store.conn.execute("UPDATE operations SET state='succeeded',step='applied',result_json=?,updated_at=? WHERE operation_id=?", (canonical_json({"grantId": grant["grant_id"], "state": "active"}), now, operation["operation_id"]))
    from .refresh import refresh_bindings
    refresh = refresh_bindings(store, harness, workspace, target_ids, wait_seconds=0)
    return {"grant": dict(_grant_row(store, grant["grant_id"])), "operation": dict(store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation["operation_id"],)).fetchone()), "refresh": refresh}


def prepare_access_revoke(store: Any, *, project_id: str, grant_id: str, idempotency_key: str) -> dict[str, Any]:
    grant = _grant_row(store, grant_id)
    if grant["project_id"] != project_id or grant["state"] in {"revoked", "revoking"}:
        raise ConflictError("access grant cannot be revoked")
    request = {"projectId": project_id, "grantId": grant_id, "subjectKind": grant["subject_kind"], "workstreamId": grant["workstream_id"], "path": grant["path"], "mode": "read"}
    operation = _operation(store, kind="access.revoke", project_id=project_id, request=request, idempotency_key=idempotency_key)
    scope = {"kind": "access.read.revoke", "operationId": operation["operation_id"], "projectId": project_id, "grantId": grant_id, **{key: request[key] for key in ("subjectKind", "workstreamId", "path", "mode")}, "effects": ["remove one Fence filesystem.allowRead entry"], "nonEffects": ["no filesystem write", "no network", "no secretary or First Mate access"]}
    return {"grant": dict(grant), "approvalScope": scope, "reused": False}


def authorize_apply_access_revoke(store: Any, *, scope: Mapping[str, Any], harness: Any, workspace: Any, actor: str = "first_mate") -> dict[str, Any]:
    if actor != "first_mate" or scope.get("kind") != "access.read.revoke":
        raise ScopeMismatchError("access revoke approval scope is invalid")
    grant = _grant_row(store, str(scope.get("grantId")))
    if grant["project_id"] != scope.get("projectId") or grant["path"] != scope.get("path"):
        raise ScopeMismatchError("access revoke scope does not match the grant")
    operation = store.conn.execute("SELECT * FROM operations WHERE operation_id=? AND kind='access.revoke'", (scope.get("operationId"),)).fetchone()
    if operation is None:
        raise ScopeMismatchError("access revoke operation was not found")
    now = utc_now()
    target_ids = _grant_targets(store, grant)
    with store.transaction():
        if store.conn.execute("SELECT 1 FROM authorizations WHERE operation_id=?", (operation["operation_id"],)).fetchone() is None:
            store.conn.execute("INSERT INTO authorizations(authorization_id,operation_id,scope_sha256,kind,scope_json,actor,consumed_at) VALUES(?,?,?,'access.revoke',?,?,?)", (new_id("az"), operation["operation_id"], json_digest(scope), canonical_json(scope), actor, now))
        store.conn.execute("UPDATE access_grants SET state='revoked',revoked_at=?,updated_at=? WHERE grant_id=?", (now, now, grant["grant_id"]))
        store.conn.execute("UPDATE operations SET state='succeeded',step='applied',result_json=?,updated_at=? WHERE operation_id=?", (canonical_json({"grantId": grant["grant_id"], "state": "revoked"}), now, operation["operation_id"]))
    from .refresh import refresh_bindings
    refresh = refresh_bindings(store, harness, workspace, target_ids, wait_seconds=0)
    return {"grant": dict(_grant_row(store, grant["grant_id"])), "operation": dict(store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation["operation_id"],)).fetchone()), "refresh": refresh}


def list_access_grants(store: Any, *, project_id: str | None = None, workstream_id: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM access_grants WHERE 1=1"
    params: list[Any] = []
    if project_id is not None:
        query += " AND project_id=?"; params.append(project_id)
    if workstream_id is not None:
        query += " AND (workstream_id=? OR subject_kind='project_workers')"; params.append(workstream_id)
    return [dict(row) for row in store.conn.execute(query + " ORDER BY created_at,grant_id", params)]


def effective_runtime_scope(store: Any, binding: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(binding["kind"])
    operation_kind = {"secretary": "secretary.ensure", "first_mate": "first_mate.ensure"}.get(kind, "workstream.create")
    row = store.conn.execute("SELECT result_json FROM operations WHERE workstream_id=? AND kind=? ORDER BY created_at LIMIT 1", (binding["workstream_id"], operation_kind)).fetchone()
    if row is None or row["result_json"] is None:
        raise NeedsAttentionError("runtime generation scope is missing")
    scope = json.loads(str(row["result_json"]))
    project = store.conn.execute("SELECT repository_path,data_dirs,external_domains FROM projects WHERE project_id=?", (binding["project_id"],)).fetchone()
    data_dirs: list[str] = []
    domains: list[str] = []
    if project is not None:
        if project["data_dirs"]:
            data_dirs = resolve_data_dirs(json.loads(project["data_dirs"]), Path(project["repository_path"]))
        if project["external_domains"]:
            domains = _canonical_domains(json.loads(project["external_domains"]))
    from .releases import fleet_scope_paths
    result = fleet_scope_paths(store, scope)
    result["dataDirs"] = sorted(set(data_dirs))
    result["externalDomains"] = domains
    result["readPathSources"] = {path: [{"kind": "project_data", "sourceId": str(binding["project_id"])}] for path in result["dataDirs"]}
    return result


def _canonical_project_paths(store: Any, project: Mapping[str, Any], values: Any) -> list[str]:
    if not isinstance(values, list) or len(values) > _MAX_PERMISSION_ENTRIES:
        raise InvalidRequestError("project data dirs must be a list of at most 64 entries")
    repository = Path(str(project["repository_path"])).resolve(strict=True)
    canonical: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
            raise InvalidRequestError("project data dir is invalid")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = repository / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise InvalidRequestError("project data dir must exist") from error
        if resolved != candidate.resolve(strict=False) or not (resolved.is_file() or resolved.is_dir()):
            raise InvalidRequestError("project data dir must use canonical spelling and be a regular file or directory")
        protected = [*(_VIRTUAL_ROOTS), *(_CREDENTIAL_ROOTS), runtime_root()]
        if any(resolved == root.resolve(strict=False) or resolved.is_relative_to(root.resolve(strict=False)) or root.resolve(strict=False).is_relative_to(resolved) for root in protected):
            raise InvalidRequestError("project data dir overlaps a protected root")
        canonical.append(str(resolved))
    if len(canonical) != len(set(canonical)):
        raise InvalidRequestError("project data dirs contain duplicates")
    return sorted(canonical)


def _canonical_domains(values: Any) -> list[str]:
    if not isinstance(values, list) or len(values) > _MAX_PERMISSION_ENTRIES:
        raise InvalidRequestError("project external domains must be a list of at most 64 entries")
    if any(not isinstance(value, str) or DOMAIN_RE.fullmatch(value) is None for value in values):
        raise InvalidRequestError("project external domain is invalid")
    canonical = sorted(set(values))
    if len(canonical) != len(values):
        raise InvalidRequestError("project external domains contain duplicates")
    return canonical


def _project_permissions(store: Any, project_id: str) -> Mapping[str, Any]:
    return _project(store, project_id)


def prepare_project_permissions(
    store: Any,
    *,
    project_id: str,
    data_dirs: list[str],
    external_domains: list[str],
    issue_id: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    project = _project_permissions(store, project_id)
    paths = _canonical_project_paths(store, project, data_dirs)
    domains = _canonical_domains(external_domains)
    if issue_id is not None:
        issue = store.conn.execute("SELECT project_id,state FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
        if issue is None or issue["project_id"] != project_id or issue["state"] == "resolved":
            raise ConflictError("issue is not an unresolved issue in the project")
    previous = {
        "dataDirs": _canonical_project_paths(store, project, json.loads(project["data_dirs"] or "[]")),
        "externalDomains": _canonical_domains(json.loads(project["external_domains"] or "[]")),
    }
    request = {"projectId": project_id, "dataDirs": paths, "externalDomains": domains, "issueId": issue_id}
    operation = _operation(store, kind="project.permissions.update", project_id=project_id, request=request, idempotency_key=idempotency_key)
    scope = {
        "kind": "project.permissions.update",
        "operationId": operation["operation_id"],
        "projectId": project_id,
        "issueId": issue_id,
        "previousPermissionsSha256": json_digest(previous),
        "dataDirs": paths,
        "externalDomains": domains,
        "effects": ["replace project-wide read-only filesystem paths", "replace project-wide network domains", "refresh idle runtimes"],
        "nonEffects": ["no filesystem write", "no runtime version selection", "no sibling-project access", "no secretary or First Mate authority"],
    }
    return {"operation": dict(operation), "approvalScope": scope, "reused": operation["state"] != "planned"}


def authorize_apply_project_permissions(
    store: Any,
    *,
    approval_scope: Mapping[str, Any],
    harness_resolver: Any,
    surface_resolver: Any,
    workspace: Any,
    actor: str,
) -> dict[str, Any]:
    if actor not in {"secretary", "first_mate"} or approval_scope.get("kind") != "project.permissions.update":
        raise ScopeMismatchError("project permission approval scope is invalid")
    project_id = str(approval_scope.get("projectId"))
    project = _project_permissions(store, project_id)
    current = {
        "dataDirs": _canonical_project_paths(store, project, json.loads(project["data_dirs"] or "[]")),
        "externalDomains": _canonical_domains(json.loads(project["external_domains"] or "[]")),
    }
    if json_digest(current) != approval_scope.get("previousPermissionsSha256"):
        raise ScopeMismatchError("project permissions changed since approval")
    paths = _canonical_project_paths(store, project, approval_scope.get("dataDirs"))
    domains = _canonical_domains(approval_scope.get("externalDomains"))
    operation = store.conn.execute(
        "SELECT * FROM operations WHERE operation_id=? AND kind='project.permissions.update'",
        (approval_scope.get("operationId"),),
    ).fetchone()
    if operation is None:
        raise ScopeMismatchError("project permission operation was not found")
    now = utc_now()
    with store.transaction():
        if store.conn.execute("SELECT 1 FROM authorizations WHERE operation_id=?", (operation["operation_id"],)).fetchone() is None:
            store.conn.execute(
                "INSERT INTO authorizations(authorization_id,operation_id,scope_sha256,kind,scope_json,actor,consumed_at) VALUES(?,?,?,?,?,?,?)",
                (new_id("az"), operation["operation_id"], json_digest(approval_scope), "project.permissions.update", canonical_json(approval_scope), actor, now),
            )
        store.conn.execute(
            "UPDATE projects SET data_dirs=?,external_domains=?,updated_at=? WHERE project_id=?",
            (canonical_json(paths), canonical_json(domains), now, project_id),
        )
        issue_id = approval_scope.get("issueId")
        if issue_id:
            secretary = store.conn.execute("SELECT secretary_workstream_id FROM projects WHERE project_id=?", (project_id,)).fetchone()
            actor_id = secretary["secretary_workstream_id"] if secretary and secretary["secretary_workstream_id"] else None
            if actor_id:
                payload = {"operationId": operation["operation_id"], "kind": "project.permissions.update"}
                store.conn.execute(
                    "INSERT OR IGNORE INTO issue_updates(issue_id,actor_id,update_kind,payload_json,payload_sha256,idempotency_key,created_at) VALUES(?,?, 'remediation_linked',?,?,?,?)",
                    (issue_id, actor_id, canonical_json(payload), json_digest(payload), operation["operation_id"], now),
                )
            store.conn.execute("UPDATE issues SET state='remediating',updated_at=? WHERE issue_id=? AND state IN ('open','acknowledged')", (now, issue_id))
        store.conn.execute(
            "UPDATE operations SET state='succeeded',step='applied',result_json=?,updated_at=? WHERE operation_id=?",
            (canonical_json({"projectId": project_id, "dataDirs": paths, "externalDomains": domains}), now, operation["operation_id"]),
        )
    from .refresh import refresh_runtimes
    refresh = refresh_runtimes(store, harness_resolver(project_id) if callable(harness_resolver) else harness_resolver, workspace, wait_seconds=0, surface_resolver=surface_resolver, project_ids=(project_id,))
    return {"operation": dict(store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation["operation_id"],)).fetchone()), "refresh": refresh}
