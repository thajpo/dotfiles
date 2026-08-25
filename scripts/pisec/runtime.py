"""Harness-neutral runtime authentication and monotonic attestation."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
import re
from typing import Any, Mapping

from .adapters import HarnessAdapter, WorkspaceAdapter
from .events import append_event_in_transaction
from .worker_repo import validate_worker_resume_git
from .models import AuthorizationError, ConflictError, InvalidRequestError, NeedsAttentionError, bounded_text, validate_id, validate_sha256
from .models import utc_now

RUNTIME_FIELDS = frozenset({"workstreamId", "runtimeInstanceId", "seq", "event", "reason", "state", "nativeSessionKind", "nativeSessionValue", "startSource", "surfaceId", "token", "generation"})
RUNTIME_AUTH_FIELDS = frozenset({"workstreamId", "runtimeInstanceId", "surfaceId", "token"})
SESSION_SWITCH_REASONS = frozenset({"new", "resume", "fork", "handoff"})
OBSERVED_STATES = frozenset({"unknown", "starting", "working", "blocked", "idle", "done", "stopped", "missing", "error"})

WORKSPACE_RUNTIME_MISSING = "workspace runtime is missing"
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")
TOOL_FAILURE_CODES = frozenset({"tool_error", "tool_timeout", "tool_cancelled", "tool_unknown"})
def start_bound_agent(
    store: Any,
    workspace: WorkspaceAdapter,
    harness: HarnessAdapter,
    binding: Mapping[str, Any],
    *,
    workstream_id: str,
    project_id: str,
    cwd: str,
) -> Mapping[str, Any]:
    if not isinstance(binding, Mapping):
        raise InvalidRequestError("runtime binding is invalid")
    row = store.conn.execute(
        "SELECT r.*,w.project_id,w.kind,w.execution_profile,w.worktree_path,w.desired_state,w.provisioning_state "
        "FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=?",
        (workstream_id,),
    ).fetchone()
    if row is None:
        raise ConflictError("runtime binding is missing")
    validate_worker_resume_git(store, dict(row))
    expected_role = "secretary" if row["kind"] == "secretary" else "first_mate" if row["kind"] == "first_mate" else "worker"
    harness.validate_execution_profile(row["execution_profile"], expected_role)
    expected = {
        "workstream_id": workstream_id,
        "project_id": project_id,
        "workspace_session_name": workspace.manifest.session_name,
        "workspace_id": binding.get("workspace_id"),
        "workspace_view_id": binding.get("workspace_view_id"),
        "workspace_surface_id": binding.get("workspace_surface_id"),
        "harness_id": harness.manifest.adapter_id,
        "worktree_path": cwd,
    }
    for key, value in expected.items():
        if key == "worktree_path":
            if str(Path(str(row[key])).resolve(strict=False)) != str(Path(str(value)).resolve(strict=False)):
                raise NeedsAttentionError("runtime binding cwd does not match the approved root")
        elif row[key] != value:
            raise NeedsAttentionError("runtime binding identity does not match durable state")
    if row["desired_state"] != "active" or row["provisioning_state"] not in {"creating", "bound", "needs_attention"}:
        raise NeedsAttentionError("runtime binding is not active")
    harness.validate_native_session(dict(row), row["native_session_kind"], row["native_session_value"])
    observation = workspace.observe_surface(
        workspace_id=str(row["workspace_id"]),
        view_id=str(row["workspace_view_id"]),
        surface_id=str(row["workspace_surface_id"]),
        cwd=str(cwd),
    )
    if observation is None:
        raise NeedsAttentionError("runtime binding pane is missing")
    if observation.agent is not None:
        expected_names = {str(row["agent_name"]), harness.manifest.agent_kind}
        if observation.agent.name not in expected_names or observation.agent.surface_id != row["workspace_surface_id"]:
            raise NeedsAttentionError("runtime binding agent identity does not match")
    runtime = workspace.observe_runtime(str(row["workspace_surface_id"]), str(row["policy_path"]))
    if runtime.state == "live":
        return {"launched": False, "observation": observation}
    if runtime.state != "stopped":
        raise NeedsAttentionError("runtime binding pane process identity is ambiguous")
    launcher = harness.launch_binding_path(workstream_id)
    argv = [str(launcher)]
    if row["native_session_kind"] is not None:
        argv.append(f"--resume={row['native_session_value']}")
    workspace.run_command(
        str(row["workspace_surface_id"]),
        argv,
        env={"HERDR_SESSION": workspace.manifest.session_name, "HERDR_PANE_ID": str(row["workspace_surface_id"])},
    )
    return {"launched": True, "observation": observation}


def verify_runtime_binding(store: Any, payload: Mapping[str, Any], *, worker_only: bool = False, allow_session_start: bool = False) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or not RUNTIME_AUTH_FIELDS <= set(payload):
        raise InvalidRequestError("runtime binding fields are incomplete")
    workstream_id = validate_id(payload["workstreamId"], prefix="ws")
    instance = payload["runtimeInstanceId"]
    surface_id = payload["surfaceId"]
    token = payload["token"]
    generation = validate_sha256(payload.get("generation"), "runtime generation")
    if not isinstance(instance, str) or not instance or len(instance) > 128 or "\x00" in instance:
        raise InvalidRequestError("runtime instance id is invalid")
    if not isinstance(surface_id, str) or not surface_id or len(surface_id) > 256 or "\x00" in surface_id:
        raise InvalidRequestError("runtime surface id is invalid")
    if not isinstance(token, str) or len(token) < 32 or len(token) > 512 or "\x00" in token:
        raise AuthorizationError("runtime token is invalid")
    row = store.conn.execute(
        "SELECT r.*,w.project_id,w.kind,w.execution_profile,w.desired_state,w.provisioning_state FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=?",
        (workstream_id,),
    ).fetchone()
    if row is None or row["desired_state"] == "retired" or row["provisioning_state"] not in {"bound", "creating"}:
        raise AuthorizationError("runtime binding is inactive")
    if worker_only and (row["kind"] != "worker" or row["execution_profile"] in {"first-mate", "secretary-project"}):
        raise AuthorizationError("runtime operation requires a worker binding")
    if not hmac.compare_digest(row["runtime_token_sha256"], hashlib.sha256(token.encode("utf-8")).hexdigest()):
        raise AuthorizationError("runtime token is invalid")
    if row["workspace_surface_id"] != surface_id:
        raise AuthorizationError("runtime surface does not match its binding")
    if not allow_session_start and row["runtime_instance_id"] != instance:
        raise ConflictError("runtime instance is stale")
    expected_generation = row["launch_generation_sha256"] if allow_session_start and row["launch_generation_sha256"] else row["applied_generation_sha256"]
    if expected_generation != generation:
        raise ConflictError("runtime generation is stale or does not match the reserved launch")
    return dict(row)


def record_runtime_tool_failure(store: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    binding = verify_runtime_binding(store, payload)
    tool_name = bounded_text(payload.get("toolName"), name="toolName", limit=128)
    if _TOOL_NAME_RE.fullmatch(tool_name) is None:
        raise InvalidRequestError("toolName contains invalid characters")
    failure_code = payload.get("failureCode")
    if failure_code not in TOOL_FAILURE_CODES:
        raise InvalidRequestError("failureCode is invalid")
    with store.transaction():
        event = append_event_in_transaction(
            store.conn,
            kind="runtime.tool_failed",
            project_id=str(binding["project_id"]),
            workstream_id=str(binding["workstream_id"]),
            payload={"toolName": tool_name, "failureCode": failure_code},
        )
    return {"recorded": True, "eventId": event["event_id"], "workstreamId": binding["workstream_id"]}

def prepare_session_switch(store: Any, payload: Mapping[str, Any], harness: HarnessAdapter) -> dict[str, Any]:
    binding = verify_runtime_binding(store, payload, allow_session_start=False)
    reason = payload.get("reason")
    target = payload.get("targetSessionFile")
    if reason not in SESSION_SWITCH_REASONS:
        raise InvalidRequestError("session switch reason is invalid")
    if reason == "resume":
        if not isinstance(target, str) or not target:
            raise InvalidRequestError("session resume target is required")
        harness.validate_native_session(binding, "path", target)
    elif target is not None:
        raise InvalidRequestError("new or fork session switch must not include a target")
    return {
        "prepared": True,
        "workstreamId": str(binding["workstream_id"]),
        "reason": reason,
    }


def report_runtime(store: Any, payload_value: Mapping[str, Any], harness: HarnessAdapter, workspace: WorkspaceAdapter) -> dict[str, Any]:
    if not isinstance(payload_value, Mapping) or set(payload_value) != RUNTIME_FIELDS:
        raise InvalidRequestError("runtime report fields do not match protocol version 1")
    payload = dict(payload_value)
    generation = validate_sha256(payload["generation"], "runtime generation")
    event = payload["event"]
    reason = payload["reason"]
    state = payload["state"]
    seq = payload["seq"]
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise InvalidRequestError("runtime report sequence is invalid")
    if event not in {"session_start", "lifecycle", "session_shutdown"} or state not in OBSERVED_STATES:
        raise InvalidRequestError("runtime report event or state is invalid")
    if event in {"session_start", "session_shutdown"}:
        if reason is not None:
            raise InvalidRequestError("runtime report reason is invalid")
    elif reason is not None and (not isinstance(reason, str) or reason not in SESSION_SWITCH_REASONS):
        raise InvalidRequestError("runtime report reason is invalid")
    binding = verify_runtime_binding(store, payload, allow_session_start=event == "session_start")
    workstream_id = str(binding["workstream_id"])
    instance = str(payload["runtimeInstanceId"])
    if event == "session_start":
        if seq != 1:
            raise ConflictError("new runtime instance must start at sequence 1")
        if binding["runtime_instance_id"] == instance and int(binding["report_seq"]) > 0:
            raise ConflictError("runtime session start is a duplicate")
    elif seq <= int(binding["report_seq"]):
        raise ConflictError("runtime report sequence is stale")
    kind, value = payload["nativeSessionKind"], payload["nativeSessionValue"]
    harness.validate_native_session(binding, kind, value)
    start_source = payload["startSource"]
    if start_source not in {"startup", "resume"}:
        raise InvalidRequestError("runtime start source is invalid")

    with store.transaction():
        current = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()
        if current is None:
            raise AuthorizationError("runtime binding was removed")
        if event != "session_start" and current["runtime_instance_id"] != instance:
            raise ConflictError("runtime instance changed")
        workspace_seq = int(current["workspace_report_seq"])
        surface_id = str(current["workspace_surface_id"])
        selected_session_changed = (
            kind is not None
            and (kind != current["native_session_kind"] or value != current["native_session_value"])
        )
        if kind is not None and (event == "session_start" or reason is not None or selected_session_changed):
            workspace_seq += 1
            workspace.report_session(surface_id, (str(kind), str(value)), workspace_seq, start_source, instance, harness.manifest)
        if event == "session_shutdown":
            workspace_seq += 1
            workspace.release_agent(surface_id, workspace_seq, instance, harness.manifest)
        elif state in {"idle", "working", "blocked", "unknown"}:
            workspace_seq += 1
            workspace.report_state(surface_id, state, None, workspace_seq, instance, harness.manifest)
        elif state in {"starting", "error", "missing"}:
            workspace_seq += 1
            workspace.report_state(surface_id, "unknown", None, workspace_seq, instance, harness.manifest)
        now = utc_now()
        session_event = None
        if event == "session_start":
            session_event = append_event_in_transaction(
                store.conn,
                kind="runtime.session_started",
                project_id=str(binding["project_id"]),
                workstream_id=workstream_id,
                payload={"runtimeInstanceId": instance, "generationSha256": generation, "reportSeq": seq},
            )
        store.conn.execute(
            "UPDATE runtime_bindings SET runtime_instance_id=?,report_seq=?,workspace_report_seq=?,native_session_kind=COALESCE(?,native_session_kind),native_session_value=COALESCE(?,native_session_value),observed_state=?,applied_generation_sha256=CASE WHEN ?='session_start' THEN ? ELSE applied_generation_sha256 END,launch_generation_sha256=CASE WHEN ?='session_start' THEN NULL ELSE launch_generation_sha256 END,refresh_pending=CASE WHEN ?='session_start' THEN 0 ELSE refresh_pending END,refresh_operation_id=CASE WHEN ?='session_start' THEN NULL ELSE refresh_operation_id END,refresh_started_at=CASE WHEN ?='session_start' THEN NULL ELSE refresh_started_at END,session_start_event_sequence=CASE WHEN ?='session_start' THEN ? ELSE session_start_event_sequence END,session_start_report_seq=CASE WHEN ?='session_start' THEN ? ELSE session_start_report_seq END,session_started_at=CASE WHEN ?='session_start' THEN ? ELSE session_started_at END,last_observed_at=?,updated_at=? WHERE workstream_id=?",
            (instance, seq, workspace_seq, kind, value, state, event, generation, event, event, event, event, event, session_event["sequence"] if session_event else None, event, seq, event, now, now, now, workstream_id),
        )
    return {"accepted": True, "workstreamId": workstream_id, "seq": seq, "reason": reason, "workspaceReportSeq": workspace_seq}
