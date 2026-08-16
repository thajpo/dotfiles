"""Exact-scope, replay-safe Pisec workstream lifecycle."""

from __future__ import annotations

import hashlib
import re
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .adapters import HarnessAdapter, WorkspaceAdapter, WorkspaceObservation, artifact_document
from .events import append_event_in_transaction
from .git_objects import GitObjectManager
from .models import AuthorizationError, ConflictError, IdempotencyConflictError, InvalidRequestError, NeedsAttentionError, NotFoundError, ScopeMismatchError, bounded_text, canonical_json, json_digest, new_id, utc_now, validate_id
from .projects import _git, get_project
from .research import issue_task_packet_in_transaction, validate_task_packet

APPLY_LOCK = threading.RLock()
CHECKPOINTS = (
    "authorized",
    "worktree_observed_or_created",
    "git_objects_materialized",
    "profile_materialized",
    "agent_started",
    "brief_delivered",
    "observed",
    "committed",
)
_FULL_SCOPE_FIELDS = frozenset({
    "operationId", "workstreamId", "projectId", "title", "purpose", "brief",
    "harnessId", "workspaceAdapterId", "executionProfile", "targetRef", "baseCommitOid", "branchName",
    "worktreePath", "privateGitObjectDir", "gitCommonObjectDir", "agentName",
    "externalDomains", "effects", "nonEffects", "taskPacket",
})
_PUBLIC_SCOPE_FIELDS = _FULL_SCOPE_FIELDS - {"privateGitObjectDir", "gitCommonObjectDir"}


def _public_scope(scope: Mapping[str, Any]) -> dict[str, Any]:
    return {key: scope[key] for key in sorted(_PUBLIC_SCOPE_FIELDS)}


def _resolve_scope(store: Any, operation: Mapping[str, Any], supplied: Mapping[str, Any]) -> dict[str, Any]:
    try:
        proposal = json.loads(str(operation["result_json"]))
    except (TypeError, json.JSONDecodeError) as error:
        raise ScopeMismatchError("workstream proposal is invalid") from error
    if not isinstance(proposal, dict) or set(proposal) != _FULL_SCOPE_FIELDS:
        raise ScopeMismatchError("workstream proposal fields are invalid")
    if set(supplied) == _FULL_SCOPE_FIELDS:
        if canonical_json(supplied) != canonical_json(proposal):
            raise ScopeMismatchError("approval scope differs from the immutable proposal")
    elif set(supplied) == _PUBLIC_SCOPE_FIELDS:
        if canonical_json(supplied) != canonical_json(_public_scope(proposal)):
            raise ScopeMismatchError("approval scope differs from the immutable proposal")
    else:
        raise ScopeMismatchError("approval scope fields do not match the proposal contract")
    return proposal




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
    target_ref: str | None = None,
    external_domains: tuple[str, ...] | list[str] = (),
    work_root: Path | None = None,
    object_root: Path | None = None,
    failpoint: Failpoint | None = None,
) -> dict[str, Any]:
    project = get_project(store, project_id)
    idempotency_key = bounded_text(idempotency_key, name="idempotency_key", limit=256)
    title = bounded_text(title, name="title", limit=512)
    purpose = bounded_text(purpose, name="purpose", limit=4096)
    brief = bounded_text(brief, name="brief", limit=4096)
    normalized_task_packet = validate_task_packet(task_packet)
    harness.validate_execution_profile(execution_profile, "worker")
    if not isinstance(external_domains, (list, tuple)):
        raise InvalidRequestError("external domains must be a list")
    domains = list(harness.profile_domains(execution_profile, external_domains))
    selected_ref = bounded_text(target_ref or project["default_ref"], name="target_ref", limit=512)
    if selected_ref.startswith("-") or any(ord(char) < 0x20 for char in selected_ref):
        raise InvalidRequestError("target_ref contains unsafe characters")
    caller_request = {
        "projectId": project_id,
        "title": title,
        "purpose": purpose,
        "brief": brief,
        "taskPacket": normalized_task_packet,
        "harnessId": harness.manifest.adapter_id,
        "workspaceAdapterId": workspace.manifest.adapter_id,
        "executionProfile": execution_profile,
        "targetRef": selected_ref,
        "externalDomains": domains,
    }
    request_sha = json_digest(caller_request)
    existing = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    if existing is not None:
        if existing["kind"] != "workstream.create" or existing["request_sha256"] != request_sha:
            raise IdempotencyConflictError("idempotency key is already bound to another request")
        scope = json.loads(existing["result_json"])
        if not isinstance(scope, dict) or set(scope) != _FULL_SCOPE_FIELDS:
            raise ScopeMismatchError("stored workstream proposal is invalid")
        return {"operation": dict(existing), "workstream": _workstream(store, scope["workstreamId"]), "approvalScope": _public_scope(scope)}

    base_oid = _git(Path(project["repository_path"]), "rev-parse", "--verify", "--end-of-options", f"{selected_ref}^{{commit}}").lower()
    if len(base_oid) not in (40, 64) or any(char not in "0123456789abcdef" for char in base_oid):
        raise InvalidRequestError("target ref did not resolve to a full commit oid")
    operation_id = new_id("op")
    workstream_id = new_id("ws")
    branch = f"pisec/{workstream_id}/work"
    home = Path.home()
    checkout = (work_root or home / ".local" / "share" / "pisec" / "worktrees") / project_id / workstream_id
    object_dir = (object_root or home / ".local" / "state" / "pisec" / "git-objects") / project_id / workstream_id / "objects"
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
        "targetRef": selected_ref,
        "baseCommitOid": base_oid,
        "branchName": branch,
        "worktreePath": str(checkout.absolute()),
        "privateGitObjectDir": str(object_dir.absolute()),
        "gitCommonObjectDir": str((Path(project["git_common_dir"]) / "objects").absolute()),
        "agentName": agent_name,
        "externalDomains": domains,
        "effects": ["create branch and execution workspace", "register private Git object store", "start fenced harness agent", "deliver full brief"],
        "nonEffects": ["no push", "no merge", "no cleanup", "no branch deletion"],
        "taskPacket": normalized_task_packet,
    }
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "INSERT INTO workstreams(workstream_id,project_id,kind,title,purpose,brief,harness_id,workspace_adapter_id,execution_profile,target_ref,base_commit_oid,branch_name,worktree_path,desired_state,provisioning_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (workstream_id, project_id, "worker", title, purpose, brief, harness.manifest.adapter_id, workspace.manifest.adapter_id, execution_profile, selected_ref, base_oid, branch, scope["worktreePath"], "active", "proposed", now, now),
        )
        store.conn.execute(
            "INSERT INTO operations(operation_id,kind,project_id,workstream_id,idempotency_key,request_json,request_sha256,state,step,result_json,created_at,updated_at) VALUES(?,'workstream.create',?,?,?,?,?,'planned','planned',?,?,?)",
            (operation_id, project_id, workstream_id, idempotency_key, canonical_json(caller_request), request_sha, canonical_json(scope), now, now),
        )
    _hit(failpoint, "after_proposal_commit", scope)
    return {"operation": _operation(store, operation_id), "workstream": _workstream(store, workstream_id), "approvalScope": _public_scope(scope)}


def _checkpoint(store: Any, operation_id: str, step: str) -> None:
    store.conn.execute("UPDATE operations SET state='applying',step=?,updated_at=? WHERE operation_id=?", (step, utc_now(), operation_id))


def _rank(step: str) -> int:
    return CHECKPOINTS.index(step) if step in CHECKPOINTS else -1

def _wait_for_agent(
    store: Any,
    workspace: WorkspaceAdapter,
    *,
    workstream_id: str,
    path: str,
    agent_name: str,
    workspace_id: str,
    surface_id: str,
    timeout: float = 5.0,
) -> WorkspaceObservation:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    matched_observation: WorkspaceObservation | None = None
    while True:
        try:
            if matched_observation is None:
                observed = workspace.observe_workstream(path=path, agent_name=agent_name)
                if observed is not None:
                    if observed.workspace_id != workspace_id or observed.surface_id != surface_id:
                        raise NeedsAttentionError("workspace identity does not match the durable binding")
                    agent = observed.agent
                    if agent is not None:
                        if agent.name != agent_name or agent.surface_id != surface_id:
                            raise NeedsAttentionError("workspace agent identity does not match the durable binding")
                        if agent.interactive_ready is True:
                            matched_observation = observed
            if matched_observation is not None:
                runtime = store.conn.execute(
                    "SELECT runtime_instance_id,report_seq FROM runtime_bindings WHERE workstream_id=?",
                    (workstream_id,),
                ).fetchone()
                if runtime is not None and runtime["runtime_instance_id"] and int(runtime["report_seq"]) >= 1:
                    return matched_observation
        except NeedsAttentionError:
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


def _authorize_apply_workstream(
    store: Any,
    *,
    scope: Mapping[str, Any],
    harness: HarnessAdapter,
    workspace: WorkspaceAdapter,
    git_objects: GitObjectManager,
    failpoint: Failpoint | None = None,
) -> dict[str, Any]:
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
                    "INSERT INTO authorizations(authorization_id,operation_id,kind,scope_json,scope_sha256,actor,consumed_at) VALUES(?,?,'workstream.create',?,?,'secretary',?)",
                    (new_id("az"), operation_id, canonical_json(scope), json_digest(scope), now),
                )
            issue_task_packet_in_transaction(store.conn, scope=scope)
            _checkpoint(store, operation_id, "authorized")
            store.conn.execute("UPDATE workstreams SET provisioning_state='creating',updated_at=? WHERE workstream_id=?", (now, workstream_id))
        _hit(failpoint, "after_authorization_consume", scope)
        operation = _operation(store, operation_id)
    with store.transaction():
        issue_task_packet_in_transaction(store.conn, scope=scope)

    project = get_project(store, scope["projectId"])
    current_oid = _git(Path(project["repository_path"]), "rev-parse", "--verify", "--end-of-options", f"{scope['targetRef']}^{{commit}}").lower()
    if current_oid != scope["baseCommitOid"]:
        _mark_attention(store, operation_id, workstream_id, "approved target ref moved")
        raise NeedsAttentionError("approved target ref moved")
    observation = workspace.observe_workstream(path=scope["worktreePath"], agent_name=scope["agentName"])
    if _rank(operation["step"]) < _rank("worktree_observed_or_created"):
        if observation is None:
            try:
                observation = workspace.create_worktree(cwd=project["repository_path"], branch=scope["branchName"], base=scope["baseCommitOid"], path=scope["worktreePath"], label=scope["title"], focus=False)
            except Exception as error:
                try:
                    observation = workspace.observe_workstream(path=scope["worktreePath"], agent_name=scope["agentName"])
                except Exception as observe_error:
                    _mark_attention(store, operation_id, workstream_id, "workspace effect could not be re-observed")
                    raise NeedsAttentionError("workspace effect is ambiguous") from observe_error
                if observation is None:
                    try:
                        observation = workspace.create_worktree(cwd=project["repository_path"], branch=scope["branchName"], base=scope["baseCommitOid"], path=scope["worktreePath"], label=scope["title"], focus=False)
                    except Exception:
                        try:
                            observation = workspace.observe_workstream(path=scope["worktreePath"], agent_name=scope["agentName"])
                        except Exception as observe_error:
                            _mark_attention(store, operation_id, workstream_id, "workspace retry could not be observed")
                            raise NeedsAttentionError("workspace effect is ambiguous") from observe_error
                        if observation is None:
                            _mark_attention(store, operation_id, workstream_id, "workspace effect is missing after retry")
                            raise NeedsAttentionError("workspace effect is ambiguous") from error
        with store.transaction():
            _checkpoint(store, operation_id, "worktree_observed_or_created")
        _hit(failpoint, "after_workspace_creation", scope)
        operation = _operation(store, operation_id)
    if observation is None:
        _mark_attention(store, operation_id, workstream_id, "workspace effect is missing")
        raise NeedsAttentionError("workspace effect is missing")
    if observation.worktree_path is not None and str(Path(observation.worktree_path).resolve(strict=False)) != str(Path(scope["worktreePath"]).resolve(strict=False)):
        _mark_attention(store, operation_id, workstream_id, "workspace worktree differs from the approved scope")
        raise NeedsAttentionError("workspace worktree identity does not match the approved scope")
    if observation.branch_name is not None and observation.branch_name not in {scope["branchName"], f"refs/heads/{scope['branchName']}"}:
        _mark_attention(store, operation_id, workstream_id, "workspace branch differs from the approved scope")
        raise NeedsAttentionError("workspace branch identity does not match the approved scope")
    if not observation.workspace_id or not observation.view_id or not observation.surface_id:
        _mark_attention(store, operation_id, workstream_id, "workspace observation is incomplete")
        raise NeedsAttentionError("workspace observation is incomplete")

    if _rank(operation["step"]) < _rank("git_objects_materialized"):
        git_objects.materialize(scope)
        with store.transaction():
            _checkpoint(store, operation_id, "git_objects_materialized")
        _hit(failpoint, "after_binding_persistence", scope)
        operation = _operation(store, operation_id)

    if _rank(operation["step"]) < _rank("profile_materialized"):
        artifacts = harness.materialize_profile(scope)
        artifact_json = artifact_document(harness.manifest, artifacts)
        now = utc_now()
        with store.transaction():
            store.conn.execute(
                "INSERT OR REPLACE INTO runtime_bindings(workstream_id,workspace_adapter_id,workspace_session_name,workspace_id,workspace_view_id,workspace_surface_id,agent_name,harness_id,harness_home,adapter_artifacts_json,native_session_kind,native_session_value,launch_secret_path,private_git_object_dir,policy_path,policy_sha256,runtime_token_sha256,runtime_instance_id,observed_state,report_seq,workspace_report_seq,last_observed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (workstream_id, workspace.manifest.adapter_id, workspace.manifest.session_name, observation.workspace_id, observation.view_id, observation.surface_id, scope["agentName"], harness.manifest.adapter_id, artifacts.harness_home, artifact_json, None, None, artifacts.launch_secret_path, scope["privateGitObjectDir"], artifacts.policy_path, artifacts.policy_sha256, artifacts.runtime_token_sha256, None, "starting", 0, 0, None, now),
            )
        harness.commit_launch_binding(scope, artifacts)
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
        agent_observation = workspace.observe_workstream(path=scope["worktreePath"], agent_name=scope["agentName"])
        agent = agent_observation.agent if agent_observation is not None else None
        if agent is not None and (agent.name != scope["agentName"] or agent.surface_id != binding["workspace_surface_id"]):
            _mark_attention(store, operation_id, workstream_id, "workspace agent identity does not match the binding")
            raise NeedsAttentionError("workspace agent identity does not match the binding")
        start_error: Exception | None = None
        if agent is None or agent.name != scope["agentName"] or agent.surface_id != binding["workspace_surface_id"]:
            with store.transaction():
                store.conn.execute("UPDATE runtime_bindings SET runtime_instance_id=NULL,report_seq=0,observed_state='starting',updated_at=? WHERE workstream_id=?", (utc_now(), workstream_id))
            try:
                workspace.start_agent(binding["workspace_surface_id"], scope["agentName"], harness.manifest.agent_kind)
            except Exception as error:
                start_error = error
        try:
            _wait_for_agent(store, workspace, workstream_id=workstream_id, path=scope["worktreePath"], agent_name=scope["agentName"], workspace_id=binding["workspace_id"], surface_id=binding["workspace_surface_id"])
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
        try:
            _prompt_agent(workspace, binding["workspace_surface_id"], scope["brief"])
        except Exception as error:
            try:
                after_prompt = workspace.observe_workstream(path=scope["worktreePath"], agent_name=scope["agentName"])
            except Exception as observe_error:
                raise RuntimeError("brief delivery is ambiguous") from observe_error
            if after_prompt is not None and (after_prompt.workspace_id != binding["workspace_id"] or after_prompt.surface_id != binding["workspace_surface_id"] or (after_prompt.agent is not None and (after_prompt.agent.name != scope["agentName"] or after_prompt.agent.surface_id != binding["workspace_surface_id"]))):
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
        agent_mismatch = exact is not None and exact.agent is not None and (exact.agent.name != scope["agentName"] or exact.agent.surface_id != binding["workspace_surface_id"])
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
    git_objects: GitObjectManager,
    failpoint: Failpoint | None = None,
) -> dict[str, Any]:
    with APPLY_LOCK:
        try:
            return _authorize_apply_workstream(store, scope=scope, harness=harness, workspace=workspace, git_objects=git_objects, failpoint=failpoint)
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
    return [dict(row) for row in store.conn.execute("SELECT w.*,r.observed_state,r.last_observed_at,r.agent_name,t.task_packet_id,t.packet_sha256 AS task_packet_sha256 FROM workstreams w LEFT JOIN runtime_bindings r USING(workstream_id) LEFT JOIN task_packets t USING(workstream_id) WHERE w.project_id=? ORDER BY w.created_at,w.workstream_id", (project_id,))]


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


def send_workstream(store: Any, project_id: str, workstream_id: str, text: str, workspace: WorkspaceAdapter) -> dict[str, Any]:
    row = inspect_workstream(store, project_id, workstream_id)
    if row["binding"] is None or row["workstream"]["desired_state"] != "active":
        raise ConflictError("workstream is not active and bound")
    message = bounded_text(text, name="message", limit=4096)
    result = dict(workspace.prompt_agent(row["binding"]["workspace_surface_id"], message, ("working", "blocked", "idle"), 30000))
    return {"workstreamId": workstream_id, "delivered": True, "workspace": result}


def focus_workstream(store: Any, project_id: str, workstream_id: str, workspace: WorkspaceAdapter) -> dict[str, Any]:
    row = inspect_workstream(store, project_id, workstream_id)
    if row["binding"] is None or row["workstream"]["provisioning_state"] != "bound":
        raise ConflictError("workstream is not bound")
    workspace.focus_agent(row["binding"]["workspace_surface_id"])
    return {"workstreamId": workstream_id, "focused": True}


def complete_workstream(store: Any, project_id: str, workstream_id: str) -> dict[str, Any]:
    row = inspect_workstream(store, project_id, workstream_id)["workstream"]
    if row["kind"] != "worker":
        raise ConflictError("secretary workstreams cannot be completed")
    if row["provisioning_state"] != "bound":
        raise ConflictError("workstream is not bound")
    if row["desired_state"] == "retired":
        raise ConflictError("retired workstream cannot be completed")
    operation_id, existing = _lifecycle_operation(store, kind="workstream.complete", project_id=project_id, workstream_id=workstream_id)
    if existing is not None:
        if existing["state"] == "succeeded":
            return row
        if existing["state"] in {"failed", "cancelled", "needs_attention"}:
            raise NeedsAttentionError("workstream completion operation cannot be applied")
    if row["desired_state"] == "completed":
        now = utc_now()
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='succeeded',step='committed',result_json=?,updated_at=? WHERE operation_id=?", (canonical_json(row), now, operation_id))
        return row
    now = utc_now()
    with store.transaction():
        store.conn.execute("UPDATE workstreams SET desired_state='completed',completed_at=?,updated_at=? WHERE workstream_id=?", (now, now, workstream_id))
        result = {"workstreamId": workstream_id}
        store.conn.execute("UPDATE operations SET state='succeeded',step='committed',result_json=?,updated_at=? WHERE operation_id=?", (canonical_json(result), now, operation_id))
        append_event_in_transaction(store.conn, kind="workstream.completed", project_id=project_id, workstream_id=workstream_id, operation_id=operation_id, payload=result)
    return _workstream(store, workstream_id)


def retire_workstream(store: Any, project_id: str, workstream_id: str, workspace: WorkspaceAdapter) -> dict[str, Any]:
    inspected = inspect_workstream(store, project_id, workstream_id)
    row = inspected["workstream"]
    if row["kind"] != "worker":
        raise ConflictError("secretary workstreams cannot be retired through the semantic tool")
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
        if binding is not None and binding["workspace_id"]:
            workspace.close_workspace(binding["workspace_id"])
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
