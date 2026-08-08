"""One-command network runner for explicitly approved project operations."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .command_requests import CommandRequestError, execute_command
from .models import validate_id


class CommandExecutionError(CommandRequestError):
    """A bounded execution failure or route mismatch."""


def _assert_place(store: Any, project_id: str, command_request_id: str, place: str) -> None:
    row = store.conn.execute("SELECT execution_place FROM command_requests WHERE command_request_id=? AND project_id=?", (command_request_id, project_id)).fetchone()
    if row is None or row["execution_place"] != place:
        raise CommandExecutionError(f"request is not approved for {place} execution")


def execute_host_command(store: Any, *, project_id: str, command_request_id: str, request_digest: str, timeout_seconds: float = 60.0) -> dict[str, Any]:
    del timeout_seconds  # The exact request duration is controller-owned.
    validate_id(project_id, prefix="prj")
    validate_id(command_request_id, prefix="cmd")
    _assert_place(store, project_id, command_request_id, "host")
    return execute_command(store, project_id=project_id, command_request_id=command_request_id, request_digest=request_digest)


def execute_container_network_command(store: Any, *, project_id: str, command_request_id: str, request_digest: str, working_copy_path: str | None = None, image: str | None = None, timeout_seconds: float = 120.0) -> dict[str, Any]:
    del timeout_seconds  # The exact request duration is controller-owned.
    validate_id(project_id, prefix="prj")
    validate_id(command_request_id, prefix="cmd")
    _assert_place(store, project_id, command_request_id, "container-network")
    if image is not None and image != os.environ.get("PI_SYSTEM_RUNTIME_IMAGE"):
        raise CommandExecutionError("caller-supplied network image does not match the controller runtime identity")
    if working_copy_path is not None:
        requested = Path(working_copy_path).resolve(strict=True)
        row = store.conn.execute("SELECT w.path FROM command_requests r JOIN runs x ON x.run_id=r.run_id JOIN working_copies w ON w.working_copy_id=x.working_copy_id WHERE r.command_request_id=? AND r.project_id=?", (command_request_id, project_id)).fetchone()
        if row is None or requested != Path(row["path"]).resolve(strict=True):
            raise CommandExecutionError("caller-supplied working copy does not match the assigned worktree")
    return execute_network_command(store, project_id=project_id, command_request_id=command_request_id, request_digest=request_digest)


def execute_network_command(store: Any, *, project_id: str, command_request_id: str, request_digest: str) -> dict[str, Any]:
    """Execute an approved request only when its recorded place is container-network.

    The actual Docker invocation lives in the command-request executor so host
    and network paths share the same digest, expiry, and one-use transition.
    This narrow entry point prevents a caller from accidentally routing a
    network-approved request through a host-only helper.
    """

    validate_id(project_id, prefix="prj")
    validate_id(command_request_id, prefix="cmd")
    row = store.conn.execute("SELECT execution_place FROM command_requests WHERE command_request_id=? AND project_id=?", (command_request_id, project_id)).fetchone()
    if row is None or row["execution_place"] != "container-network":
        raise CommandRequestError("request is not a container-network command")
    return execute_command(store, project_id=project_id, command_request_id=command_request_id, request_digest=request_digest)


__all__ = ["CommandExecutionError", "execute_container_network_command", "execute_host_command", "execute_network_command"]
