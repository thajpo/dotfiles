"""Herdr workspace adapter for public protocol 19."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import socket
import stat
from typing import Any, Mapping

from ..adapters import AdapterHealth, AgentObservation, HarnessManifest, WorkspaceAdapter, WorkspaceManifest, WorkspaceObservation
from ..models import InvalidRequestError, NeedsAttentionError, PisecError, canonical_json, new_id, parse_json_strict, utc_now
from ..runtime import WORKSPACE_RUNTIME_MISSING

HERDR_PROTOCOL = 19
HERDR_MIN_VERSION = (0, 8, 0)
MAX_RESPONSE = 2 * 1024 * 1024


def default_socket_path() -> Path:
    return Path.home() / ".config" / "herdr" / "sessions" / "pisec" / "herdr.sock"


def _version_tuple(value: str) -> tuple[int, int, int]:
    try:
        parts = value.split(".")
        return int(parts[0]), int(parts[1]), int(parts[2].split("-")[0])
    except (ValueError, IndexError) as error:
        raise PisecError("workspace returned an invalid version") from error


def _runtime_source(runtime_instance_id: str) -> str:
    if not isinstance(runtime_instance_id, str) or not runtime_instance_id:
        raise ValueError("runtime instance id is required")
    return "pisec:omp:" + hashlib.sha256(runtime_instance_id.encode("utf-8")).hexdigest()[:32]


class HerdrWorkspaceAdapter:
    manifest = WorkspaceManifest(adapter_id="herdr", session_name="pisec", version_label="0.8.x", protocol_version=HERDR_PROTOCOL)

    @classmethod
    def from_config(cls, config: Mapping[str, Any], *, timeout: float = 30.0, validate: bool = True) -> "HerdrWorkspaceAdapter":
        if not isinstance(config, Mapping) or set(config) != {"sessionName", "socketPath"}:
            raise InvalidRequestError("Herdr workspace configuration fields are invalid")
        if config.get("sessionName") != cls.manifest.session_name:
            raise InvalidRequestError("workspace session must use the dedicated Pisec session")
        socket_path = config.get("socketPath")
        if not isinstance(socket_path, str) or not socket_path or "\x00" in socket_path:
            raise InvalidRequestError("Herdr workspace socketPath is invalid")
        path = Path(socket_path).expanduser()
        if not path.is_absolute():
            raise InvalidRequestError("Herdr workspace socketPath must be absolute or home-relative")
        return cls(path.absolute(), session_name=cls.manifest.session_name, timeout=timeout, validate=validate)

    def __init__(self, socket_path: Path | str | None = None, *, session_name: str = "pisec", timeout: float = 30.0, validate: bool = True):
        if session_name != self.manifest.session_name:
            raise PisecError("workspace session is not the dedicated Pisec session")
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
        response = parse_json_strict(b"".join(chunks).rstrip(b"\n"), max_bytes=MAX_RESPONSE)
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
        if _version_tuple(version) < HERDR_MIN_VERSION or _version_tuple(version) >= (0, 9, 0):
            self._validated = False
            self._validated_socket = None
            raise PisecError("workspace version is outside the tested 0.8.x range")
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

    @staticmethod
    def _observation(identity: Mapping[str, Any], *, worktree_path: str | None, branch_name: str | None, agent: Mapping[str, Any] | None) -> WorkspaceObservation:
        agent_observation = None
        if agent is not None:
            name = agent.get("name")
            surface_id = agent.get("pane_id")
            state = agent.get("agent_status", "unknown")
            if isinstance(name, str) and isinstance(surface_id, str):
                state = state if state in {"unknown", "starting", "working", "blocked", "idle", "done", "stopped", "missing", "error"} else "unknown"
                agent_observation = AgentObservation(name=name, surface_id=surface_id, interactive_ready=state in {"working", "blocked", "idle"}, state=state)
        return WorkspaceObservation(
            workspace_id=str(identity["workspace_id"]),
            view_id=str(identity["view_id"]),
            surface_id=str(identity["surface_id"]),
            worktree_path=worktree_path,
            branch_name=branch_name,
            agent=agent_observation,
        )

    def create_workspace(self, cwd: str, label: str, focus: bool = False) -> WorkspaceObservation:
        identity, _result = self._created(self._request("workspace.create", {"cwd": cwd, "label": label, "focus": focus}), "workspace_created")
        return self._observation(identity, worktree_path=None, branch_name=None, agent=None)

    def create_worktree(self, *, cwd: str, branch: str, base: str, path: str, label: str, focus: bool = False) -> WorkspaceObservation:
        existing = self.observe_workstream(path=path, agent_name="")
        if existing is not None:
            if existing.branch_name not in {branch, f"refs/heads/{branch}"}:
                raise NeedsAttentionError("existing workspace worktree does not match the approved branch")
            return existing
        identity, result = self._created(self._request("worktree.create", {"cwd": cwd, "branch": branch, "base": base, "path": path, "label": label, "focus": focus}), "worktree_created")
        worktree = result.get("worktree")
        if not isinstance(worktree, dict) or worktree.get("branch") not in (branch, f"refs/heads/{branch}"):
            raise NeedsAttentionError("workspace create response does not match the approved branch")
        return self._observation(identity, worktree_path=str(worktree.get("path", path)), branch_name=branch, agent=None)

    def start_agent(self, surface_id: str, name: str, agent_kind: str) -> dict[str, Any]:
        result = self._request("agent.start", {"pane_id": surface_id, "name": name, "kind": agent_kind})
        if result.get("type") != "agent_started":
            raise PisecError("workspace did not start the requested agent")
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

    def focus_agent(self, surface_id: str) -> dict[str, Any]:
        return self._request("agent.focus", {"target": surface_id})

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
            workspace_ids = {item.get("workspace_id") for item in panes if isinstance(item, dict) and isinstance(item.get("workspace_id"), str) and isinstance(item.get("cwd"), str) and str(Path(item["cwd"]).resolve(strict=False)) == target}
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
        surface_id = agent.get("pane_id") if agent is not None else next((item.get("pane_id") for item in panes if isinstance(item, dict) and item.get("workspace_id") == workspace_id), None)
        if not isinstance(surface_id, str) or not surface_id:
            return None
        view_id = agent.get("tab_id") if agent is not None else next((item.get("tab_id") for item in panes if isinstance(item, dict) and item.get("pane_id") == surface_id), None)
        if not isinstance(view_id, str) or not view_id:
            return None
        worktree = workspace.get("worktree")
        worktree_path = worktree.get("checkout_path") if isinstance(worktree, dict) else None
        branch_name = worktree.get("branch") if isinstance(worktree, dict) else None
        return self._observation({"workspace_id": workspace_id, "view_id": view_id, "surface_id": surface_id}, worktree_path=worktree_path, branch_name=branch_name, agent=agent)

    def report_session(self, surface_id: str, native_session: tuple[str, str], seq: int, start_source: str, runtime_instance_id: str, harness: HarnessManifest) -> dict[str, Any]:
        kind, value = native_session
        if kind not in {"path", "id"} or start_source not in {"startup", "resume"}:
            raise ValueError("invalid native session report")
        params: dict[str, Any] = {"pane_id": surface_id, "source": f"herdr:{harness.agent_kind}", "agent": harness.agent_kind, "seq": seq, "session_start_source": start_source}
        params["agent_session_path" if kind == "path" else "agent_session_id"] = value
        return self._request("pane.report_agent_session", params)

    def report_state(self, surface_id: str, state: str, message: str | None, seq: int, runtime_instance_id: str, harness: HarnessManifest) -> dict[str, Any]:
        if state not in {"idle", "working", "blocked", "unknown"}:
            raise ValueError("invalid workspace report state")
        return self._request("pane.report_agent", {"pane_id": surface_id, "source": _runtime_source(runtime_instance_id), "agent": harness.agent_kind, "state": state, "message": message, "seq": seq})

    def release_agent(self, surface_id: str, seq: int, runtime_instance_id: str, harness: HarnessManifest) -> dict[str, Any]:
        return self._request("pane.release_agent", {"pane_id": surface_id, "source": _runtime_source(runtime_instance_id), "agent": harness.agent_kind, "seq": seq})

    def reconcile(self, store: Any, event: Mapping[str, Any] | None = None) -> dict[str, Any]:
        snapshot = self.snapshot()
        agents = {item.get("name"): item for item in snapshot.get("agents", []) if isinstance(item, dict) and item.get("name")}
        workspaces = snapshot.get("workspaces", [])
        rows = store.conn.execute(
            "SELECT w.workstream_id,w.kind,w.worktree_path,w.desired_state,w.provisioning_state,r.agent_name,r.workspace_id,r.workspace_view_id,r.workspace_surface_id,(SELECT o.state FROM operations o WHERE o.workstream_id=w.workstream_id ORDER BY o.created_at DESC LIMIT 1) AS latest_operation_state FROM workstreams w LEFT JOIN runtime_bindings r USING(workstream_id) WHERE w.desired_state <> 'retired' AND w.provisioning_state <> 'proposed'"
        ).fetchall()
        updated = 0
        missing = 0
        for row in rows:
            agent = agents.get(row["agent_name"])
            workspace = next((item for item in workspaces if isinstance(item, dict) and item.get("workspace_id") == (agent or {}).get("workspace_id")), None) if agent is not None else None
            if workspace is None and row["kind"] == "worker":
                target = str(Path(row["worktree_path"]).resolve(strict=False))
                workspace = next((item for item in workspaces if isinstance(item, dict) and isinstance(item.get("worktree"), dict) and str(Path(item["worktree"].get("checkout_path", "")).resolve(strict=False)) == target), None)
            if row["provisioning_state"] in {"creating", "needs_attention"} and row["latest_operation_state"] not in {"succeeded", "cancelled"}:
                continue
            if workspace is None or agent is None:
                with store.transaction():
                    now = utc_now()
                    store.conn.execute("UPDATE runtime_bindings SET observed_state='missing',last_observed_at=?,updated_at=? WHERE workstream_id=?", (now, now, row["workstream_id"]))
                    store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?", (WORKSPACE_RUNTIME_MISSING, now, row["workstream_id"]))
                missing += 1
                continue
            view_id = agent.get("tab_id")
            surface_id = agent.get("pane_id")
            mismatch = not all(isinstance(agent.get(key), str) and agent.get(key) for key in ("workspace_id", "tab_id", "pane_id"))
            for column, observed in (("workspace_id", agent.get("workspace_id")), ("workspace_view_id", view_id), ("workspace_surface_id", surface_id)):
                if row[column] and row[column] != observed:
                    mismatch = True
            if row["kind"] == "worker":
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
            status = agent.get("agent_status", "unknown")
            observed = status if status in {"idle", "working", "blocked", "done"} else "unknown"
            with store.transaction():
                now = utc_now()
                store.conn.execute("UPDATE runtime_bindings SET workspace_id=?,workspace_view_id=?,workspace_surface_id=?,observed_state=?,last_observed_at=?,updated_at=? WHERE workstream_id=?", (agent["workspace_id"], agent["tab_id"], agent["pane_id"], observed, now, now, row["workstream_id"]))
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


