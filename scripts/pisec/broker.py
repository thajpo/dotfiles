"""Pisec broker dispatch and three-socket Unix service."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import os
from pathlib import Path
import socket
import socketserver
import stat
import threading
from typing import Any, Callable, Mapping

from .adapters import AdapterRegistry, HarnessAdapter, WorkspaceAdapter
from .cleanup import cleanup_workstream
from .decisions import list_decisions, record_decision, resolve_decision
from .models import AuthorizationError, InvalidRequestError, NotFoundError, PisecError, UnsafeStateError, bounded_text, validate_id
from .pi_store import PiStore
from .projects import list_projects, project_status, register_project, resolve_project
from .protocol import MAX_MESSAGE_BYTES, decode_request, error_response, success_response
from .research import (
    acknowledge_research,
    add_research_context,
    answer_research,
    claim_research,
    decline_research,
    get_task_packet,
    list_research_requests,
    list_unacknowledged_research,
    mark_research_wake_notified,
    pending_research_wakes,
    request_research,
    request_research_context,
)
from .runtime import report_runtime, verify_runtime_binding
from .secretary_git import apply_workstream_merge, git_status, inspect_workstream_changes, prepare_workstream_merge
from .workstreams import authorize_apply_workstream, complete_workstream, focus_workstream, inspect_workstream, list_workstreams, prepare_workstream, retire_workstream, send_workstream
ADMIN_OPERATIONS = frozenset({"project.register", "project.list", "secretary.ensure", "secretary.focus", "workstream.focus", "workstream.cleanup", "system.status", "system.reconcile", "system.doctor", "workspace.startup", "workspace.event"})
SECRETARY_OPERATIONS = frozenset({"project.status", "git.status", "git.workstream_changes", "git.merge.prepare", "git.merge.apply", "workstream.list", "workstream.inspect", "workstream.prepare", "workstream.authorize_apply", "workstream.send", "workstream.focus", "workstream.complete", "workstream.retire", "decision.list", "decision.record", "decision.resolve", "research.list", "research.claim", "research.request_context", "research.answer", "research.decline"})

logger = logging.getLogger(__name__)
RUNTIME_OPERATIONS = frozenset({"runtime.report", "task.get", "research.request", "research.list", "research.add_context", "research.acknowledge"})
WORKSPACE_STARTUP_GRACE_SECONDS = 2.0
RESEARCH_WAKE_DEBOUNCE_SECONDS = 0.1


def default_runtime_root() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return Path(os.environ.get("PISEC_RUNTIME_ROOT", base / "pisec"))


def socket_paths(root: Path | None = None) -> dict[str, Path]:
    base = root or default_runtime_root()
    return {kind: base / kind / "control.sock" for kind in ("admin", "secretary", "runtime")}


def _exact(payload: Mapping[str, Any], required: set[str], optional: set[str] = set()) -> None:
    if set(payload) < required or not set(payload) <= required | optional:
        raise InvalidRequestError("payload fields do not match the operation contract")


_PUBLIC_PROJECT_FIELDS = ("project_id", "display_name", "default_ref", "secretary_workstream_id", "created_at", "updated_at")
_PUBLIC_WORKSTREAM_FIELDS = (
    "workstream_id", "project_id", "kind", "title", "purpose", "brief", "harness_id", "workspace_adapter_id",
    "execution_profile", "target_ref", "base_commit_oid", "branch_name",
    "desired_state", "provisioning_state", "created_at", "updated_at", "completed_at", "retired_at",
    "observed_state", "last_observed_at", "agent_name", "task_packet_id", "task_packet_sha256",
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
        except BaseException:
            logger.exception("could not queue durable research wakes")

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
        while not self._reconcile_stop.is_set():
            try:
                payload = self._reconcile_queue.get(timeout=0.2)
            except queue.Empty:
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

    def _secretary_project(self, store: PiStore, payload: dict[str, Any]) -> str:
        return str(self._secretary_binding(store, payload)["project_id"])

    def dispatch(self, socket_kind: str, operation: str, payload_value: Mapping[str, Any]) -> Any:
        allowlist = {"admin": ADMIN_OPERATIONS, "secretary": SECRETARY_OPERATIONS, "runtime": RUNTIME_OPERATIONS}.get(socket_kind)
        if allowlist is None or operation not in allowlist:
            raise AuthorizationError("operation is not allowed on this socket")
        payload = dict(payload_value)
        with self.store_factory() as store:
            if socket_kind == "admin":
                return self._admin(store, operation, payload)
            if socket_kind == "secretary":
                binding = self._secretary_binding(store, payload)
                return self._secretary(store, operation, str(binding["project_id"]), str(binding["workstream_id"]), payload)
            if operation == "runtime.report":
                return report_runtime(store, payload, self.harness, self.workspace)
            return self._runtime(store, operation, payload)

    def _runtime(self, store: PiStore, operation: str, payload: dict[str, Any]) -> Any:
        binding = verify_runtime_binding(store, payload, worker_only=True)
        project_id = str(binding["project_id"])
        workstream_id = str(binding["workstream_id"])
        auth_fields = {"workstreamId", "runtimeInstanceId", "surfaceId", "token"}
        if operation == "task.get":
            _exact(payload, auth_fields)
            return get_task_packet(store, project_id, workstream_id)
        if operation == "research.request":
            _exact(payload, auth_fields | {"idempotencyKey", "request"})
            result = request_research(store, project_id=project_id, workstream_id=workstream_id, idempotency_key=payload["idempotencyKey"], request=payload["request"])
            self._queue_research_wake(project_id)
            return result
        if operation == "research.list":
            _exact(payload, auth_fields)
            return {"requests": list_research_requests(store, project_id=project_id, workstream_id=workstream_id)}
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
        result: dict[str, Any] = {"reconciled": False, "resumed": [], "errors": []}
        result["workspace"] = self.workspace.reconcile(store, payload)
        result["reconciled"] = True
        for wake in pending_research_wakes(store):
            self._queue_research_wake(wake["project_id"])
        rows = store.conn.execute(
            "SELECT o.*, EXISTS(SELECT 1 FROM authorizations a WHERE a.operation_id=o.operation_id) AS has_authorization "
            "FROM operations o WHERE o.state IN ('applying','failed') ORDER BY o.created_at"
        ).fetchall()
        for row in rows:
            operation = dict(row)
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
            except BaseException as error:
                result["errors"].append({"operationId": operation["operation_id"], "code": getattr(error, "code", "internal_error")})
        return result

    def _admin(self, store: PiStore, operation: str, payload: dict[str, Any]) -> Any:
        if operation == "project.register":
            _exact(payload, {"path"}, {"displayName", "defaultRef"})
            return _public_project(register_project(store, payload["path"], display_name=payload.get("displayName"), default_ref=payload.get("defaultRef")))
        if operation == "project.list":
            _exact(payload, set())
            return {"projects": [_public_project(row) for row in list_projects(store)]}
        if operation == "system.status":
            _exact(payload, set(), {"project"})
            if payload.get("project"):
                project = resolve_project(store, str(payload["project"]))
                return _public_project_status(project_status(store, project["project_id"]))
            return {"projects": [_public_project(row) for row in list_projects(store)], "schema": "pisec-core", "version": 3}
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
            _exact(payload, set())
            from .doctor import run_doctor
            return run_doctor(store, self.config, self.registry)
        raise InvalidRequestError("unsupported admin operation")

    def _secretary(self, store: PiStore, operation: str, project_id: str, secretary_workstream_id: str, payload: dict[str, Any]) -> Any:
        if operation == "project.status":
            _exact(payload, set())
            return _public_project_status(project_status(store, project_id))
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
            _exact(payload, {"title", "purpose", "brief", "taskPacket", "idempotencyKey"}, {"targetRef", "executionProfile", "externalDomains"})
            profile = payload.get("executionProfile", "worker-default")
            prepared = prepare_workstream(store, project_id=project_id, title=payload["title"], purpose=payload["purpose"], brief=payload["brief"], task_packet=payload["taskPacket"], idempotency_key=payload["idempotencyKey"], harness=self.harness, workspace=self.workspace, target_ref=payload.get("targetRef"), execution_profile=profile, external_domains=payload.get("externalDomains", []))
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
            _exact(payload, {"workstreamId"})
            return _public_workstream(complete_workstream(store, project_id, payload["workstreamId"]))
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


