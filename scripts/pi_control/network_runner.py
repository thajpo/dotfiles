"""One-command approved execution runners.

The container runner fails closed when Docker is unavailable; it never silently
falls back to host execution.  Host execution is a distinct, explicit path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any


class CommandExecutionError(RuntimeError):
    pass


def _argv(row: Any) -> list[str]:
    value = json.loads(row["command_json"])
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        raise CommandExecutionError("stored command is invalid")
    return value


def execute_host_command(store: Any, *, project_id: str, command_request_id: str, request_digest: str, timeout_seconds: float = 60.0) -> dict[str, Any]:
    from .command_requests import consume_authorization
    row = consume_authorization(store, project_id=project_id, command_request_id=command_request_id, request_digest=request_digest)
    if row["execution_place"] != "host":
        raise CommandExecutionError("command is not approved for host execution")
    argv = _argv(row)
    cwd = Path(row["working_directory"]).resolve(strict=True)
    env = {"PATH": os.defpath, "HOME": str(cwd), "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=min(timeout_seconds, 3600), check=False, shell=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        _finish(store, command_request_id, "failed", {"error": type(error).__name__})
        raise CommandExecutionError("approved host command failed to execute") from error
    payload = {"returnCode": result.returncode, "stdout": result.stdout[-65536:], "stderr": result.stderr[-65536:]}
    _finish(store, command_request_id, "succeeded" if result.returncode == 0 else "failed", payload)
    return payload


def execute_container_network_command(store: Any, *, project_id: str, command_request_id: str, request_digest: str, working_copy_path: str | Path, image: str, timeout_seconds: float = 120.0) -> dict[str, Any]:
    from .command_requests import consume_authorization
    row = consume_authorization(store, project_id=project_id, command_request_id=command_request_id, request_digest=request_digest)
    if row["execution_place"] != "container-network":
        raise CommandExecutionError("command is not approved for container network execution")
    docker = shutil.which("docker", path=os.defpath)
    if docker is None:
        _finish(store, command_request_id, "failed", {"error": "docker-unavailable"})
        raise CommandExecutionError("Docker is unavailable; host fallback is forbidden")
    argv = _argv(row)
    source = Path(working_copy_path).resolve(strict=True)
    command = [docker, "run", "--rm", "--network", "bridge", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "-v", f"{source}:/workspace:rw", "-w", "/workspace", image, *argv]
    try:
        result = subprocess.run(command, cwd=str(source), env={"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C"}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=min(timeout_seconds, 3600), check=False, shell=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        _finish(store, command_request_id, "failed", {"error": type(error).__name__})
        raise CommandExecutionError("approved container network command failed to execute") from error
    payload = {"returnCode": result.returncode, "stdout": result.stdout[-65536:], "stderr": result.stderr[-65536:], "executionPlace": "container-network"}
    _finish(store, command_request_id, "succeeded" if result.returncode == 0 else "failed", payload)
    return payload


def _finish(store: Any, command_request_id: str, state: str, result: dict[str, Any]) -> None:
    with store.transaction():
        store.conn.execute("UPDATE command_requests SET state=?,result_json=?,completed_at=? WHERE command_request_id=? AND state='running'", (state, json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")), __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z'), command_request_id))


__all__ = ["CommandExecutionError", "execute_container_network_command", "execute_host_command"]
