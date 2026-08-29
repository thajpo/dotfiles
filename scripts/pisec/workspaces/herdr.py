"""Herdr workspace adapter for public protocol 19."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import time
from typing import Any, Mapping, Sequence

from ..adapters import AdapterHealth, AgentObservation, HarnessManifest, RuntimeProcessObservation, WorkspaceAdapter, WorkspaceManifest, WorkspaceObservation
from ..models import MAX_JSON_ITEMS, MAX_TEXT, InvalidRequestError, NeedsAttentionError, PisecError, canonical_json, new_id, parse_json_strict, utc_now
from ..runtime import WORKSPACE_RUNTIME_MISSING

HERDR_PROTOCOL = 19
HERDR_MIN_VERSION = (0, 8, 0)
PANE_READY_MAX_SECONDS = 30.0
RUNTIME_TRIGGER_SETTLE_SECONDS = 1.5
MAX_RESPONSE = 2 * 1024 * 1024
MAX_SNAPSHOT_ITEMS = MAX_JSON_ITEMS * 4
PROCESS_INFO_MAX_TEXT = 128 * 1024
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RUNTIME_TRIGGER_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


def default_socket_path() -> Path:
    return Path.home() / ".config" / "herdr" / "sessions" / "main" / "herdr.sock"


def _version_tuple(value: str) -> tuple[int, int, int]:
    try:
        parts = value.split(".")
        return int(parts[0]), int(parts[1]), int(parts[2].split("-")[0])
    except (ValueError, IndexError) as error:
        raise PisecError("workspace returned an invalid version") from error


def _runtime_source(runtime_instance_id: str, harness_id: str = "omp") -> str:
    if not isinstance(runtime_instance_id, str) or not runtime_instance_id:
        raise ValueError("runtime instance id is required")
    return f"pisec:{harness_id}:" + hashlib.sha256(runtime_instance_id.encode("utf-8")).hexdigest()[:32]


def _harness_identity(harness: Any) -> tuple[str, str]:
    manifest = harness if isinstance(harness, HarnessManifest) else harness.manifest
    return manifest.adapter_id, manifest.agent_kind


class HerdrWorkspaceAdapter:
    manifest = WorkspaceManifest(adapter_id="herdr", session_name="main", version_label="0.8.0", protocol_version=HERDR_PROTOCOL)

    @classmethod
    def from_config(cls, config: Mapping[str, Any], *, timeout: float = 30.0, validate: bool = True) -> "HerdrWorkspaceAdapter":
        if not isinstance(config, Mapping) or set(config) != {"sessionName", "socketPath"}:
            raise InvalidRequestError("Herdr workspace configuration fields are invalid")
        if config.get("sessionName") != cls.manifest.session_name:
            raise InvalidRequestError("workspace session must use the persistent main session")
        socket_path = config.get("socketPath")
        if not isinstance(socket_path, str) or not socket_path or "\x00" in socket_path:
            raise InvalidRequestError("Herdr workspace socketPath is invalid")
        path = Path(socket_path).expanduser()
        if not path.is_absolute():
            raise InvalidRequestError("Herdr workspace socketPath must be absolute or home-relative")
        return cls(path.absolute(), session_name=cls.manifest.session_name, timeout=timeout, validate=validate)

    def __init__(self, socket_path: Path | str | None = None, *, session_name: str = "main", timeout: float = 30.0, validate: bool = True):
        if session_name != self.manifest.session_name:
            raise PisecError("workspace session must use the persistent main session")
        self.socket_path = Path(socket_path) if socket_path is not None else default_socket_path()
        self.timeout = timeout
        self._validated = False
        self._validated_socket: tuple[int, int, int, int] | None = None
        if validate:
            self.validate()

    def _check_socket(self) -> tuple[int, int, int, int]:
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError as error:
            self._validated = False
            self._validated_socket = None
            raise PisecError("workspace socket is unavailable") from error
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            self._validated = False
            self._validated_socket = None
            raise PisecError("workspace socket is not an owner-controlled Unix socket")
        if stat.S_IMODE(info.st_mode) & 0o077:
            self._validated = False
            self._validated_socket = None
            raise PisecError("workspace socket is accessible to group or other")
        return (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))

    def _request(self, method: str, params: Mapping[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        identity = self._check_socket()
        if method != "ping" and (not self._validated or identity != self._validated_socket):
            self._validated = False
            self._validated_socket = None
            self.validate()
        request_id = "pisec_" + new_id("req")[4:]
        wire = (canonical_json({"id": request_id, "method": method, "params": dict(params)}) + "\n").encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout if timeout is None else timeout)
            client.connect(str(self.socket_path))
            client.sendall(wire)
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise PisecError("workspace response exceeds the safety bound")
                chunks.append(chunk)
        response = parse_json_strict(
            b"".join(chunks).rstrip(b"\n"),
            max_bytes=MAX_RESPONSE,
            max_items=MAX_SNAPSHOT_ITEMS if method == "session.snapshot" else MAX_JSON_ITEMS,
            max_text=PROCESS_INFO_MAX_TEXT if method == "pane.process_info" else MAX_TEXT,
        )
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise PisecError("workspace returned a mismatched response")
        if "error" in response:
            error = response["error"]
            message = error.get("message", "workspace request failed") if isinstance(error, dict) else "workspace request failed"
            raise PisecError(str(message))
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("type"), str):
            raise PisecError("workspace returned an invalid result")
        return result

    def validate(self) -> dict[str, Any]:
        result = self._request("ping", {})
        if result.get("type") != "pong" or result.get("protocol") != HERDR_PROTOCOL:
            self._validated = False
            self._validated_socket = None
            raise PisecError("workspace socket protocol mismatch")
        version = str(result.get("version", ""))
        if version != "0.8.0":
            self._validated = False
            self._validated_socket = None
            raise PisecError("workspace version is not the pinned v1 version")
        self._validated_socket = self._check_socket()
        self._validated = True
        return result

    def _ensure_validated(self) -> None:
        identity = self._check_socket()
        if not self._validated or identity != self._validated_socket:
            self._validated = False
            self._validated_socket = None
            self.validate()

    def snapshot(self) -> dict[str, Any]:
        self._ensure_validated()
        result = self._request("session.snapshot", {})
        if result.get("type") != "session_snapshot" or not isinstance(result.get("snapshot"), dict):
            raise PisecError("workspace returned an invalid session snapshot")
        snapshot = result["snapshot"]
        if snapshot.get("protocol") != HERDR_PROTOCOL:
            raise PisecError("workspace snapshot protocol mismatch")
        if any(not isinstance(snapshot.get(key), list) for key in ("workspaces", "tabs", "panes", "layouts", "agents")):
            raise PisecError("workspace snapshot collections are invalid")
        return snapshot

    @staticmethod
    def _created(result: Mapping[str, Any], expected_type: str) -> tuple[dict[str, str], Mapping[str, Any]]:
        if result.get("type") != expected_type:
            raise PisecError("workspace returned an unexpected create result")
        workspace = result.get("workspace")
        tab = result.get("tab")
        pane = result.get("root_pane")
        if not all(isinstance(item, dict) for item in (workspace, tab, pane)):
            raise PisecError("workspace create response lacks identity")
        identity = {"workspace_id": workspace.get("workspace_id"), "view_id": tab.get("tab_id"), "surface_id": pane.get("pane_id")}
        if any(not isinstance(value, str) or not value for value in identity.values()):
            raise PisecError("workspace create response contains invalid identity")
        return identity, result

    def _wait_for_pane_ready(self, surface_id: str) -> None:
        deadline = time.monotonic() + min(max(float(self.timeout), 0.1), PANE_READY_MAX_SECONDS)
        while True:
            try:
                result = self._request(
                    "pane.read",
                    {"pane_id": surface_id, "source": "visible", "lines": 1, "format": "text"},
                )
                read = result.get("read")
                if result.get("type") == "pane_read" and isinstance(read, dict) and isinstance(read.get("text"), str) and read["text"]:
                    # Herdr can expose the shell prompt before its PTY accepts
                    # the first input.  Let the newly created pane settle so
                    # the launch command is not silently dropped.  A short
                    # settle is not enough on a busy persistent session; keep
                    # this bounded by the existing readiness deadline.
                    time.sleep(min(1.5, max(0.0, deadline - time.monotonic())))
                    return
            except PisecError:
                pass
            if time.monotonic() >= deadline:
                raise NeedsAttentionError("workspace pane did not become ready")
            time.sleep(0.05)

    @staticmethod
    def _observation(identity: Mapping[str, Any], *, worktree_path: str | None, branch_name: str | None, agent: Mapping[str, Any] | None) -> WorkspaceObservation:
        agent_observation = None
        if agent is not None:
            name = agent.get("name", agent.get("agent"))
            surface_id = agent.get("pane_id")
            state = agent.get("agent_status", "unknown")
            if isinstance(name, str) and isinstance(surface_id, str):
                state = state if state in {"unknown", "starting", "working", "blocked", "idle", "done", "stopped", "missing", "error"} else "unknown"
                agent_observation = AgentObservation(name=name, surface_id=surface_id, identity_usable=state in {"working", "blocked", "idle", "done"}, state=state)
        return WorkspaceObservation(
            workspace_id=str(identity["workspace_id"]),
            view_id=str(identity["view_id"]),
            surface_id=str(identity["surface_id"]),
            worktree_path=worktree_path,
            branch_name=branch_name,
            agent=agent_observation,
        )

    def create_workspace(self, cwd: str, label: str, focus: bool = False) -> WorkspaceObservation:
        params: dict[str, Any] = {"cwd": cwd, "label": label, "focus": focus}
        identity, _result = self._created(self._request("workspace.create", params), "workspace_created")
        self._wait_for_pane_ready(identity["surface_id"])
        return self._observation(identity, worktree_path=None, branch_name=None, agent=None)


    def create_tab(self, *, workspace_id: str, cwd: str, label: str, focus: bool = False) -> WorkspaceObservation:
        params = {"workspace_id": workspace_id, "cwd": cwd, "label": label, "focus": focus}
        identity, _result = self._created(self._request("tab.create", params), "tab_created")
        if identity["workspace_id"] != workspace_id:
            raise NeedsAttentionError("workspace tab create response escaped the project workspace")
        self._wait_for_pane_ready(identity["surface_id"])
        return self._observation(identity, worktree_path=None, branch_name=None, agent=None)
    def rename_tab(self, view_id: str, label: str) -> dict[str, Any]:
        if not isinstance(view_id, str) or not view_id or not isinstance(label, str) or not label or "\x00" in label:
            raise InvalidRequestError("workspace tab label is invalid")
        return self._request("tab.rename", {"tab_id": view_id, "label": label})

    def move_surface_to_tab(self, *, surface_id: str, workspace_id: str, label: str, focus: bool = False) -> WorkspaceObservation:
        if any(not isinstance(value, str) or not value or "\x00" in value for value in (surface_id, workspace_id, label)):
            raise InvalidRequestError("workspace pane move identity is invalid")
        result = self._request(
            "pane.move",
            {
                "pane_id": surface_id,
                "destination": {"type": "new_tab", "workspace_id": workspace_id, "label": label},
                "focus": focus,
            },
        )
        move_result = result.get("move_result") if result.get("type") == "pane_move" else result
        pane = move_result.get("pane") if isinstance(move_result, dict) else None
        moved = result.get("type") == "pane_moved" or result.get("type") == "pane_move" and isinstance(move_result, dict) and move_result.get("changed") is True
        if not moved or not isinstance(pane, dict):
            raise PisecError("workspace returned an invalid pane move result")
        identity = {
            "workspace_id": pane.get("workspace_id"),
            "view_id": pane.get("tab_id"),
            "surface_id": pane.get("pane_id"),
        }
        if identity["workspace_id"] != workspace_id or any(not isinstance(value, str) or not value for value in identity.values()):
            raise NeedsAttentionError("moved pane escaped the target workspace")
        observed_cwd = pane.get("foreground_cwd") if isinstance(pane.get("foreground_cwd"), str) else pane.get("cwd")
        return self._observation(identity, worktree_path=observed_cwd if isinstance(observed_cwd, str) else None, branch_name=None, agent=None)

    def run_command(self, surface_id: str, argv: Sequence[str], env: Mapping[str, str] | None = None) -> dict[str, Any]:
        if not isinstance(surface_id, str) or not surface_id or "\x00" in surface_id:
            raise InvalidRequestError("workspace surface id is invalid")
        values = list(argv)
        if not values or any(not isinstance(value, str) or not value or "\x00" in value for value in values):
            raise InvalidRequestError("workspace command argv is invalid")
        assignments: list[str] = []
        for key, value in (env or {}).items():
            if not isinstance(key, str) or _ENV_KEY_RE.fullmatch(key) is None or not isinstance(value, str) or "\x00" in value:
                raise InvalidRequestError("workspace command environment is invalid")
            assignments.append(f"{key}={shlex.quote(value)}")
        command = shlex.join(values)
        if assignments:
            command = " ".join((*assignments, command))
        sent = self._request("pane.send_text", {"pane_id": surface_id, "text": command})
        if sent.get("type") not in {"ok", "pane_text_sent"}:
            raise PisecError("workspace did not accept the runtime command text")
        entered = self._request("pane.send_keys", {"pane_id": surface_id, "keys": ["Enter"]})
        if entered.get("type") != "ok":
            raise PisecError("workspace did not accept the runtime command submission")
        return entered

    def stop_runtime(self, surface_id: str, harness_id: str | None = None) -> dict[str, Any]:
        if not isinstance(surface_id, str) or not surface_id or "\x00" in surface_id:
            raise InvalidRequestError("workspace surface id is invalid")
        if harness_id == "codex":
            sent = self._request("pane.send_text", {"pane_id": surface_id, "text": "/exit"})
            if sent.get("type") not in {"ok", "pane_text_sent"}:
                raise PisecError("workspace did not accept the Codex runtime exit command")
            entered = self._request("pane.send_keys", {"pane_id": surface_id, "keys": ["Enter"]})
            if entered.get("type") != "ok":
                raise PisecError("workspace did not accept the Codex runtime exit submission")
            return entered
        result = self._request("pane.send_keys", {"pane_id": surface_id, "keys": ["ctrl+d"]})
        if result.get("type") != "ok":
            raise PisecError("workspace did not accept the runtime stop request")
        return result

    def prompt_agent(self, surface_id: str, text: str, wait_until: tuple[str, ...], timeout_ms: int) -> dict[str, Any]:
        result = self._request("agent.prompt", {"target": surface_id, "text": text, "wait": {"until": list(wait_until), "timeout_ms": timeout_ms}}, timeout=max(self.timeout, timeout_ms / 1000.0 + 5.0))
        if result.get("type") != "agent_prompted":
            raise PisecError("workspace did not deliver the prompt")
        return result

    def prompt_agent_nowait(self, surface_id: str, text: str) -> dict[str, Any]:
        result = self._request("agent.prompt", {"target": surface_id, "text": text})
        if result.get("type") != "agent_prompted":
            raise PisecError("workspace did not deliver the prompt")
        return result

    def trigger_agent_nowait(self, surface_id: str, trigger: str, process_identity: str) -> dict[str, Any]:
        """Submit an inert turn trigger only to the exact live fenced runtime."""
        if not isinstance(trigger, str) or _RUNTIME_TRIGGER_RE.fullmatch(trigger) is None:
            raise InvalidRequestError("workspace runtime trigger is invalid")
        observation = self.observe_runtime(surface_id, process_identity)
        if observation.state != "live":
            raise NeedsAttentionError("workspace runtime trigger target is not the live fenced runtime")
        sent = self._request("pane.send_text", {"pane_id": surface_id, "text": trigger})
        if sent.get("type") not in {"ok", "pane_text_sent"}:
            raise PisecError("workspace did not accept the runtime trigger text")
        # Codex handles pasted text asynchronously.  Submitting Enter before
        # the paste settles can leave the trigger in the composer instead of
        # starting a turn.
        time.sleep(RUNTIME_TRIGGER_SETTLE_SECONDS)
        entered = self._request("pane.send_keys", {"pane_id": surface_id, "keys": ["Enter"]})
        if entered.get("type") != "ok":
            raise PisecError("workspace did not accept the runtime trigger submission")
        return {"type": "runtime_triggered", "pane_id": surface_id, "verified_runtime": True}

    def prompt_eligible(self, agent_observation: AgentObservation) -> bool:
        return bool(agent_observation.identity_usable and agent_observation.state in {"idle", "done"})

    def focus_pane(self, surface_id: str) -> dict[str, Any]:
        return self._request("pane.focus", {"pane_id": surface_id})

    def close_tab(self, view_id: str) -> dict[str, Any]:
        try:
            return self._request("tab.close", {"tab_id": view_id})
        except PisecError as error:
            if str(error) == f"tab {view_id} not found":
                return {"type": "tab_closed", "tab_id": view_id, "already_closed": True}
            raise
    def close_workspace(self, workspace_id: str) -> dict[str, Any]:
        try:
            return self._request("workspace.close", {"workspace_id": workspace_id})
        except PisecError as error:
            if str(error) == f"workspace {workspace_id} not found":
                return {"type": "workspace_closed", "workspace_id": workspace_id, "already_closed": True}
            raise

    def list_worktrees(self, project_root: str) -> list[dict[str, Any]]:
        result = self._request("worktree.list", {"cwd": project_root})
        if result.get("type") != "worktree_list" or not isinstance(result.get("worktrees"), list):
            raise PisecError("workspace returned an invalid worktree list")
        return result["worktrees"]

    def observe_workstream(self, *, path: str, agent_name: str) -> WorkspaceObservation | None:
        target = str(Path(path).resolve(strict=False))
        snapshot = self.snapshot()
        agents = snapshot.get("agents", [])
        workspaces = snapshot.get("workspaces", [])
        panes = snapshot.get("panes", [])
        agent: Mapping[str, Any] | None = None
        if agent_name:
            agent = next((item for item in agents if isinstance(item, dict) and item.get("name") == agent_name), None)
        candidates: list[Mapping[str, Any]] = []
        for item in workspaces:
            if not isinstance(item, dict):
                continue
            worktree = item.get("worktree")
            if isinstance(worktree, dict) and str(Path(worktree.get("checkout_path", "")).resolve(strict=False)) == target:
                candidates.append(item)
        if not candidates:
            workspace_ids = {
                item.get("workspace_id")
                for item in panes
                if isinstance(item, dict)
                and isinstance(item.get("workspace_id"), str)
                and any(isinstance(value, str) and str(Path(value).resolve(strict=False)) == target for value in (item.get("cwd"), item.get("foreground_cwd")))
            }
            candidates = [item for item in workspaces if isinstance(item, dict) and item.get("workspace_id") in workspace_ids]
        workspace: Mapping[str, Any] | None = None
        if agent is not None:
            workspace = next((item for item in candidates if item.get("workspace_id") == agent.get("workspace_id")), None)
            if workspace is None and not candidates:
                workspace = next((item for item in workspaces if isinstance(item, dict) and item.get("workspace_id") == agent.get("workspace_id")), None)
        if workspace is None and len(candidates) == 1:
            workspace = candidates[0]
        if workspace is None:
            return None
        workspace_id = workspace.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise PisecError("workspace observation lacks workspace identity")
        matching_panes = [
            item
            for item in panes
            if isinstance(item, dict)
            and item.get("workspace_id") == workspace_id
            and any(isinstance(value, str) and str(Path(value).resolve(strict=False)) == target for value in (item.get("cwd"), item.get("foreground_cwd")))
        ]
        if len(matching_panes) > 1:
            raise NeedsAttentionError("workspace has duplicate panes for the approved checkout")
        matching_pane = matching_panes[0] if matching_panes else None
        surface_id = agent.get("pane_id") if agent is not None else (matching_pane or {}).get("pane_id")
        if not isinstance(surface_id, str) or not surface_id:
            return None
        view_id = agent.get("tab_id") if agent is not None else (matching_pane or {}).get("tab_id")
        if not isinstance(view_id, str) or not view_id:
            return None
        if agent is None:
            pane_agents = [item for item in agents if isinstance(item, dict) and item.get("pane_id") == surface_id]
            if len(pane_agents) > 1:
                raise NeedsAttentionError("workspace pane has duplicate agent identities")
            agent = pane_agents[0] if pane_agents else None
        worktree = workspace.get("worktree")
        worktree_path = worktree.get("checkout_path") if isinstance(worktree, dict) else None
        branch_name = worktree.get("branch") if isinstance(worktree, dict) else None
        return self._observation({"workspace_id": workspace_id, "view_id": view_id, "surface_id": surface_id}, worktree_path=worktree_path, branch_name=branch_name, agent=agent)
    def observe_tab(self, *, workspace_id: str, cwd: str) -> WorkspaceObservation | None:
        snapshot = self.snapshot()
        workspace = next((item for item in snapshot["workspaces"] if isinstance(item, dict) and item.get("workspace_id") == workspace_id), None)
        if workspace is None:
            return None
        expected = str(Path(cwd).resolve(strict=False))
        panes = [
            item
            for item in snapshot["panes"]
            if isinstance(item, dict)
            and item.get("workspace_id") == workspace_id
            and any(isinstance(value, str) and str(Path(value).resolve(strict=False)) == expected for value in (item.get("cwd"), item.get("foreground_cwd")))
        ]
        if len(panes) > 1:
            raise NeedsAttentionError("workspace has duplicate tabs for the approved checkout")
        if not panes:
            return None
        pane = panes[0]
        view_id = pane.get("tab_id")
        surface_id = pane.get("pane_id")
        if not isinstance(view_id, str) or not view_id or not isinstance(surface_id, str) or not surface_id:
            raise NeedsAttentionError("workspace tab observation lacks pane identity")
        agents = [item for item in snapshot["agents"] if isinstance(item, dict) and item.get("pane_id") == surface_id]
        if len(agents) > 1:
            raise NeedsAttentionError("workspace tab has duplicate agent identities")
        return self._observation(
            {"workspace_id": workspace_id, "view_id": view_id, "surface_id": surface_id},
            worktree_path=expected,
            branch_name=None,
            agent=agents[0] if agents else None,
        )


    def observe_surface(self, *, workspace_id: str, view_id: str, surface_id: str, cwd: str) -> WorkspaceObservation | None:
        snapshot = self.snapshot()
        workspace = next((item for item in snapshot["workspaces"] if isinstance(item, dict) and item.get("workspace_id") == workspace_id), None)
        tab = next((item for item in snapshot["tabs"] if isinstance(item, dict) and item.get("tab_id") == view_id and item.get("workspace_id") == workspace_id), None)
        pane = next((item for item in snapshot["panes"] if isinstance(item, dict) and item.get("pane_id") == surface_id and item.get("tab_id") == view_id and item.get("workspace_id") == workspace_id), None)
        if workspace is None or tab is None or pane is None:
            return None
        expected = str(Path(cwd).resolve(strict=False))
        observed_cwds = [pane.get("cwd"), pane.get("foreground_cwd")]
        if not any(isinstance(value, str) and str(Path(value).resolve(strict=False)) == expected for value in observed_cwds):
            raise NeedsAttentionError("workspace pane cwd does not match the durable binding")
        agents = [item for item in snapshot["agents"] if isinstance(item, dict) and item.get("pane_id") == surface_id]
        if len(agents) > 1:
            raise NeedsAttentionError("workspace pane has duplicate agent identities")
        worktree = workspace.get("worktree")
        worktree_path = worktree.get("checkout_path") if isinstance(worktree, dict) else None
        branch_name = worktree.get("branch") if isinstance(worktree, dict) else None
        return self._observation(
            {"workspace_id": workspace_id, "view_id": view_id, "surface_id": surface_id},
            worktree_path=worktree_path,
            branch_name=branch_name,
            agent=agents[0] if agents else None,
        )

    def observe_runtime(self, surface_id: str, process_identity: str) -> RuntimeProcessObservation:
        if not isinstance(surface_id, str) or not surface_id or "\x00" in surface_id:
            raise InvalidRequestError("workspace surface id is invalid")
        if not isinstance(process_identity, str) or not process_identity or "\x00" in process_identity:
            raise InvalidRequestError("runtime process identity is invalid")
        result = self._request("pane.process_info", {"pane_id": surface_id})
        process_info = result.get("process_info")
        if result.get("type") != "pane_process_info" or not isinstance(process_info, dict) or process_info.get("pane_id") != surface_id:
            raise PisecError("workspace returned invalid pane process information")
        processes = process_info.get("foreground_processes")
        shell_pid = process_info.get("shell_pid")
        if not isinstance(processes, list) or len(processes) > 128 or not isinstance(shell_pid, int) or isinstance(shell_pid, bool) or shell_pid < 1:
            return RuntimeProcessObservation("unknown", "invalid foreground process information")
        valid_processes: list[Mapping[str, Any]] = []
        for process in processes:
            if not isinstance(process, dict):
                return RuntimeProcessObservation("unknown", "invalid foreground process information")
            argv = process.get("argv")
            pid = process.get("pid")
            if not isinstance(argv, list) or any(not isinstance(value, str) for value in argv) or not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
                return RuntimeProcessObservation("unknown", "invalid foreground process information")
            valid_processes.append(process)
            for index, value in enumerate(argv[:-1]):
                if value in {"--settings", "--config"} and argv[index + 1] == process_identity and "--" in argv[index + 2 :]:
                    return RuntimeProcessObservation("live", f"pid={pid}")
        if len(valid_processes) == 1 and valid_processes[0]["pid"] == shell_pid:
            return RuntimeProcessObservation("stopped", f"shell_pid={shell_pid}")
        if not valid_processes:
            return RuntimeProcessObservation("unknown", "no foreground process information")
        return RuntimeProcessObservation("unknown", "foreground process does not match the durable runtime")

    def report_session(self, surface_id: str, native_session: tuple[str, str], seq: int, start_source: str, runtime_instance_id: str, harness: HarnessManifest) -> dict[str, Any]:
        kind, value = native_session
        if kind not in {"path", "id"} or start_source not in {"startup", "resume"}:
            raise ValueError("invalid native session report")
        harness_id, agent_kind = _harness_identity(harness)
        params: dict[str, Any] = {"pane_id": surface_id, "source": _runtime_source(runtime_instance_id, harness_id), "agent": agent_kind, "seq": seq, "session_start_source": start_source}
        params["agent_session_path" if kind == "path" else "agent_session_id"] = value
        result = self._request("pane.report_agent_session", params)
        if result.get("type") != "ok":
            raise PisecError("workspace rejected the runtime session report")
        return result

    def report_state(self, surface_id: str, state: str, message: str | None, seq: int, runtime_instance_id: str, harness: HarnessManifest) -> dict[str, Any]:
        if state not in {"idle", "working", "blocked", "unknown"}:
            raise ValueError("invalid workspace report state")
        harness_id, agent_kind = _harness_identity(harness)
        result = self._request("pane.report_agent", {"pane_id": surface_id, "source": _runtime_source(runtime_instance_id, harness_id), "agent": agent_kind, "state": state, "message": message, "seq": seq})
        if result.get("type") != "ok":
            raise PisecError("workspace rejected the runtime state report")
        return result

    def release_agent(self, surface_id: str, seq: int, runtime_instance_id: str, harness: HarnessManifest) -> dict[str, Any]:
        harness_id, agent_kind = _harness_identity(harness)
        result = self._request("pane.release_agent", {"pane_id": surface_id, "source": _runtime_source(runtime_instance_id, harness_id), "agent": agent_kind, "seq": seq})
        if result.get("type") != "ok":
            raise PisecError("workspace rejected the runtime lifecycle report")
        return result

    def reconcile(self, store: Any, event: Mapping[str, Any] | None = None) -> dict[str, Any]:
        snapshot = self.snapshot()
        agents = [item for item in snapshot.get("agents", []) if isinstance(item, dict)]
        workspaces = snapshot.get("workspaces", [])
        panes = snapshot.get("panes", [])
        workstream_columns = {str(item[1]) for item in store.conn.execute("PRAGMA table_info(workstreams)").fetchall()}
        harness_column = "w.harness_id" if "harness_id" in workstream_columns else "'omp'"
        rows = store.conn.execute(
            f"SELECT w.workstream_id,w.kind,{harness_column} AS harness_id,w.worktree_path,w.desired_state,w.provisioning_state,r.observed_state,r.agent_name,r.workspace_id,r.workspace_view_id,r.workspace_surface_id,r.policy_path,(SELECT o.state FROM operations o WHERE o.workstream_id=w.workstream_id ORDER BY o.created_at DESC LIMIT 1) AS latest_operation_state FROM workstreams w LEFT JOIN runtime_bindings r USING(workstream_id) WHERE w.desired_state <> 'retired' AND w.provisioning_state <> 'proposed'"
        ).fetchall()
        updated = 0
        missing = 0
        skipped = {
            str(workstream_id)
            for workstream_id in (event or {}).get("skipWorkstreams", [])
            if isinstance(workstream_id, str) and workstream_id
        }
        for row in rows:
            if row["workstream_id"] in skipped:
                continue
            workspace = next((item for item in workspaces if isinstance(item, dict) and item.get("workspace_id") == row["workspace_id"]), None)
            pane = next(
                (
                    item
                    for item in panes
                    if isinstance(item, dict)
                    and item.get("pane_id") == row["workspace_surface_id"]
                    and item.get("tab_id") == row["workspace_view_id"]
                    and item.get("workspace_id") == row["workspace_id"]
                ),
                None,
            )
            agent = next((item for item in agents if item.get("pane_id") == row["workspace_surface_id"]), None)
            if row["provisioning_state"] in {"creating", "needs_attention"} and row["latest_operation_state"] not in {"succeeded", "cancelled"}:
                continue
            if workspace is None or pane is None:
                with store.transaction():
                    now = utc_now()
                    store.conn.execute("UPDATE runtime_bindings SET observed_state='missing',last_observed_at=?,updated_at=? WHERE workstream_id=?", (now, now, row["workstream_id"]))
                    store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?", (WORKSPACE_RUNTIME_MISSING, now, row["workstream_id"]))
                missing += 1
                continue
            view_id = pane.get("tab_id")
            surface_id = pane.get("pane_id")
            mismatch = not isinstance(view_id, str) or not view_id or not isinstance(surface_id, str) or not surface_id
            if agent is not None:
                expected_names = {str(row["agent_name"]), str(row["harness_id"])}
                mismatch = mismatch or agent.get("name", agent.get("agent")) not in expected_names
            if row["kind"] == "worker":
                observed_path = pane.get("cwd") if isinstance(pane, dict) else None
                if not isinstance(observed_path, str) and isinstance(pane, dict):
                    observed_path = pane.get("foreground_cwd")
                if not isinstance(observed_path, str):
                    worktree = workspace.get("worktree") if isinstance(workspace, dict) else None
                    observed_path = worktree.get("checkout_path") if isinstance(worktree, dict) else None
                mismatch = mismatch or not isinstance(observed_path, str) or str(Path(observed_path).resolve(strict=False)) != str(Path(row["worktree_path"]).resolve(strict=False))
            if mismatch:
                with store.transaction():
                    now = utc_now()
                    store.conn.execute("UPDATE runtime_bindings SET observed_state='error',last_observed_at=?,updated_at=? WHERE workstream_id=?", (now, now, row["workstream_id"]))
                    store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?", ("workspace identity does not match the durable binding", now, row["workstream_id"]))
                missing += 1
                continue
            runtime = self.observe_runtime(str(surface_id), str(row["policy_path"]))
            if runtime.state != "live":
                observed_state = "stopped" if runtime.state == "stopped" else "error"
                reason = WORKSPACE_RUNTIME_MISSING if runtime.state == "stopped" else "workspace pane process identity is ambiguous"
                with store.transaction():
                    now = utc_now()
                    store.conn.execute("UPDATE runtime_bindings SET observed_state=?,last_observed_at=?,updated_at=? WHERE workstream_id=?", (observed_state, now, now, row["workstream_id"]))
                    store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?", (reason, now, row["workstream_id"]))
                missing += 1
                continue
            # Runtime reports are authenticated and ordered; a workspace snapshot
            # is only an identity/presence observation and must not overwrite the
            # runtime activity state (for example, a stale "working" snapshot
            # must not erase a just-reported "idle" state).
            with store.transaction():
                now = utc_now()
                store.conn.execute("UPDATE runtime_bindings SET last_observed_at=?,updated_at=? WHERE workstream_id=?", (now, now, row["workstream_id"]))
                restored_state = "bound" if row["provisioning_state"] == "needs_attention" else row["provisioning_state"]
                store.conn.execute("UPDATE workstreams SET provisioning_state=?,attention_reason=NULL,updated_at=? WHERE workstream_id=?", (restored_state, now, row["workstream_id"]))
            updated += 1
        return {"reconciled": True, "updated": updated, "missing": missing, "eventAccepted": event is not None}

    def health_checks(self) -> tuple[AdapterHealth, ...]:
        try:
            self._ensure_validated()
            return (AdapterHealth("Herdr protocol", True, f"protocol={HERDR_PROTOCOL}"),)
        except Exception as error:
            return (AdapterHealth("Herdr protocol", False, str(error)[:256]),)
