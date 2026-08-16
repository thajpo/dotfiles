"""Harness-neutral runtime authentication and monotonic attestation."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Any, Mapping

from .adapters import HarnessAdapter, WorkspaceAdapter
from .models import AuthorizationError, ConflictError, InvalidRequestError, validate_id
from .models import utc_now

RUNTIME_FIELDS = frozenset({"workstreamId", "runtimeInstanceId", "seq", "event", "state", "nativeSessionKind", "nativeSessionValue", "startSource", "surfaceId", "token"})
RUNTIME_AUTH_FIELDS = frozenset({"workstreamId", "runtimeInstanceId", "surfaceId", "token"})
OBSERVED_STATES = frozenset({"unknown", "starting", "working", "blocked", "idle", "done", "stopped", "missing", "error"})

WORKSPACE_RUNTIME_MISSING = "workspace runtime is missing"


def verify_runtime_binding(store: Any, payload: Mapping[str, Any], *, worker_only: bool = False, allow_session_start: bool = False) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or not RUNTIME_AUTH_FIELDS <= set(payload):
        raise InvalidRequestError("runtime binding fields are incomplete")
    workstream_id = validate_id(payload["workstreamId"], prefix="ws")
    instance = payload["runtimeInstanceId"]
    surface_id = payload["surfaceId"]
    token = payload["token"]
    if not isinstance(instance, str) or not instance or len(instance) > 128 or "\x00" in instance:
        raise InvalidRequestError("runtime instance id is invalid")
    if not isinstance(surface_id, str) or not surface_id or len(surface_id) > 256 or "\x00" in surface_id:
        raise InvalidRequestError("runtime surface id is invalid")
    if not isinstance(token, str) or len(token) < 32 or len(token) > 512 or "\x00" in token:
        raise AuthorizationError("runtime token is invalid")
    row = store.conn.execute(
        "SELECT r.*,w.project_id,w.kind,w.desired_state,w.provisioning_state FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=?",
        (workstream_id,),
    ).fetchone()
    if row is None or row["desired_state"] == "retired" or row["provisioning_state"] not in {"bound", "creating"}:
        raise AuthorizationError("runtime binding is inactive")
    if worker_only and row["kind"] != "worker":
        raise AuthorizationError("runtime operation requires a worker binding")
    if not hmac.compare_digest(row["runtime_token_sha256"], hashlib.sha256(token.encode("utf-8")).hexdigest()):
        raise AuthorizationError("runtime token is invalid")
    if row["workspace_surface_id"] != surface_id:
        raise AuthorizationError("runtime surface does not match its binding")
    if not allow_session_start and row["runtime_instance_id"] != instance:
        raise ConflictError("runtime instance is stale")
    return dict(row)


def report_runtime(store: Any, payload_value: Mapping[str, Any], harness: HarnessAdapter, workspace: WorkspaceAdapter) -> dict[str, Any]:
    if not isinstance(payload_value, Mapping) or set(payload_value) != RUNTIME_FIELDS:
        raise InvalidRequestError("runtime report fields do not match protocol version 1")
    payload = dict(payload_value)
    event = payload["event"]
    state = payload["state"]
    seq = payload["seq"]
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise InvalidRequestError("runtime report sequence is invalid")
    if event not in {"session_start", "lifecycle", "session_shutdown"} or state not in OBSERVED_STATES:
        raise InvalidRequestError("runtime report event or state is invalid")
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
        if event == "session_start" and kind is not None:
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
        store.conn.execute(
            "UPDATE runtime_bindings SET runtime_instance_id=?,report_seq=?,workspace_report_seq=?,native_session_kind=COALESCE(?,native_session_kind),native_session_value=COALESCE(?,native_session_value),observed_state=?,last_observed_at=?,updated_at=? WHERE workstream_id=?",
            (instance, seq, workspace_seq, kind, value, state, now, now, workstream_id),
        )
    return {"accepted": True, "workstreamId": workstream_id, "seq": seq, "workspaceReportSeq": workspace_seq}
