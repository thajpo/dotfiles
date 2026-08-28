"""Project-wide read-only permission composition for runtime scopes."""

from __future__ import annotations

import json
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adapters import HarnessArtifacts, StagedHarnessArtifacts, artifact_document, artifacts_from_mapping
from .fence import DOMAIN_RE
from .models import ConflictError, InvalidRequestError, NeedsAttentionError, NotFoundError, ScopeMismatchError, canonical_json, json_digest, new_id, utc_now
from .operations import authoritative_workstream_creation
from .runtime_surface import capture_runtime_surface, verify_surface
from .platform import runtime_root
from .runtime import start_bound_agent, usable_runtime_binding
from .worker_repo import project_permissions_lock
from .control_plane import control_plane_mutation

_VIRTUAL_ROOTS = tuple(Path(value) for value in ("/proc", "/sys", "/dev", "/run"))
_CREDENTIAL_ROOTS = tuple(Path.home() / value for value in (".ssh", ".gnupg", ".aws", ".azure", ".config/gcloud", ".config/gh"))
_MAX_PERMISSION_ENTRIES = 64
_WORKER_PROFILE = "worker-default"
_SUPERVISOR_PROFILES = frozenset({"secretary-project", "first-mate"})


def compose_runtime_domains(harness: Any, profile: str, project_domains: Sequence[str]) -> tuple[str, ...]:
    """Compose the network scope for one role without widening supervisors."""
    if profile == _WORKER_PROFILE:
        additional_domains = tuple(project_domains)
    elif profile in _SUPERVISOR_PROFILES:
        additional_domains = ()
    else:
        raise InvalidRequestError("runtime profile is invalid")
    return tuple(harness.profile_domains(profile, additional_domains))


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


def prepare_project_permissions(store: Any, *, project_id: str, data_dirs: list[str], external_domains: list[str], issue_id: str | None, idempotency_key: str, harness_resolver: Any | None = None, surface_resolver: Any | None = None) -> dict[str, Any]:
    project = _project(store, project_id)
    paths = _canonical_project_paths(store, project, data_dirs)
    domains = _canonical_domains(external_domains)
    if issue_id is not None:
        issue = store.conn.execute("SELECT project_id,state FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
        if issue is None or issue["project_id"] != project_id or issue["state"] == "resolved":
            raise ConflictError("issue is not an unresolved issue in the project")
    previous = _current_permissions(store, project)
    operation = _operation(store, project_id=project_id, request={"projectId": project_id, "dataDirs": paths, "externalDomains": domains, "issueId": issue_id}, idempotency_key=idempotency_key)
    if operation["result_json"]:
        try:
            stored = json.loads(str(operation["result_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            stored = None
        if isinstance(stored, dict) and isinstance(stored.get("approvalScope"), dict):
            return {"operation": dict(operation), "approvalScope": stored["approvalScope"], "reused": True}
    bindings = [dict(row) for row in store.conn.execute(
        "SELECT r.*,w.project_id,w.kind,w.execution_profile,w.worktree_path,w.desired_state,w.provisioning_state FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE w.project_id=? AND w.desired_state='active' ORDER BY w.created_at,w.workstream_id",
        (project_id,),
    )]
    intended: dict[str, str | None] = {str(binding["workstream_id"]): binding["desired_generation_sha256"] for binding in bindings}
    if callable(harness_resolver):
        surfaces: dict[str, Any] = {}
        for binding in bindings:
            selected = harness_resolver(str(binding["workstream_id"]))
            if selected is None:
                raise NeedsAttentionError("affected workstream harness is unavailable", detail={"workstreamId": binding["workstream_id"]})
            harness_id = str(selected.manifest.adapter_id)
            surface = surfaces.get(harness_id)
            if surface is None:
                surface = surface_resolver(harness_id) if callable(surface_resolver) else capture_runtime_surface(selected)
                surfaces[harness_id] = verify_surface(surface)
            effective = effective_runtime_scope(store, binding, harness=selected)
            effective["dataDirs"] = paths
            effective["externalDomains"] = list(compose_runtime_domains(selected, str(binding["execution_profile"]), domains))
            intended[str(binding["workstream_id"])] = selected.desired_generation({**effective, "runtimeSurfaceSha256": surface.content_sha256, "runtimeSurfaceRoot": surface.root_path, "runtimeSurfaceId": "surface_" + surface.content_sha256[:32]}, surface)
    scope = {"kind": "project.permissions.update", "operationId": operation["operation_id"], "projectId": project_id, "issueId": issue_id, "previousPermissions": previous, "previousPermissionsSha256": json_digest(previous), "dataDirs": paths, "externalDomains": domains, "affectedWorkstreamIds": sorted(intended), "intendedGenerationByWorkstream": intended, "effects": ["replace project-wide read-only filesystem paths", "replace project-wide network domains", "refresh idle runtimes"], "nonEffects": ["no filesystem write", "no runtime version selection", "no sibling-project access", "no secretary or First Mate authority"]}
    with store.transaction():
        store.conn.execute("UPDATE operations SET result_json=?,updated_at=? WHERE operation_id=? AND state='planned'", (canonical_json({"approvalScope": scope}, max_bytes=256 * 1024, max_text=64 * 64 * 1024), utc_now(), operation["operation_id"]))
    operation = store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation["operation_id"],)).fetchone()
    return {"operation": dict(operation), "approvalScope": scope, "reused": operation["state"] != "planned"}


def _permission_artifacts(binding: Mapping[str, Any]) -> HarnessArtifacts:
    try:
        document = json.loads(str(binding["adapter_artifacts_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise NeedsAttentionError("permission batch prior artifacts are invalid") from error
    values = document.get("values") if isinstance(document, dict) else None
    if not isinstance(values, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in values.items()):
        raise NeedsAttentionError("permission batch prior artifacts are invalid")
    return artifacts_from_mapping({
        "harnessHome": str(binding["harness_home"]),
        "launchSecretPath": str(binding["launch_secret_path"]),
        "policyPath": str(binding["policy_path"]),
        "policySha256": str(binding["policy_sha256"]),
        "runtimeTokenSha256": str(binding["runtime_token_sha256"]),
        "generationSha256": str(binding["applied_generation_sha256"] or binding["desired_generation_sha256"]),
        "adapterData": values,
    })


def _artifacts(value: Mapping[str, Any]) -> HarnessArtifacts:
    try:
        return artifacts_from_mapping(value)
    except (TypeError, ValueError, InvalidRequestError) as error:
        raise NeedsAttentionError("permission batch artifacts are invalid") from error


def _staged_from_document(operation_id: str, item: Mapping[str, Any]) -> StagedHarnessArtifacts:
    try:
        candidate = _artifacts(item["candidate"])
        prior = _artifacts(item["prior"]) if item.get("prior") is not None else None
        compensation = item["compensation"]
        compensation_json = canonical_json(compensation)
        return StagedHarnessArtifacts(
            operation_id=operation_id,
            workstream_id=str(item["workstreamId"]),
            staging_root=str(Path(str(item["stagingRoot"])).resolve(strict=False)),
            candidate_manifest_json=str(item["candidateManifestJson"]),
            candidate_content_sha256=str(item["candidateContentSha256"]),
            candidate=candidate,
            prior=prior,
            compensation_json=compensation_json,
        )
    except (KeyError, TypeError, ValueError, InvalidRequestError) as error:
        raise NeedsAttentionError("permission batch staged artifacts are invalid") from error


def _permission_attested(store: Any, workspace: Any, binding: Mapping[str, Any], operation_id: str) -> bool:
    row = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (binding["workstream_id"],)).fetchone()
    if row is None or row["refresh_operation_id"] != operation_id or not int(row["refresh_pending"]):
        return False
    if row["launch_generation_sha256"] != row["desired_generation_sha256"] or row["applied_generation_sha256"] != row["desired_generation_sha256"]:
        return False
    if row["runtime_instance_id"] is None or int(row["report_seq"]) < 1 or row["session_start_event_sequence"] is None:
        return False
    if row["observed_state"] not in {"idle", "working", "blocked"}:
        return False
    event = store.conn.execute("SELECT kind,workstream_id,payload_json FROM events WHERE sequence=?", (row["session_start_event_sequence"],)).fetchone()
    if event is None or event["kind"] != "runtime.session_started" or event["workstream_id"] != row["workstream_id"]:
        return False
    try:
        payload = json.loads(str(event["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if payload != {"generationSha256": row["applied_generation_sha256"], "reportSeq": int(row["session_start_report_seq"]), "runtimeInstanceId": row["runtime_instance_id"]}:
        return False
    try:
        observed = workspace.observe_surface(
            workspace_id=str(row["workspace_id"]),
            view_id=str(row["workspace_view_id"]),
            surface_id=str(row["workspace_surface_id"]),
            cwd=str(row["worktree_path"] if "worktree_path" in row.keys() else store.conn.execute("SELECT worktree_path FROM workstreams WHERE workstream_id=?", (row["workstream_id"],)).fetchone()[0]),
        )
        return observed is not None and observed.agent is not None and observed.agent.surface_id == row["workspace_surface_id"] and observed.agent.identity_usable and workspace.observe_runtime(str(row["workspace_surface_id"]), str(row["policy_path"])).state == "live"
    except Exception:
        return False


def _wait_permission_attestation(store: Any, workspace: Any, binding: Mapping[str, Any], operation_id: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not _permission_attested(store, workspace, binding, operation_id):
        if time.monotonic() >= deadline:
            raise NeedsAttentionError("permission candidate did not attest its reserved generation")
        time.sleep(0.05)


def _wait_usable_permission_binding(store: Any, workspace: Any, harness: Any, workstream_id: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while not usable_runtime_binding(store, workstream_id, workspace, harness, allowed_states=frozenset({"idle", "working", "blocked"})):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _permission_failure(store: Any, workspace: Any, operation_id: str, prepared: Sequence[tuple[Mapping[str, Any], Any, Mapping[str, Any], Any, Path]], reason: str, *, requested: Mapping[str, Sequence[str]]) -> bool:
    """Compensate a failed batch; return whether all affected bindings are safe."""
    old = _current_permissions(store, _project(store, str(prepared[0][0]["project_id"]))) if prepared else {"dataDirs": [], "externalDomains": []}
    old_is_subset = set(old["dataDirs"]).issubset(requested["dataDirs"]) and set(old["externalDomains"]).issubset(requested["externalDomains"])
    safe = True
    failure_details: list[str] = []
    restored: list[tuple[str, Any, Mapping[str, Any], HarnessArtifacts]] = []
    for binding, selected_harness, scope, staged, _stage_root in prepared:
        current = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (binding["workstream_id"],)).fetchone()
        if current is None or current["refresh_operation_id"] != operation_id:
            continue
        try:
            workspace.stop_runtime(str(current["workspace_surface_id"]))
        except Exception:
            pass
        try:
            previous = _permission_artifacts(binding)
            old_scope = dict(scope)
            old_scope["dataDirs"] = list(old["dataDirs"])
            old_scope["externalDomains"] = list(compose_runtime_domains(selected_harness, str(binding["execution_profile"]), old["externalDomains"]))
            old_scope["readPathSources"] = {path: [{"kind": "project_data", "sourceId": str(binding["project_id"])}] for path in old["dataDirs"]}
            old_scope["permissionBackupRoot"] = scope.get("permissionBackupRoot")
            restored_artifacts = selected_harness.restore_profile(old_scope, previous)
            selected_harness.commit_launch_binding(
                old_scope,
                restored_artifacts,
                workspace_session_name=str(binding["workspace_session_name"]),
                workspace_id=str(binding["workspace_id"]),
                workspace_view_id=str(binding["workspace_view_id"]),
                workspace_surface_id=str(binding["workspace_surface_id"]),
                replace=True,
            )
            restored.append((str(binding["workstream_id"]), selected_harness, binding, restored_artifacts))
        except Exception as error:
            safe = False
            failure_details.append(f"{binding['workstream_id']}: {error}")
            now = utc_now()
            with store.transaction():
                store.conn.execute("UPDATE runtime_bindings SET observed_state='error',updated_at=? WHERE workstream_id=? AND refresh_operation_id=?", (now, binding["workstream_id"], operation_id))
                store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?", (f"permission compensation failed: {error}"[:512], now, binding["workstream_id"]))

    for workstream_id, selected_harness, binding, restored_artifacts in restored:
        old_generation = str(binding["applied_generation_sha256"] or binding["desired_generation_sha256"] or restored_artifacts.generation_sha256)
        now = utc_now()
        with store.transaction():
            store.conn.execute(
                "UPDATE runtime_bindings SET harness_home=?,adapter_artifacts_json=?,launch_secret_path=?,policy_path=?,policy_sha256=?,runtime_token_sha256=?,desired_generation_sha256=?,applied_generation_sha256=?,runtime_instance_id=NULL,report_seq=0,session_start_event_sequence=NULL,session_start_report_seq=NULL,session_started_at=NULL,observed_state=?,refresh_pending=?,refresh_operation_id=?,refresh_started_at=?,launch_generation_sha256=?,updated_at=? WHERE workstream_id=? AND refresh_operation_id=?",
                (
                    restored_artifacts.harness_home,
                    artifact_document(selected_harness.manifest, restored_artifacts),
                    restored_artifacts.launch_secret_path,
                    restored_artifacts.policy_path,
                    restored_artifacts.policy_sha256,
                    restored_artifacts.runtime_token_sha256,
                    old_generation,
                    old_generation if old_is_subset else None,
                    "starting" if old_is_subset else "stopped",
                    0 if old_is_subset else 1,
                    None if old_is_subset else operation_id,
                    None if old_is_subset else now,
                    None if old_is_subset else old_generation,
                    now,
                    workstream_id,
                    operation_id,
                ),
            )
        if old_is_subset:
            try:
                start_bound_agent(store, workspace, selected_harness, dict(store.conn.execute("SELECT r.*,w.project_id,w.worktree_path FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=?", (workstream_id,)).fetchone()), workstream_id=workstream_id, project_id=str(binding["project_id"]), cwd=str(binding["worktree_path"]))
                if not _wait_usable_permission_binding(store, workspace, selected_harness, workstream_id):
                    raise NeedsAttentionError("restored permission runtime did not attest")
            except Exception as error:
                safe = False
                failure_details.append(f"{workstream_id}: {error}")
                now = utc_now()
                with store.transaction():
                    store.conn.execute("UPDATE runtime_bindings SET refresh_pending=1,refresh_operation_id=?,refresh_started_at=?,launch_generation_sha256=?,applied_generation_sha256=NULL,observed_state='error',updated_at=? WHERE workstream_id=?", (operation_id, now, old_generation, now, workstream_id))
                    store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?", (str(error)[:512], now, workstream_id))
            else:
                with store.transaction():
                    store.conn.execute("UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=?", (utc_now(), workstream_id))

    now = utc_now()
    with store.transaction():
        state = "failed" if safe else "needs_attention"
        step = "compensated" if safe else "compensation"
        detail = reason if not failure_details else f"{reason}; compensation: {'; '.join(failure_details)}"
        store.conn.execute("UPDATE operations SET state=?,step=?,error_code='project_permission_batch_failed',error_message=?,updated_at=? WHERE operation_id=? AND state IN ('planned','applying')", (state, step, detail[:512], now, operation_id))
    return safe


def _commit_permission_batch(store: Any, workspace: Any, *, operation: Mapping[str, Any], approval_scope: Mapping[str, Any], batch_bindings: Sequence[Mapping[str, Any]], paths: Sequence[str], domains: Sequence[str], actor: str) -> dict[str, Any]:
    now = utc_now()
    operation_id = str(operation["operation_id"])
    project_id = str(approval_scope["projectId"])
    with store.transaction():
        current_project = _project(store, project_id)
        if json_digest(_current_permissions(store, current_project)) != approval_scope.get("previousPermissionsSha256"):
            raise ScopeMismatchError("project permissions changed before final commit")
        for item in batch_bindings:
            current = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (item["workstreamId"],)).fetchone()
            if current is None or not _permission_attested(store, workspace, current, operation_id):
                raise NeedsAttentionError("permission candidate attestation is incomplete")
        authorization = store.conn.execute("SELECT authorization_id FROM authorizations WHERE operation_id=?", (operation_id,)).fetchone()
        if authorization is None:
            store.conn.execute("INSERT INTO authorizations(authorization_id,operation_id,scope_sha256,kind,scope_json,actor,consumed_at) VALUES(?,?,?,?,?,?,?)", (new_id("az"), operation_id, json_digest(approval_scope), "project.permissions.update", canonical_json(approval_scope), actor, now))
        store.conn.execute("UPDATE projects SET data_dirs=?,external_domains=?,updated_at=? WHERE project_id=?", (canonical_json(list(paths)), canonical_json(list(domains)), now, project_id))
        issue_id = approval_scope.get("issueId")
        if issue_id:
            actor_row = store.conn.execute("SELECT secretary_workstream_id FROM projects WHERE project_id=?", (project_id,)).fetchone()
            actor_id = actor_row["secretary_workstream_id"] if actor_row and actor_row["secretary_workstream_id"] else None
            if actor_id:
                payload = {"operationId": operation_id, "kind": "project.permissions.update"}
                store.conn.execute("INSERT OR IGNORE INTO issue_updates(update_id,issue_id,actor_kind,actor_id,update_kind,payload_json,payload_sha256,idempotency_key,created_at) VALUES(?,?, 'secretary',?, 'remediation_linked',?,?,?,?)", (new_id("upd"), issue_id, actor_id, canonical_json(payload), json_digest(payload), operation_id, now))
            store.conn.execute("UPDATE issues SET state='remediating',updated_at=? WHERE issue_id=? AND state IN ('open','acknowledged')", (now, issue_id))
        for item in batch_bindings:
            store.conn.execute("UPDATE runtime_bindings SET refresh_pending=0,refresh_operation_id=NULL,refresh_started_at=NULL,launch_generation_sha256=NULL,updated_at=? WHERE workstream_id=? AND refresh_operation_id=?", (now, item["workstreamId"], operation_id))
        result = {"projectId": project_id, "dataDirs": list(paths), "externalDomains": list(domains), "bindings": [{"workstreamId": item["workstreamId"], "generationSha256": item["desiredGenerationSha256"]} for item in batch_bindings]}
        store.conn.execute("UPDATE operations SET state='succeeded',step='committed',result_json=?,updated_at=? WHERE operation_id=? AND state='applying'", (canonical_json(result), now, operation_id))
    return result


def _authorize_apply_project_permissions_locked(store: Any, *, approval_scope: Mapping[str, Any], harness_resolver: Any, surface_resolver: Any, workspace: Any, actor: str) -> dict[str, Any]:
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
    if operation["state"] == "succeeded" and operation["step"] == "committed" and operation["result_json"]:
        try:
            result = json.loads(str(operation["result_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise NeedsAttentionError("completed project permission result is invalid") from error
        return {"operation": dict(operation), "refresh": {"generation": "per-binding", "upgraded": result.get("bindings", []), "pending": [], "skipped": [], "failed": [], "ok": True}, "reused": True}
    if operation["state"] in {"failed", "needs_attention", "cancelled"}:
        raise NeedsAttentionError("project permission operation requires recorded compensation", detail={"operationId": operation["operation_id"], "step": operation["step"]})
    if operation["state"] == "applying" and operation["step"] != "planned":
        recovered = recover_project_permission_operation(store, operation=operation, harness_resolver=harness_resolver, workspace=workspace, actor=actor)
        if recovered is not None:
            return recovered
    bindings = [dict(row) for row in store.conn.execute(
        "SELECT r.*,w.project_id,w.kind,w.execution_profile,w.worktree_path,w.desired_state,w.provisioning_state FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE w.project_id=? AND w.desired_state='active' ORDER BY w.created_at,w.workstream_id",
        (project_id,),
    )]
    expected_workstreams = sorted(str(value) for value in approval_scope.get("affectedWorkstreamIds", []))
    if expected_workstreams and expected_workstreams != sorted(str(binding["workstream_id"]) for binding in bindings):
        raise ScopeMismatchError("affected project workstreams changed since approval")
    prepared: list[tuple[Mapping[str, Any], Any, Mapping[str, Any], Any, Path]] = []
    batch_bindings: list[dict[str, Any]] = []
    surfaces: dict[str, Any] = {}
    completed = False
    compensated = False
    with store.transaction():
        current_project = _project(store, project_id)
        if json_digest(_current_permissions(store, current_project)) != approval_scope.get("previousPermissionsSha256"):
            raise ScopeMismatchError("project permissions changed since approval")
        if store.conn.execute("SELECT 1 FROM authorizations WHERE operation_id=?", (operation["operation_id"],)).fetchone() is not None:
            raise ScopeMismatchError("project permission operation was already authorized")
        store.conn.execute("UPDATE operations SET state='applying',step='preflighted',updated_at=? WHERE operation_id=? AND state IN ('planned','applying')", (utc_now(), operation["operation_id"]))
    try:
        for binding in bindings:
            if int(binding.get("refresh_pending", 0)) or binding.get("observed_state") not in {"idle", "stopped"}:
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
            scope = effective_runtime_scope(store, binding, harness=selected_harness)
            scope["dataDirs"] = paths
            scope["externalDomains"] = list(
                compose_runtime_domains(selected_harness, str(binding["execution_profile"]), domains)
            )
            desired = selected_harness.desired_generation({**scope, "runtimeSurfaceSha256": surface.content_sha256, "runtimeSurfaceRoot": surface.root_path, "runtimeSurfaceId": "surface_" + surface.content_sha256[:32]}, surface)
            stage_root = Path(tempfile.mkdtemp(prefix=f"pisec-permissions-{workstream_id}-"))
            batch_scope = {**scope, "operationId": str(operation["operation_id"]), "runtimeSurfaceSha256": surface.content_sha256, "runtimeSurfaceRoot": surface.root_path, "runtimeSurfaceId": "surface_" + surface.content_sha256[:32], "permissionBackupRoot": str(stage_root / "previous")}
            staged = selected_harness.stage_profile(batch_scope, surface, stage_root)
            prepared.append((binding, selected_harness, batch_scope, staged, stage_root))
            prior_artifacts = _permission_artifacts(binding)
            prior_descriptor = None
            try:
                descriptor_path = selected_harness.launch_binding_path(workstream_id).with_name("binding.json")
                prior_descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                prior_descriptor = None
            batch_bindings.append({
                "workstreamId": workstream_id,
                "harnessId": harness_id,
                "scope": batch_scope,
                "desiredGenerationSha256": desired,
                "stagingRoot": str(stage_root.resolve()),
                "candidateManifestJson": staged.candidate_manifest_json,
                "candidateContentSha256": staged.candidate_content_sha256,
                "candidate": staged.candidate.as_mapping(),
                "prior": prior_artifacts.as_mapping(),
                "priorArtifactDocument": json.loads(str(binding["adapter_artifacts_json"])),
                "candidateArtifactDocument": json.loads(artifact_document(selected_harness.manifest, staged.candidate)),
                "priorPointer": {"harnessHome": str(binding["harness_home"]), "policyPath": str(binding["policy_path"])},
                "candidatePointer": {"harnessHome": str(staged.candidate.harness_home), "policyPath": str(staged.candidate.policy_path)},
                "priorLaunchDescriptor": prior_descriptor,
                "compensation": json.loads(staged.compensation_json),
                "compensationSteps": ["stop_candidate", "restore_previous", "restore_project_record", "relaunch_previous"],
                "runtimeInstanceId": binding["runtime_instance_id"],
                "reportSeq": binding["report_seq"],
                "observedState": binding["observed_state"],
            })
    except Exception:
        for _binding, selected_harness, _scope, staged, _stage_root in prepared:
            selected_harness.discard_staged_profile(staged)
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='failed',step='preflighted',error_code='project_permission_preflight_failed',error_message=?,updated_at=? WHERE operation_id=? AND state='applying'", ("permission preflight or staging failed", utc_now(), operation["operation_id"]))
        raise
    try:
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='applying',step='staged',result_json=?,updated_at=? WHERE operation_id=? AND state='applying'", (canonical_json({"schemaVersion": 1, "projectId": project_id, "approvalScope": dict(approval_scope), "oldPermissions": _current_permissions(store, _project(store, project_id)), "newPermissions": {"dataDirs": paths, "externalDomains": domains}, "bindings": batch_bindings}, max_bytes=256 * 1024, max_text=64 * 64 * 1024), utc_now(), operation["operation_id"]))
        with store.transaction():
            current_project = _project(store, project_id)
            if json_digest(_current_permissions(store, current_project)) != approval_scope.get("previousPermissionsSha256"):
                raise ScopeMismatchError("project permissions changed since approval")
            for item in batch_bindings:
                current = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (item["workstreamId"],)).fetchone()
                if current is None or int(current["refresh_pending"]) or current["runtime_instance_id"] != item["runtimeInstanceId"] or int(current["report_seq"]) != int(item["reportSeq"]) or current["observed_state"] != item["observedState"]:
                    raise ConflictError("project permission batch changed before reservation", detail={"workstreamId": item["workstreamId"]})
            now = utc_now()
            for item in batch_bindings:
                cursor = store.conn.execute("UPDATE runtime_bindings SET refresh_pending=1,refresh_operation_id=?,refresh_started_at=?,launch_generation_sha256=?,applied_generation_sha256=NULL,runtime_instance_id=NULL,report_seq=0,session_start_event_sequence=NULL,session_start_report_seq=NULL,session_started_at=NULL,observed_state='starting',updated_at=? WHERE workstream_id=? AND refresh_pending=0 AND observed_state IN ('idle','stopped')", (operation["operation_id"], now, item["desiredGenerationSha256"], now, item["workstreamId"]))
                if cursor.rowcount != 1:
                    raise ConflictError("project permission batch reservation failed", detail={"workstreamId": item["workstreamId"]})
            store.conn.execute("UPDATE operations SET state='applying',step='reserved',updated_at=? WHERE operation_id=?", (now, operation["operation_id"]))
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='applying',step='activating',updated_at=? WHERE operation_id=?", (utc_now(), operation["operation_id"]))
        for binding, selected_harness, scope, staged, _stage_root in prepared:
            runtime = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
            if runtime.state == "unknown":
                raise NeedsAttentionError("permission batch runtime identity is ambiguous before activation")
            if runtime.state == "live":
                workspace.stop_runtime(str(binding["workspace_surface_id"]))
                deadline = time.monotonic() + 20.0
                while workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"])).state != "stopped":
                    if time.monotonic() >= deadline:
                        raise NeedsAttentionError("permission batch runtime did not stop")
                    time.sleep(0.05)
            batch_scope = next(item["scope"] for item in batch_bindings if item["workstreamId"] == binding["workstream_id"])
            activated = selected_harness.activate_profile(batch_scope, staged)
            if activated.generation_sha256 != next(item["desiredGenerationSha256"] for item in batch_bindings if item["workstreamId"] == binding["workstream_id"]):
                raise NeedsAttentionError("permission candidate generation changed during activation")
            selected_harness.commit_launch_binding(batch_scope, activated, workspace_session_name=str(binding["workspace_session_name"]), workspace_id=str(binding["workspace_id"]), workspace_view_id=str(binding["workspace_view_id"]), workspace_surface_id=str(binding["workspace_surface_id"]), replace=True)
            with store.transaction():
                store.conn.execute("UPDATE runtime_bindings SET harness_home=?,adapter_artifacts_json=?,launch_secret_path=?,policy_path=?,policy_sha256=?,runtime_token_sha256=?,desired_generation_sha256=?,launch_generation_sha256=?,applied_generation_sha256=NULL,runtime_instance_id=NULL,report_seq=0,observed_state='starting',updated_at=? WHERE workstream_id=? AND refresh_operation_id=?", (activated.harness_home, artifact_document(selected_harness.manifest, activated), activated.launch_secret_path, activated.policy_path, activated.policy_sha256, activated.runtime_token_sha256, activated.generation_sha256, activated.generation_sha256, utc_now(), binding["workstream_id"], operation["operation_id"]))
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='applying',step='verifying',updated_at=? WHERE operation_id=?", (utc_now(), operation["operation_id"]))
        for binding, selected_harness, _scope, _staged, _stage_root in prepared:
            current = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (binding["workstream_id"],)).fetchone()
            if current is None:
                raise NeedsAttentionError("permission candidate binding disappeared")
            start_bound_agent(store, workspace, selected_harness, dict(current), workstream_id=str(binding["workstream_id"]), project_id=project_id, cwd=str(binding["worktree_path"]))
            _wait_permission_attestation(store, workspace, dict(current), str(operation["operation_id"]))
        result = _commit_permission_batch(store, workspace, operation=operation, approval_scope=approval_scope, batch_bindings=batch_bindings, paths=paths, domains=domains, actor=actor)
        completed = True
        refresh = {"generation": "per-binding", "upgraded": [{"workstreamId": item["workstreamId"], "generation": item["desiredGenerationSha256"]} for item in batch_bindings], "pending": [], "skipped": [], "failed": [], "ok": True}
        return {"operation": dict(store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation["operation_id"],)).fetchone()), "refresh": refresh}
    except Exception as error:
        compensated = _permission_failure(store, workspace, str(operation["operation_id"]), prepared, str(error), requested={"dataDirs": paths, "externalDomains": domains})
        raise
    finally:
        if completed or compensated:
            for _binding, selected_harness, _scope, staged, _stage_root in prepared:
                selected_harness.discard_staged_profile(staged)


@control_plane_mutation
def authorize_apply_project_permissions(store: Any, *, approval_scope: Mapping[str, Any], harness_resolver: Any, surface_resolver: Any, workspace: Any, actor: str) -> dict[str, Any]:
    project_id = str(approval_scope.get("projectId"))
    with project_permissions_lock(store.state_root, project_id):
        return _authorize_apply_project_permissions_locked(store, approval_scope=approval_scope, harness_resolver=harness_resolver, surface_resolver=surface_resolver, workspace=workspace, actor=actor)


def _permission_batch_document(operation: Mapping[str, Any]) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]] | None:
    if not operation.get("result_json"):
        return None
    try:
        value = json.loads(str(operation["result_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise NeedsAttentionError("permission batch operation record is invalid") from error
    if not isinstance(value, dict) or not isinstance(value.get("approvalScope"), dict) or not isinstance(value.get("bindings"), list):
        raise NeedsAttentionError("permission batch operation record is incomplete")
    return value["approvalScope"], value["bindings"]


def _permission_prepared_from_document(store: Any, operation: Mapping[str, Any], bindings: Sequence[Mapping[str, Any]], harness_resolver: Any) -> list[tuple[Mapping[str, Any], Any, Mapping[str, Any], StagedHarnessArtifacts, Path]]:
    prepared: list[tuple[Mapping[str, Any], Any, Mapping[str, Any], StagedHarnessArtifacts, Path]] = []
    for item in bindings:
        workstream_id = str(item["workstreamId"])
        current = store.conn.execute("SELECT r.*,w.project_id,w.kind,w.execution_profile,w.worktree_path,w.desired_state,w.provisioning_state FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=?", (workstream_id,)).fetchone()
        if current is None:
            raise NeedsAttentionError("permission batch binding disappeared", detail={"workstreamId": workstream_id})
        selected_harness = harness_resolver(workstream_id) if callable(harness_resolver) else harness_resolver
        if selected_harness is None:
            raise NeedsAttentionError("permission batch harness is unavailable", detail={"workstreamId": workstream_id})
        prior = _artifacts(item["prior"])
        binding = dict(current)
        binding.update({
            "harness_home": prior.harness_home,
            "adapter_artifacts_json": str(item.get("priorArtifactDocument") or artifact_document(selected_harness.manifest, prior)),
            "launch_secret_path": prior.launch_secret_path,
            "policy_path": prior.policy_path,
            "policy_sha256": prior.policy_sha256,
            "runtime_token_sha256": prior.runtime_token_sha256,
            "desired_generation_sha256": prior.generation_sha256,
            "applied_generation_sha256": prior.generation_sha256,
        })
        staged = _staged_from_document(str(operation["operation_id"]), item)
        scope = item.get("scope")
        if not isinstance(scope, Mapping):
            raise NeedsAttentionError("permission batch scope is invalid")
        prepared.append((binding, selected_harness, scope, staged, Path(staged.staging_root)))
    return prepared


def recover_project_permission_operation(store: Any, *, operation: Mapping[str, Any], harness_resolver: Any, workspace: Any, actor: str = "secretary") -> dict[str, Any] | None:
    """Resume a recorded permission batch or complete its recorded compensation."""
    if operation.get("kind") != "project.permissions.update" or operation.get("state") != "applying":
        return None
    document = _permission_batch_document(operation)
    if document is None:
        now = utc_now()
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='needs_attention',step='compensation',error_code='project_permission_batch_record_missing',error_message=?,updated_at=? WHERE operation_id=? AND state='applying'", ("permission batch has no durable staged artifact record", now, operation["operation_id"]))
        raise NeedsAttentionError("permission batch staged artifact record is missing")
    approval_scope, binding_documents = document
    prepared = _permission_prepared_from_document(store, operation, binding_documents, harness_resolver)
    paths = list(approval_scope.get("dataDirs", []))
    domains = list(approval_scope.get("externalDomains", []))
    if operation.get("step") == "verifying":
        try:
            result = _commit_permission_batch(store, workspace, operation=operation, approval_scope=approval_scope, batch_bindings=binding_documents, paths=paths, domains=domains, actor=actor)
        except Exception as error:
            compensated = _permission_failure(store, workspace, str(operation["operation_id"]), prepared, str(error), requested={"dataDirs": paths, "externalDomains": domains})
            if compensated:
                for _binding, selected_harness, _scope, staged, _stage_root in prepared:
                    selected_harness.discard_staged_profile(staged)
            raise
        for _binding, selected_harness, _scope, staged, _stage_root in prepared:
            selected_harness.discard_staged_profile(staged)
        return {"operation": dict(store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation["operation_id"],)).fetchone()), "refresh": {"generation": "per-binding", "upgraded": result["bindings"], "pending": [], "skipped": [], "failed": [], "ok": True}, "recovered": True}
    compensated = _permission_failure(store, workspace, str(operation["operation_id"]), prepared, "broker recovered an incomplete permission batch", requested={"dataDirs": paths, "externalDomains": domains})
    if compensated:
        for _binding, selected_harness, _scope, staged, _stage_root in prepared:
            selected_harness.discard_staged_profile(staged)
    else:
        raise NeedsAttentionError("permission batch compensation requires attention")
    raise NeedsAttentionError("permission batch was compensated before retry")


def effective_runtime_scope(store: Any, binding: Mapping[str, Any], *, harness: Any) -> dict[str, Any]:
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
    scope["externalDomains"] = list(
        compose_runtime_domains(harness, str(scope["executionProfile"]), permissions["externalDomains"])
    )
    scope["readPathSources"] = {path: [{"kind": "project_data", "sourceId": str(binding["project_id"])}] for path in permissions["dataDirs"]}
    return scope
