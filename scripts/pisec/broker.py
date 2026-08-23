"""Pisec broker dispatch and four-socket Unix service."""

from __future__ import annotations
import hashlib
import logging
import hmac
import json
import queue
import os
from pathlib import Path
import time
import socket
import socketserver
import stat
import threading
from typing import Any, Callable, Mapping
from .adapters import AdapterRegistry, HarnessAdapter, WorkspaceAdapter
from .cleanup import cleanup_workstream
from .decisions import list_decisions, record_decision, resolve_decision
from .events import list_events
from .migration import migrate_legacy_bindings
from .models import AuthorizationError, ConflictError, InvalidRequestError, NotFoundError, PisecError, ScopeMismatchError, UnsafeStateError, bounded_text, utc_now, validate_id
from .pi_store import PiStore
from .projects import assert_project_writable, fleet_activity, fleet_project_ids, get_project, list_fleet_projects, list_projects, project_activity, project_status, register_project, require_fleet_project, resolve_project, update_project_policy
from .protocol import MAX_MESSAGE_BYTES, decode_request, error_response, success_response
from .research import (
    acknowledge_research,
    add_research_context,
    answer_research,
    claim_research,
    decline_research,
    get_task_packet,
    inspect_research,
    list_research_requests,
    list_unacknowledged_research,
    mark_research_wake_notified,
    pending_research_wakes,
    request_research,
    request_research_context,
)
from .runtime import WORKSPACE_RUNTIME_MISSING, prepare_session_switch, record_runtime_tool_failure, report_runtime, start_bound_agent, verify_runtime_binding
from .first_mate import ensure_first_mate, focus_first_mate
from .secretary_git import apply_workstream_merge, git_status, inspect_workstream_changes, prepare_workstream_merge, push_branch
from .workflow import acknowledge_coordination, acknowledge_issue, add_issue_context, answer_coordination, checkpoint, inspect_issue, link_issue_remediation, list_issues, request_coordination, request_help, request_issue_verification, report_issue, resolve_issue, submit_completion, verify_issue
from .workstreams import authorize_apply_workstream, complete_workstream, focus_workstream, inspect_workstream, list_workstreams, prepare_workstream, retire_workstream, send_workstream
from .operation_contracts import SOCKET_OPERATIONS
ADMIN_OPERATIONS = SOCKET_OPERATIONS["admin"]
SECRETARY_OPERATIONS = SOCKET_OPERATIONS["secretary"]
FLEET_OPERATIONS = SOCKET_OPERATIONS["fleet"]
RUNTIME_OPERATIONS = SOCKET_OPERATIONS["runtime"]
from .operation_contracts import operation_manifest
WORKSPACE_RECONCILE_INTERVAL_SECONDS = 5.0
WORKSPACE_STARTUP_GRACE_SECONDS = 2.0
RUNTIME_RESTART_BACKOFF_SECONDS = 30.0
RESEARCH_WAKE_DEBOUNCE_SECONDS = 0.1
logger = logging.getLogger(__name__)


def default_runtime_root() -> Path:
    from .platform import runtime_root

    return runtime_root()


def socket_paths(root: Path | None = None) -> dict[str, Path]:
    base = root or default_runtime_root()
    return {kind: base / kind / "control.sock" for kind in ("admin", "secretary", "fleet", "runtime")}


def _exact(payload: Mapping[str, Any], required: set[str], optional: set[str] = set()) -> None:
    if set(payload) < required or not set(payload) <= required | optional:
        raise InvalidRequestError("payload fields do not match the operation contract")


_PUBLIC_PROJECT_FIELDS = ("project_id", "display_name", "default_ref", "data_dirs", "secretary_workstream_id", "coordination_mode", "worker_creation_policy", "worker_creation_policy_json", "merge_policy", "merge_policy_json", "active", "created_at", "updated_at", "deactivated_at")
_PUBLIC_WORKSTREAM_FIELDS = (
    "workstream_id", "project_id", "kind", "title", "purpose", "brief", "harness_id", "workspace_adapter_id",
    "execution_profile", "target_ref", "base_commit_oid", "branch_name",
    "desired_state", "provisioning_state", "created_at", "updated_at", "completed_at", "retired_at",
    "observed_state", "last_observed_at", "agent_name", "task_packet_id", "task_packet_sha256",
    "desired_release_id", "applied_release_id", "desired_generation_sha256", "applied_generation_sha256", "runtime_stale",
)
_PUBLIC_BINDING_FIELDS = ("workstream_id", "workspace_adapter_id", "workspace_session_name", "harness_id", "agent_name", "desired_release_id", "applied_release_id", "observed_state", "runtime_instance_id", "last_observed_at", "updated_at")
_PUBLIC_OPERATION_FIELDS = ("operation_id", "kind", "project_id", "workstream_id", "idempotency_key", "request_sha256", "state", "step", "error_code", "created_at", "updated_at")


def _public_row(row: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: row[field] for field in fields if field in row}


def _public_project(project: Mapping[str, Any]) -> dict[str, Any]:
    return _public_row(project, _PUBLIC_PROJECT_FIELDS)


def _public_workstream(workstream: Mapping[str, Any]) -> dict[str, Any]:
    return _public_row(workstream, _PUBLIC_WORKSTREAM_FIELDS)


def _public_binding(binding: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return None if binding is None else _public_row(binding, _PUBLIC_BINDING_FIELDS)


def _public_operation(operation: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return None if operation is None else _public_row(operation, _PUBLIC_OPERATION_FIELDS)


def _public_inspect(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "workstream": _public_workstream(value["workstream"]),
        "binding": _public_binding(value.get("binding")),
        "operation": _public_operation(value.get("operation")),
    }


def _public_project_status(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "project": _public_project(value["project"]),
        "workstreams": [_public_workstream(row) for row in value.get("workstreams", [])],
        "decisions": list(value.get("decisions", [])),
        "researchCounts": dict(value.get("researchCounts", {})),
        "source": value.get("source", "pisec-sqlite"),
    }

def _first_mate_summary(store: PiStore) -> dict[str, Any]:
    row = store.conn.execute(
        "SELECT w.workstream_id,w.provisioning_state,r.observed_state "
        "FROM workstreams w LEFT JOIN runtime_bindings r USING(workstream_id) "
        "WHERE w.kind='first_mate' AND w.desired_state <> 'retired' ORDER BY w.created_at LIMIT 1"
    ).fetchone()
    if row is None:
        return {"present": False}
    return {
        "present": True,
        "workstreamId": row["workstream_id"],
        "provisioningState": row["provisioning_state"],
        "observedState": row["observed_state"],
    }


def _presentation_snapshot(store: PiStore) -> dict[str, Any]:
    """Return local-only project/worktree identities for Collie mode filtering.

    This projection intentionally includes repository and worktree paths because it is
    consumed only over the owner-only admin socket by the local Collie bridge.  The
    normal public project/workstream projections remain path-free.
    """
    projects = [
        dict(row)
        for row in store.conn.execute(
            "SELECT project_id,display_name,repository_path,default_ref "
            "FROM projects WHERE active=1 ORDER BY display_name,project_id"
        )
    ]
    worktrees = [
        dict(row)
        for row in store.conn.execute(
            """
            SELECT
                w.workstream_id,
                w.project_id,
                w.kind,
                w.title,
                w.branch_name,
                w.worktree_path,
                r.workspace_session_name,
                r.workspace_id,
                r.workspace_view_id
            FROM workstreams AS w
            JOIN runtime_bindings AS r USING(workstream_id)
            WHERE w.desired_state='active'
              AND w.provisioning_state='bound'
              AND r.workspace_session_name <> ''
              AND r.workspace_id IS NOT NULL
              AND r.workspace_view_id IS NOT NULL
            ORDER BY w.project_id,w.created_at,w.workstream_id
            """
        )
    ]
    return {"projects": projects, "worktrees": worktrees}
class BrokerDispatcher:
    def __init__(
        self,
        store_factory: Callable[[], PiStore],
        *,
        registry: AdapterRegistry,
        harness: HarnessAdapter,
        workspace: WorkspaceAdapter,
        git_objects: Any,
        config: Mapping[str, Any] | None = None,
    ):
        self.store_factory = store_factory
        self.registry = registry
        self.harness = harness
        self.workspace = workspace
        self.git_objects = git_objects
        self.config = dict(config or {})
        self._reconcile_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._wake_queue: queue.Queue[str] = queue.Queue(maxsize=256)
        self._reconcile_stop = threading.Event()
        self._reconcile_thread: threading.Thread | None = None
        self._wake_thread: threading.Thread | None = None
        self._reconcile_lock = threading.Lock()
        self._last_resume_attempt: dict[str, float] = {}

    def wait_for_workspace(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while True:
            try:
                self.workspace.snapshot()
                return
            except Exception as error:
                last_error = error
                if time.monotonic() >= deadline:
                    raise PisecError("Herdr main workspace did not become ready") from last_error
                time.sleep(0.25)

    def startup_reconcile(self) -> dict[str, Any]:
        with self.store_factory() as store:
            return self._reconcile(store, {"event": "startup"})
    def _resume_restored_agents(
        self,
        store: PiStore,
        candidates: list[Mapping[str, Any]],
        skipped: set[str],
    ) -> list[dict[str, Any]]:
        resumed: list[dict[str, Any]] = []
        for candidate in candidates:
            workstream_id = str(candidate["workstream_id"])
            if workstream_id in skipped:
                continue
            last_attempt = self._last_resume_attempt.get(workstream_id)
            if last_attempt is not None and time.monotonic() - last_attempt < RUNTIME_RESTART_BACKOFF_SECONDS:
                continue
            try:
                current_row = store.conn.execute(
                    "SELECT r.*,w.project_id,w.kind,w.worktree_path,w.desired_state,w.provisioning_state "
                    "FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=?",
                    (workstream_id,),
                ).fetchone()
                if current_row is None or current_row["refresh_pending"] or current_row["desired_state"] != "active" or current_row["provisioning_state"] not in {"bound", "needs_attention"}:
                    continue
                selected_generation = current_row["launch_generation_sha256"] or current_row["applied_generation_sha256"]
                if not isinstance(selected_generation, str) or len(selected_generation) != 64:
                    continue
                binding = dict(current_row)
                observation = self.workspace.observe_surface(
                    workspace_id=str(current_row["workspace_id"]),
                    view_id=str(current_row["workspace_view_id"]),
                    surface_id=str(current_row["workspace_surface_id"]),
                    cwd=str(current_row["worktree_path"]),
                )
                if observation is None:
                    continue
                if observation.agent is not None:
                    expected_names = {str(current_row["agent_name"]), self.harness.manifest.agent_kind}
                    if observation.agent.name not in expected_names:
                        now = utc_now()
                        with store.transaction():
                            store.conn.execute(
                                "UPDATE runtime_bindings SET observed_state='error',last_observed_at=?,updated_at=? WHERE workstream_id=?",
                                (now, now, workstream_id),
                            )
                            store.conn.execute(
                                "UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?",
                                ("workspace pane has an unexpected agent identity", now, workstream_id),
                            )
                        continue
                runtime = self.workspace.observe_runtime(str(current_row["workspace_surface_id"]), str(current_row["policy_path"]))
                if runtime.state == "live":
                    continue
                if runtime.state != "stopped":
                    now = utc_now()
                    with store.transaction():
                        store.conn.execute(
                            "UPDATE runtime_bindings SET observed_state='error',last_observed_at=?,updated_at=? WHERE workstream_id=?",
                            (now, now, workstream_id),
                        )
                        store.conn.execute(
                            "UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?",
                            ("workspace pane process identity is ambiguous", now, workstream_id),
                        )
                    continue
                self._last_resume_attempt[workstream_id] = time.monotonic()
                now = utc_now()
                with store.transaction():
                    store.conn.execute(
                        "UPDATE runtime_bindings SET observed_state='starting',launch_release_id=IFNULL(applied_release_id,launch_release_id),launch_generation_sha256=IFNULL(applied_generation_sha256,launch_generation_sha256),last_observed_at=?,updated_at=? WHERE workstream_id=?",
                        (now, now, workstream_id),
                    )
                    store.conn.execute(
                        "UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=?",
                        (now, workstream_id),
                    )
                result = start_bound_agent(
                    store,
                    self.workspace,
                    self.harness,
                    binding,
                    workstream_id=workstream_id,
                    project_id=str(current_row["project_id"]),
                    cwd=str(current_row["worktree_path"]),
                )
                resumed.append({"workstreamId": workstream_id, "launched": bool(result.get("launched"))})
            except BaseException as error:
                now = utc_now()
                with store.transaction():
                    store.conn.execute(
                        "UPDATE runtime_bindings SET observed_state='error',last_observed_at=?,updated_at=? WHERE workstream_id=?",
                        (now, now, workstream_id),
                    )
                    store.conn.execute(
                        "UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?",
                        (f"restored agent start failed: {error}"[:512], now, workstream_id),
                    )
                logger.exception("restored agent resume failed for %s", workstream_id)
        return resumed

    def start_background(self) -> None:
        if self._reconcile_thread is not None and self._reconcile_thread.is_alive():
            return
        self._reconcile_stop.clear()
        self._reconcile_thread = threading.Thread(target=self._run_reconcile_queue, name="pisec-reconcile", daemon=True)
        self._wake_thread = threading.Thread(target=self._run_research_wakes, name="pisec-research-wake", daemon=True)
        self._reconcile_thread.start()
        self._wake_thread.start()
        self._queue_all_research_wakes()
    def stop_background(self) -> None:
        self._reconcile_stop.set()
        thread = self._reconcile_thread
        wake_thread = self._wake_thread
        if thread is not None:
            thread.join(timeout=5)
        if wake_thread is not None:
            wake_thread.join(timeout=5)
        self._reconcile_thread = None
    def _queue_all_research_wakes(self) -> None:
        try:
            with self.store_factory() as store:
                for row in pending_research_wakes(store):
                    self._queue_research_wake(row["project_id"])
                for row in store.conn.execute(
                    "SELECT DISTINCT w.project_id FROM issue_inbox i JOIN workstreams w ON w.workstream_id=i.workstream_id WHERE i.generation>i.notified_generation"
                ):
                    self._queue_research_wake(row["project_id"])
        except BaseException:
            logger.exception("could not queue durable notification wakes")

    def _queue_research_wake(self, project_id: str) -> None:
        try:
            self._wake_queue.put_nowait(project_id)
        except queue.Full:
            pass

    def _run_research_wakes(self) -> None:
        while not self._reconcile_stop.is_set():
            try:
                project_id = self._wake_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            projects = {project_id}
            if self._reconcile_stop.wait(RESEARCH_WAKE_DEBOUNCE_SECONDS):
                self._wake_queue.task_done()
                break
            while True:
                try:
                    projects.add(self._wake_queue.get_nowait())
                    self._wake_queue.task_done()
                except queue.Empty:
                    break
            for selected in projects:
                try:
                    self._wake_project(selected)
                except BaseException:
                    logger.exception("research wake failed")
            self._wake_queue.task_done()

    def _wake_project(self, project_id: str) -> None:
        with self.store_factory() as store:
            project = store.conn.execute("SELECT coordination_mode FROM projects WHERE project_id=? AND active=1", (project_id,)).fetchone()
            if project is None or project["coordination_mode"] != "fleet":
                return
            issue = store.conn.execute(
                "SELECT i.workstream_id,i.generation,r.workspace_surface_id FROM issue_inbox i JOIN workstreams w ON w.workstream_id=i.workstream_id JOIN runtime_bindings r ON r.workstream_id=w.workstream_id WHERE w.project_id=? AND w.desired_state='active' AND i.generation>i.notified_generation ORDER BY i.updated_at LIMIT 1",
                (project_id,),
            ).fetchone()
            if issue is not None:
                self.workspace.prompt_agent_nowait(issue["workspace_surface_id"], f"Pending Pisec remediation issue notification for project {project_id}; list and inspect issues through Pisec.")
                with store.transaction():
                    store.conn.execute("UPDATE issue_inbox SET notified_generation=?,updated_at=? WHERE workstream_id=? AND generation=?", (issue["generation"], utc_now(), issue["workstream_id"], issue["generation"]))
                return
            row = store.conn.execute(
                "SELECT i.generation FROM research_inbox i JOIN workstreams w ON w.project_id=i.project_id AND w.kind='secretary' AND w.desired_state='active' JOIN runtime_bindings r ON r.workstream_id=w.workstream_id WHERE i.project_id=? AND i.generation>i.notified_generation ORDER BY w.created_at LIMIT 1",
                (project_id,),
            ).fetchone()
            if row is None:
                return
            binding = store.conn.execute(
                "SELECT workspace_surface_id FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE w.project_id=? AND w.kind='secretary' AND w.desired_state='active' LIMIT 1",
                (project_id,),
            ).fetchone()
            if binding is None:
                return
            text = f"Pending worker research for project {project_id}; generation {int(row['generation'])}. List all pending requests through Pisec."
            self.workspace.prompt_agent_nowait(binding["workspace_surface_id"], text)
            mark_research_wake_notified(store, project_id, int(row["generation"]))

    def _defer_reconcile(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            self._reconcile_queue.put_nowait(dict(payload))
        except queue.Full:
            pass
        return {"accepted": True, "reconcileQueued": True}

    def _run_reconcile_queue(self) -> None:
        next_periodic = time.monotonic() + WORKSPACE_RECONCILE_INTERVAL_SECONDS
        while not self._reconcile_stop.is_set():
            try:
                payload = self._reconcile_queue.get(timeout=0.2)
            except queue.Empty:
                if time.monotonic() >= next_periodic:
                    try:
                        with self.store_factory() as store:
                            self._reconcile(store, {"event": "periodic"})
                    except BaseException:
                        logger.exception("periodic workspace reconciliation failed")
                    next_periodic = time.monotonic() + WORKSPACE_RECONCILE_INTERVAL_SECONDS
                continue
            if set(payload) == {"adapterId", "socketPath"} and payload.get("adapterId") == self.workspace.manifest.adapter_id and self._reconcile_stop.wait(WORKSPACE_STARTUP_GRACE_SECONDS):
                self._reconcile_queue.task_done()
                break
            try:
                with self.store_factory() as store:
                    self._reconcile(store, payload)
            except BaseException:
                logger.exception("deferred workspace reconciliation failed")
            finally:
                self._reconcile_queue.task_done()
            next_periodic = time.monotonic() + WORKSPACE_RECONCILE_INTERVAL_SECONDS

    def _secretary_binding(self, store: PiStore, payload: dict[str, Any]) -> dict[str, Any]:
        token = payload.pop("authToken", None)
        if not isinstance(token, str) or len(token) < 32 or len(token) > 512:
            raise AuthorizationError("secretary binding token is required")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        rows = store.conn.execute("SELECT w.project_id,w.workstream_id,r.runtime_token_sha256 FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE w.kind='secretary' AND w.desired_state='active'")
        for row in rows:
            if hmac.compare_digest(row["runtime_token_sha256"], digest):
                return dict(row)
        raise AuthorizationError("secretary binding token is invalid")
    def _first_mate_binding(self, store: PiStore, payload: dict[str, Any]) -> dict[str, Any]:
        token = payload.pop("authToken", None)
        if not isinstance(token, str) or len(token) < 32 or len(token) > 512:
            raise AuthorizationError("First Mate binding token is required")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        rows = store.conn.execute(
            "SELECT w.project_id,w.workstream_id,r.runtime_token_sha256 FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE w.kind='first_mate' AND w.desired_state='active'"
        ).fetchall()
        matches = [dict(row) for row in rows if hmac.compare_digest(row["runtime_token_sha256"], digest)]
        if len(matches) != 1:
            raise AuthorizationError("First Mate binding token is invalid")
        return matches[0]

    def _secretary_project(self, store: PiStore, payload: dict[str, Any]) -> str:
        return str(self._secretary_binding(store, payload)["project_id"])
    def dispatch(self, socket_kind: str, operation: str, payload_value: Mapping[str, Any]) -> Any:
        allowlist = {"admin": ADMIN_OPERATIONS, "secretary": SECRETARY_OPERATIONS, "fleet": FLEET_OPERATIONS, "runtime": RUNTIME_OPERATIONS}.get(socket_kind)
        if allowlist is None or operation not in allowlist:
            raise AuthorizationError("operation is not allowed on this socket")
        payload = dict(payload_value)
        with self.store_factory() as store:
            if socket_kind == "admin":
                return self._admin(store, operation, payload)
            if socket_kind == "secretary":
                binding = self._secretary_binding(store, payload)
                return self._secretary(store, operation, str(binding["project_id"]), str(binding["workstream_id"]), payload)
            if socket_kind == "fleet":
                binding = self._first_mate_binding(store, payload)
                return self._fleet(store, operation, str(binding["workstream_id"]), payload)
            if operation == "runtime.report":
                return report_runtime(store, payload, self.harness, self.workspace)
            return self._runtime(store, operation, payload)

    def _runtime(self, store: PiStore, operation: str, payload: dict[str, Any]) -> Any:
        auth_fields = {"workstreamId", "runtimeInstanceId", "surfaceId", "token"}
        if operation == "runtime.turn.prepare":
            _exact(payload, auth_fields)
            binding = verify_runtime_binding(store, payload)
            if binding["refresh_pending"]:
                raise PisecError("runtime is reserved for a generation refresh")
            return {"prepared": True}
        if operation == "session.switch.prepare":
            _exact(payload, auth_fields | {"reason", "targetSessionFile"})
            return prepare_session_switch(store, payload, self.harness)
        if operation == "runtime.tool_failure":
            _exact(payload, auth_fields | {"toolName", "failureCode"})
            return record_runtime_tool_failure(store, payload)
        binding = verify_runtime_binding(store, payload, worker_only=True)
        project_id = str(binding["project_id"])
        workstream_id = str(binding["workstream_id"])
        if operation in {"issue.report", "secretary.issue.report"}:
            _exact(payload, auth_fields | {"category", "severity", "summary", "details", "requestedAction", "evidence", "idempotencyKey"})
            return report_issue(store, project_id=project_id, reporter_workstream_id=workstream_id, category=payload["category"], severity=payload["severity"], summary=payload["summary"], details=payload["details"], requested_action=payload["requestedAction"], evidence=payload["evidence"], idempotency_key=payload["idempotencyKey"])
        if operation == "help.request":
            _exact(payload, auth_fields | {"kind", "summary", "details", "requestedAction", "blocking", "evidence", "idempotencyKey"})
            return request_help(store, project_id=project_id, workstream_id=workstream_id, kind=payload["kind"], summary=payload["summary"], details=payload["details"], requested_action=payload["requestedAction"], blocking=bool(payload["blocking"]), evidence=payload["evidence"], idempotency_key=payload["idempotencyKey"])
        if operation == "issue.list":
            _exact(payload, auth_fields, {"state", "limit"})
            return {"issues": list_issues(store, reporter_workstream_id=workstream_id, state=payload.get("state"), limit=int(payload.get("limit", 100)))}
        if operation == "issue.inspect":
            _exact(payload, auth_fields | {"issueId"})
            return inspect_issue(store, issue_id=payload["issueId"], reporter_workstream_id=workstream_id)
        if operation == "issue.add_context":
            _exact(payload, auth_fields | {"issueId", "context", "idempotencyKey"})
            inspect_issue(store, issue_id=payload["issueId"], reporter_workstream_id=workstream_id)
            return add_issue_context(store, project_id=project_id, issue_id=payload["issueId"], actor_id=workstream_id, context=payload["context"], idempotency_key=payload["idempotencyKey"])
        if operation == "issue.verify":
            _exact(payload, auth_fields | {"issueId", "status", "evidence", "idempotencyKey"})
            return verify_issue(store, project_id=project_id, issue_id=payload["issueId"], actor_id=workstream_id, status=payload["status"], evidence=payload["evidence"], idempotency_key=payload["idempotencyKey"])
        if operation == "access.effective":
            _exact(payload, auth_fields)
            from .access import effective_runtime_scope
            scope = effective_runtime_scope(store, binding)
            return {"workstreamId": workstream_id, "readPaths": [{"path": path, "sources": scope.get("readPathSources", {}).get(path, [])} for path in scope.get("dataDirs", [])], "pythonEnv": scope.get("pythonEnv")}
        if operation == "runtime.bootstrap.get":
            _exact(payload, auth_fields | {"sessionFile"})
            session_file = str(payload["sessionFile"])
            packet = get_task_packet(store, project_id, workstream_id)
            from .access import effective_runtime_scope
            scope = effective_runtime_scope(store, binding)
            runtime_scope = {"pythonEnv": scope.get("pythonEnv"), "readPaths": [{"path": path, "sources": scope.get("readPathSources", {}).get(path, [])} for path in scope.get("dataDirs", [])]}
            row = store.conn.execute("SELECT * FROM runtime_bootstrap_sessions WHERE workstream_id=? AND session_file=?", (workstream_id, session_file)).fetchone()
            full = row is None
            if row is None:
                now = utc_now()
                with store.transaction():
                    store.conn.execute("INSERT INTO runtime_bootstrap_sessions(workstream_id,session_file,task_packet_delivered,bootstrap_generation,acknowledged_generation,last_event_sequence,updated_at) VALUES(?,?,0,1,0,0,?)", (workstream_id, session_file, now))
                row = store.conn.execute("SELECT * FROM runtime_bootstrap_sessions WHERE workstream_id=? AND session_file=?", (workstream_id, session_file)).fetchone()
            if row is None:
                raise PisecError("runtime bootstrap session could not be created")
            full = full or not bool(row["task_packet_delivered"])
            latest_event = store.conn.execute("SELECT COALESCE(MAX(sequence),0) FROM events WHERE workstream_id=?", (workstream_id,)).fetchone()[0]
            changed = int(latest_event) > int(row["last_event_sequence"])
            if full:
                return {"sessionFile": session_file, "generation": row["bootstrap_generation"], "fullPacket": packet, "runtimeScope": runtime_scope, "changed": True}
            return {"sessionFile": session_file, "generation": row["bootstrap_generation"], "fullPacket": None, "runtimeScope": runtime_scope, "changed": bool(changed), "coordination": [], "research": []}
        if operation == "runtime.bootstrap.ack":
            _exact(payload, auth_fields | {"sessionFile", "generation"})
            with store.transaction():
                store.conn.execute("UPDATE runtime_bootstrap_sessions SET task_packet_delivered=1,acknowledged_generation=?,last_event_sequence=(SELECT COALESCE(MAX(sequence),0) FROM events WHERE workstream_id=?),updated_at=? WHERE workstream_id=? AND session_file=?", (int(payload["generation"]), workstream_id, utc_now(), workstream_id, str(payload["sessionFile"])))
            return {"acknowledged": True, "generation": int(payload["generation"])}
        if operation == "workstream.checkpoint":
            _exact(payload, auth_fields | {"idempotencyKey", "phase", "summary", "nextAction", "evidence"}, {"blockerCode", "blocker", "completionPacket"})
            result = checkpoint(store, workstream_id=workstream_id, runtime_instance_id=str(payload["runtimeInstanceId"]), phase=payload["phase"], summary=payload["summary"], next_action=payload["nextAction"], blocker_code=payload.get("blockerCode"), blocker=payload.get("blocker"), evidence=payload["evidence"], idempotency_key=payload["idempotencyKey"], completion_packet=payload.get("completionPacket"))
            return {"checkpoint": result}
        if operation == "workstream.completion.submit":
            _exact(payload, auth_fields | {"completionPacket"})
            return {"completionPacket": submit_completion(store, workstream_id=workstream_id, runtime_instance_id=str(payload["runtimeInstanceId"]), packet=payload["completionPacket"])}
        if operation == "coordination.request":
            _exact(payload, auth_fields | {"kind", "summary", "question", "blocking", "idempotencyKey"})
            return request_coordination(store, project_id=project_id, workstream_id=workstream_id, kind=payload["kind"], summary=payload["summary"], question=payload["question"], blocking=bool(payload["blocking"]), idempotency_key=payload["idempotencyKey"])
        if operation in {"coordination.list", "coordination.inspect"}:
            if operation == "coordination.list":
                _exact(payload, auth_fields, {"includeResolved"})
                from .workflow import list_coordination
                return {"requests": list_coordination(store, project_id=project_id, workstream_id=workstream_id, include_resolved=bool(payload.get("includeResolved")))}
            _exact(payload, auth_fields | {"requestId"})
            from .workflow import list_coordination
            rows = [row for row in list_coordination(store, project_id=project_id, workstream_id=workstream_id, include_resolved=True) if row["request_id"] == payload["requestId"]]
            if not rows:
                raise NotFoundError("coordination request was not found")
            return rows[0]
        if operation == "coordination.resolve":
            _exact(payload, auth_fields | {"requestId"})
            return acknowledge_coordination(store, project_id=project_id, workstream_id=workstream_id, request_id=payload["requestId"])
        if operation == "task.get":
            _exact(payload, auth_fields)
            result = get_task_packet(store, project_id, workstream_id)
            scope_row = store.conn.execute(
                "SELECT result_json FROM operations WHERE workstream_id=? AND kind='workstream.create' ORDER BY created_at LIMIT 1",
                (workstream_id,),
            ).fetchone()
            python_env: str | None = None
            if scope_row is not None:
                try:
                    parsed_scope = json.loads(str(scope_row["result_json"]))
                except ValueError:
                    parsed_scope = None
                if isinstance(parsed_scope, dict) and isinstance(parsed_scope.get("pythonEnv"), str):
                    python_env = parsed_scope["pythonEnv"]
            from .access import effective_runtime_scope
            scope = effective_runtime_scope(store, binding)
            result["runtimeScope"] = {"pythonEnv": scope.get("pythonEnv"), "readPaths": [{"path": path, "sources": scope.get("readPathSources", {}).get(path, [])} for path in scope.get("dataDirs", [])]}
            result["pythonEnv"] = result["runtimeScope"]["pythonEnv"]
            return result
        if operation == "research.request":
            _exact(payload, auth_fields | {"idempotencyKey", "request"})
            result = request_research(store, project_id=project_id, workstream_id=workstream_id, idempotency_key=payload["idempotencyKey"], request=payload["request"])
            self._queue_research_wake(project_id)
            return result
        if operation == "research.list":
            _exact(payload, auth_fields, {"state", "limit"})
            states = None if payload.get("state") is None else {payload["state"]}
            return {"requests": list_research_requests(store, project_id=project_id, workstream_id=workstream_id, states=states, limit=int(payload.get("limit", 32)))}
        if operation == "research.inspect":
            _exact(payload, auth_fields | {"requestId"})
            return inspect_research(store, project_id=project_id, workstream_id=workstream_id, request_id=payload["requestId"])
        if operation == "research.add_context":
            _exact(payload, auth_fields | {"requestId", "idempotencyKey", "context"})
            result = add_research_context(store, project_id=project_id, workstream_id=workstream_id, request_id=payload["requestId"], idempotency_key=payload["idempotencyKey"], context=payload["context"])
            self._queue_research_wake(project_id)
            return result
        if operation == "research.acknowledge":
            _exact(payload, auth_fields | {"requestId"})
            return acknowledge_research(store, project_id=project_id, workstream_id=workstream_id, request_id=payload["requestId"])
        raise InvalidRequestError("unsupported runtime operation")
    def _reconcile(self, store: PiStore, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._reconcile_lock:
            return self._reconcile_locked(store, payload)

    def _reconcile_locked(self, store: PiStore, payload: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"reconciled": False, "resumed": [], "errors": []}
        migration = migrate_legacy_bindings(store, self.harness, self.workspace)
        result["migration"] = migration
        result["errors"].extend({"workstreamId": item["workstreamId"], "code": "binding_migration_failed"} for item in migration["errors"])
        from .refresh import mark_stale_bindings
        result["generations"] = mark_stale_bindings(store, self.harness)
        reconciler_payload = dict(payload)
        reconciler_payload["skipWorkstreams"] = [item["workstreamId"] for item in migration["migrated"]]
        resume_candidates = [
            dict(row)
            for row in store.conn.execute(
                "SELECT r.*,w.project_id,w.kind,w.worktree_path,w.desired_state,w.provisioning_state,w.attention_reason "
                "FROM runtime_bindings r JOIN workstreams w USING(workstream_id) "
                "WHERE w.desired_state='active' "
                "AND (w.provisioning_state='bound' OR (w.provisioning_state='needs_attention' AND w.attention_reason=?)) "
                "AND r.workspace_session_name=? AND r.workspace_id IS NOT NULL "
                "AND r.workspace_view_id IS NOT NULL AND r.workspace_surface_id IS NOT NULL",
                (WORKSPACE_RUNTIME_MISSING, self.workspace.manifest.session_name),
            )
        ]
        result["workspace"] = self.workspace.reconcile(store, reconciler_payload)
        result["resumed"].extend(
            self._resume_restored_agents(
                store,
                resume_candidates,
                set(reconciler_payload["skipWorkstreams"]),
            )
        )
        result["reconciled"] = True
        for wake in pending_research_wakes(store):
            self._queue_research_wake(wake["project_id"])
        rows = store.conn.execute(
            "SELECT o.*, EXISTS(SELECT 1 FROM authorizations a WHERE a.operation_id=o.operation_id) AS has_authorization, "
            "(SELECT p.active FROM projects p WHERE p.project_id=o.project_id) AS project_active "
            "FROM operations o WHERE o.state IN ('applying','failed') ORDER BY o.created_at"
        ).fetchall()
        for row in rows:
            operation = dict(row)
            if operation.pop("project_active") == 0:
                result["resumed"].append({"operationId": operation["operation_id"], "state": "skipped-inactive-project"})
                continue
            try:
                if operation["kind"] == "workstream.create" and operation["has_authorization"] and operation["result_json"]:
                    scope = json.loads(operation["result_json"])
                    if not isinstance(scope, dict):
                        raise InvalidRequestError("durable workstream scope is invalid")
                    applied = authorize_apply_workstream(store, scope=scope, harness=self.harness, workspace=self.workspace, git_objects=self.git_objects)
                    result["resumed"].append({"operationId": applied["operation"]["operation_id"], "state": applied["operation"]["state"]})
                elif operation["kind"] == "secretary.ensure":
                    from .secretary import ensure_secretary
                    resumed = ensure_secretary(store, str(operation["project_id"]), self.harness, self.workspace)
                    result["resumed"].append({"operationId": operation["operation_id"], "reused": resumed.get("reused", False)})
                elif operation["kind"] == "first_mate.ensure":
                    resumed = ensure_first_mate(store, str(operation["project_id"]), self.harness, self.workspace)
                    result["resumed"].append({"operationId": operation["operation_id"], "reused": resumed.get("reused", False)})
            except BaseException as error:
                result["errors"].append({"operationId": operation["operation_id"], "code": getattr(error, "code", "internal_error")})
        return result

    def _admin(self, store: PiStore, operation: str, payload: dict[str, Any]) -> Any:
        if operation == "project.register":
            _exact(payload, {"path"}, {"displayName", "defaultRef", "dataDirs"})
            return _public_project(register_project(store, payload["path"], display_name=payload.get("displayName"), default_ref=payload.get("defaultRef"), data_dirs=payload.get("dataDirs")))
        if operation == "project.policy.update":
            _exact(payload, {"project"}, {"coordinationMode", "workerCreationPolicy", "workerCreationPolicyJson", "mergePolicy", "mergePolicyJson"})
            project = update_project_policy(
                store,
                str(payload["project"]),
                coordination_mode=payload.get("coordinationMode"),
                worker_creation_policy=payload.get("workerCreationPolicy"),
                worker_creation_policy_json=payload.get("workerCreationPolicyJson"),
                merge_policy=payload.get("mergePolicy"),
                merge_policy_json=payload.get("mergePolicyJson"),
            )
            return _public_project(project)
        if operation == "project.list":
            _exact(payload, set(), {"includeInactive"})
            include_inactive = payload.get("includeInactive") is True
            every_project = [_public_project(row) for row in list_projects(store, include_inactive=True)]
            active_projects = [row for row in every_project if row.get("active")]
            return {
                "projects": every_project if include_inactive else active_projects,
                "inactiveProjects": [row for row in every_project if not row.get("active")],
                "includeInactive": include_inactive,
            }
        if operation == "presentation.snapshot":
            _exact(payload, set())
            return _presentation_snapshot(store)
        if operation == "project.activity":
            _exact(payload, set(), {"project", "after"})
            if payload.get("project"):
                project = resolve_project(store, str(payload["project"]))
                return project_activity(store, project["project_id"], int(payload.get("after", 0)))
            raise InvalidRequestError("project.activity requires a project selector")
        if operation == "fleet.activity":
            _exact(payload, set(), {"after"})
            return fleet_activity(store, int(payload.get("after", 0)))
        if operation == "system.status":
            _exact(payload, set(), {"project", "includeInactive"})
            if payload.get("project"):
                project = resolve_project(store, str(payload["project"]))
                return _public_project_status(project_status(store, project["project_id"]))
            include_inactive = payload.get("includeInactive") is True
            every_project = [_public_project(row) for row in list_projects(store, include_inactive=True)]
            active_projects = [row for row in every_project if row.get("active")]
            return {
                "projects": every_project if include_inactive else active_projects,
                "inactiveProjects": [row for row in every_project if not row.get("active")],
                "firstMate": _first_mate_summary(store),
                "schema": "pisec-core",
                "version": 14,
                "operationContracts": operation_manifest(),
            }
        if operation == "project.open":
            _exact(payload, {"project"})
            from .secretary import ensure_secretary, focus_secretary
            result = ensure_secretary(store, str(payload["project"]), self.harness, self.workspace)
            focus_secretary(store, str(payload["project"]), self.workspace)
            return {
                "project": _public_project(result["project"]),
                "workstream": _public_workstream(result["workstream"]),
                "binding": _public_binding(result.get("binding")),
                "focused": True,
                "reused": bool(result.get("reused", False)),
            }
        if operation == "project.deactivate":
            _exact(payload, {"project", "confirm"})
            if payload["confirm"] != payload["project"]:
                raise ConflictError("deactivation confirmation does not match the project selector")
            from .projects import deactivate_project
            result = deactivate_project(store, str(payload["project"]), self.workspace, self.harness)
            project = get_project(store, result["projectId"])
            return {
                "project": _public_project(project),
                "workstreamId": result.get("workstreamId"),
                "retainedSessionRoot": result.get("retainedSessionRoot"),
                "reused": bool(result.get("reused", False)),
            }
        if operation == "project.activate":
            _exact(payload, {"project"})
            from .projects import activate_project
            result = activate_project(store, str(payload["project"]))
            project = get_project(store, result["projectId"])
            return {"project": _public_project(project), "reused": bool(result.get("reused", False))}
        if operation == "project.refresh":
            _exact(payload, {"all"}, {"waitSeconds"})
            if payload["all"] is not True:
                raise InvalidRequestError("project refresh currently requires --all")
            wait_seconds = payload.get("waitSeconds", 300)
            from .refresh import refresh_projects
            with self._reconcile_lock:
                return refresh_projects(store, self.harness, self.workspace, wait_seconds=wait_seconds)
        if operation == "runtime.release.build":
            _exact(payload, set())
            from .releases import build_runtime_release
            return build_runtime_release(store, self.harness)
        if operation == "runtime.release.list":
            _exact(payload, set())
            current = store.conn.execute("SELECT release_id,activated_at FROM runtime_release_channels WHERE channel='current'").fetchone()
            return {
                "currentReleaseId": None if current is None else current["release_id"],
                "activatedAt": None if current is None else current["activated_at"],
                "releases": [dict(row) for row in store.conn.execute("SELECT * FROM runtime_releases ORDER BY created_at DESC,release_id")],
            }
        if operation == "runtime.release.activate":
            _exact(payload, {"releaseId"})
            from .releases import activate_runtime_release
            return activate_runtime_release(store, str(payload["releaseId"]))
        if operation == "secretary.ensure":
            _exact(payload, {"project"})
            from .secretary import ensure_secretary
            result = ensure_secretary(store, str(payload["project"]), self.harness, self.workspace)
            return {
                "project": _public_project(result["project"]),
                "workstream": _public_workstream(result["workstream"]),
                "binding": _public_binding(result.get("binding")),
                "reused": bool(result.get("reused", False)),
            }
        if operation == "first_mate.ensure":
            _exact(payload, {"project"})
            result = ensure_first_mate(store, str(payload["project"]), self.harness, self.workspace)
            return {
                "project": _public_project(result["project"]),
                "workstream": _public_workstream(result["workstream"]),
                "binding": _public_binding(result.get("binding")),
                "reused": bool(result.get("reused", False)),
            }
        if operation == "first_mate.focus":
            _exact(payload, set())
            return focus_first_mate(store, self.workspace)
        if operation == "secretary.focus":
            _exact(payload, {"project"})
            from .secretary import focus_secretary
            return focus_secretary(store, str(payload["project"]), self.workspace)
        if operation == "workstream.focus":
            _exact(payload, {"project", "workstreamId"})
            project = resolve_project(store, str(payload["project"]))
            return focus_workstream(store, project["project_id"], str(payload["workstreamId"]), self.workspace)
        if operation == "system.reconcile":
            _exact(payload, set(), {"event", "payload"})
            return self._reconcile(store, payload)
        if operation == "workspace.startup":
            _exact(payload, {"adapterId", "socketPath"})
            if payload["adapterId"] != self.workspace.manifest.adapter_id:
                raise InvalidRequestError("workspace adapter does not match the configured adapter")
            return self._defer_reconcile(payload)
        if operation == "workspace.event":
            _exact(payload, {"adapterId", "event", "payload"})
            if payload["adapterId"] != self.workspace.manifest.adapter_id:
                raise InvalidRequestError("workspace adapter does not match the configured adapter")
            return self._defer_reconcile(payload)
        if operation == "workstream.cleanup":
            _exact(payload, {"workstreamId", "confirm"}, {"project", "forceDirty"})
            result = cleanup_workstream(store, payload, self.workspace, self.harness)
            return {
                "operation": _public_operation(result.get("operation")),
                "workstream": _public_workstream(result["workstream"]),
                "reused": bool(result.get("reused", False)),
            }
        if operation == "system.doctor":
            _exact(payload, set(), {"liveSearchWorkstream"})
            from .doctor import run_doctor
            return run_doctor(store, self.config, self.registry, live_search_workstream=payload.get("liveSearchWorkstream"), workspace=self.workspace, harness=self.harness)
        raise InvalidRequestError("unsupported admin operation")

    def _secretary(self, store: PiStore, operation: str, project_id: str, secretary_workstream_id: str, payload: dict[str, Any]) -> Any:
        if operation not in {"project.status", "project.activity", "issue.list", "issue.inspect", "git.status", "git.workstream_changes", "workstream.list", "workstream.inspect", "coordination.list", "coordination.inspect", "decision.list", "research.list", "research.inspect"}:
            assert_project_writable(store, project_id)
        if operation == "project.status":
            _exact(payload, set())
            return _public_project_status(project_status(store, project_id))
        if operation == "project.activity":
            _exact(payload, set(), {"after"})
            return project_activity(store, project_id, int(payload.get("after", 0)))
        if operation == "coordination.list":
            _exact(payload, set(), {"workstreamId", "includeResolved"})
            from .workflow import list_coordination
            return {"requests": list_coordination(store, project_id=project_id, workstream_id=payload.get("workstreamId"), include_resolved=bool(payload.get("includeResolved")))}
        if operation == "coordination.inspect":
            _exact(payload, {"requestId"})
            from .workflow import list_coordination
            rows = [row for row in list_coordination(store, project_id=project_id, include_resolved=True) if row["request_id"] == payload["requestId"]]
            if not rows:
                raise NotFoundError("coordination request was not found")
            return rows[0]
        if operation == "coordination.answer":
            _exact(payload, {"requestId", "response", "idempotencyKey"}, {"decisionId"})
            return answer_coordination(store, project_id=project_id, secretary_workstream_id=secretary_workstream_id, request_id=payload["requestId"], response=payload["response"], idempotency_key=payload["idempotencyKey"], decision_id=payload.get("decisionId"))
        if operation == "access.list":
            _exact(payload, set())
            from .access import list_access_grants
            return {"grants": list_access_grants(store, project_id=project_id)}
        if operation in {"issue.report", "secretary.issue.report"}:
            _exact(payload, {"category", "severity", "summary", "details", "requestedAction", "evidence", "idempotencyKey"})
            return report_issue(store, project_id=project_id, reporter_workstream_id=secretary_workstream_id, category=payload["category"], severity=payload["severity"], summary=payload["summary"], details=payload["details"], requested_action=payload["requestedAction"], evidence=payload["evidence"], idempotency_key=payload["idempotencyKey"])
        if operation == "issue.list":
            _exact(payload, set(), {"state", "limit"})
            return {"issues": list_issues(store, project_id=project_id, state=payload.get("state"), limit=int(payload.get("limit", 100)))}
        if operation == "issue.inspect":
            _exact(payload, {"issueId"})
            return inspect_issue(store, issue_id=payload["issueId"], project_id=project_id)
        if operation == "issue.add_context":
            _exact(payload, {"issueId", "context", "idempotencyKey"})
            return add_issue_context(store, project_id=project_id, issue_id=payload["issueId"], actor_id=secretary_workstream_id, context=payload["context"], idempotency_key=payload["idempotencyKey"])
        if operation == "issue.verify":
            _exact(payload, {"issueId", "status", "evidence", "idempotencyKey"})
            return verify_issue(store, project_id=project_id, issue_id=payload["issueId"], actor_id=secretary_workstream_id, status=payload["status"], evidence=payload["evidence"], idempotency_key=payload["idempotencyKey"])
        if operation == "git.status":
            _exact(payload, set())
            return git_status(store, project_id)
        if operation == "git.workstream_changes":
            _exact(payload, {"workstreamId"})
            return inspect_workstream_changes(store, project_id, payload["workstreamId"])
        if operation == "git.merge.prepare":
            _exact(payload, {"workstreamId"})
            return {"approvalScope": prepare_workstream_merge(store, project_id, payload["workstreamId"])}
        if operation == "git.merge.apply":
            _exact(payload, {"approvalScope"})
            return apply_workstream_merge(store, project_id, payload["approvalScope"])
        if operation == "workstream.list":
            _exact(payload, set())
            return {"workstreams": [_public_workstream(row) for row in list_workstreams(store, project_id)]}
        if operation == "workstream.inspect":
            _exact(payload, {"workstreamId"})
            return _public_inspect(inspect_workstream(store, project_id, payload["workstreamId"]))
        if operation == "workstream.prepare":
            _exact(payload, {"title", "purpose", "brief", "taskPacket", "idempotencyKey"}, {"targetRef", "executionProfile", "externalDomains", "pythonEnv", "workMode", "learningOverlay", "learningSeam", "decisionIds"})
            profile = payload.get("executionProfile", "worker-default")
            prepared = prepare_workstream(store, project_id=project_id, title=payload["title"], purpose=payload["purpose"], brief=payload["brief"], task_packet=payload["taskPacket"], idempotency_key=payload["idempotencyKey"], harness=self.harness, workspace=self.workspace, target_ref=payload.get("targetRef"), execution_profile=profile, work_mode=payload.get("workMode", "BUILD"), learning_overlay=payload.get("learningOverlay", "LIGHT"), learning_seam=payload.get("learningSeam"), decision_ids=payload.get("decisionIds", []), external_domains=payload.get("externalDomains", []), python_env=payload.get("pythonEnv"))
            return {"operation": _public_operation(prepared["operation"]), "workstream": _public_workstream(prepared["workstream"]), "approvalScope": prepared["approvalScope"]}
        if operation == "workstream.authorize_apply":
            _exact(payload, {"approvalScope"})
            applied = authorize_apply_workstream(store, scope=payload["approvalScope"], harness=self.harness, workspace=self.workspace, git_objects=self.git_objects)
            return {"operation": _public_operation(applied["operation"]), "workstream": _public_workstream(applied["workstream"])}
        if operation == "workstream.send":
            _exact(payload, {"workstreamId", "text"})
            result = send_workstream(store, project_id, payload["workstreamId"], payload["text"], self.workspace)
            return {"workstreamId": result["workstreamId"], "delivered": result["delivered"]}
        if operation == "workstream.focus":
            _exact(payload, {"workstreamId"})
            return focus_workstream(store, project_id, payload["workstreamId"], self.workspace)
        if operation == "workstream.complete":
            _exact(payload, {"workstreamId", "completionPacketSha256"})
            return _public_workstream(complete_workstream(store, project_id, payload["workstreamId"], payload["completionPacketSha256"], self.workspace))
        if operation == "workstream.retire":
            _exact(payload, {"workstreamId"})
            return _public_workstream(retire_workstream(store, project_id, payload["workstreamId"], self.workspace))
        if operation == "decision.list":
            _exact(payload, set(), {"state"})
            return {"decisions": list_decisions(store, project_id, state=payload.get("state"))}
        if operation == "decision.record":
            _exact(payload, {"summary", "context"}, {"workstreamId"})
            return record_decision(store, project_id=project_id, summary=payload["summary"], context=payload["context"], workstream_id=payload.get("workstreamId"))
        if operation == "decision.resolve":
            _exact(payload, {"decisionId", "resolution"})
            return resolve_decision(store, project_id=project_id, decision_id=payload["decisionId"], resolution=payload["resolution"])
        if operation == "research.list":
            _exact(payload, set(), {"state", "limit", "workstreamId"})
            states = None if payload.get("state") is None else {payload["state"]}
            return {"requests": list_research_requests(store, project_id=project_id, workstream_id=payload.get("workstreamId"), states=states, limit=int(payload.get("limit", 32)))}
        if operation == "research.inspect":
            _exact(payload, {"requestId"}, {"workstreamId"})
            return inspect_research(store, project_id=project_id, request_id=payload["requestId"], workstream_id=payload.get("workstreamId"))
        if operation == "research.claim":
            _exact(payload, {"requestId"})
            return claim_research(store, project_id=project_id, secretary_workstream_id=secretary_workstream_id, request_id=payload["requestId"])
        if operation == "research.request_context":
            _exact(payload, {"requestId", "idempotencyKey", "contextRequest"})
            result = request_research_context(store, project_id=project_id, secretary_workstream_id=secretary_workstream_id, request_id=payload["requestId"], idempotency_key=payload["idempotencyKey"], context_request=payload["contextRequest"])
            self._queue_research_wake(project_id)
            return result
        if operation == "research.answer":
            _exact(payload, {"requestId", "idempotencyKey", "result"})
            result = answer_research(store, project_id=project_id, secretary_workstream_id=secretary_workstream_id, request_id=payload["requestId"], idempotency_key=payload["idempotencyKey"], result=payload["result"])
            self._notify_worker_research(store, result, "answered")
            return result
        if operation == "research.decline":
            _exact(payload, {"requestId", "idempotencyKey", "decline"})
            result = decline_research(store, project_id=project_id, secretary_workstream_id=secretary_workstream_id, request_id=payload["requestId"], idempotency_key=payload["idempotencyKey"], decline=payload["decline"])
            self._notify_worker_research(store, result, "declined")
            return result
        raise InvalidRequestError("unsupported secretary operation")
    def _fleet_project(self, store: PiStore, payload: Mapping[str, Any]) -> str:
        project_id = payload.get("projectId")
        if not isinstance(project_id, str):
            raise InvalidRequestError("fleet projectId is required")
        return str(require_fleet_project(store, project_id)["project_id"])

    def _fleet(self, store: PiStore, operation: str, first_mate_workstream_id: str, payload: dict[str, Any]) -> Any:
        if operation == "fleet.activity":
            _exact(payload, set(), {"after"})
            return fleet_activity(store, int(payload.get("after", 0)))
        if operation == "fleet.status":
            _exact(payload, set(), {"projectId"})
            if payload.get("projectId") is not None:
                project_id = self._fleet_project(store, payload)
                return _public_project_status(project_status(store, project_id))
            statuses = [_public_project_status(project_status(store, project["project_id"])) for project in list_fleet_projects(store)]
            return {"projects": statuses}
        if operation == "fleet.issue.list":
            _exact(payload, set(), {"projectId", "state", "limit"})
            project_id = payload.get("projectId")
            if project_id is not None:
                project_id = self._fleet_project(store, payload)
            return {"issues": list_issues(store, project_id=project_id, project_ids=None if project_id is not None else fleet_project_ids(store), state=payload.get("state"), limit=int(payload.get("limit", 100)))}
        if operation == "fleet.issue.inspect":
            _exact(payload, {"issueId"}, {"projectId"})
            project_id = payload.get("projectId")
            if project_id is not None:
                project_id = self._fleet_project(store, payload)
            issue = inspect_issue(store, issue_id=payload["issueId"], project_id=project_id)
            if project_id is None:
                require_fleet_project(store, str(issue["project_id"]))
            return issue
        if operation == "fleet.issue.add_context":
            _exact(payload, {"projectId", "issueId", "context", "idempotencyKey"})
            project_id = self._fleet_project(store, payload)
            return add_issue_context(store, project_id=project_id, issue_id=payload["issueId"], actor_id=first_mate_workstream_id, context=payload["context"], idempotency_key=payload["idempotencyKey"])
        if operation == "fleet.issue.acknowledge":
            _exact(payload, {"projectId", "issueId"})
            project_id = self._fleet_project(store, payload)
            return acknowledge_issue(store, project_id=project_id, issue_id=payload["issueId"], actor_id=first_mate_workstream_id)
        if operation == "fleet.issue.resolve":
            _exact(payload, {"projectId", "issueId", "disposition", "reason", "decisionId"})
            project_id = self._fleet_project(store, payload)
            return resolve_issue(store, project_id=project_id, issue_id=payload["issueId"], actor_id=first_mate_workstream_id, disposition=payload["disposition"], reason=payload["reason"], decision_id=payload["decisionId"])
        if operation == "fleet.access.list":
            _exact(payload, {"projectId"}, {"workstreamId"})
            from .access import list_access_grants
            return {"grants": list_access_grants(store, project_id=self._fleet_project(store, payload), workstream_id=payload.get("workstreamId"))}
        if operation == "fleet.access.inspect":
            _exact(payload, {"projectId", "grantId"})
            from .access import _grant_row
            project_id = self._fleet_project(store, payload)
            grant = _grant_row(store, payload["grantId"])
            if grant["project_id"] != project_id:
                raise NotFoundError("access grant was not found")
            return dict(grant)
        if operation == "fleet.access.grant.prepare":
            _exact(payload, {"projectId", "subjectKind", "path", "idempotencyKey"}, {"workstreamId", "issueId"})
            from .access import prepare_access_grant
            return prepare_access_grant(store, project_id=self._fleet_project(store, payload), subject_kind=payload["subjectKind"], workstream_id=payload.get("workstreamId"), path=payload["path"], issue_id=payload.get("issueId"), idempotency_key=payload["idempotencyKey"])
        if operation == "fleet.access.grant.apply":
            _exact(payload, {"projectId", "approvalScope"})
            from .access import authorize_apply_access_grant
            project_id = self._fleet_project(store, payload)
            approval_scope = payload["approvalScope"]
            if not isinstance(approval_scope, Mapping) or approval_scope.get("projectId") != project_id:
                raise ScopeMismatchError("access grant project does not match the selected project")
            return authorize_apply_access_grant(store, scope=approval_scope, harness=self.harness, workspace=self.workspace)
        if operation == "fleet.access.revoke.prepare":
            _exact(payload, {"projectId", "grantId", "idempotencyKey"})
            from .access import prepare_access_revoke
            return prepare_access_revoke(store, project_id=self._fleet_project(store, payload), grant_id=payload["grantId"], idempotency_key=payload["idempotencyKey"])
        if operation == "fleet.access.revoke.apply":
            _exact(payload, {"projectId", "approvalScope"})
            from .access import authorize_apply_access_revoke
            project_id = self._fleet_project(store, payload)
            approval_scope = payload["approvalScope"]
            if not isinstance(approval_scope, Mapping) or approval_scope.get("projectId") != project_id:
                raise ScopeMismatchError("access revoke project does not match the selected project")
            return authorize_apply_access_revoke(store, scope=approval_scope, harness=self.harness, workspace=self.workspace)
        if operation == "fleet.events":
            _exact(payload, set(), {"after", "limit"})
            rows = list_events(store, after=int(payload.get("after", 0)), limit=int(payload.get("limit", 256)), project_ids=fleet_project_ids(store))
            return {"events": [{"sequence": row["sequence"], "eventId": row["event_id"], "kind": row["kind"], "projectId": row["project_id"], "workstreamId": row["workstream_id"], "operationId": row["operation_id"], "createdAt": row["created_at"]} for row in rows]}
        if operation == "fleet.secretary.send":
            _exact(payload, {"projectId", "text"}, {"workstreamId"})
            project_id = self._fleet_project(store, payload)
            project = get_project(store, project_id)
            workstream_id = payload.get("workstreamId") or project.get("secretary_workstream_id")
            if not isinstance(workstream_id, str):
                raise NotFoundError("project has no secretary")
            secretary = store.conn.execute("SELECT kind,desired_state FROM workstreams WHERE project_id=? AND workstream_id=?", (project_id, workstream_id)).fetchone()
            if secretary is None or secretary["kind"] != "secretary" or secretary["desired_state"] == "retired":
                raise NotFoundError("project secretary was not found")
            result = send_workstream(store, project_id, workstream_id, payload["text"], self.workspace)
            return {"projectId": project_id, "workstreamId": result["workstreamId"], "delivered": result["delivered"]}
        if operation == "fleet.workstream.list":
            _exact(payload, {"projectId"})
            return {"workstreams": [_public_workstream(row) for row in list_workstreams(store, self._fleet_project(store, payload))]}
        if operation == "fleet.workstream.inspect":
            _exact(payload, {"projectId", "workstreamId"})
            return _public_inspect(inspect_workstream(store, self._fleet_project(store, payload), payload["workstreamId"]))
        if operation == "fleet.git.workstream_changes":
            _exact(payload, {"projectId", "workstreamId"})
            return inspect_workstream_changes(store, self._fleet_project(store, payload), payload["workstreamId"])
        if operation == "fleet.workstream.prepare":
            _exact(payload, {"projectId", "title", "purpose", "brief", "taskPacket", "idempotencyKey"}, {"targetRef", "executionProfile", "externalDomains", "pythonEnv"})
            project_id = self._fleet_project(store, payload)
            profile = payload.get("executionProfile", "worker-default")
            prepared = prepare_workstream(store, project_id=project_id, title=payload["title"], purpose=payload["purpose"], brief=payload["brief"], task_packet=payload["taskPacket"], idempotency_key=payload["idempotencyKey"], harness=self.harness, workspace=self.workspace, target_ref=payload.get("targetRef"), execution_profile=profile, external_domains=payload.get("externalDomains", []), python_env=payload.get("pythonEnv"))
            return {"operation": _public_operation(prepared["operation"]), "workstream": _public_workstream(prepared["workstream"]), "approvalScope": prepared["approvalScope"]}
        if operation == "fleet.workstream.authorize_apply":
            _exact(payload, {"projectId", "approvalScope"})
            project_id = self._fleet_project(store, payload)
            scope = payload["approvalScope"]
            if not isinstance(scope, Mapping) or scope.get("projectId") != project_id:
                raise InvalidRequestError("fleet approval scope project does not match projectId")
            applied = authorize_apply_workstream(store, scope=scope, harness=self.harness, workspace=self.workspace, git_objects=self.git_objects, actor="first_mate")
            return {"operation": _public_operation(applied["operation"]), "workstream": _public_workstream(applied["workstream"])}
        if operation == "fleet.git.merge.prepare":
            _exact(payload, {"projectId", "workstreamId"})
            return {"approvalScope": prepare_workstream_merge(store, self._fleet_project(store, payload), payload["workstreamId"])}
        if operation == "fleet.git.merge.apply":
            _exact(payload, {"projectId", "approvalScope"})
            project_id = self._fleet_project(store, payload)
            scope = payload["approvalScope"]
            if not isinstance(scope, Mapping) or scope.get("projectId") != project_id:
                raise InvalidRequestError("fleet merge scope project does not match projectId")
            return apply_workstream_merge(store, project_id, scope)
        raise InvalidRequestError("unsupported fleet operation")

    def _notify_worker_research(self, store: PiStore, result: Mapping[str, Any], disposition: str) -> None:
        workstream_id = result.get("workstream_id")
        request_id = result.get("request_id")
        if not isinstance(workstream_id, str) or not isinstance(request_id, str):
            return
        binding = store.conn.execute("SELECT workspace_surface_id FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()
        if binding is None:
            return
        try:
            self.workspace.prompt_agent(binding["workspace_surface_id"], f"Secretary {disposition} research request {request_id}; retrieve it through Pisec.", ("working", "blocked", "idle"), 30000)
        except Exception:
            logger.exception("best-effort worker research delivery failed")


class _UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class BrokerService:
    def __init__(self, dispatcher: BrokerDispatcher, *, runtime_root: Path | None = None):
        self.dispatcher = dispatcher
        self.paths = socket_paths(runtime_root)
        self.servers: list[_UnixServer] = []
        self.threads: list[threading.Thread] = []

    def _prepare_path(self, path: Path) -> None:
        runtime_root = path.parent.parent
        if not runtime_root.is_absolute():
            raise UnsafeStateError("broker runtime directory must be absolute", detail={"path": str(runtime_root)})
        if runtime_root.is_symlink():
            raise UnsafeStateError("refusing to use an unsafe broker runtime directory", detail={"path": str(runtime_root)})
        if runtime_root.exists():
            info = runtime_root.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise UnsafeStateError("refusing to use an unsafe broker runtime directory", detail={"path": str(runtime_root)})
        else:
            parent = runtime_root.parent
            if parent.is_symlink():
                raise UnsafeStateError("refusing to create a broker runtime directory below a symlink", detail={"path": str(parent)})
            runtime_root.mkdir(parents=True, mode=0o700)
            os.chmod(runtime_root, 0o700)
        parent = path.parent
        if parent.is_symlink():
            raise UnsafeStateError("refusing to use an unsafe broker socket directory", detail={"path": str(parent)})
        parent.mkdir(mode=0o700, exist_ok=True)
        for directory in (runtime_root, parent):
            info = directory.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise UnsafeStateError("refusing to use an unsafe broker runtime directory", detail={"path": str(directory)})
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise UnsafeStateError("refusing to replace unsafe broker socket path", detail={"path": str(path)})
            path.unlink()

    def start(self) -> None:
        self.dispatcher.start_background()
        for kind, path in self.paths.items():
            self._prepare_path(path)
            dispatcher = self.dispatcher

            class Handler(socketserver.StreamRequestHandler):
                socket_kind = kind
                def handle(self) -> None:
                    request_id: str | None = None
                    try:
                        raw = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
                        request = decode_request(raw)
                        request_id = request["requestId"]
                        result = dispatcher.dispatch(self.socket_kind, request["operation"], request["payload"])
                        response = success_response(request_id, result)
                    except BaseException as error:
                        response = error_response(request_id, error)
                    try:
                        self.wfile.write(response)
                    except BrokenPipeError:
                        pass

            server = _UnixServer(str(path), Handler)
            os.chmod(path, 0o600)
            self.servers.append(server)
            thread = threading.Thread(target=server.serve_forever, name=f"pisec-{kind}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self) -> None:
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=5)
        for path in self.paths.values():
            if path.exists() and stat.S_ISSOCK(path.lstat().st_mode):
                path.unlink()
        self.servers.clear()
        self.threads.clear()
        self.dispatcher.stop_background()
