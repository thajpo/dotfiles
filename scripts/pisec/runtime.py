"""Harness-neutral runtime authentication and monotonic attestation."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import re
import time
from typing import Any, Mapping

from .adapters import HarnessAdapter, WorkspaceAdapter
from .events import append_event_in_transaction
from .attention import compact_attention, list_open_attention, present_attention_in_transaction
from .worker_repo import validate_worker_resume_git
from .models import AuthorizationError, ConflictError, InvalidRequestError, NeedsAttentionError, bounded_text, canonical_json, validate_id, validate_sha256
from .models import utc_now

RUNTIME_FIELDS = frozenset({"workstreamId", "runtimeInstanceId", "seq", "event", "reason", "state", "nativeSessionKind", "nativeSessionValue", "startSource", "surfaceId", "token", "generation"})
RUNTIME_AUTH_FIELDS = frozenset({"workstreamId", "runtimeInstanceId", "surfaceId", "token", "generation"})
SESSION_SWITCH_REASONS = frozenset({"new", "resume", "fork", "handoff"})
OBSERVED_STATES = frozenset({"unknown", "starting", "working", "blocked", "idle", "stopped", "missing", "error"})

WORKSPACE_RUNTIME_MISSING = "workspace runtime is missing"
RUNTIME_STARTUP_MAX_SECONDS = 5.0
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
    runtime_deadline = time.monotonic() + RUNTIME_STARTUP_MAX_SECONDS
    while True:
        runtime = workspace.observe_runtime(str(row["workspace_surface_id"]), str(row["policy_path"]))
        if runtime.state == "live":
            return {"launched": False, "observation": observation}
        if runtime.state == "stopped":
            break
        if time.monotonic() >= runtime_deadline:
            raise NeedsAttentionError("runtime binding pane process identity is ambiguous")
        time.sleep(0.1)
    launcher = harness.launch_binding_path(workstream_id)
    argv = [str(launcher)]
    if row["native_session_kind"] is not None:
        argv.append(f"--resume={row['native_session_value']}")
    surface_id = str(row["workspace_surface_id"])
    environment = {"HERDR_SESSION": workspace.manifest.session_name, "HERDR_PANE_ID": surface_id}
    workspace.run_command(surface_id, argv, env=environment)
    # A persistent Herdr pane can acknowledge input while still dropping the
    # first command during PTY reattachment.  Re-observe the exact bound
    # process before one bounded retry; never retry a live or ambiguous tree.
    retry_at = time.monotonic() + 2.0
    retry_deadline = retry_at + 2.0
    retried = False
    while time.monotonic() < retry_deadline:
        try:
            process = workspace.observe_runtime(surface_id, str(row["policy_path"]))
        except Exception:
            process = None
        if process is not None and process.state == "live":
            return {"launched": True, "observation": observation}
        if not retried and process is not None and process.state == "stopped" and time.monotonic() >= retry_at:
            workspace.run_command(surface_id, argv, env=environment)
            retried = True
            return {"launched": True, "observation": observation}
        time.sleep(0.1)
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
    initial_session_start_pending = (
        allow_session_start
        and row is not None
        and row["launch_generation_sha256"] is not None
        and row["applied_generation_sha256"] is None
        and row["provisioning_state"] == "creating"
        and not int(row["refresh_pending"])
        and row["refresh_operation_id"] is None
        and row["refresh_started_at"] is None
    )
    session_start_pending = (
        allow_session_start
        and row is not None
        and row["launch_generation_sha256"] is not None
        and int(row["refresh_pending"]) == 1
        and row["refresh_operation_id"] is not None
        and row["refresh_started_at"] is not None
    ) or initial_session_start_pending
    if row is None or row["desired_state"] == "retired" or (
        allow_session_start and row["launch_generation_sha256"] is not None and not session_start_pending
    ) or (
        row["provisioning_state"] not in {"bound", "creating"} and not session_start_pending
    ):
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


def usable_runtime_binding(
    store: Any,
    workstream_id: str,
    workspace: WorkspaceAdapter,
    harness: HarnessAdapter | None = None,
    *,
    allowed_states: set[str] | frozenset[str] = frozenset({"idle"}),
    require_prompt_eligible: bool = False,
) -> bool:
    """Apply the common live, attested, unreserved binding predicate."""
    row = store.conn.execute(
        "SELECT w.desired_state,w.provisioning_state,w.worktree_path,r.harness_id,r.refresh_pending,r.refresh_operation_id,r.refresh_started_at,r.launch_generation_sha256,r.desired_generation_sha256,r.applied_generation_sha256,r.observed_state,r.workspace_id,r.workspace_view_id,r.workspace_surface_id,r.agent_name,r.policy_path,r.adapter_artifacts_json,r.runtime_instance_id,r.report_seq,r.session_start_event_sequence,r.session_start_report_seq,r.session_started_at "
        "FROM workstreams w JOIN runtime_bindings r USING(workstream_id) WHERE w.workstream_id=?",
        (workstream_id,),
    ).fetchone()
    if row is None or row["desired_state"] != "active" or row["provisioning_state"] != "bound":
        return False
    if harness is None or row["harness_id"] != harness.manifest.adapter_id:
        return False
    if (
        int(row["refresh_pending"])
        or row["refresh_operation_id"] is not None
        or row["refresh_started_at"] is not None
        or row["launch_generation_sha256"] is not None
        or row["observed_state"] not in allowed_states
        or row["desired_generation_sha256"] is None
        or row["applied_generation_sha256"] != row["desired_generation_sha256"]
        or row["runtime_instance_id"] is None
        or row["report_seq"] is None
        or int(row["report_seq"]) < 1
        or row["session_start_event_sequence"] is None
        or row["session_start_report_seq"] is None
        or not 1 <= int(row["session_start_report_seq"]) <= int(row["report_seq"])
        or row["session_started_at"] is None
    ):
        return False
    try:
        artifacts = json.loads(str(row["adapter_artifacts_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(artifacts, dict) or artifacts.get("generationSha256") != row["desired_generation_sha256"]:
        return False
    event = store.conn.execute(
        "SELECT kind,workstream_id,payload_json FROM events WHERE sequence=?",
        (row["session_start_event_sequence"],),
    ).fetchone()
    expected_event = {
        "generationSha256": str(row["applied_generation_sha256"]),
        "reportSeq": int(row["session_start_report_seq"]),
        "runtimeInstanceId": str(row["runtime_instance_id"]),
    }
    if event is None or event["kind"] != "runtime.session_started" or event["workstream_id"] != workstream_id:
        return False
    try:
        event_payload = __import__("json").loads(str(event["payload_json"]))
    except (TypeError, ValueError):
        return False
    if event_payload != expected_event or canonical_json(event_payload) != str(event["payload_json"]):
        return False
    try:
        observed = workspace.observe_surface(
            workspace_id=str(row["workspace_id"]),
            view_id=str(row["workspace_view_id"]),
            surface_id=str(row["workspace_surface_id"]),
            cwd=str(row["worktree_path"]),
        )
        if observed is None or observed.agent is None:
            return False
        expected_names = {str(row["agent_name"])}
        expected_names.add(str(harness.manifest.agent_kind))
        if (
            observed.agent.surface_id != str(row["workspace_surface_id"])
            or observed.agent.name not in expected_names
            or not observed.agent.identity_usable
        ):
            return False
        if require_prompt_eligible and not workspace.prompt_eligible(observed.agent):
            return False
        return workspace.observe_runtime(str(row["workspace_surface_id"]), str(row["policy_path"])).state == "live"
    except Exception:
        return False


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


def prepare_runtime_turn(store: Any, payload: Mapping[str, Any], workspace: WorkspaceAdapter, harness: HarnessAdapter) -> dict[str, Any]:
    """Present the current immutable packet and attention in one durable turn."""
    binding = verify_runtime_binding(store, payload, worker_only=False)
    if not usable_runtime_binding(store, str(binding["workstream_id"]), workspace, harness, allowed_states={"idle"}, require_prompt_eligible=True):
        raise ConflictError("runtime is not a usable idle attested binding")
    if binding["refresh_pending"] or binding["launch_generation_sha256"] is not None:
        raise ConflictError("runtime is reserved for a generation refresh")
    if binding["applied_generation_sha256"] is None or binding["applied_generation_sha256"] != binding["desired_generation_sha256"]:
        raise ConflictError("runtime generation is not usable")
    workstream_id = str(binding["workstream_id"])
    with store.transaction():
        packet = store.conn.execute("SELECT * FROM task_packets WHERE workstream_id=?", (workstream_id,)).fetchone()
        packet_value = None
        if packet is not None:
            packet_value = {"taskPacketId": packet["task_packet_id"], "projectId": packet["project_id"], "workstreamId": packet["workstream_id"], "scopeSha256": packet["scope_sha256"], "packetSha256": packet["packet_sha256"], "issuedAt": packet["issued_at"], "packet": __import__("json").loads(packet["packet_json"])}
        bootstrap = store.conn.execute(
            "SELECT e.sequence,e.event_id,e.kind,e.payload_json FROM events e "
            "WHERE e.workstream_id=? AND e.kind='runtime.bootstrap' ORDER BY e.sequence DESC LIMIT 1",
            (workstream_id,),
        ).fetchone()
        acknowledged_bootstraps = store.conn.execute(
            "SELECT payload_json FROM events WHERE workstream_id=? AND kind='runtime.bootstrap.acknowledged'",
            (workstream_id,),
        ).fetchall()
        acknowledged = {
            (str(payload.get("bootstrapEventId")), int(payload["bootstrapRevision"]))
            for row in acknowledged_bootstraps
            if isinstance((payload := json.loads(str(row["payload_json"]))), dict)
            and isinstance(payload.get("bootstrapEventId"), str)
            and isinstance(payload.get("bootstrapRevision"), int)
            and not isinstance(payload.get("bootstrapRevision"), bool)
        }
        bootstrap_value = None
        if bootstrap is not None and (str(bootstrap["event_id"]), int(bootstrap["sequence"])) not in acknowledged:
            bootstrap_payload = __import__("json").loads(str(bootstrap["payload_json"]))
            bootstrap_value = {
                "eventType": str(bootstrap_payload.get("eventType", "worker.bootstrap")),
                "sourceRecordId": str(bootstrap["event_id"]),
                "sourceRevision": int(bootstrap["sequence"]),
                "workstreamId": workstream_id,
                "role": str(bootstrap_payload.get("role", "worker")),
            }
        attention = list_open_attention(store, recipient_workstream_id=workstream_id)
        presented = []
        for item in attention:
            presented.append(compact_attention(present_attention_in_transaction(store.conn, recipient_workstream_id=workstream_id, attention_id=item["attention_id"], revision=int(item["source_event_sequence"]))))
    return {"prepared": True, "taskPacket": packet_value, "bootstrap": bootstrap_value, "attention": presented}


def acknowledge_runtime_bootstrap(store: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically consume the authenticated bootstrap event after extension delivery."""
    binding = verify_runtime_binding(store, payload, worker_only=False)
    event_id = bounded_text(payload.get("bootstrapEventId"), name="bootstrapEventId", limit=256)
    revision = payload.get("bootstrapRevision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise InvalidRequestError("bootstrapRevision is invalid")
    workstream_id = str(binding["workstream_id"])
    event = store.conn.execute(
        "SELECT sequence,event_id,kind,workstream_id FROM events WHERE event_id=? AND sequence=?",
        (event_id, revision),
    ).fetchone()
    if event is None or event["kind"] != "runtime.bootstrap" or event["workstream_id"] != workstream_id:
        raise ConflictError("bootstrap event is stale or does not match the runtime binding")
    with store.transaction():
        existing = store.conn.execute(
            "SELECT payload_json FROM events WHERE workstream_id=? AND kind='runtime.bootstrap.acknowledged'",
            (workstream_id,),
        ).fetchall()
        already_acknowledged = any(
            isinstance((ack_payload := json.loads(str(row["payload_json"]))), dict)
            and ack_payload.get("bootstrapEventId") == event_id
            and ack_payload.get("bootstrapRevision") == revision
            for row in existing
        )
        if not already_acknowledged:
            append_event_in_transaction(
                store.conn,
                kind="runtime.bootstrap.acknowledged",
                project_id=str(binding["project_id"]),
                workstream_id=workstream_id,
                payload={"bootstrapEventId": event_id, "bootstrapRevision": revision},
            )
    return {"acknowledged": True, "bootstrapEventId": event_id, "bootstrapRevision": revision, "workstreamId": workstream_id}


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
        permission_batch = False
        if event == "session_start" and current["refresh_operation_id"] is not None:
            permission_batch = store.conn.execute(
                "SELECT 1 FROM operations WHERE operation_id=? AND kind='project.permissions.update' AND state='applying'",
                (current["refresh_operation_id"],),
            ).fetchone() is not None
        clear_reservation = int(not permission_batch)
        store.conn.execute(
            "UPDATE runtime_bindings SET runtime_instance_id=?,report_seq=?,workspace_report_seq=?,native_session_kind=COALESCE(?,native_session_kind),native_session_value=COALESCE(?,native_session_value),observed_state=?,applied_generation_sha256=CASE WHEN ?='session_start' THEN ? ELSE applied_generation_sha256 END,launch_generation_sha256=CASE WHEN ?='session_start' AND ?=1 THEN NULL ELSE launch_generation_sha256 END,refresh_pending=CASE WHEN ?='session_start' AND ?=1 THEN 0 ELSE refresh_pending END,refresh_operation_id=CASE WHEN ?='session_start' AND ?=1 THEN NULL ELSE refresh_operation_id END,refresh_started_at=CASE WHEN ?='session_start' AND ?=1 THEN NULL ELSE refresh_started_at END,session_start_event_sequence=CASE WHEN ?='session_start' THEN ? ELSE session_start_event_sequence END,session_start_report_seq=CASE WHEN ?='session_start' THEN ? ELSE session_start_report_seq END,session_started_at=CASE WHEN ?='session_start' THEN ? ELSE session_started_at END,last_observed_at=?,updated_at=? WHERE workstream_id=?",
            (instance, seq, workspace_seq, kind, value, state, event, generation, event, clear_reservation, event, clear_reservation, event, clear_reservation, event, clear_reservation, event, session_event["sequence"] if session_event else None, event, seq, event, now, now, now, workstream_id),
        )
        if event == "session_start" and current["refresh_operation_id"] is not None:
            operation = store.conn.execute(
                "SELECT operation_id,project_id,state FROM operations WHERE operation_id=? AND kind='runtime.refresh'",
                (current["refresh_operation_id"],),
            ).fetchone()
            if operation is not None and operation["state"] in {"planned", "applying"}:
                refresh_result = {
                    "workstreamId": workstream_id,
                    "generationSha256": generation,
                    "runtimeInstanceId": instance,
                    "reportSeq": seq,
                }
                store.conn.execute(
                    "UPDATE operations SET state='succeeded',step='verified',result_json=?,error_code=NULL,error_message=NULL,updated_at=? WHERE operation_id=? AND state IN ('planned','applying')",
                    (canonical_json(refresh_result), now, operation["operation_id"]),
                )
                append_event_in_transaction(
                    store.conn,
                    kind="runtime.refresh_completed",
                    project_id=str(operation["project_id"]),
                    workstream_id=workstream_id,
                    operation_id=str(operation["operation_id"]),
                    payload=refresh_result,
                )
    return {"accepted": True, "workstreamId": workstream_id, "seq": seq, "reason": reason, "workspaceReportSeq": workspace_seq}
