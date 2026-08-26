"""Exact-scope, replay-safe Pisec workstream lifecycle."""

from __future__ import annotations

import hashlib
import os
import re
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .adapters import HarnessAdapter, RuntimeSurfaceArtifacts, WorkspaceAdapter, WorkspaceObservation, artifact_document
from .access import compose_runtime_domains
from .events import append_event_in_transaction
from .fence import resolve_data_dirs
from .models import AuthorizationError, ConflictError, IdempotencyConflictError, InvalidRequestError, NeedsAttentionError, NotFoundError, ScopeMismatchError, bounded_text, canonical_json, json_digest, new_id, utc_now, validate_id
from .projects import _git, assert_project_writable, get_project
from .project_workspaces import ensure_project_workspace
from .research import issue_task_packet_in_transaction, validate_task_packet
from .runtime_surface import capture_runtime_surface, materialize_current_surface, verify_surface
from .runtime import start_bound_agent, usable_runtime_binding
from .worker_repo import create_worker_repository, project_git_lock, project_permissions_lock, project_target_state, validate_worker_repository
APPLY_LOCK = threading.RLock()
CHECKPOINTS = (
    "authorized",
    "worker_repo_created",
    "worker_repo_verified",
    "workspace_tab_observed_or_created",
    "profile_materialized",
    "agent_started",
    "brief_delivered",
    "observed",
    "committed",
)
_FULL_SCOPE_FIELDS = frozenset({
    "operationId", "workstreamId", "projectId", "title", "purpose", "brief",
    "harnessId", "workspaceAdapterId", "executionProfile", "workMode", "learningOverlay", "learningSeam", "decisionIds",
    "targetRef", "targetBranchRef", "baseCommitOid", "branchName",
    "worktreePath", "agentName",
    "dataDirs", "externalDomains", "pythonEnv", "implementationModel", "harnessModel", "reasoningEffort", "effects", "nonEffects", "taskPacket",
    "runtimeSurfaceSha256", "runtimeSurfaceRoot", "runtimeSurfaceId", "runtimeSurfaceManifest",
})
_PUBLIC_SCOPE_FIELDS = _FULL_SCOPE_FIELDS
_OPTIONAL_SCOPE_FIELDS = frozenset({"pythonEnv", "learningSeam", "decisionIds", "implementationModel", "harnessModel", "reasoningEffort", "runtimeSurfaceSha256", "runtimeSurfaceRoot", "runtimeSurfaceId", "runtimeSurfaceManifest"})
_SCOPE_REQUIRED = _FULL_SCOPE_FIELDS - _OPTIONAL_SCOPE_FIELDS
_SURFACE_SCOPE_FIELDS = frozenset({"runtimeSurfaceSha256", "runtimeSurfaceRoot", "runtimeSurfaceId", "runtimeSurfaceManifest"})


def _public_scope(scope: Mapping[str, Any]) -> dict[str, Any]:
    return {key: scope[key] for key in sorted(_PUBLIC_SCOPE_FIELDS) if key in scope}


def _resolve_scope(store: Any, operation: Mapping[str, Any], supplied: Mapping[str, Any]) -> dict[str, Any]:
    try:
        proposal = json.loads(str(operation["result_json"]))
    except (TypeError, json.JSONDecodeError) as error:
        raise ScopeMismatchError("workstream proposal is invalid") from error
    if not isinstance(proposal, dict) or not _SCOPE_REQUIRED.issubset(proposal) or not set(proposal).issubset(_FULL_SCOPE_FIELDS):
        raise ScopeMismatchError("workstream proposal fields are invalid")
    def _non_optional(mapping: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in mapping.items() if key not in _OPTIONAL_SCOPE_FIELDS}
    if set(supplied).issubset(_FULL_SCOPE_FIELDS) and _SCOPE_REQUIRED.issubset(supplied):
        if canonical_json(_non_optional(supplied)) != canonical_json(_non_optional(proposal)):
            raise ScopeMismatchError("approval scope differs from the immutable proposal")
    elif set(supplied).issubset(_PUBLIC_SCOPE_FIELDS) and _SCOPE_REQUIRED.issubset(set(supplied) | (_FULL_SCOPE_FIELDS - _PUBLIC_SCOPE_FIELDS)):
        if canonical_json(_non_optional(supplied)) != canonical_json(_non_optional(_public_scope(proposal))):
            raise ScopeMismatchError("approval scope differs from the immutable proposal")
    else:
        raise ScopeMismatchError("approval scope fields do not match the proposal contract")
    return {key: value for key, value in proposal.items() if key not in _SURFACE_SCOPE_FIELDS}


def _surface_scope(scope: Mapping[str, Any], surface: RuntimeSurfaceArtifacts) -> dict[str, Any]:
    return {
        **scope,
        "runtimeSurfaceSha256": surface.content_sha256,
        "runtimeSurfaceRoot": surface.root_path,
        "runtimeSurfaceId": "surface_" + surface.content_sha256[:32],
        "runtimeSurfaceManifest": surface.manifest_json,
    }


def _surface_from_operation(operation: Mapping[str, Any], scope: Mapping[str, Any]) -> RuntimeSurfaceArtifacts | None:
    try:
        stored = json.loads(str(operation["result_json"]))
    except (TypeError, json.JSONDecodeError) as error:
        raise NeedsAttentionError("workstream operation snapshot is invalid") from error
    if not isinstance(stored, dict):
        raise NeedsAttentionError("workstream operation snapshot is invalid")
    if not (_SURFACE_SCOPE_FIELDS & set(stored)):
        return None
    try:
        surface = RuntimeSurfaceArtifacts(str(stored["runtimeSurfaceSha256"]), str(stored["runtimeSurfaceManifest"]), str(stored["runtimeSurfaceRoot"]))
        if stored["runtimeSurfaceId"] != "surface_" + surface.content_sha256[:32]:
            raise NeedsAttentionError("workstream runtime surface identity is invalid")
        return verify_surface(surface)
    except Exception as error:
        raise NeedsAttentionError("workstream runtime surface snapshot is invalid") from error


def _ensure_surface_snapshot(store: Any, operation: Mapping[str, Any], scope: Mapping[str, Any], harness: HarnessAdapter) -> tuple[dict[str, Any], RuntimeSurfaceArtifacts]:
    surface = _surface_from_operation(operation, scope)
    if surface is None:
        surface = capture_runtime_surface(harness)
        persisted = _surface_scope(scope, surface)
        with store.transaction():
            store.conn.execute("UPDATE operations SET result_json=?,updated_at=? WHERE operation_id=?", (canonical_json(persisted, max_bytes=256 * 1024, max_text=64 * 1024), utc_now(), operation["operation_id"]))
    return dict(scope), surface




class Failpoint(Protocol):
    def hit(self, name: str, context: Mapping[str, str]) -> None: ...


def _hit(failpoint: Failpoint | None, name: str, scope: Mapping[str, Any]) -> None:
    if failpoint is not None:
        failpoint.hit(name, {"operation_id": str(scope["operationId"]), "workstream_id": str(scope["workstreamId"])})


def _operation(store: Any, operation_id: str) -> dict[str, Any]:
    row = store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row is None:
        raise NotFoundError("workstream proposal was not found")
    return dict(row)




def _workstream(store: Any, workstream_id: str) -> dict[str, Any]:
    validate_id(workstream_id, prefix="ws")
    row = store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()
    if row is None:
        raise NotFoundError("workstream was not found")
    return dict(row)


def prepare_workstream(
    store: Any,
    *,
    project_id: str,
    title: str,
    purpose: str,
    brief: str,
    task_packet: Mapping[str, Any],
    idempotency_key: str,
    harness: HarnessAdapter,
    workspace: WorkspaceAdapter,
    execution_profile: str = "worker-default",
    work_mode: str = "BUILD",
    learning_overlay: str = "LIGHT",
    learning_seam: str | None = None,
    decision_ids: list[str] | tuple[str, ...] = (),
    target_ref: str | None = None,
    python_env: str | None = None,
    implementation_model: str | None = None,
    harness_model: str | None = None,
    reasoning_effort: str | None = None,
    work_root: Path | None = None,
    failpoint: Failpoint | None = None,
) -> dict[str, Any]:
    assert_project_writable(store, project_id)
    project = get_project(store, project_id)
    idempotency_key = bounded_text(idempotency_key, name="idempotency_key", limit=256)
    title = bounded_text(title, name="title", limit=512)
    purpose = bounded_text(purpose, name="purpose", limit=4096)
    brief = bounded_text(brief, name="brief", limit=4096)
    normalized_task_packet = validate_task_packet(task_packet)
    harness.validate_execution_profile(execution_profile, "worker")
    if execution_profile != "worker-default":
        raise InvalidRequestError("only the worker-default execution profile is supported")
    if python_env is None:
        candidate = Path(project["repository_path"]) / ".venv"
        python_env = str(candidate) if (candidate / "pyvenv.cfg").is_file() else None
    if python_env is None:
        normalized_python_env = None
    else:
        if not isinstance(python_env, str) or not python_env or len(python_env) > 4096 or "\x00" in python_env:
            raise InvalidRequestError("python env must be an absolute path")
        env_path = Path(python_env)
        if not env_path.is_absolute():
            raise InvalidRequestError("python env must be an absolute path")
        resolved_env = env_path.resolve(strict=False)
        if resolved_env != env_path:
            raise NeedsAttentionError("python env is a symlink or resolves elsewhere")
        normalized_python_env = str(resolved_env)
    selected_ref = bounded_text(target_ref or project["default_ref"], name="target_ref", limit=512)
    if selected_ref.startswith("-") or any(ord(char) < 0x20 for char in selected_ref):
        raise InvalidRequestError("target_ref contains unsafe characters")
    target_branch, target_branch_ref, base_oid = project_target_state(Path(project["repository_path"]), selected_ref)
    selected_ref = target_branch_ref
    if work_mode not in {"FAST", "RIP", "BUILD", "MAJOR"} or learning_overlay not in {"OFF", "LIGHT", "DEEP"}:
        raise InvalidRequestError("work mode or learning overlay is invalid")
    if learning_overlay == "DEEP" and learning_seam is None:
        raise InvalidRequestError("DEEP learning requires a seam")
    for value, name in ((implementation_model, "implementation model"), (harness_model, "harness model")):
        if value is not None and (not isinstance(value, str) or not value or len(value) > 256 or any(ord(char) < 0x20 for char in value)):
            raise InvalidRequestError(f"{name} is invalid")
    if reasoning_effort is not None and reasoning_effort not in {"low", "medium", "high", "xhigh"}:
        raise InvalidRequestError("reasoning effort is invalid")
    if not isinstance(decision_ids, (list, tuple)) or any(not isinstance(item, str) or not item for item in decision_ids):
        raise InvalidRequestError("decision ids are invalid")
    caller_request = {
        "projectId": project_id,
        "title": title,
        "purpose": purpose,
        "brief": brief,
        "taskPacket": normalized_task_packet,
        "harnessId": harness.manifest.adapter_id,
        "workspaceAdapterId": workspace.manifest.adapter_id,
        "executionProfile": execution_profile,
        "workMode": work_mode,
        "learningOverlay": learning_overlay,
        "learningSeam": learning_seam,
        "decisionIds": list(decision_ids),
        "targetRef": selected_ref,
        "pythonEnv": normalized_python_env,
    }
    if implementation_model is not None:
        caller_request["implementationModel"] = implementation_model
    if harness_model is not None:
        caller_request["harnessModel"] = harness_model
    if reasoning_effort is not None:
        caller_request["reasoningEffort"] = reasoning_effort
    request_sha = json_digest(caller_request)
    existing = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    if existing is not None:
        if existing["kind"] != "workstream.create" or existing["request_sha256"] != request_sha:
            raise IdempotencyConflictError("idempotency key is already bound to another request")
        scope = json.loads(existing["result_json"])
        if not isinstance(scope, dict) or not _SCOPE_REQUIRED.issubset(scope) or not set(scope).issubset(_FULL_SCOPE_FIELDS):
            raise ScopeMismatchError("stored workstream proposal is invalid")
        return {"operation": dict(existing), "workstream": _workstream(store, scope["workstreamId"]), "approvalScope": _public_scope(scope)}

    operation_id = new_id("op")
    workstream_id = new_id("ws")
    branch = f"pisec/{workstream_id}/work"
    home = Path.home()
    worktrees_root = work_root or Path(store.state_root) / "workers"
    checkout = worktrees_root / project_id / workstream_id
    agent_name = f"pisec-{workstream_id[-12:]}"
    scope = {
        "operationId": operation_id,
        "workstreamId": workstream_id,
        "projectId": project_id,
        "title": title,
        "purpose": purpose,
        "brief": brief,
        "harnessId": harness.manifest.adapter_id,
        "workspaceAdapterId": workspace.manifest.adapter_id,
        "executionProfile": execution_profile,
        "workMode": work_mode,
        "learningOverlay": learning_overlay,
        "learningSeam": learning_seam,
        "decisionIds": list(decision_ids),
        "targetRef": selected_ref,
        "targetBranchRef": target_branch_ref,
        "baseCommitOid": base_oid,
        "branchName": branch,
        "worktreePath": str(checkout.absolute()),
        "agentName": agent_name,
        "dataDirs": resolve_data_dirs(project.get("data_dirs"), Path(project["repository_path"])),
        "externalDomains": list(
            compose_runtime_domains(
                harness,
                execution_profile,
                json.loads(project.get("external_domains") or "[]"),
            )
        ),
        "pythonEnv": normalized_python_env,
        "effects": ["create independent worker repository and execution workspace", "start fenced harness agent", "deliver full brief"],
        "nonEffects": ["no push", "no merge", "no cleanup", "no branch deletion"],
        "taskPacket": normalized_task_packet,
    }
    if implementation_model is not None:
        scope["implementationModel"] = implementation_model
    if harness_model is not None:
        scope["harnessModel"] = harness_model
    if reasoning_effort is not None:
        scope["reasoningEffort"] = reasoning_effort
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "INSERT INTO workstreams(workstream_id,project_id,kind,title,purpose,brief,harness_id,workspace_adapter_id,execution_profile,target_ref,base_commit_oid,branch_name,worktree_path,desired_state,provisioning_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (workstream_id, project_id, "worker", title, purpose, brief, harness.manifest.adapter_id, workspace.manifest.adapter_id, execution_profile, selected_ref, base_oid, branch, scope["worktreePath"], "active", "proposed", now, now),
        )
        store.conn.execute(
            "INSERT INTO operations(operation_id,kind,project_id,workstream_id,idempotency_key,request_json,request_sha256,state,step,result_json,created_at,updated_at) VALUES(?,'workstream.create',?,?,?,?,?,'planned','planned',?,?,?)",
            (operation_id, project_id, workstream_id, idempotency_key, canonical_json(caller_request), request_sha, canonical_json(scope, max_bytes=256 * 1024, max_text=64 * 1024), now, now),
        )
    _hit(failpoint, "after_proposal_commit", scope)
    return {"operation": _operation(store, operation_id), "workstream": _workstream(store, workstream_id), "approvalScope": _public_scope(scope)}


def _checkpoint(store: Any, operation_id: str, step: str) -> None:
    store.conn.execute("UPDATE operations SET state='applying',step=?,updated_at=? WHERE operation_id=?", (step, utc_now(), operation_id))


def _rank(step: str) -> int:
    return CHECKPOINTS.index(step) if step in CHECKPOINTS else -1


def _session_attestation_matches(store: Any, runtime: Mapping[str, Any]) -> bool:
    event_sequence = runtime.get("session_start_event_sequence")
    report_seq = runtime.get("session_start_report_seq")
    if event_sequence is None or report_seq != runtime.get("report_seq") or not runtime.get("runtime_instance_id") or int(runtime.get("report_seq") or 0) < 1:
        return False
    event = store.conn.execute("SELECT kind,workstream_id,payload_json FROM events WHERE sequence=?", (event_sequence,)).fetchone()
    if event is None or event["kind"] != "runtime.session_started" or event["workstream_id"] is None:
        return False
    try:
        payload = json.loads(event["payload_json"])
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        event["workstream_id"] == runtime["workstream_id"]
        and isinstance(payload, dict)
        and payload.get("runtimeInstanceId") == runtime["runtime_instance_id"]
        and payload.get("generationSha256") == runtime["applied_generation_sha256"]
        and payload.get("reportSeq") == runtime["session_start_report_seq"]
        and runtime["applied_generation_sha256"] == runtime["desired_generation_sha256"]
        and runtime["launch_generation_sha256"] is None
        and not runtime["refresh_pending"]
    )

def _wait_for_agent(
    store: Any,
    workspace: WorkspaceAdapter,
    *,
    workstream_id: str,
    path: str,
    agent_name: str,
    workspace_id: str,
    view_id: str,
    surface_id: str,
    allow_unidentified_agent: bool = False,
    timeout: float = 5.0,
) -> WorkspaceObservation:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    matched_observation: WorkspaceObservation | None = None
    while True:
        try:
            if matched_observation is None:
                observed = workspace.observe_workstream(path=path, agent_name=agent_name)
                if observed is None:
                    # A repository can have several Herdr panes with the same cwd. Once the
                    # binding is committed, its workspace identity is the authoritative lookup.
                    observed = workspace.observe_surface(
                        workspace_id=workspace_id,
                        view_id=view_id,
                        surface_id=surface_id,
                        cwd=path,
                    )
                if observed is not None:
                    if observed.workspace_id != workspace_id or observed.surface_id != surface_id:
                        raise NeedsAttentionError("workspace identity does not match the durable binding")
                    agent = observed.agent
                    if agent is not None:
                        if agent.surface_id != surface_id:
                            raise NeedsAttentionError("workspace agent identity does not match the durable binding")
                        if agent.identity_usable is True:
                            matched_observation = observed
                    elif allow_unidentified_agent:
                        matched_observation = observed
            if matched_observation is not None:
                runtime = store.conn.execute(
                    "SELECT * FROM runtime_bindings WHERE workstream_id=?",
                    (workstream_id,),
                ).fetchone()
                if runtime is not None and _session_attestation_matches(store, dict(runtime)):
                    return matched_observation
        except NeedsAttentionError as error:
            raise
        except Exception as error:
            last_error = error
        if time.monotonic() >= deadline:
            if last_error is not None:
                raise NeedsAttentionError("agent start could not be observed") from last_error
            if matched_observation is not None:
                raise NeedsAttentionError("agent started without Pisec runtime attestation")
            raise NeedsAttentionError("agent start did not become active")
        time.sleep(0.05)


def _prompt_agent(workspace: WorkspaceAdapter, target: str, text: str) -> Mapping[str, Any]:
    return workspace.prompt_agent_nowait(target, text)
def _ensure_worker_repository(store: Any, project: Mapping[str, Any], scope: Mapping[str, Any]) -> None:
    project_root = Path(str(project["repository_path"]))
    target_branch = str(scope["targetBranchRef"]).removeprefix("refs/heads/")
    with project_git_lock(store.state_root, str(scope["projectId"])):
        create_worker_repository(
            primary=project_root,
            worker=Path(str(scope["worktreePath"])),
            project_id=str(scope["projectId"]),
            workstream_id=str(scope["workstreamId"]),
            target_branch_ref=str(scope["targetBranchRef"]),
            base_oid=str(scope["baseCommitOid"]),
            target_branch=target_branch,
        )
        validate_worker_repository(
            Path(str(scope["worktreePath"])),
            branch_name=str(scope["branchName"]),
            base_oid=str(scope["baseCommitOid"]),
            target_branch=target_branch,
        )


def _authorize_apply_workstream(
    store: Any,
    *,
    scope: Mapping[str, Any],
    harness: HarnessAdapter,
    workspace: WorkspaceAdapter,
    failpoint: Failpoint | None = None,
    actor: str = "secretary",
) -> dict[str, Any]:
    if actor not in {"secretary", "first_mate"}:
        raise InvalidRequestError("authorization actor is invalid")
    operation_id = validate_id(scope["operationId"], prefix="op") if isinstance(scope, Mapping) and isinstance(scope.get("operationId"), str) else None
    if operation_id is None:
        raise ScopeMismatchError("approval scope fields do not match the proposal contract")
    operation = _operation(store, operation_id)
    if operation["kind"] != "workstream.create":
        raise ScopeMismatchError("approval scope identifies another operation")
    scope = _resolve_scope(store, operation, scope)
    if scope["harnessId"] != harness.manifest.adapter_id or scope["workspaceAdapterId"] != workspace.manifest.adapter_id:
        raise NeedsAttentionError("configured adapter does not match the approved scope")
    workstream_id = validate_id(scope["workstreamId"], prefix="ws")
    if operation["workstream_id"] != workstream_id or operation["project_id"] != scope["projectId"]:
        raise ScopeMismatchError("approval scope identifies another operation")
    if operation["state"] == "succeeded":
        packet = store.conn.execute("SELECT * FROM task_packets WHERE workstream_id=?", (workstream_id,)).fetchone()
        if packet is None or packet["scope_sha256"] != json_digest(scope):
            raise NeedsAttentionError("succeeded workstream task packet is missing or drifted")
        return {"operation": operation, "workstream": _workstream(store, workstream_id)}
    if operation["state"] == "failed":
        with store.transaction():
            next_state = "planned" if operation["step"] == "planned" else "applying"
            store.conn.execute("UPDATE operations SET state=?,error_code=NULL,error_message=NULL,updated_at=? WHERE operation_id=? AND state='failed'", (next_state, utc_now(), operation_id))
        operation = _operation(store, operation_id)
    if operation["state"] in {"cancelled", "needs_attention"}:
        raise NeedsAttentionError("workstream operation cannot be applied", detail={"state": operation["state"]})
    if operation["state"] == "planned":
        now = utc_now()
        with store.transaction():
            current = _operation(store, operation_id)
            if current["state"] != "planned":
                raise ConflictError("proposal state changed during authorization")
            existing_receipt = store.conn.execute("SELECT * FROM authorizations WHERE operation_id=?", (operation_id,)).fetchone()
            if existing_receipt is None:
                store.conn.execute(
                    "INSERT INTO authorizations(authorization_id,operation_id,kind,scope_json,scope_sha256,actor,consumed_at) VALUES(?,?,'workstream.create',?,?,?,?)",
                    (new_id("az"), operation_id, canonical_json(scope, max_bytes=256 * 1024, max_text=64 * 1024), json_digest(scope), actor, now),
                )
            issue_task_packet_in_transaction(store.conn, scope=scope)
            _checkpoint(store, operation_id, "authorized")
            store.conn.execute("UPDATE workstreams SET provisioning_state='creating',updated_at=? WHERE workstream_id=?", (now, workstream_id))
        _hit(failpoint, "after_authorization_consume", scope)
        operation = _operation(store, operation_id)
    with store.transaction():
        issue_task_packet_in_transaction(store.conn, scope=scope)

    scope, surface = _ensure_surface_snapshot(store, operation, scope, harness)
    operation = _operation(store, operation_id)
    project = get_project(store, scope["projectId"])
    current_data_dirs = resolve_data_dirs(project.get("data_dirs"), Path(project["repository_path"]))
    current_external_domains = list(
        compose_runtime_domains(
            harness,
            str(scope["executionProfile"]),
            json.loads(project.get("external_domains") or "[]"),
        )
    )
    if scope["dataDirs"] != current_data_dirs or scope["externalDomains"] != current_external_domains:
        raise ScopeMismatchError("project permissions changed since worker proposal")
    current_oid = _git(Path(project["repository_path"]), "rev-parse", "--verify", "--end-of-options", f"{scope['targetRef']}^{{commit}}").lower()
    if current_oid != scope["baseCommitOid"]:
        _mark_attention(store, operation_id, workstream_id, "approved target ref moved")
        raise NeedsAttentionError("approved target ref moved")
    try:
        project_workspace = ensure_project_workspace(
            store,
            project,
            workspace,
            label=f"Project: {project['display_name']}",
            create_tab=False,
        )
    except Exception as error:
        _mark_attention(store, operation_id, workstream_id, "project workspace could not be established")
        raise NeedsAttentionError("project workspace could not be established") from error
    coordinator_workspace_id = str(project_workspace["workspace_id"])
    tab_label = f"{project['display_name']}: {scope['title']}" if project.get("coordination_mode") == "fleet" else f"Task: {scope['title']}"
    def observe_worker() -> WorkspaceObservation | None:
        return workspace.observe_tab(workspace_id=coordinator_workspace_id, cwd=scope["worktreePath"])

    def create_worker_tab() -> WorkspaceObservation:
        created = workspace.create_tab(
            workspace_id=coordinator_workspace_id,
            cwd=scope["worktreePath"],
            label=tab_label,
            focus=False,
        )
        if created.workspace_id != coordinator_workspace_id:
            raise NeedsAttentionError("worker tab create response escaped the project workspace")
        observed = observe_worker()
        if observed is None:
            raise NeedsAttentionError("created worker tab could not be corroborated")
        return observed

    observation = observe_worker()
    if _rank(operation["step"]) < _rank("worker_repo_created"):
        try:
            _ensure_worker_repository(store, project, scope)
        except Exception as error:
            _mark_attention(store, operation_id, workstream_id, f"worker repository could not be prepared: {error}")
            raise NeedsAttentionError("worker repository could not be prepared") from error
        with store.transaction():
            _checkpoint(store, operation_id, "worker_repo_created")
        _hit(failpoint, "after_worker_repo_creation", scope)
        operation = _operation(store, operation_id)
    if _rank(operation["step"]) < _rank("worker_repo_verified"):
        try:
            validate_worker_repository(
                Path(str(scope["worktreePath"])),
                branch_name=str(scope["branchName"]),
                base_oid=str(scope["baseCommitOid"]),
                target_branch=str(scope["targetBranchRef"]).removeprefix("refs/heads/"),
            )
        except Exception as error:
            _mark_attention(store, operation_id, workstream_id, f"worker repository verification failed: {error}")
            raise NeedsAttentionError("worker repository verification failed") from error
        with store.transaction():
            _checkpoint(store, operation_id, "worker_repo_verified")
        _hit(failpoint, "after_worker_repo_verification", scope)
        operation = _operation(store, operation_id)
    if _rank(operation["step"]) < _rank("workspace_tab_observed_or_created"):
        if observation is None:
            try:
                observation = create_worker_tab()
            except Exception as error:
                try:
                    observation = observe_worker()
                except Exception as observe_error:
                    _mark_attention(store, operation_id, workstream_id, "workspace tab effect could not be re-observed")
                    raise NeedsAttentionError("workspace tab effect is ambiguous") from observe_error
                if observation is None:
                    try:
                        observation = create_worker_tab()
                    except Exception:
                        try:
                            observation = observe_worker()
                        except Exception as observe_error:
                            _mark_attention(store, operation_id, workstream_id, "workspace tab retry could not be observed")
                            raise NeedsAttentionError("workspace tab effect is ambiguous") from observe_error
                        if observation is None:
                            _mark_attention(store, operation_id, workstream_id, "workspace tab effect is missing after retry")
                            raise NeedsAttentionError("workspace tab effect is ambiguous") from error
        with store.transaction():
            _checkpoint(store, operation_id, "workspace_tab_observed_or_created")
        _hit(failpoint, "after_tab_creation", scope)
        operation = _operation(store, operation_id)
    if observation is None:
        try:
            observation = observe_worker()
        except Exception as error:
            _mark_attention(store, operation_id, workstream_id, "workspace tab observation failed")
            raise NeedsAttentionError("workspace tab observation failed") from error
    if observation is None:
        _mark_attention(store, operation_id, workstream_id, "workspace tab effect is missing")
        raise NeedsAttentionError("workspace tab effect is missing")
    if observation.workspace_id != coordinator_workspace_id:
        _mark_attention(store, operation_id, workstream_id, "worker tab is outside the project workspace")
        raise NeedsAttentionError("worker tab workspace identity does not match the project workspace")
    if observation.worktree_path is not None and str(Path(observation.worktree_path).resolve(strict=False)) != str(Path(scope["worktreePath"]).resolve(strict=False)):
        _mark_attention(store, operation_id, workstream_id, "worker tab cwd differs from the approved worktree")
        raise NeedsAttentionError("worker tab cwd does not match the approved worktree")
    if observation.branch_name is not None and observation.branch_name not in {scope["branchName"], f"refs/heads/{scope['branchName']}"}:
        _mark_attention(store, operation_id, workstream_id, "worker tab branch differs from the approved scope")
        raise NeedsAttentionError("worker tab branch does not match the approved scope")
    if not observation.view_id or not observation.surface_id:
        _mark_attention(store, operation_id, workstream_id, "worker tab observation is incomplete")
        raise NeedsAttentionError("worker tab observation is incomplete")

    if _rank(operation["step"]) < _rank("profile_materialized"):
        scope, surface = _ensure_surface_snapshot(store, operation, scope, harness)
        artifacts, _surface, materialized_scope = materialize_current_surface(store, harness, scope, surface=surface)
        artifact_json = artifact_document(harness.manifest, artifacts)
        now = utc_now()
        with store.transaction():
            store.conn.execute(
                "INSERT OR REPLACE INTO runtime_bindings(workstream_id,workspace_adapter_id,workspace_session_name,workspace_id,workspace_view_id,workspace_surface_id,agent_name,harness_id,harness_home,adapter_artifacts_json,native_session_kind,native_session_value,launch_secret_path,policy_path,policy_sha256,runtime_token_sha256,desired_generation_sha256,applied_generation_sha256,launch_generation_sha256,runtime_instance_id,observed_state,report_seq,workspace_report_seq,last_observed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (workstream_id, workspace.manifest.adapter_id, workspace.manifest.session_name, observation.workspace_id, observation.view_id, observation.surface_id, scope["agentName"], harness.manifest.adapter_id, artifacts.harness_home, artifact_json, None, None, artifacts.launch_secret_path, artifacts.policy_path, artifacts.policy_sha256, artifacts.runtime_token_sha256, artifacts.generation_sha256, None, artifacts.generation_sha256, None, "starting", 0, 0, None, now),
            )
        harness.commit_launch_binding(
            materialized_scope,
            artifacts,
            workspace_session_name=workspace.manifest.session_name,
            workspace_id=observation.workspace_id,
            workspace_view_id=observation.view_id,
            workspace_surface_id=observation.surface_id,
        )
        with store.transaction():
            _checkpoint(store, operation_id, "profile_materialized")
        _hit(failpoint, "after_policy_map_materialization", scope)
        operation = _operation(store, operation_id)

    binding_row = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()
    if binding_row is None:
        _mark_attention(store, operation_id, workstream_id, "runtime binding is missing")
        raise NeedsAttentionError("runtime binding is missing")
    binding = dict(binding_row)
    if _rank(operation["step"]) < _rank("agent_started"):
        start_error: Exception | None = None
        try:
            start_bound_agent(
                store,
                workspace,
                harness,
                binding,
                workstream_id=workstream_id,
                project_id=str(scope["projectId"]),
                cwd=str(scope["worktreePath"]),
            )
        except Exception as error:
            start_error = error
        try:
            _wait_for_agent(store, workspace, workstream_id=workstream_id, path=scope["worktreePath"], agent_name=scope["agentName"], workspace_id=binding["workspace_id"], view_id=binding["workspace_view_id"], surface_id=binding["workspace_surface_id"], allow_unidentified_agent=bool(getattr(harness, "allow_unidentified_agent", False)))
        except NeedsAttentionError as wait_error:
            _mark_attention(store, operation_id, workstream_id, str(wait_error))
            if start_error is not None:
                raise wait_error from start_error
            raise
        with store.transaction():
            _checkpoint(store, operation_id, "agent_started")
        _hit(failpoint, "after_agent_start", scope)
        operation = _operation(store, operation_id)
    if _rank(operation["step"]) < _rank("brief_delivered"):
        if not bool(getattr(harness, "launches_with_brief", False)):
            try:
                _prompt_agent(workspace, binding["workspace_surface_id"], scope["brief"])
            except Exception as error:
                try:
                    after_prompt = workspace.observe_workstream(path=scope["worktreePath"], agent_name=scope["agentName"])
                except Exception as observe_error:
                    raise RuntimeError("brief delivery is ambiguous") from observe_error
                if after_prompt is not None and (after_prompt.workspace_id != binding["workspace_id"] or after_prompt.surface_id != binding["workspace_surface_id"] or (after_prompt.agent is not None and after_prompt.agent.surface_id != binding["workspace_surface_id"])):
                    _mark_attention(store, operation_id, workstream_id, "brief target identity does not match the binding")
                    raise NeedsAttentionError("brief target identity does not match the binding")
                try:
                    _prompt_agent(workspace, binding["workspace_surface_id"], scope["brief"])
                except Exception:
                    raise RuntimeError("brief delivery remained ambiguous") from error
        with store.transaction():
            _checkpoint(store, operation_id, "brief_delivered")
        _hit(failpoint, "after_brief_delivery", scope)
        operation = _operation(store, operation_id)

    if _rank(operation["step"]) < _rank("observed"):
        exact = workspace.observe_workstream(path=scope["worktreePath"], agent_name=scope["agentName"])
        agent_mismatch = exact is not None and exact.agent is not None and exact.agent.surface_id != binding["workspace_surface_id"]
        if exact is None or exact.workspace_id != binding["workspace_id"] or exact.surface_id != binding["workspace_surface_id"] or agent_mismatch:
            _mark_attention(store, operation_id, workstream_id, "workspace identity does not match the durable binding")
            raise NeedsAttentionError("workspace identity does not match the durable binding")
        with store.transaction():
            _checkpoint(store, operation_id, "observed")
        operation = _operation(store, operation_id)

    _hit(failpoint, "before_final_event_commit", scope)
    if _rank(operation["step"]) < _rank("committed"):
        now = utc_now()
        with store.transaction():
            store.conn.execute("UPDATE workstreams SET provisioning_state='bound',updated_at=? WHERE workstream_id=? AND provisioning_state='creating'", (now, workstream_id))
            if not usable_runtime_binding(
                store,
                workstream_id,
                workspace,
                harness,
                allowed_states=frozenset({"idle", "working", "blocked"}),
            ):
                raise NeedsAttentionError("worker runtime binding is not usable at finalization")
            if not usable_runtime_binding(
                store,
                workstream_id,
                workspace,
                harness,
                allowed_states=frozenset({"idle", "working", "blocked"}),
            ):
                raise NeedsAttentionError("worker runtime identity changed before finalization")
            packet = issue_task_packet_in_transaction(store.conn, scope=scope)
            result = {"workstreamId": workstream_id, "projectId": scope["projectId"], "workspaceId": binding["workspace_id"], "viewId": binding["workspace_view_id"], "surfaceId": binding["workspace_surface_id"], "agentName": scope["agentName"], "taskPacketId": packet["task_packet_id"], "taskPacketSha256": packet["packet_sha256"]}
            store.conn.execute("UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=?", (now, workstream_id))
            store.conn.execute("UPDATE operations SET state='succeeded',step='committed',updated_at=? WHERE operation_id=?", (now, operation_id))
            append_event_in_transaction(store.conn, kind="workstream.created", project_id=scope["projectId"], workstream_id=workstream_id, operation_id=operation_id, payload=result)
        _hit(failpoint, "after_final_event_commit", scope)
    else:
        packet = store.conn.execute("SELECT * FROM task_packets WHERE workstream_id=?", (workstream_id,)).fetchone()
        if packet is None:
            _mark_attention(store, operation_id, workstream_id, "committed workstream has no immutable task packet")
            raise NeedsAttentionError("committed workstream has no immutable task packet")
    return {"operation": _operation(store, operation_id), "workstream": _workstream(store, workstream_id)}
def authorize_apply_workstream(
    store: Any,
    *,
    scope: Mapping[str, Any],
    harness: HarnessAdapter,
    workspace: WorkspaceAdapter,
    failpoint: Failpoint | None = None,
    actor: str = "secretary",
) -> dict[str, Any]:
    assert_project_writable(store, str(scope["projectId"]))
    with project_permissions_lock(store.state_root, str(scope["projectId"])), APPLY_LOCK:
        try:
            return _authorize_apply_workstream(store, scope=scope, harness=harness, workspace=workspace, failpoint=failpoint, actor=actor)
        except NeedsAttentionError as error:
            operation_id = scope.get("operationId") if isinstance(scope, Mapping) else None
            if isinstance(operation_id, str):
                row = store.conn.execute("SELECT state,step,workstream_id FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
                if row is not None and row["step"] != "planned" and row["state"] in {"planned", "applying"} and isinstance(row["workstream_id"], str):
                    _mark_attention(store, operation_id, row["workstream_id"], str(error)[:512] or "workstream effect requires attention")
            raise
        except Exception as error:
            operation_id = scope.get("operationId") if isinstance(scope, Mapping) else None
            if isinstance(operation_id, str):
                with store.transaction():
                    row = store.conn.execute("SELECT state FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
                    if row is not None and row["state"] in {"planned", "applying"}:
                        message = str(error)[:512] or "workstream apply failed"
                        store.conn.execute("UPDATE operations SET state='failed',error_code='apply_failed',error_message=?,updated_at=? WHERE operation_id=?", (message, utc_now(), operation_id))
            raise


def _mark_attention(store: Any, operation_id: str, workstream_id: str, reason: str) -> None:
    with store.transaction():
        now = utc_now()
        store.conn.execute("UPDATE operations SET state='needs_attention',error_code='effect_mismatch',error_message=?,updated_at=? WHERE operation_id=?", (reason, now, operation_id))
        store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?", (reason, now, workstream_id))


def _lifecycle_operation(store: Any, *, kind: str, project_id: str, workstream_id: str) -> tuple[str, dict[str, Any] | None]:
    request = {"kind": kind, "projectId": project_id, "workstreamId": workstream_id}
    key = f"{kind}:{workstream_id}"
    request_sha = json_digest(request)
    existing = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (key,)).fetchone()
    if existing is not None:
        if existing["request_sha256"] != request_sha:
            raise IdempotencyConflictError("lifecycle idempotency key is bound to another request")
        return str(existing["operation_id"]), dict(existing)
    operation_id = new_id("op")
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "INSERT INTO operations(operation_id,kind,project_id,workstream_id,idempotency_key,request_json,request_sha256,state,step,created_at,updated_at) VALUES(?,?,?, ?,?,?,?,'applying','planned',?,?)",
            (operation_id, kind, project_id, workstream_id, key, canonical_json(request), request_sha, now, now),
        )
    return operation_id, None

def list_workstreams(store: Any, project_id: str) -> list[dict[str, Any]]:
    get_project(store, project_id)
    return [dict(row) for row in store.conn.execute("SELECT w.*,r.observed_state,r.last_observed_at,r.agent_name,r.desired_generation_sha256,r.applied_generation_sha256,CASE WHEN r.desired_generation_sha256 IS NOT r.applied_generation_sha256 THEN 1 ELSE 0 END AS runtime_stale,t.task_packet_id,t.packet_sha256 AS task_packet_sha256 FROM workstreams w LEFT JOIN runtime_bindings r USING(workstream_id) LEFT JOIN task_packets t USING(workstream_id) WHERE w.project_id=? ORDER BY w.created_at,w.workstream_id", (project_id,))]


def inspect_workstream(store: Any, project_id: str, workstream_id: str) -> dict[str, Any]:
    row = _workstream(store, workstream_id)
    if row["project_id"] != project_id:
        raise NotFoundError("workstream was not found in the project")
    packet = store.conn.execute("SELECT task_packet_id,packet_sha256 FROM task_packets WHERE workstream_id=?", (workstream_id,)).fetchone()
    if packet is not None:
        row["task_packet_id"] = packet["task_packet_id"]
        row["task_packet_sha256"] = packet["packet_sha256"]
    binding = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()
    operation = store.conn.execute("SELECT * FROM operations WHERE workstream_id=? ORDER BY created_at DESC LIMIT 1", (workstream_id,)).fetchone()
    return {"workstream": row, "binding": None if binding is None else dict(binding), "operation": None if operation is None else dict(operation)}


def focus_workstream(store: Any, project_id: str, workstream_id: str, workspace: WorkspaceAdapter) -> dict[str, Any]:
    row = inspect_workstream(store, project_id, workstream_id)
    if row["binding"] is None or row["workstream"]["provisioning_state"] != "bound":
        raise ConflictError("workstream is not bound")
    workspace.focus_pane(row["binding"]["workspace_surface_id"])
    return {"workstreamId": workstream_id, "focused": True}


def complete_workstream(
    store: Any,
    project_id: str,
    workstream_id: str,
    completion_packet_sha256: str | None = None,
    workspace: WorkspaceAdapter | None = None,
) -> dict[str, Any]:
    from .projects import assert_project_writable
    assert_project_writable(store, project_id)
    inspected = inspect_workstream(store, project_id, workstream_id)
    row = inspected["workstream"]
    if row["kind"] != "worker":
        raise ConflictError("secretary workstreams cannot be completed")
    if completion_packet_sha256 is None:
        raise InvalidRequestError("completion packet digest is required")
    packet = store.conn.execute("SELECT * FROM completion_packets WHERE workstream_id=? AND packet_sha256=?", (workstream_id, completion_packet_sha256)).fetchone()
    if packet is None:
        raise ConflictError("completion packet was not found")
    accepted = store.conn.execute(
        "SELECT 1 FROM workstream_acceptances a LEFT JOIN integration_jobs j ON j.acceptance_id=a.acceptance_id AND j.candidate_completion_packet_id=? WHERE a.workstream_id=? AND (a.completion_packet_id=? OR j.integration_id IS NOT NULL) LIMIT 1",
        (packet["completion_packet_id"], workstream_id, packet["completion_packet_id"]),
    ).fetchone()
    if accepted is None:
        raise ConflictError("completion packet has not been accepted")
    blockers = store.conn.execute(
        "SELECT 1 FROM coordination_requests WHERE workstream_id=? AND blocking=1 AND state <> 'acknowledged' LIMIT 1",
        (workstream_id,),
    ).fetchone()
    if blockers is not None:
        raise ConflictError("workstream has unresolved blocking coordination")
    if row["desired_state"] == "retired":
        raise ConflictError("retired workstream cannot be completed")
    if row["desired_state"] == "completed":
        return row
    operation_id, existing = _lifecycle_operation(store, kind="workstream.complete", project_id=project_id, workstream_id=workstream_id)
    if existing is not None and existing["state"] == "succeeded":
        return _workstream(store, workstream_id)
    binding = inspected["binding"]
    if binding is None or workspace is None:
        raise ConflictError("completion requires a bound runtime workspace")
    try:
        observed = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
        if observed.state != "stopped":
            workspace.stop_runtime(str(binding["workspace_surface_id"]))
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                observed = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
                if observed.state == "stopped":
                    break
                time.sleep(0.05)
        if observed.state != "stopped":
            raise NeedsAttentionError("workstream runtime did not stop after completion request")
        now = utc_now()
        with store.transaction():
            store.conn.execute("UPDATE runtime_bindings SET observed_state='stopped',updated_at=? WHERE workstream_id=?", (now, workstream_id))
            store.conn.execute("UPDATE workstreams SET desired_state='completed',completed_at=?,updated_at=? WHERE workstream_id=?", (now, now, workstream_id))
            result = {"workstreamId": workstream_id, "completionPacketSha256": completion_packet_sha256, "sourceCommit": packet["source_commit_oid"]}
            store.conn.execute("UPDATE operations SET state='succeeded',step='committed',result_json=?,updated_at=? WHERE operation_id=?", (canonical_json(result), now, operation_id))
            append_event_in_transaction(store.conn, kind="workstream.completed", project_id=project_id, workstream_id=workstream_id, operation_id=operation_id, payload=result)
        return _workstream(store, workstream_id)
    except (ConflictError, InvalidRequestError, NeedsAttentionError):
        raise
    except Exception as error:
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='needs_attention',error_code='completion_failed',error_message=?,updated_at=? WHERE operation_id=?", (str(error)[:512], utc_now(), operation_id))
        raise NeedsAttentionError("workstream completion requires attention") from error


def retire_workstream(store: Any, project_id: str, workstream_id: str, workspace: WorkspaceAdapter, *, actor_workstream_id: str | None = None, remediation_issue_id: str | None = None, failure_reason: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
    inspected = inspect_workstream(store, project_id, workstream_id)
    row = inspected["workstream"]
    if row["kind"] != "worker":
        raise ConflictError("secretary workstreams cannot be retired through the semantic tool")
    failure_form = any(value is not None for value in (remediation_issue_id, failure_reason, idempotency_key))
    if failure_form and not all(isinstance(value, str) and value for value in (remediation_issue_id, failure_reason, idempotency_key)):
        raise InvalidRequestError("remediation-failure retirement requires all exact fields")
    if failure_form:
        from .workflow import _append_issue_update, _issue_row
        if actor_workstream_id is None or store.conn.execute("SELECT 1 FROM projects WHERE project_id=? AND secretary_workstream_id=? AND active=1", (project_id, actor_workstream_id)).fetchone() is None:
            raise ConflictError("only the active project Secretary may use remediation-failure retirement")
        failure_reason = bounded_text(failure_reason, name="failureReason", limit=4096)
        issue = _issue_row(store, str(remediation_issue_id), project_id)
        if issue["reporter_kind"] != "worker" or store.conn.execute("SELECT 1 FROM issue_remediations WHERE issue_id=? AND workstream_id=?", (remediation_issue_id, workstream_id)).fetchone() is None:
            raise ConflictError("remediation issue is not immutably linked to this worker")
        for table in ("completion_packets", "workstream_acceptances", "integration_jobs", "merge_receipts"):
            if store.conn.execute(f"SELECT 1 FROM {table} WHERE workstream_id=? LIMIT 1", (workstream_id,)).fetchone() is not None:
                raise ConflictError("remediation-failure retirement requires an unaccepted worker")
        request = {"kind": "workstream.retire", "projectId": project_id, "workstreamId": workstream_id, "remediationIssueId": remediation_issue_id, "failureReason": failure_reason}
        request_sha = json_digest(request)
        existing_operation = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing_operation is not None:
            if existing_operation["request_sha256"] != request_sha:
                raise IdempotencyConflictError("retirement idempotency key is bound to another failure payload")
            if existing_operation["state"] == "succeeded":
                return _workstream(store, workstream_id)
            raise NeedsAttentionError("remediation-failure retirement operation requires attention")
        operation_id = new_id("op")
        now = utc_now()
        with store.transaction():
            store.conn.execute("INSERT INTO operations(operation_id,kind,project_id,workstream_id,idempotency_key,request_json,request_sha256,state,step,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'applying','planned',?,?)", (operation_id, "workstream.retire", project_id, workstream_id, idempotency_key, canonical_json(request), request_sha, now, now))
        binding = inspected["binding"]
        if binding is None or binding["provisioning_state"] != "bound" or binding["observed_state"] in {"starting", "working", "blocked"}:
            with store.transaction():
                store.conn.execute("UPDATE operations SET state='needs_attention',error_code='retire_ambiguous',error_message=?,updated_at=? WHERE operation_id=?", ("remediation-failure retirement requires a confirmed stopped runtime", utc_now(), operation_id))
                store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?", ("remediation-failure retirement requires a confirmed stopped runtime", utc_now(), workstream_id))
            raise NeedsAttentionError("remediation-failure retirement requires a confirmed stopped runtime")
        try:
            runtime = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
            if runtime.state != "stopped":
                workspace.stop_runtime(str(binding["workspace_surface_id"]))
                runtime = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
            if runtime.state != "stopped":
                raise NeedsAttentionError("remediation-failure retirement runtime stop is ambiguous")
            if binding["workspace_view_id"]:
                workspace.close_tab(binding["workspace_view_id"])
            linked = [issue]
            if issue["escalated_from_issue_id"] is not None:
                linked.append(_issue_row(store, str(issue["escalated_from_issue_id"]), project_id))
            with store.transaction():
                now = utc_now()
                result = {"workstreamId": workstream_id, "remediationIssueId": remediation_issue_id, "failureReason": failure_reason, "branchRetained": row["branch_name"], "checkoutRetained": row["worktree_path"]}
                store.conn.execute("UPDATE runtime_bindings SET observed_state='stopped',updated_at=? WHERE workstream_id=?", (now, workstream_id))
                store.conn.execute("UPDATE workstreams SET desired_state='retired',retired_at=?,updated_at=? WHERE workstream_id=?", (now, now, workstream_id))
                store.conn.execute("UPDATE operations SET state='succeeded',step='committed',result_json=?,updated_at=? WHERE operation_id=?", (canonical_json(result), now, operation_id))
                append_event_in_transaction(store.conn, kind="workstream.retired", project_id=project_id, workstream_id=workstream_id, operation_id=operation_id, payload=result)
                for linked_issue in linked:
                    changed = _append_issue_update(store, issue=linked_issue, actor_kind="secretary", actor_id=actor_workstream_id, update_kind="remediation_failed", payload={"workstreamId": workstream_id, "failureReason": failure_reason}, idempotency_key=f"{idempotency_key}:{linked_issue['issue_id']}")
                    store.conn.execute("UPDATE issues SET state='acknowledged',updated_at=? WHERE issue_id=?", (now, linked_issue["issue_id"]))
                    if changed:
                        append_event_in_transaction(store.conn, kind="issue.remediation_failed", project_id=project_id, workstream_id=actor_workstream_id, payload={"issueId": linked_issue["issue_id"], "workstreamId": workstream_id})
            return _workstream(store, workstream_id)
        except NeedsAttentionError:
            raise
        except Exception as error:
            with store.transaction():
                store.conn.execute("UPDATE operations SET state='needs_attention',error_code='retire_failed',error_message=?,updated_at=? WHERE operation_id=?", (str(error)[:512], utc_now(), operation_id))
                store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?", (str(error)[:512], utc_now(), workstream_id))
            raise NeedsAttentionError("remediation-failure retirement requires attention") from error
    if row["desired_state"] not in {"completed", "retired"}:
        raise ConflictError("workstream must be completed before retirement")
    if row["provisioning_state"] != "bound":
        raise ConflictError("workstream is not bound")
    operation_id, existing = _lifecycle_operation(store, kind="workstream.retire", project_id=project_id, workstream_id=workstream_id)
    if existing is not None:
        if existing["state"] == "succeeded":
            return row
        if existing["state"] in {"failed", "cancelled", "needs_attention"}:
            raise NeedsAttentionError("workstream retirement operation cannot be applied")
    if row["desired_state"] == "retired":
        now = utc_now()
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='succeeded',step='committed',result_json=?,updated_at=? WHERE operation_id=?", (canonical_json(row), now, operation_id))
        return row
    binding = inspected["binding"]
    if binding is not None and binding["observed_state"] in {"starting", "working", "blocked"}:
        raise ConflictError("workstream runtime is still active")
    try:
        if binding is not None and binding["workspace_view_id"]:
            workspace.close_tab(binding["workspace_view_id"])
        now = utc_now()
        with store.transaction():
            result = {"workstreamId": workstream_id, "branchRetained": row["branch_name"], "checkoutRetained": row["worktree_path"]}
            store.conn.execute("UPDATE runtime_bindings SET observed_state='stopped',updated_at=? WHERE workstream_id=?", (now, workstream_id))
            store.conn.execute("UPDATE workstreams SET desired_state='retired',retired_at=?,updated_at=? WHERE workstream_id=?", (now, now, workstream_id))
            store.conn.execute("UPDATE operations SET state='succeeded',step='committed',result_json=?,updated_at=? WHERE operation_id=?", (canonical_json(result), now, operation_id))
            append_event_in_transaction(store.conn, kind="workstream.retired", project_id=project_id, workstream_id=workstream_id, operation_id=operation_id, payload=result)
        return _workstream(store, workstream_id)
    except ConflictError:
        raise
    except Exception as error:
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='needs_attention',error_code='retire_failed',error_message=?,updated_at=? WHERE operation_id=?", (str(error)[:512], utc_now(), operation_id))
            store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?", (str(error)[:512], utc_now(), workstream_id))
        raise NeedsAttentionError("workstream retirement requires attention") from error
