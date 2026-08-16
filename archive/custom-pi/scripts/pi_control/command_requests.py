"""Structured, TTY-approved, one-use host and network operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Mapping

from .events import append_event_in_transaction
from .models import canonical_json, json_digest, new_id, utc_now, validate_id


class CommandRequestError(ValueError):
    pass


@dataclass(frozen=True)
class Operation:
    name: str
    place: str
    argv: tuple[str, ...]
    effect: str
    scope: Mapping[str, Any]
    timeout_ms: int

    def normalized(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "place": self.place,
            "argv": list(self.argv),
            "effect": self.effect,
            "scope": dict(self.scope),
            "timeoutMs": self.timeout_ms,
        }


def _executable(name: str) -> str:
    value = shutil.which(name, path=os.defpath)
    if value is None:
        raise CommandRequestError(f"required host executable is unavailable: {name}")
    return str(Path(value).resolve(strict=True))


def normalize_operation(name: str) -> Operation:
    """Map a small release-1 grammar to controller-owned exact argv."""

    if name == "host.controller-status":
        return Operation(name, "host", (_executable("true"),), "read controller host readiness", {"mutation": "none"}, 5_000)
    if name == "host.fixture-success":
        return Operation(name, "host", (_executable("true"),), "deterministic successful no-op", {"mutation": "none", "fixture": True}, 5_000)
    if name == "host.fixture-failure":
        return Operation(name, "host", (_executable("false"),), "deterministic failed no-op", {"mutation": "none", "fixture": True}, 5_000)
    if name == "host.fixture-timeout":
        return Operation(name, "host", (_executable("sleep"), "2"), "deterministic bounded timeout", {"mutation": "none", "fixture": True}, 100)
    if name == "network.namespace-probe":
        code = "import os;print('NETWORK_NAMESPACE_OK',os.readlink('/proc/self/ns/net'))"
        return Operation(name, "container-network", ("python3", "-c", code), "observe an isolated network namespace without contacting it", {"mutation": "none", "networkContact": "none"}, 10_000)
    raise CommandRequestError("operation is not in the release-1 command grammar")


def _expires(milliseconds: int = 300_000) -> str:
    return (datetime.now(timezone.utc) + timedelta(milliseconds=milliseconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _expired(value: str) -> bool:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) <= datetime.now(timezone.utc)


def _binding(store: Any, project_id: str, conversation_id: str, run_id: str, writer_generation: int) -> Any:
    row = store.conn.execute(
        "SELECT c.role,c.desired_state AS conversation_desired,c.observed_state AS conversation_observed,"
        "r.writer_epoch AS run_writer_epoch,r.project_id AS run_project_id,r.conversation_id AS run_conversation_id,"
        "r.working_copy_id,r.authority AS run_authority,r.desired_state AS run_desired,r.observed_state AS run_observed,"
        "p.desired_state AS project_desired,p.observed_state AS project_observed,"
        "w.path,w.writer_epoch AS current_writer_epoch,w.active_writer_run_id,w.desired_state AS working_desired,w.observed_state AS working_observed "
        "FROM conversations c JOIN runs r ON r.conversation_id=c.conversation_id JOIN projects p ON p.project_id=r.project_id "
        "JOIN working_copies w ON w.working_copy_id=r.working_copy_id WHERE c.conversation_id=? AND r.run_id=?",
        (conversation_id, run_id),
    ).fetchone()
    if row is None or row["run_project_id"] != project_id or row["run_conversation_id"] != conversation_id:
        raise CommandRequestError("command binding crosses project or conversation boundary")
    if row["role"] not in {"personal", "workstream", "integration"} or row["run_authority"] != "writer-container":
        raise CommandRequestError("only an authenticated writer run may request an operation")
    if row["project_desired"] != "active" or row["project_observed"] != "ready" or row["conversation_desired"] != "active" or row["conversation_observed"] != "ready" or row["working_desired"] != "present" or row["working_observed"] not in {"ready", "dirty"} or row["run_desired"] != "running" or row["run_observed"] not in {"ready", "running"}:
        raise CommandRequestError("command binding is stale or terminal")
    if not isinstance(writer_generation, int) or isinstance(writer_generation, bool) or writer_generation < 1 or row["run_writer_epoch"] != writer_generation or row["current_writer_epoch"] != writer_generation or row["active_writer_run_id"] != run_id:
        raise CommandRequestError("command writer epoch or working-copy claim is stale")
    root = Path(str(row["path"])).resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise CommandRequestError("assigned working copy is unavailable")
    return row


def request_command(store: Any, *, project_id: str, conversation_id: str, run_id: str, writer_generation: int, operation: str, purpose: str) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(conversation_id, prefix="conv")
    validate_id(run_id, prefix="run")
    if not isinstance(purpose, str) or not purpose or "\x00" in purpose or len(purpose.encode("utf-8")) > 2048:
        raise CommandRequestError("command purpose is invalid")
    normalized = normalize_operation(operation)
    with store.transaction():
        binding = _binding(store, project_id, conversation_id, run_id, writer_generation)
        cwd = str(Path(str(binding["path"])).resolve(strict=True))
        operation_value = normalized.normalized()
        if normalized.place == "container-network":
            run = store.conn.execute("SELECT manifest_path FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None or not run["manifest_path"]:
                raise CommandRequestError("network operation has no exact runtime image binding")
            from .run_manifest import read_manifest
            tool = read_manifest(run["manifest_path"]).manifest.get("toolRuntime") or {}
            image = {key: tool.get(key) for key in ("imageReference", "imageConfigId", "platform")}
            if any(not isinstance(value, str) or not value for value in image.values()):
                raise CommandRequestError("network operation runtime image identity is incomplete")
            operation_value["image"] = image
        body = {
            "projectId": project_id, "conversationId": conversation_id, "runId": run_id,
            "writerEpoch": writer_generation, "operation": operation_value, "cwd": cwd,
            "purpose": purpose,
        }
        digest = json_digest(body)
        existing = store.conn.execute("SELECT * FROM command_requests WHERE request_digest=?", (digest,)).fetchone()
        if existing is not None:
            return dict(existing)
        request_id = new_id("cmd")
        now = utc_now()
        store.conn.execute(
            "INSERT INTO command_requests(command_request_id,project_id,workstream_id,conversation_id,run_id,writer_generation,execution_place,operation_name,operation_json,working_directory,required_resource,purpose,expected_effect,change_scope_json,expected_output,sensitive_output,expected_duration_ms,request_digest,state,authorization_id,result_json,created_at,expires_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, project_id, None, conversation_id, run_id, writer_generation, normalized.place, normalized.name, canonical_json(operation_value), cwd, "assigned-working-copy", purpose, normalized.effect, canonical_json(dict(normalized.scope)), "bounded stdout/stderr", 0, normalized.timeout_ms, digest, "requested", None, None, now, _expires(), None),
        )
        append_event_in_transaction(store.conn, event_kind="command.requested", resource_type="command-request", resource_id=request_id, payload={"projectId": project_id, "executionPlace": normalized.place, "operation": normalized.name, "requestDigest": digest})
        return dict(store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=?", (request_id,)).fetchone())


def command_status(store: Any, *, project_id: str, command_request_id: str) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(command_request_id, prefix="cmd")
    row = store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=? AND project_id=?", (command_request_id, project_id)).fetchone()
    if row is None:
        raise CommandRequestError("command request was not found in the authenticated project")
    return dict(row)


def approval_display(store: Any, *, request_id: str, request_digest: str) -> dict[str, Any]:
    validate_id(request_id, prefix="cmd")
    row = store.conn.execute(
        "SELECT r.*,p.display_name,c.role FROM command_requests r JOIN projects p ON p.project_id=r.project_id JOIN conversations c ON c.conversation_id=r.conversation_id WHERE r.command_request_id=?",
        (request_id,),
    ).fetchone()
    if row is None or row["request_digest"] != request_digest:
        raise CommandRequestError("request ID and digest do not identify one exact command request")
    operation = json.loads(row["operation_json"])
    return {
        "requestId": row["command_request_id"], "digest": row["request_digest"], "state": row["state"],
        "project": {"id": row["project_id"], "name": row["display_name"]},
        "conversation": {"id": row["conversation_id"], "role": row["role"]}, "runId": row["run_id"],
        "operation": operation["name"], "argv": operation["argv"], "cwd": row["working_directory"],
        "effectScope": json.loads(row["change_scope_json"]), "executionPlace": row["execution_place"],
        "expiresAt": row["expires_at"], "purpose": row["purpose"],
    }


def _receipt_scope(store: Any, row: Mapping[str, Any]) -> dict[str, Any]:
    operation = json.loads(str(row["operation_json"]))
    return {
        "controller": store.controller_identity(), "requestId": row["command_request_id"],
        "requestDigest": row["request_digest"], "projectId": row["project_id"],
        "conversationId": row["conversation_id"], "runId": row["run_id"],
        "writerEpoch": row["writer_generation"], "operation": operation,
        "place": row["execution_place"], "cwd": row["working_directory"],
        "scope": json.loads(str(row["change_scope_json"])), "expiresAt": row["expires_at"], "oneUse": True,
    }


def approve_command(store: Any, *, command_request_id: str, request_digest: str, actor_id: str = "controlling-tty") -> dict[str, Any]:
    validate_id(command_request_id, prefix="cmd")
    with store.transaction():
        row = store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=?", (command_request_id,)).fetchone()
        if row is None or row["request_digest"] != request_digest or row["state"] != "requested":
            raise CommandRequestError("command request is changed, stale, replayed, or already decided")
        _binding(store, row["project_id"], row["conversation_id"], row["run_id"], int(row["writer_generation"]))
        if _expired(row["expires_at"]):
            store.conn.execute("UPDATE command_requests SET state='expired',completed_at=? WHERE command_request_id=?", (utc_now(), command_request_id))
            raise CommandRequestError("command request has expired")
        scope = _receipt_scope(store, row)
        authorization_id = new_id("auth")
        now = utc_now()
        kind = "host-command" if row["execution_place"] == "host" else "container-network-command"
        store.conn.execute(
            "INSERT INTO authorizations(authorization_id,kind,actor_type,actor_id,project_id,resource_type,resource_id,request_context_id,scope_json,scope_digest,issued_at,expires_at,consumed_at,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (authorization_id, kind, "user", actor_id, row["project_id"], "command-request", command_request_id, command_request_id, canonical_json(scope), json_digest(scope), now, row["expires_at"], None, "active"),
        )
        store.conn.execute("UPDATE command_requests SET state='approved',authorization_id=? WHERE command_request_id=?", (authorization_id, command_request_id))
        append_event_in_transaction(store.conn, event_kind="command.authorized", resource_type="command-request", resource_id=command_request_id, payload={"authorizationId": authorization_id, "requestDigest": request_digest})
        return {"authorizationId": authorization_id, "receipt": scope, "receiptDigest": json_digest(scope)}


def reject_command(store: Any, *, command_request_id: str, request_digest: str, reason: str = "rejected at controlling TTY") -> dict[str, Any]:
    validate_id(command_request_id, prefix="cmd")
    with store.transaction():
        row = store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=?", (command_request_id,)).fetchone()
        if row is None or row["request_digest"] != request_digest or row["state"] != "requested":
            raise CommandRequestError("command request is changed, stale, replayed, or already decided")
        _binding(store, row["project_id"], row["conversation_id"], row["run_id"], int(row["writer_generation"]))
        store.conn.execute("UPDATE command_requests SET state='rejected',result_json=?,completed_at=? WHERE command_request_id=?", (canonical_json({"reason": reason[:1024]}), utc_now(), command_request_id))
        append_event_in_transaction(store.conn, event_kind="command.rejected", resource_type="command-request", resource_id=command_request_id, payload={"requestDigest": request_digest})
        return dict(store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=?", (command_request_id,)).fetchone())


def _consume(store: Any, command_request_id: str, request_digest: str) -> dict[str, Any]:
    with store.transaction():
        row = store.conn.execute("SELECT r.*,a.scope_json,a.scope_digest,a.state AS authorization_state,a.expires_at AS authorization_expires FROM command_requests r JOIN authorizations a ON a.authorization_id=r.authorization_id WHERE r.command_request_id=?", (command_request_id,)).fetchone()
        if row is None or row["request_digest"] != request_digest or row["state"] != "approved" or row["authorization_state"] != "active":
            raise CommandRequestError("command approval is missing, changed, stale, replayed, or already consumed")
        _binding(store, row["project_id"], row["conversation_id"], row["run_id"], int(row["writer_generation"]))
        if _expired(row["expires_at"]) or (row["authorization_expires"] and _expired(row["authorization_expires"])):
            store.conn.execute("UPDATE command_requests SET state='expired',completed_at=? WHERE command_request_id=?", (utc_now(), command_request_id))
            store.conn.execute("UPDATE authorizations SET state='expired' WHERE authorization_id=?", (row["authorization_id"],))
            raise CommandRequestError("command approval has expired")
        expected = _receipt_scope(store, row)
        if canonical_json(expected) != row["scope_json"] or json_digest(expected) != row["scope_digest"]:
            raise CommandRequestError("command approval belongs to another controller generation or request binding")
        now = utc_now()
        if store.conn.execute("UPDATE authorizations SET state='consumed',consumed_at=? WHERE authorization_id=? AND state='active'", (now, row["authorization_id"])).rowcount != 1:
            raise CommandRequestError("command approval was already consumed")
        if store.conn.execute("UPDATE command_requests SET state='running' WHERE command_request_id=? AND state='approved'", (command_request_id,)).rowcount != 1:
            raise CommandRequestError("command request changed before receipt consumption")
        return dict(row)


def _runner_environment() -> dict[str, str]:
    return {"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C", "TMPDIR": tempfile.gettempdir()}


def _bounded(value: str, limit: int = 64 * 1024) -> tuple[str, bool]:
    body = value.encode("utf-8", errors="replace")
    return (value, False) if len(body) <= limit else (body[:limit].decode("utf-8", errors="replace"), True)


def _run_host(operation: Mapping[str, Any], cwd: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(operation["argv"], cwd=cwd, env=_runner_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=max(0.001, int(operation["timeoutMs"]) / 1000), shell=False, check=False)
        stdout, stdout_truncated = _bounded(completed.stdout)
        stderr, stderr_truncated = _bounded(completed.stderr)
        return {"executionPlace": "host", "exitCode": completed.returncode, "timedOut": False, "stdout": stdout, "stderr": stderr, "stdoutTruncated": stdout_truncated, "stderrTruncated": stderr_truncated}
    except subprocess.TimeoutExpired as error:
        stdout, stdout_truncated = _bounded(error.stdout if isinstance(error.stdout, str) else "")
        stderr, stderr_truncated = _bounded(error.stderr if isinstance(error.stderr, str) else "")
        return {"executionPlace": "host", "exitCode": None, "timedOut": True, "stdout": stdout, "stderr": stderr, "stdoutTruncated": stdout_truncated, "stderrTruncated": stderr_truncated}


def execute_approved_command(store: Any, *, command_request_id: str, request_digest: str) -> dict[str, Any]:
    validate_id(command_request_id, prefix="cmd")
    row = _consume(store, command_request_id, request_digest)
    operation = json.loads(row["operation_json"])
    try:
        if row["execution_place"] == "host":
            result = _run_host(operation, row["working_directory"])
        else:
            run = store.conn.execute("SELECT manifest_path FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()
            if run is None or not run["manifest_path"]:
                raise CommandRequestError("network operation has no current runtime image binding")
            from .run_manifest import read_manifest
            manifest = read_manifest(run["manifest_path"]).manifest
            image = manifest.get("toolRuntime") or {}
            approved_image = operation.get("image") or {}
            if {key: image.get(key) for key in ("imageReference", "imageConfigId", "platform")} != approved_image:
                raise CommandRequestError("network operation runtime image changed after approval")
            from .docker_runtime import run_one_shot_network
            result = run_one_shot_network(
                request_id=command_request_id, image_reference=image.get("imageReference"), expected_image_config_id=image.get("imageConfigId"),
                expected_platform=image.get("platform"), working_copy=row["working_directory"], argv=operation["argv"], timeout_ms=int(operation["timeoutMs"]), mount_read_only=True,
            )
    except (OSError, RuntimeError, ValueError) as error:
        result = {"executionPlace": row["execution_place"], "exitCode": None, "timedOut": False, "error": type(error).__name__, "message": str(error)[:1024]}
    state = "succeeded" if result.get("exitCode") == 0 and not result.get("timedOut") and "error" not in result else "failed"
    with store.transaction():
        if store.conn.execute("UPDATE command_requests SET state=?,result_json=?,completed_at=? WHERE command_request_id=? AND state='running'", (state, canonical_json(result), utc_now(), command_request_id)).rowcount != 1:
            raise CommandRequestError("command result could not finalize its consumed request")
        append_event_in_transaction(store.conn, event_kind=f"command.{state}", resource_type="command-request", resource_id=command_request_id, payload={"requestDigest": request_digest, "executionPlace": row["execution_place"], "exitCode": result.get("exitCode")})
        return dict(store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=?", (command_request_id,)).fetchone())


__all__ = [
    "CommandRequestError", "Operation", "approval_display", "approve_command", "command_status",
    "execute_approved_command", "normalize_operation", "reject_command", "request_command",
]
