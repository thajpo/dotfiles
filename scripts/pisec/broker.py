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
from .attention import ATTENTION_WAKE_PROMPT, compact_attention, inspect_attention, list_open_attention, wait_for_attention_hint
from .cleanup import cleanup_workstream
from .decisions import list_decisions, record_decision, resolve_decision
from .events import append_event_in_transaction, list_events
from .models import AuthorizationError, ConflictError, InvalidRequestError, NeedsAttentionError, NotFoundError, PisecError, ScopeMismatchError, UnsafeStateError, bounded_text, canonical_json, utc_now, validate_id
from .pi_store import PiStore
from .projects import assert_project_writable, fleet_activity, first_mate_issue_project_ids, fleet_project_ids, get_project, is_first_mate_issue_project, list_fleet_projects, list_projects, project_activity, project_status, register_project, require_fleet_project, resolve_project
from .protocol import MAX_MESSAGE_BYTES, decode_request, error_response, success_response
from .runtime_eligibility import runtime_eligible_sql, runtime_lifecycle_eligible
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
    request_research,
    request_research_context,
)
from .runtime import WORKSPACE_RUNTIME_MISSING, prepare_runtime_turn, prepare_session_switch, record_runtime_tool_failure, report_runtime, start_bound_agent, usable_runtime_binding, verify_runtime_binding
from .first_mate import ensure_first_mate, focus_first_mate
from .secretary_git import git_status, inspect_workstream_changes, push_branch
from .integration import apply_workstream_acceptance, inspect_integration, list_integrations, prepare_workstream_acceptance, reconcile_integrations
from .workflow import acknowledge_coordination, acknowledge_issue, add_issue_context, answer_coordination, checkpoint, escalate_issue, inspect_issue, link_issue_remediation, list_issues, request_help, request_issue_remediation, request_issue_verification, report_issue, resolve_issue, submit_completion, verify_issue
from .workstreams import authorize_apply_workstream, focus_workstream, inspect_workstream, list_workstreams, prepare_workstream, retire_workstream
from .operation_contracts import SOCKET_OPERATIONS
ADMIN_OPERATIONS = SOCKET_OPERATIONS["admin"]
SECRETARY_OPERATIONS = SOCKET_OPERATIONS["secretary"]
FLEET_OPERATIONS = SOCKET_OPERATIONS["fleet"]
RUNTIME_OPERATIONS = SOCKET_OPERATIONS["runtime"]
from .operation_contracts import operation_manifest
from .runtime_surface import capture_runtime_surface
from .control_plane import control_plane_lock
WORKSPACE_RECONCILE_INTERVAL_SECONDS = 5.0
WORKSPACE_STARTUP_GRACE_SECONDS = 2.0
RUNTIME_RESTART_BACKOFF_SECONDS = 30.0
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


_PUBLIC_PROJECT_FIELDS = ("project_id", "display_name", "default_ref", "data_dirs", "external_domains", "secretary_workstream_id", "coordination_mode", "active", "lifecycle_attention_reason", "created_at", "updated_at", "deactivated_at", "taskState", "runtimeState", "attentionCount", "attentionPriority", "nextAction")
_PUBLIC_WORKSTREAM_FIELDS = (
    "workstream_id", "project_id", "kind", "title", "purpose", "brief", "harness_id", "workspace_adapter_id",
    "execution_profile", "target_ref", "base_commit_oid", "branch_name",
    "desired_state", "provisioning_state", "created_at", "updated_at", "completed_at", "retired_at",
    "observed_state", "last_observed_at", "agent_name", "task_packet_id", "task_packet_sha256",
    "desired_generation_sha256", "applied_generation_sha256", "runtime_stale",
    "taskState", "runtimeState", "attentionCount", "attentionPriority", "nextAction", "taskStateError",
)
_PUBLIC_BINDING_FIELDS = ("workstream_id", "workspace_adapter_id", "workspace_session_name", "harness_id", "agent_name", "observed_state", "runtime_instance_id", "last_observed_at", "updated_at")
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
    result = {
        "workstream": _public_workstream(value["workstream"]),
        "binding": _public_binding(value.get("binding")),
        "operation": _public_operation(value.get("operation")),
    }
    operation = value.get("operation")
    if isinstance(operation, Mapping):
        try:
            scope = json.loads(str(operation["result_json"]))
        except (KeyError, TypeError, json.JSONDecodeError):
            scope = None
        if isinstance(scope, Mapping) and isinstance(scope.get("importSource"), Mapping):
            result["importSource"] = dict(scope["importSource"])
    return result


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
            WHERE """ + runtime_eligible_sql("w") + """
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
        config: Mapping[str, Any] | None = None,
        prepare_surfaces: bool = True,
    ):
        self.store_factory = store_factory
        self.registry = registry
        self.harness = harness
        self.workspace = workspace
        self.config = dict(config or {})
        self._reconcile_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._reconcile_stop = threading.Event()
        self._reconcile_thread: threading.Thread | None = None
        self._attention_thread: threading.Thread | None = None
        self._attention_wake_deadlines: dict[str, float] = {}
        self._attention_wake_lock = threading.Lock()
        self._reconcile_lock = threading.Lock()
        self._last_resume_attempt: dict[str, float] = {}
        self._surfaces: dict[str, Any] = {}
        if prepare_surfaces:
            for harness_id in self.registry.harness_ids():
                selected = self.registry.resolve_harness(harness_id)
                selected.prepare_runtime_surface()
                self._surfaces[harness_id] = capture_runtime_surface(selected)

    def _worker_route(self, model: str | None) -> tuple[HarnessAdapter, str | None, str | None, str]:
        routing = self.config.get("workerRouting")
        if model is not None:
            if not isinstance(model, str) or not model:
                raise InvalidRequestError("implementationModel must be a configured worker route")
            requested = model
        elif isinstance(routing, Mapping):
            requested = routing.get("defaultModel")
        else:
            return self.harness, None, None, self.harness.manifest.adapter_id
        if not isinstance(requested, str) or not requested:
            raise InvalidRequestError("worker routing defaultModel is missing")
        route = routing.get("routes", {}).get(requested) if isinstance(routing, Mapping) and isinstance(routing.get("routes"), Mapping) else None
        if not isinstance(route, Mapping):
            raise InvalidRequestError(f"implementationModel is not a configured worker route: {requested}")
        if not isinstance(route.get("harness"), str) or not isinstance(route.get("model"), str) or not isinstance(route.get("reasoningEffort"), str):
            raise InvalidRequestError(f"configured worker route is invalid: {requested}")
        if model is None:
            from .config import DEFAULT_WORKER_MODEL, DEFAULT_WORKER_ROUTE
            if requested != DEFAULT_WORKER_MODEL or {key: route.get(key) for key in DEFAULT_WORKER_ROUTE} != DEFAULT_WORKER_ROUTE:
                raise InvalidRequestError("worker routing defaultModel must resolve to Codex GPT-5.6 Luna with high reasoning")
        adapter = self.registry.resolve_harness(str(route["harness"]))
        return adapter, str(route["model"]), str(route["reasoningEffort"]), adapter.manifest.adapter_id

    def _harness_for_workstream(self, store: PiStore, workstream_id: str) -> HarnessAdapter:
        row = store.conn.execute("SELECT harness_id FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()
        if row is None:
            raise NotFoundError("workstream was not found")
        return self.registry.resolve_harness(str(row["harness_id"]))

    def _harness_for_scope(self, scope: Mapping[str, Any]) -> HarnessAdapter:
        return self.registry.resolve_harness(str(scope["harnessId"]))

    def _surface_for_harness(self, harness_id: str) -> Any:
        surface = self._surfaces.get(str(harness_id))
        if surface is None:
            raise NeedsAttentionError("current runtime surface is missing or corrupt; run pisec update")
        return surface

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
                if current_row is None or current_row["refresh_pending"] or not runtime_lifecycle_eligible(store, workstream_id) or current_row["provisioning_state"] not in {"bound", "needs_attention"}:
                    continue
                current_harness = self.registry.resolve_harness(str(current_row["harness_id"]))
                selected_generation = current_row["applied_generation_sha256"]
                from .models import validate_sha256
                try:
                    validate_sha256(selected_generation, "runtime generation")
                except InvalidRequestError:
                    continue
                if current_row["launch_generation_sha256"] is not None or current_row["desired_generation_sha256"] != selected_generation:
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
                    expected_names = {str(current_row["agent_name"]), current_harness.manifest.agent_kind}
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
                    cursor = store.conn.execute(
                        "UPDATE runtime_bindings SET observed_state='starting',runtime_instance_id=NULL,report_seq=0,session_start_event_sequence=NULL,session_start_report_seq=NULL,session_started_at=NULL,last_observed_at=?,updated_at=? WHERE workstream_id=? AND refresh_pending=0 AND refresh_operation_id IS NULL AND refresh_started_at IS NULL AND launch_generation_sha256 IS NULL AND desired_generation_sha256=? AND applied_generation_sha256=?",
                        (now, now, workstream_id, selected_generation, selected_generation),
                    )
                    if cursor.rowcount != 1:
                        continue
                    store.conn.execute(
                        "UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=?",
                        (now, workstream_id),
                    )
                result = start_bound_agent(
                    store,
                    self.workspace,
                    current_harness,
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
        self._attention_thread = threading.Thread(target=self._run_attention_watcher, name="pisec-attention-watcher", daemon=True)
        self._reconcile_thread.start()
        self._attention_thread.start()

    def stop_background(self) -> None:
        self._reconcile_stop.set()
        thread = self._reconcile_thread
        attention_thread = self._attention_thread
        if thread is not None:
            thread.join(timeout=5)
        if attention_thread is not None:
            attention_thread.join(timeout=5)
        self._reconcile_thread = None

    def _clear_attention_wake(self, workstream_id: str) -> None:
        with self._attention_wake_lock:
            self._attention_wake_deadlines.pop(workstream_id, None)

    def _attention_runtime_ready(self, store: PiStore, row: Mapping[str, Any]) -> bool:
        try:
            harness = self.registry.resolve_harness(str(row["harness_id"]))
            return usable_runtime_binding(store, str(row["workstream_id"]), self.workspace, harness, allowed_states={"idle"}, require_prompt_eligible=True)
        except BaseException:
            return False

    def _run_attention_watcher(self) -> None:
        while not self._reconcile_stop.is_set():
            wait_for_attention_hint(1.0)
            try:
                self._scan_attention()
            except BaseException:
                logger.exception("attention watcher scan failed")

    def _scan_attention(self) -> None:
        now_monotonic = time.monotonic()
        with self.store_factory() as store:
            recipients = store.conn.execute(
                "SELECT DISTINCT a.recipient_workstream_id FROM attention_items a JOIN workstreams w ON w.workstream_id=a.recipient_workstream_id WHERE " + runtime_eligible_sql("w") + " AND w.provisioning_state='bound'"
            ).fetchall()
            due: list[tuple[int, str, str]] = []
            for recipient_row in recipients:
                recipient_id = str(recipient_row["recipient_workstream_id"])
                items = list_open_attention(store, recipient_workstream_id=recipient_id, limit=32, due_only=True)
                if items:
                    item = sorted(items, key=lambda value: (int(value["priority"]), str(value["revision_at"]), recipient_id))[0]
                    due.append((int(item["priority"]), str(item["revision_at"]), recipient_id))
            for _priority, _revision, recipient_id in sorted(due):
                with self._attention_wake_lock:
                    deadline = self._attention_wake_deadlines.get(recipient_id)
                    if deadline is not None and deadline > now_monotonic:
                        continue
                binding = store.conn.execute(
                    "SELECT r.*,w.worktree_path FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=? AND " + runtime_eligible_sql("w") + " AND w.provisioning_state='bound'",
                    (recipient_id,),
                ).fetchone()
                if binding is None or not self._attention_runtime_ready(store, binding):
                    continue
                try:
                    self.workspace.trigger_agent_nowait(
                        str(binding["workspace_surface_id"]),
                        ATTENTION_WAKE_PROMPT,
                        str(binding["policy_path"]),
                    )
                except BaseException as error:
                    # One stale or temporarily unavailable pane must not starve
                    # later recipients.  The durable attention row remains due.
                    logger.warning("attention wake failed for %s: %s", recipient_id, error)
                finally:
                    with self._attention_wake_lock:
                        self._attention_wake_deadlines[recipient_id] = time.monotonic() + 30.0

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
                workstream_id = payload.get("workstreamId")
                harness = self._harness_for_workstream(store, str(workstream_id)) if isinstance(workstream_id, str) else self.harness
                return report_runtime(store, payload, harness, self.workspace)
            return self._runtime(store, operation, payload)

    def _runtime(self, store: PiStore, operation: str, payload: dict[str, Any]) -> Any:
        auth_fields = {"workstreamId", "runtimeInstanceId", "surfaceId", "token", "generation"}
        if operation == "runtime.turn.prepare":
            _exact(payload, auth_fields)
            binding = verify_runtime_binding(store, payload, worker_only=False)
            result = prepare_runtime_turn(store, payload, self.workspace, self.registry.resolve_harness(str(binding["harness_id"])))
            self._clear_attention_wake(str(payload["workstreamId"]))
            return result
        if operation == "runtime.bootstrap.ack":
            _exact(payload, auth_fields | {"bootstrapEventId", "bootstrapRevision"})
            from .runtime import acknowledge_runtime_bootstrap
            return acknowledge_runtime_bootstrap(store, payload)
        if operation == "session.switch.prepare":
            _exact(payload, auth_fields | {"reason", "targetSessionFile"})
            return prepare_session_switch(store, payload, self._harness_for_workstream(store, str(payload["workstreamId"])))
        if operation == "runtime.tool_failure":
            _exact(payload, auth_fields | {"toolName", "failureCode"})
            return record_runtime_tool_failure(store, payload)
        binding = verify_runtime_binding(store, payload, worker_only=True)
        project_id = str(binding["project_id"])
        workstream_id = str(binding["workstream_id"])
        if operation == "attention.list":
            _exact(payload, auth_fields, {"limit"})
            return {"items": [compact_attention(item) for item in list_open_attention(store, recipient_workstream_id=workstream_id, limit=int(payload.get("limit", 32)))]}
        if operation == "attention.inspect":
            _exact(payload, auth_fields | {"attentionId"})
            return inspect_attention(store, recipient_workstream_id=workstream_id, attention_id=str(payload["attentionId"]))
        if operation == "issue.report":
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
            scope = effective_runtime_scope(store, binding, harness=self._harness_for_workstream(store, workstream_id))
            return {"workstreamId": workstream_id, "readPaths": [{"path": path, "sources": scope.get("readPathSources", {}).get(path, [])} for path in scope.get("dataDirs", [])], "pythonEnv": scope.get("pythonEnv")}
        if operation == "workstream.checkpoint":
            _exact(payload, auth_fields | {"idempotencyKey", "phase", "summary", "nextAction", "evidence"}, {"remediationIssueId"})
            result = checkpoint(store, workstream_id=workstream_id, runtime_instance_id=str(payload["runtimeInstanceId"]), phase=payload["phase"], summary=payload["summary"], next_action=payload["nextAction"], evidence=payload["evidence"], idempotency_key=payload["idempotencyKey"], remediation_issue_id=payload.get("remediationIssueId"))
            return {"checkpoint": result}
        if operation == "workstream.completion.submit":
            _exact(payload, auth_fields | {"completionPacket"})
            return {"completionPacket": submit_completion(store, workstream_id=workstream_id, runtime_instance_id=str(payload["runtimeInstanceId"]), packet=payload["completionPacket"])}
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
            from .operations import authoritative_workstream_creation
            scope_row = authoritative_workstream_creation(store, workstream_id)
            python_env: str | None = None
            try:
                parsed_scope = json.loads(str(scope_row["result_json"]))
            except ValueError:
                parsed_scope = None
            if isinstance(parsed_scope, dict) and isinstance(parsed_scope.get("pythonEnv"), str):
                python_env = parsed_scope["pythonEnv"]
            from .access import effective_runtime_scope
            scope = effective_runtime_scope(store, binding, harness=self._harness_for_workstream(store, workstream_id))
            result["runtimeScope"] = {"pythonEnv": scope.get("pythonEnv"), "readPaths": [{"path": path, "sources": scope.get("readPathSources", {}).get(path, [])} for path in scope.get("dataDirs", [])]}
            result["pythonEnv"] = result["runtimeScope"]["pythonEnv"]
            return result
        if operation == "research.request":
            _exact(payload, auth_fields | {"idempotencyKey", "request"})
            return request_research(store, project_id=project_id, workstream_id=workstream_id, idempotency_key=payload["idempotencyKey"], request=payload["request"])
        if operation == "research.list":
            _exact(payload, auth_fields, {"state", "limit"})
            states = None if payload.get("state") is None else {payload["state"]}
            return {"requests": list_research_requests(store, project_id=project_id, workstream_id=workstream_id, states=states, limit=int(payload.get("limit", 32)))}
        if operation == "research.inspect":
            _exact(payload, auth_fields | {"requestId"})
            return inspect_research(store, project_id=project_id, workstream_id=workstream_id, request_id=payload["requestId"])
        if operation == "research.add_context":
            _exact(payload, auth_fields | {"requestId", "idempotencyKey", "context"})
            return add_research_context(store, project_id=project_id, workstream_id=workstream_id, request_id=payload["requestId"], idempotency_key=payload["idempotencyKey"], context=payload["context"])
        if operation == "research.acknowledge":
            _exact(payload, auth_fields | {"requestId"})
            return acknowledge_research(store, project_id=project_id, workstream_id=workstream_id, request_id=payload["requestId"])
        raise InvalidRequestError("unsupported runtime operation")
    def _reconcile(self, store: PiStore, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._reconcile_lock:
            with control_plane_lock(store.state_root):
                return self._reconcile_locked(store, payload)

    def _recover_completed_refresh_operations(self, store: PiStore) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        rows = store.conn.execute(
            "SELECT o.operation_id,o.project_id,o.workstream_id,o.request_json,r.workspace_surface_id,r.policy_path,r.desired_generation_sha256,r.applied_generation_sha256,r.launch_generation_sha256,r.refresh_pending,r.refresh_operation_id,r.refresh_started_at,r.observed_state,r.runtime_instance_id,r.report_seq,r.session_start_event_sequence,r.session_start_report_seq "
            "FROM operations o JOIN runtime_bindings r USING(workstream_id) "
            "WHERE o.kind='runtime.refresh' AND o.state='applying'"
        ).fetchall()
        for row in rows:
            if (
                int(row["refresh_pending"])
                or row["refresh_operation_id"] is not None
                or row["refresh_started_at"] is not None
                or row["launch_generation_sha256"] is not None
            ):
                continue
            try:
                request = json.loads(str(row["request_json"]))
            except (TypeError, ValueError):
                continue
            if not isinstance(request, dict) or not isinstance(request.get("desiredGenerationSha256"), str):
                continue
            requested_generation = request["desiredGenerationSha256"]
            current_generation = row["desired_generation_sha256"]
            safe_current = bool(
                row["observed_state"] == "idle"
                and row["runtime_instance_id"] is not None
                and row["report_seq"] is not None
                and int(row["report_seq"]) >= 1
                and row["session_start_event_sequence"] is not None
                and row["session_start_report_seq"] == row["report_seq"]
                and current_generation is not None
                and row["applied_generation_sha256"] == current_generation
            )
            if safe_current:
                event = store.conn.execute(
                    "SELECT kind,payload_json FROM events WHERE sequence=? AND workstream_id=?",
                    (row["session_start_event_sequence"], row["workstream_id"]),
                ).fetchone()
                try:
                    payload = json.loads(str(event["payload_json"])) if event is not None else None
                except (TypeError, ValueError):
                    payload = None
                safe_current = bool(
                    event is not None
                    and event["kind"] == "runtime.session_started"
                    and payload == {
                        "generationSha256": str(current_generation),
                        "reportSeq": int(row["report_seq"]),
                        "runtimeInstanceId": str(row["runtime_instance_id"]),
                    }
                )
            try:
                safe_current = safe_current and self.workspace.observe_runtime(str(row["workspace_surface_id"]), str(row["policy_path"])).state == "live"
            except BaseException:
                safe_current = False
            now = utc_now()
            result = {
                "workstreamId": str(row["workstream_id"]),
                "requestedGenerationSha256": requested_generation,
                "currentGenerationSha256": current_generation,
                "recovered": True,
            }
            if safe_current and requested_generation == current_generation:
                state, step, error_code, error_message, event_kind = "succeeded", "verified", None, None, "runtime.refresh_completed"
                result.update({"generationSha256": str(current_generation), "runtimeInstanceId": str(row["runtime_instance_id"]), "reportSeq": int(row["report_seq"])})
            elif safe_current:
                state, step, error_code, error_message, event_kind = "failed", "superseded", "superseded_by_newer_refresh", "refresh was superseded by a later authenticated generation", "runtime.refresh_superseded"
            else:
                state, step, error_code, error_message, event_kind = "needs_attention", "attention", "refresh_recovery_required", "refresh operation lost a safe authenticated completion state", "runtime.refresh_recovery_required"
            with store.transaction():
                cursor = store.conn.execute(
                    "UPDATE operations SET state=?,step=?,result_json=?,error_code=?,error_message=?,updated_at=? WHERE operation_id=? AND state='applying'",
                    (state, step, canonical_json(result), error_code, error_message, now, row["operation_id"]),
                )
                if cursor.rowcount != 1:
                    continue
                append_event_in_transaction(
                    store.conn,
                    kind=event_kind,
                    project_id=str(row["project_id"]),
                    workstream_id=str(row["workstream_id"]),
                    operation_id=str(row["operation_id"]),
                    payload=result,
                )
            recovered.append({"operationId": str(row["operation_id"]), "state": state, "recovered": True})
        return recovered

    def _reconcile_locked(self, store: PiStore, payload: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"reconciled": False, "resumed": [], "errors": []}
        from .attention import backfill_attention
        for supervisor in store.conn.execute("SELECT workstream_id FROM workstreams WHERE kind IN ('secretary','first_mate') AND desired_state='active' AND provisioning_state='bound'"):
            backfill_attention(store, recipient_workstream_id=str(supervisor["workstream_id"]), limit=128)
        from .access import recover_project_permission_operation
        for operation in store.conn.execute("SELECT * FROM operations WHERE kind='project.permissions.update' AND state='applying' ORDER BY created_at,operation_id").fetchall():
            project_id = operation["project_id"]
            if project_id is None:
                continue
            try:
                from .worker_repo import project_permissions_lock
                with project_permissions_lock(store.state_root, str(project_id)):
                    recovered = recover_project_permission_operation(store, operation=operation, harness_resolver=lambda value: self._harness_for_workstream(store, value), workspace=self.workspace)
                if recovered is not None:
                    result["resumed"].append({"operationId": operation["operation_id"], "state": recovered["operation"]["state"], "recovered": True})
            except BaseException as error:
                result["errors"].append({"operationId": operation["operation_id"], "code": getattr(error, "code", "internal_error")})
        from .refresh import mark_stale_bindings, reconcile_superseded_pre_stop_refreshes
        result["generations"] = mark_stale_bindings(store, self.harness, harness_resolver=lambda workstream_id: self._harness_for_workstream(store, workstream_id), surface_resolver=self._surface_for_harness)
        result["resumed"].extend(self._recover_completed_refresh_operations(store))
        result["resumed"].extend(
            reconcile_superseded_pre_stop_refreshes(
                store,
                self.workspace,
                harness_resolver=lambda workstream_id: self._harness_for_workstream(store, workstream_id),
            )
        )
        reconciler_payload = dict(payload)
        reconciler_payload["skipWorkstreams"] = []
        resume_candidates = [
            dict(row)
            for row in store.conn.execute(
                "SELECT r.*,w.project_id,w.kind,w.worktree_path,w.desired_state,w.provisioning_state,w.attention_reason "
                "FROM runtime_bindings r JOIN workstreams w USING(workstream_id) "
                "WHERE " + runtime_eligible_sql("w") + " "
                "AND (w.provisioning_state='bound' OR (w.provisioning_state='needs_attention' AND w.attention_reason=?)) "
                "AND r.workspace_session_name=? AND r.workspace_id IS NOT NULL "
                "AND r.workspace_view_id IS NOT NULL AND r.workspace_surface_id IS NOT NULL",
                (WORKSPACE_RUNTIME_MISSING, self.workspace.manifest.session_name),
            )
        ]
        result["workspace"] = self.workspace.reconcile(store, reconciler_payload)
        result["integrations"] = reconcile_integrations(store, self.workspace, self.harness, harness_resolver=lambda workstream_id: self._harness_for_workstream(store, workstream_id))
        result["errors"].extend(result["integrations"].get("errors", []))
        result["resumed"].extend(
            self._resume_restored_agents(
                store,
                resume_candidates,
                set(reconciler_payload["skipWorkstreams"]),
            )
        )
        result["reconciled"] = True
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
                    applied = authorize_apply_workstream(store, scope=scope, harness=self._harness_for_scope(scope), workspace=self.workspace)
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
            _exact(payload, {"path"}, {"displayName", "defaultRef", "dataDirs", "externalDomains", "coordinationMode"})
            with self._reconcile_lock:
                return _public_project(register_project(store, payload["path"], display_name=payload.get("displayName"), default_ref=payload.get("defaultRef"), data_dirs=payload.get("dataDirs"), external_domains=payload.get("externalDomains"), coordination_mode=payload.get("coordinationMode"), workspace=self.workspace, harness=self.harness))
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
            every_project = [_public_project(project_status(store, row["project_id"])["project"]) for row in list_projects(store, include_inactive=True)]
            active_projects = [row for row in every_project if row.get("active")]
            return {
                "projects": every_project if include_inactive else active_projects,
                "inactiveProjects": [row for row in every_project if not row.get("active")],
                "firstMate": _first_mate_summary(store),
                "schema": "pisec-core-v1",
                "version": 1,
                "operationContracts": operation_manifest(),
            }
        if operation == "project.open":
            _exact(payload, {"project"})
            from .secretary import ensure_secretary, focus_secretary
            with self._reconcile_lock:
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
            with self._reconcile_lock:
                result = deactivate_project(store, str(payload["project"]), self.workspace, self.harness)
            project = get_project(store, result["projectId"])
            return {
                "project": _public_project(project),
                "workstreamId": result.get("workstreamId"),
                "retainedSessionRoot": result.get("retainedSessionRoot"),
                "reused": bool(result.get("reused", False)),
            }
        if operation == "runtime.ensure":
            _exact(payload, {"workstreamId"}, {"waitSeconds", "resetSession"})
            workstream_id = str(payload["workstreamId"])
            if not runtime_lifecycle_eligible(store, workstream_id):
                raise NotFoundError("runtime-eligible workstream was not found")
            from .refresh import ensure_runtime
            with self._reconcile_lock:
                return ensure_runtime(
                    store,
                    self._harness_for_workstream(store, workstream_id),
                    self.workspace,
                    workstream_id=workstream_id,
                    wait_seconds=payload.get("waitSeconds", 30.0),
                    reset_session=bool(payload.get("resetSession")),
                    harness_resolver=lambda value: self._harness_for_workstream(store, value),
                    surface_resolver=self._surface_for_harness,
                )
        if operation == "project.refresh":
            _exact(payload, {"all"}, {"waitSeconds"})
            if payload["all"] is not True:
                raise InvalidRequestError("project refresh currently requires --all")
            wait_seconds = payload.get("waitSeconds", 300)
            from .refresh import refresh_runtimes
            with self._reconcile_lock:
                return refresh_runtimes(store, self.harness, self.workspace, wait_seconds=wait_seconds, harness_resolver=lambda workstream_id: self._harness_for_workstream(store, workstream_id), surface_resolver=self._surface_for_harness)
        if operation == "secretary.ensure":
            _exact(payload, {"project"})
            from .secretary import ensure_secretary
            with self._reconcile_lock:
                result = ensure_secretary(store, str(payload["project"]), self.harness, self.workspace)
            return {
                "project": _public_project(result["project"]),
                "workstream": _public_workstream(result["workstream"]),
                "binding": _public_binding(result.get("binding")),
                "reused": bool(result.get("reused", False)),
            }
        if operation == "first_mate.ensure":
            _exact(payload, {"project"})
            with self._reconcile_lock:
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
            result = cleanup_workstream(store, payload, self.workspace, self._harness_for_workstream(store, str(payload["workstreamId"])))
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
        if operation == "attention.list":
            _exact(payload, set(), {"limit"})
            return {"items": [compact_attention(item) for item in list_open_attention(store, recipient_workstream_id=secretary_workstream_id, limit=int(payload.get("limit", 32)))]}
        if operation == "attention.inspect":
            _exact(payload, {"attentionId"})
            return inspect_attention(store, recipient_workstream_id=secretary_workstream_id, attention_id=str(payload["attentionId"]))
        if operation not in {"project.status", "project.activity", "issue.list", "issue.inspect", "git.status", "git.workstream_changes", "workstream.list", "workstream.inspect", "workstream.accept.prepare", "integration.list", "integration.inspect", "coordination.list", "coordination.inspect", "decision.list", "research.list", "research.inspect"}:
            assert_project_writable(store, project_id)
        if operation == "project.status":
            _exact(payload, set())
            return _public_project_status(project_status(store, project_id))
        if operation == "project.activity":
            _exact(payload, set(), {"after"})
            return project_activity(store, project_id, int(payload.get("after", 0)))
        if operation == "project.refresh":
            _exact(payload, set(), {"waitSeconds"})
            wait_seconds = payload.get("waitSeconds", 300)
            if not isinstance(wait_seconds, (int, float)) or isinstance(wait_seconds, bool) or not 0 <= wait_seconds <= 3600:
                raise InvalidRequestError("refresh wait must be between 0 and 3600 seconds")
            from .refresh import refresh_runtimes
            rows = store.conn.execute(
                "SELECT workstream_id FROM workstreams w WHERE project_id=? AND " + runtime_eligible_sql("w") + " AND provisioning_state='bound'",
                (project_id,),
            )
            workstream_ids = [str(row["workstream_id"]) for row in rows]
            with self._reconcile_lock:
                return refresh_runtimes(
                    store,
                    self.harness,
                    self.workspace,
                    wait_seconds=wait_seconds,
                    harness_resolver=lambda workstream_id: self._harness_for_workstream(store, workstream_id),
                    surface_resolver=self._surface_for_harness,
                    project_ids=(project_id,),
                    workstream_ids=workstream_ids,
                )
        if operation == "runtime.ensure":
            _exact(payload, {"workstreamId"}, {"waitSeconds", "resetSession"})
            workstream_id = str(payload["workstreamId"])
            if not runtime_lifecycle_eligible(store, workstream_id, project_id=project_id):
                raise NotFoundError("runtime-eligible project workstream was not found")
            from .refresh import ensure_runtime
            with self._reconcile_lock:
                return ensure_runtime(
                    store,
                    self._harness_for_workstream(store, workstream_id),
                    self.workspace,
                    workstream_id=workstream_id,
                    wait_seconds=payload.get("waitSeconds", 30.0),
                    reset_session=bool(payload.get("resetSession")),
                    harness_resolver=lambda value: self._harness_for_workstream(store, value),
                    surface_resolver=self._surface_for_harness,
                )
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
        if operation == "project.permissions.prepare":
            _exact(payload, {"dataDirs", "externalDomains", "idempotencyKey"}, {"issueId"})
            from .access import prepare_project_permissions
            return prepare_project_permissions(store, project_id=project_id, data_dirs=payload["dataDirs"], external_domains=payload["externalDomains"], issue_id=payload.get("issueId"), idempotency_key=payload["idempotencyKey"], harness_resolver=lambda value: self._harness_for_workstream(store, value), surface_resolver=self._surface_for_harness)
        if operation == "project.permissions.apply":
            _exact(payload, {"approvalScope"})
            from .access import authorize_apply_project_permissions
            with self._reconcile_lock:
                return authorize_apply_project_permissions(store, approval_scope=payload["approvalScope"], harness_resolver=lambda value: self._harness_for_workstream(store, value), surface_resolver=self._surface_for_harness, workspace=self.workspace, actor="secretary")
        if operation == "issue.report":
            _exact(payload, {"category", "severity", "summary", "details", "requestedAction", "evidence", "idempotencyKey"}, {"escalatedFromIssueId"})
            return report_issue(store, project_id=project_id, reporter_workstream_id=secretary_workstream_id, category=payload["category"], severity=payload["severity"], summary=payload["summary"], details=payload["details"], requested_action=payload["requestedAction"], evidence=payload["evidence"], idempotency_key=payload["idempotencyKey"], escalated_from_issue_id=payload.get("escalatedFromIssueId"))
        if operation == "issue.escalate":
            _exact(payload, {"sourceIssueId", "category", "severity", "summary", "details", "requestedAction", "evidence", "idempotencyKey"})
            return escalate_issue(store, project_id=project_id, reporter_workstream_id=secretary_workstream_id, source_issue_id=payload["sourceIssueId"], category=payload["category"], severity=payload["severity"], summary=payload["summary"], details=payload["details"], requested_action=payload["requestedAction"], evidence=payload["evidence"], idempotency_key=payload["idempotencyKey"])
        if operation == "issue.list":
            _exact(payload, set(), {"state", "limit"})
            return {"issues": list_issues(store, project_id=project_id, state=payload.get("state"), limit=int(payload.get("limit", 100)))}
        if operation == "issue.inspect":
            _exact(payload, {"issueId"})
            return inspect_issue(store, issue_id=payload["issueId"], project_id=project_id)
        if operation == "issue.add_context":
            _exact(payload, {"issueId", "context", "idempotencyKey"})
            return add_issue_context(store, project_id=project_id, issue_id=payload["issueId"], actor_id=secretary_workstream_id, context=payload["context"], idempotency_key=payload["idempotencyKey"])
        if operation == "issue.acknowledge":
            _exact(payload, {"issueId"})
            return acknowledge_issue(store, project_id=project_id, issue_id=payload["issueId"], actor_id=secretary_workstream_id)
        if operation == "issue.link_remediation":
            _exact(payload, {"issueId", "workstreamId", "idempotencyKey"})
            return link_issue_remediation(store, project_id=project_id, issue_id=payload["issueId"], actor_id=secretary_workstream_id, target_id=payload["workstreamId"], idempotency_key=payload["idempotencyKey"])
        if operation == "issue.request_verification":
            _exact(payload, {"issueId", "evidence", "idempotencyKey"})
            return request_issue_verification(store, project_id=project_id, issue_id=payload["issueId"], actor_id=secretary_workstream_id, evidence=payload["evidence"], idempotency_key=payload["idempotencyKey"])
        if operation == "issue.resolve":
            _exact(payload, {"issueId", "disposition", "reason", "decisionId"})
            return resolve_issue(store, project_id=project_id, issue_id=payload["issueId"], actor_id=secretary_workstream_id, disposition=payload["disposition"], reason=payload["reason"], decision_id=payload["decisionId"])
        if operation == "issue.verify":
            _exact(payload, {"issueId", "status", "evidence", "idempotencyKey"})
            return verify_issue(store, project_id=project_id, issue_id=payload["issueId"], actor_id=secretary_workstream_id, status=payload["status"], evidence=payload["evidence"], idempotency_key=payload["idempotencyKey"])
        if operation == "git.status":
            _exact(payload, set())
            return git_status(store, project_id)
        if operation == "git.push":
            _exact(payload, {"branch", "expectedLocalOid", "expectedRemoteOid"})
            return push_branch(
                store,
                project_id,
                branch=payload["branch"],
                expected_local_oid=payload["expectedLocalOid"],
                expected_remote_oid=payload["expectedRemoteOid"],
            )
        if operation == "git.workstream_changes":
            _exact(payload, {"workstreamId"})
            return inspect_workstream_changes(store, project_id, payload["workstreamId"])
        if operation == "workstream.accept.prepare":
            _exact(payload, {"workstreamId"})
            return prepare_workstream_acceptance(store, project_id, payload["workstreamId"])
        if operation == "workstream.accept.apply":
            _exact(payload, {"approvalScope"})
            if not isinstance(payload["approvalScope"], Mapping):
                raise InvalidRequestError("acceptance scope must be an object")
            return apply_workstream_acceptance(store, project_id, payload["approvalScope"])
        if operation == "integration.list":
            _exact(payload, set(), {"state"})
            states = None if payload.get("state") is None else {payload["state"]}
            return {"integrations": list_integrations(store, project_id, states=states)}
        if operation == "integration.inspect":
            _exact(payload, {"integrationId"})
            return inspect_integration(store, project_id, payload["integrationId"])
        if operation == "workstream.list":
            _exact(payload, set())
            return {"workstreams": [_public_workstream(row) for row in list_workstreams(store, project_id)]}
        if operation == "workstream.inspect":
            _exact(payload, {"workstreamId"})
            return _public_inspect(inspect_workstream(store, project_id, payload["workstreamId"]))
        if operation == "workstream.prepare":
            _exact(payload, {"title", "purpose", "brief", "taskPacket", "idempotencyKey"}, {"targetRef", "source", "executionProfile", "pythonEnv", "workMode", "learningOverlay", "learningSeam", "decisionIds", "implementationModel"})
            profile = payload.get("executionProfile", "worker-default")
            worker_harness, harness_model, reasoning_effort, _harness_id = self._worker_route(payload.get("implementationModel"))
            prepared = prepare_workstream(store, project_id=project_id, title=payload["title"], purpose=payload["purpose"], brief=payload["brief"], task_packet=payload["taskPacket"], idempotency_key=payload["idempotencyKey"], harness=worker_harness, workspace=self.workspace, target_ref=payload.get("targetRef"), source=payload.get("source"), execution_profile=profile, work_mode=payload.get("workMode", "BUILD"), learning_overlay=payload.get("learningOverlay", "LIGHT"), learning_seam=payload.get("learningSeam"), decision_ids=payload.get("decisionIds", []), python_env=payload.get("pythonEnv"), implementation_model=payload.get("implementationModel") or self.config.get("workerRouting", {}).get("defaultModel"), harness_model=harness_model, reasoning_effort=reasoning_effort)
            return {"operation": _public_operation(prepared["operation"]), "workstream": _public_workstream(prepared["workstream"]), "approvalScope": prepared["approvalScope"]}
        if operation == "workstream.authorize_apply":
            _exact(payload, {"approvalScope"})
            applied = authorize_apply_workstream(store, scope=payload["approvalScope"], harness=self._harness_for_scope(payload["approvalScope"]), workspace=self.workspace)
            return {"operation": _public_operation(applied["operation"]), "workstream": _public_workstream(applied["workstream"])}
        if operation == "workstream.focus":
            _exact(payload, {"workstreamId"})
            return focus_workstream(store, project_id, payload["workstreamId"], self.workspace)
        if operation == "workstream.retire":
            _exact(payload, {"workstreamId"}, {"remediationIssueId", "failureReason", "idempotencyKey"})
            failure_keys = {"remediationIssueId", "failureReason", "idempotencyKey"} & set(payload)
            if failure_keys and failure_keys != {"remediationIssueId", "failureReason", "idempotencyKey"}:
                raise InvalidRequestError("remediation-failure retirement requires all exact fields")
            return _public_workstream(retire_workstream(store, project_id, payload["workstreamId"], self.workspace, actor_workstream_id=secretary_workstream_id, remediation_issue_id=payload.get("remediationIssueId"), failure_reason=payload.get("failureReason"), idempotency_key=payload.get("idempotencyKey")))
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
            return request_research_context(store, project_id=project_id, secretary_workstream_id=secretary_workstream_id, request_id=payload["requestId"], idempotency_key=payload["idempotencyKey"], context_request=payload["contextRequest"])
        if operation == "research.answer":
            _exact(payload, {"requestId", "idempotencyKey", "result"})
            result = answer_research(store, project_id=project_id, secretary_workstream_id=secretary_workstream_id, request_id=payload["requestId"], idempotency_key=payload["idempotencyKey"], result=payload["result"])
            return result
        if operation == "research.decline":
            _exact(payload, {"requestId", "idempotencyKey", "decline"})
            result = decline_research(store, project_id=project_id, secretary_workstream_id=secretary_workstream_id, request_id=payload["requestId"], idempotency_key=payload["idempotencyKey"], decline=payload["decline"])
            return result
        raise InvalidRequestError("unsupported secretary operation")
    def _fleet_project(self, store: PiStore, payload: Mapping[str, Any]) -> str:
        project_id = payload.get("projectId")
        if not isinstance(project_id, str):
            raise InvalidRequestError("fleet projectId is required")
        return str(require_fleet_project(store, project_id)["project_id"])

    def _fleet_issue_project(self, store: PiStore, payload: Mapping[str, Any]) -> str:
        project_id = payload.get("projectId")
        if not isinstance(project_id, str):
            raise InvalidRequestError("fleet projectId is required")
        if not is_first_mate_issue_project(store, project_id):
            raise AuthorizationError("project is outside the First Mate issue scope")
        return str(get_project(store, project_id)["project_id"])

    def _fleet(self, store: PiStore, operation: str, first_mate_workstream_id: str, payload: dict[str, Any]) -> Any:
        if operation == "attention.list":
            _exact(payload, set(), {"limit", "projectId"})
            recipients = [first_mate_workstream_id]
            items = []
            for recipient in recipients:
                items.extend(list_open_attention(store, recipient_workstream_id=recipient, limit=int(payload.get("limit", 32))))
            return {"items": [compact_attention(item) for item in items]}
        if operation == "attention.inspect":
            _exact(payload, {"attentionId"})
            return inspect_attention(store, recipient_workstream_id=first_mate_workstream_id, attention_id=str(payload["attentionId"]))
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
                project_id = self._fleet_issue_project(store, payload)
            return {"issues": list_issues(store, project_id=project_id, project_ids=None if project_id is not None else first_mate_issue_project_ids(store), state=payload.get("state"), limit=int(payload.get("limit", 100)))}
        if operation == "fleet.issue.inspect":
            _exact(payload, {"issueId"}, {"projectId"})
            project_id = payload.get("projectId")
            if project_id is not None:
                project_id = self._fleet_issue_project(store, payload)
            issue = inspect_issue(store, issue_id=payload["issueId"], project_id=project_id)
            if project_id is None:
                if not is_first_mate_issue_project(store, str(issue["project_id"])):
                    raise AuthorizationError("issue is outside the First Mate issue scope")
            return issue
        if operation == "fleet.issue.add_context":
            _exact(payload, {"projectId", "issueId", "context", "idempotencyKey"})
            project_id = self._fleet_issue_project(store, payload)
            return add_issue_context(store, project_id=project_id, issue_id=payload["issueId"], actor_id=first_mate_workstream_id, context=payload["context"], idempotency_key=payload["idempotencyKey"])
        if operation == "fleet.issue.acknowledge":
            _exact(payload, {"projectId", "issueId"})
            project_id = self._fleet_issue_project(store, payload)
            return acknowledge_issue(store, project_id=project_id, issue_id=payload["issueId"], actor_id=first_mate_workstream_id)
        if operation == "fleet.issue.request_remediation":
            _exact(payload, {"projectId", "issueId", "outcome", "allowedPaths", "verification", "nonEffects", "idempotencyKey"})
            project_id = self._fleet_issue_project(store, payload)
            return request_issue_remediation(store, project_id=project_id, issue_id=payload["issueId"], actor_id=first_mate_workstream_id, outcome=payload["outcome"], allowed_paths=payload["allowedPaths"], verification=payload["verification"], non_effects=payload["nonEffects"], idempotency_key=payload["idempotencyKey"])
        if operation == "fleet.issue.request_verification":
            _exact(payload, {"projectId", "issueId", "evidence", "idempotencyKey"})
            project_id = self._fleet_issue_project(store, payload)
            return request_issue_verification(store, project_id=project_id, issue_id=payload["issueId"], actor_id=first_mate_workstream_id, evidence=payload["evidence"], idempotency_key=payload["idempotencyKey"])
        if operation == "fleet.issue.resolve":
            _exact(payload, {"projectId", "issueId", "disposition", "reason", "decisionId"})
            project_id = self._fleet_issue_project(store, payload)
            return resolve_issue(store, project_id=project_id, issue_id=payload["issueId"], actor_id=first_mate_workstream_id, disposition=payload["disposition"], reason=payload["reason"], decision_id=payload["decisionId"])
        if operation == "fleet.events":
            _exact(payload, set(), {"after", "limit"})
            rows = list_events(store, after=int(payload.get("after", 0)), limit=int(payload.get("limit", 256)), project_ids=first_mate_issue_project_ids(store))
            return {"events": [{"sequence": row["sequence"], "eventId": row["event_id"], "kind": row["kind"], "projectId": row["project_id"], "workstreamId": row["workstream_id"], "operationId": row["operation_id"], "createdAt": row["created_at"]} for row in rows]}
        if operation == "fleet.workstream.list":
            _exact(payload, {"projectId"})
            return {"workstreams": [_public_workstream(row) for row in list_workstreams(store, self._fleet_project(store, payload))]}
        if operation == "fleet.workstream.inspect":
            _exact(payload, {"projectId", "workstreamId"})
            return _public_inspect(inspect_workstream(store, self._fleet_project(store, payload), payload["workstreamId"]))
        if operation == "fleet.git.workstream_changes":
            _exact(payload, {"projectId", "workstreamId"})
            return inspect_workstream_changes(store, self._fleet_project(store, payload), payload["workstreamId"])
        if operation == "fleet.integration.list":
            _exact(payload, {"projectId"}, {"state"})
            project_id = self._fleet_project(store, payload)
            states = None if payload.get("state") is None else {payload["state"]}
            return {"integrations": list_integrations(store, project_id, states=states)}
        if operation == "fleet.integration.inspect":
            _exact(payload, {"projectId", "integrationId"})
            project_id = self._fleet_project(store, payload)
            return inspect_integration(store, project_id, payload["integrationId"])
        raise InvalidRequestError("unsupported fleet operation")

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
