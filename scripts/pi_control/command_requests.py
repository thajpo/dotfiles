"""Exact, one-use host and container-network command requests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import shutil
from typing import Any, Mapping, Sequence

from .events import append_event_in_transaction
from .models import canonical_json, json_digest, new_id, utc_now, validate_id


class CommandRequestError(ValueError):
    pass


def _expires(ms: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(milliseconds=ms)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _binding(store: Any, project_id: str, conversation_id: str, run_id: str) -> Any:
    row = store.conn.execute("SELECT c.*,r.writer_epoch AS run_writer_epoch,r.project_id AS run_project_id,r.conversation_id AS run_conversation_id FROM conversations c JOIN runs r ON r.conversation_id=c.conversation_id WHERE c.conversation_id=? AND r.run_id=?", (conversation_id, run_id)).fetchone()
    if row is None or row["project_id"] != project_id or row["run_project_id"] != project_id:
        raise CommandRequestError("command binding crosses project boundary")
    if row["role"] not in {"personal", "workstream", "integration"} or row["run_writer_epoch"] is None:
        raise CommandRequestError("only active coding runs may request commands")
    return row


def request_command(store: Any, *, project_id: str, conversation_id: str, run_id: str, execution_place: str, command: Sequence[str] | str, working_directory: str, required_resource: str, purpose: str, expected_effect: str, change_scope: Mapping[str, Any], expected_output: str = "", sensitive_output: bool = False, expected_duration_ms: int = 30000, workstream_id: str | None = None) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(conversation_id, prefix="conv")
    validate_id(run_id, prefix="run")
    if execution_place not in {"container-network", "host"}:
        raise CommandRequestError("execution place is invalid")
    if isinstance(command, str):
        command_value: list[str] = [command]
    elif isinstance(command, Sequence) and not isinstance(command, (bytes, bytearray)) and command and all(isinstance(item, str) and item and "\x00" not in item for item in command):
        command_value = list(command)
    else:
        raise CommandRequestError("command must be an exact non-empty argv")
    if not isinstance(working_directory, str) or not working_directory.startswith("/") or "\x00" in working_directory:
        raise CommandRequestError("working directory must be absolute")
    if not isinstance(expected_duration_ms, int) or not 1 <= expected_duration_ms <= 3600000:
        raise CommandRequestError("expected duration is outside its bound")
    binding = _binding(store, project_id, conversation_id, run_id)
    if workstream_id is not None:
        validate_id(workstream_id, prefix="ws")
        ws = store.conn.execute("SELECT project_id FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()
        if ws is None or ws[0] != project_id:
            raise CommandRequestError("workstream does not belong to project")
    body = {"projectId": project_id, "conversationId": conversation_id, "runId": run_id, "writerGeneration": int(binding["run_writer_epoch"]), "executionPlace": execution_place, "command": command_value, "workingDirectory": working_directory, "requiredResource": required_resource, "purpose": purpose, "expectedEffect": expected_effect, "changeScope": dict(change_scope), "expectedOutput": expected_output, "sensitiveOutput": bool(sensitive_output), "expectedDurationMs": expected_duration_ms}
    digest = json_digest(body)
    request_id = new_id("cmd")
    now = utc_now()
    with store.transaction():
        store.conn.execute("INSERT INTO command_requests(command_request_id,project_id,workstream_id,conversation_id,run_id,writer_generation,execution_place,command_json,working_directory,required_resource,purpose,expected_effect,change_scope_json,expected_output,sensitive_output,expected_duration_ms,request_digest,state,authorization_id,result_json,created_at,expires_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (request_id, project_id, workstream_id, conversation_id, run_id, int(binding["run_writer_epoch"]), execution_place, canonical_json(command_value), working_directory, required_resource, purpose, expected_effect, canonical_json(dict(change_scope)), expected_output, int(bool(sensitive_output)), expected_duration_ms, digest, "requested", None, None, now, _expires(expected_duration_ms), None))
        append_event_in_transaction(store.conn, event_kind="command.requested", resource_type="command-request", resource_id=request_id, payload={"projectId": project_id, "executionPlace": execution_place, "requestDigest": digest})
        return dict(store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=?", (request_id,)).fetchone())


def authorize_command(store: Any, *, project_id: str, command_request_id: str, request_digest: str, actor_id: str, scope: Mapping[str, Any] | None = None, authorization_id: str | None = None) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(command_request_id, prefix="cmd")
    with store.transaction():
        request = store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=? AND project_id=?", (command_request_id, project_id)).fetchone()
        if request is None:
            raise CommandRequestError("command request not found")
        if request["request_digest"] != request_digest:
            raise CommandRequestError("command request digest or state is stale")
        if request["state"] == "approved" and request["authorization_id"]:
            existing = store.conn.execute("SELECT * FROM authorizations WHERE authorization_id=?", (request["authorization_id"],)).fetchone()
            if existing is not None and existing["actor_id"] == actor_id:
                return dict(request)
        if request["state"] != "requested":
            raise CommandRequestError("command request digest or state is stale")
        if datetime.fromisoformat(str(request["expires_at"]).replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            store.conn.execute("UPDATE command_requests SET state='expired',completed_at=? WHERE command_request_id=?", (utc_now(), command_request_id))
            raise CommandRequestError("command request has expired")
        kind = "host-command" if request["execution_place"] == "host" else "container-network-command"
        auth_id = authorization_id or new_id("auth")
        scope_value = dict(scope or {"requestDigest": request_digest, "commandRequestId": command_request_id})
        scope_json = canonical_json(scope_value)
        now = utc_now()
        store.conn.execute("INSERT INTO authorizations(authorization_id,kind,actor_type,actor_id,project_id,resource_type,resource_id,request_context_id,scope_json,scope_digest,issued_at,expires_at,consumed_at,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (auth_id, kind, "user", actor_id, project_id, "command-request", command_request_id, command_request_id, scope_json, json_digest(scope_value), now, request["expires_at"], None, "active"))
        store.conn.execute("UPDATE command_requests SET state='approved',authorization_id=? WHERE command_request_id=?", (auth_id, command_request_id))
        append_event_in_transaction(store.conn, event_kind="command.authorized", resource_type="command-request", resource_id=command_request_id, payload={"projectId": project_id, "authorizationId": auth_id, "requestDigest": request_digest})
        return dict(store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=?", (command_request_id,)).fetchone())


def reject_command(store: Any, *, project_id: str, command_request_id: str, reason: str) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(command_request_id, prefix="cmd")
    with store.transaction():
        row = store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=? AND project_id=?", (command_request_id, project_id)).fetchone()
        if row is None:
            raise CommandRequestError("command request not found")
        if row["state"] != "requested":
            raise CommandRequestError("command request is not pending")
        store.conn.execute("UPDATE command_requests SET state='rejected',result_json=?,completed_at=? WHERE command_request_id=?", (canonical_json({"reason": reason[:1024]}), utc_now(), command_request_id))
        return dict(store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=?", (command_request_id,)).fetchone())


def consume_authorization(store: Any, *, project_id: str, command_request_id: str, request_digest: str) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    with store.transaction():
        row = store.conn.execute("SELECT r.*,a.state AS authorization_state,a.kind AS authorization_kind FROM command_requests r JOIN authorizations a ON a.authorization_id=r.authorization_id WHERE r.command_request_id=? AND r.project_id=?", (command_request_id, project_id)).fetchone()
        if row is None or row["request_digest"] != request_digest or row["state"] != "approved" or row["authorization_state"] != "active":
            raise CommandRequestError("command approval is missing, stale, or already used")
        if datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            store.conn.execute("UPDATE command_requests SET state='expired',completed_at=? WHERE command_request_id=?", (utc_now(), command_request_id))
            store.conn.execute("UPDATE authorizations SET state='expired' WHERE authorization_id=? AND state='active'", (row["authorization_id"],))
            raise CommandRequestError("command approval has expired")
        store.conn.execute("UPDATE authorizations SET state='consumed',consumed_at=? WHERE authorization_id=? AND state='active'", (utc_now(), row["authorization_id"]))
        store.conn.execute("UPDATE command_requests SET state='running' WHERE command_request_id=?", (command_request_id,))
        return dict(store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=?", (command_request_id,)).fetchone())


def _runner_environment() -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "TMPDIR": "/private/tmp",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_EDITOR": "true",
        "GIT_ASKPASS": "true",
    }


def _bounded_output(value: str, limit: int = 512 * 1024) -> tuple[str, bool]:
    raw = value.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return value, False
    return raw[:limit].decode("utf-8", errors="replace"), True


def _working_copy_binding(store: Any, row: Mapping[str, Any]) -> Path:
    working_copy_id = row.get("working_copy_id")
    if not working_copy_id:
        raise CommandRequestError("command request has no working-copy boundary")
    working = store.conn.execute("SELECT path,project_id FROM working_copies WHERE working_copy_id=?", (working_copy_id,)).fetchone()
    if working is None or working["project_id"] != row["project_id"]:
        raise CommandRequestError("command working copy is not registered for the project")
    root = Path(str(working["path"])).resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise CommandRequestError("command working copy is unavailable")
    return root


def _working_directory(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise CommandRequestError("command working directory is invalid")
    candidate = Path(value).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise CommandRequestError("command working directory escapes the assigned working copy") from error
    if not candidate.is_dir() or candidate.is_symlink():
        raise CommandRequestError("command working directory is unavailable")
    return candidate


def _docker_executable() -> str:
    executable = shutil.which("docker", path=os.defpath)
    if executable is None:
        raise CommandRequestError("Docker is unavailable for a container-network command")
    return executable


def _run_container_network(root: Path, cwd: Path, command: list[str], duration_ms: int) -> dict[str, Any]:
    image = os.environ.get("PI_SYSTEM_RUNTIME_IMAGE")
    if not image or "\x00" in image or any(char.isspace() for char in image):
        raise CommandRequestError("container-network commands require PI_SYSTEM_RUNTIME_IMAGE")
    docker = _docker_executable()
    uid, gid = os.getuid(), os.getgid()
    relative = cwd.relative_to(root).as_posix()
    container_cwd = "/workspace" if relative == "." else "/workspace/" + relative
    args = [
        docker, "run", "--rm", "--network", "bridge", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", f"{uid}:{gid}",
        "--mount", f"type=bind,src={root},dst=/workspace,rw,bind-propagation=rprivate",
        "--workdir", container_cwd, image, *command,
    ]
    try:
        result = subprocess.run(args, cwd=str(root), env=_runner_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=max(1, duration_ms / 1000), check=False, shell=False)
    except subprocess.TimeoutExpired as error:
        stdout, stdout_truncated = _bounded_output((error.stdout or "") if isinstance(error.stdout, str) else "")
        stderr, stderr_truncated = _bounded_output((error.stderr or "") if isinstance(error.stderr, str) else "")
        return {"executionPlace": "container-network", "exitCode": None, "timedOut": True, "stdout": stdout, "stderr": stderr, "stdoutTruncated": stdout_truncated, "stderrTruncated": stderr_truncated, "network": "bridge", "image": image}
    stdout, stdout_truncated = _bounded_output(result.stdout)
    stderr, stderr_truncated = _bounded_output(result.stderr)
    return {"executionPlace": "container-network", "exitCode": result.returncode, "timedOut": False, "stdout": stdout, "stderr": stderr, "stdoutTruncated": stdout_truncated, "stderrTruncated": stderr_truncated, "network": "bridge", "image": image}


def _run_host(cwd: Path, command: list[str], duration_ms: int) -> dict[str, Any]:
    try:
        result = subprocess.run(command, cwd=str(cwd), env=_runner_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=max(1, duration_ms / 1000), check=False, shell=False)
    except subprocess.TimeoutExpired as error:
        stdout, stdout_truncated = _bounded_output((error.stdout or "") if isinstance(error.stdout, str) else "")
        stderr, stderr_truncated = _bounded_output((error.stderr or "") if isinstance(error.stderr, str) else "")
        return {"executionPlace": "host", "exitCode": None, "timedOut": True, "stdout": stdout, "stderr": stderr, "stdoutTruncated": stdout_truncated, "stderrTruncated": stderr_truncated}
    stdout, stdout_truncated = _bounded_output(result.stdout)
    stderr, stderr_truncated = _bounded_output(result.stderr)
    return {"executionPlace": "host", "exitCode": result.returncode, "timedOut": False, "stdout": stdout, "stderr": stderr, "stdoutTruncated": stdout_truncated, "stderrTruncated": stderr_truncated}


def execute_command(store: Any, *, project_id: str, command_request_id: str, request_digest: str) -> dict[str, Any]:
    """Consume one exact approval and execute only inside its recorded boundary."""

    validate_id(project_id, prefix="prj")
    validate_id(command_request_id, prefix="cmd")
    row = store.conn.execute("SELECT r.*,w.working_copy_id AS bound_working_copy_id FROM command_requests r JOIN runs x ON x.run_id=r.run_id LEFT JOIN working_copies w ON w.working_copy_id=x.working_copy_id WHERE r.command_request_id=? AND r.project_id=?", (command_request_id, project_id)).fetchone()
    if row is None or row["state"] != "approved" or row["request_digest"] != request_digest:
        raise CommandRequestError("command authorization could not be bound to a pending request")
    row = dict(row)
    row["working_copy_id"] = row.get("bound_working_copy_id")
    root = _working_copy_binding(store, row)
    cwd = _working_directory(root, str(row["working_directory"]))
    command = json.loads(str(row["command_json"]))
    if not isinstance(command, list) or not command or any(not isinstance(item, str) or not item or "\x00" in item for item in command):
        raise CommandRequestError("stored command request is invalid")
    consumed = consume_authorization(store, project_id=project_id, command_request_id=command_request_id, request_digest=request_digest)
    if consumed["state"] != "running":
        raise CommandRequestError("command authorization was not consumed")
    try:
        result = _run_host(cwd, command, int(row["expected_duration_ms"])) if row["execution_place"] == "host" else _run_container_network(root, cwd, command, int(row["expected_duration_ms"]))
    except (OSError, subprocess.SubprocessError, CommandRequestError) as error:
        result = {"executionPlace": row["execution_place"], "exitCode": None, "timedOut": False, "error": type(error).__name__, "message": str(error)[:1024]}
    state = "succeeded" if result.get("exitCode") == 0 and not result.get("timedOut") and "error" not in result else "failed"
    with store.transaction():
        store.conn.execute("UPDATE command_requests SET state=?,result_json=?,completed_at=? WHERE command_request_id=? AND state='running'", (state, canonical_json(result), utc_now(), command_request_id))
        append_event_in_transaction(store.conn, event_kind=f"command.{state}", resource_type="command-request", resource_id=command_request_id, payload={"projectId": project_id, "requestDigest": request_digest, "executionPlace": row["execution_place"], "exitCode": result.get("exitCode")})
        return dict(store.conn.execute("SELECT * FROM command_requests WHERE command_request_id=?", (command_request_id,)).fetchone())


__all__ = ["CommandRequestError", "authorize_command", "consume_authorization", "execute_command", "reject_command", "request_command"]
