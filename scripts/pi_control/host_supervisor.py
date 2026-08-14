"""Production supervisor for exact controller-selected host Pi processes."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Mapping

from .controller_channel import ChannelReader, ControllerChannelError, PROTOCOL_VERSION, receive_frame, send_frame, validate_handshake
from .docker_runtime import (
    PINNED_ACCEPTANCE_IMAGE, DockerRuntimeError, attest_container, cleanup_run_container, create_start_container,
    execute_file_tool, execute_shell_tool, inspect_container, prepare_tool_runtime,
)
from .pi_install import RESOURCE_INVENTORY
from .installed_builds import verify_registered_build
from .launch import LaunchError, attest_run, fail_run, prepare_run, stop_run
from .models import canonical_json, new_id, utc_now
from .process_adapter import process_start_identity
from .run_manifest import executable_sha256, read_manifest, require_manifest_active
from .scoped_read import ScopedProjectReader
from .subagents import READ_ONLY_ROLES


CHANNEL_ENVIRONMENT_KEY = "PI_CONTROLLER_CHANNEL_FD"
ACCEPTANCE_PROFILE = "scripted-v1"
_MODEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROVIDER_ENVIRONMENT = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN"),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GEMINI_API_KEY",),
}
_ROLE_OPERATIONS = {
    "secretary": {"scoped-read", "message.post", "message.list", "message.acknowledge", "message.reply", "dependency.disposition", "package-review.gate", "subagent.spawn", "subagent.start", "subagent.status", "subagent.wait", "subagent.list", "subagent.interrupt", "subagent.stop", "subagent.resume", "subagent.steer", "project.work-index", "change.list", "review.request", "integration.analyze", "observe.tasks", "observe.fleet", "observe.messages", "observe.queue", "investigation.start", "workstream.propose", "workstream.approve", "workstream.apply", "review.propose", "integration.propose"},
    "investigator": {"scoped-read", "package-review.record", "message.post", "message.list", "message.acknowledge", "message.reply"},
    "reviewer": {"scoped-read", "package-review.gate", "message.post", "message.list", "message.acknowledge", "message.reply"},
    "personal": {"writer-tool", "message.post", "message.list", "message.acknowledge", "message.reply", "command.request", "command.status", "dependency.inventory", "package.request", "package.status", "subagent.spawn", "subagent.start", "subagent.status", "subagent.wait", "subagent.list", "subagent.interrupt", "subagent.stop", "subagent.resume", "subagent.steer", "worker.start", "change.submit", "project.work-index", "observe.tasks", "observe.fleet", "observe.messages", "observe.queue"},
    "workstream": {"writer-tool", "message.post", "message.list", "message.acknowledge", "message.reply", "command.request", "command.status", "dependency.inventory", "package.request", "package.status", "subagent.spawn", "subagent.start", "subagent.status", "subagent.wait", "subagent.list", "subagent.interrupt", "subagent.stop", "subagent.resume", "subagent.steer", "worker.start", "change.submit", "project.work-index", "observe.tasks", "observe.fleet", "observe.messages", "observe.queue"},
    "integration": {"writer-tool", "message.post", "message.list", "message.acknowledge", "message.reply", "command.request", "command.status", "dependency.inventory", "package.request", "package.status", "subagent.spawn", "subagent.start", "subagent.status", "subagent.wait", "subagent.list", "subagent.interrupt", "subagent.stop", "subagent.resume", "subagent.steer", "worker.start", "change.submit", "project.work-index", "observe.tasks", "observe.fleet", "observe.messages", "observe.queue"},
}
_TOOL_RESOURCE = {
    "read": "scoped-or-writer", "ls": "extension:scoped-project-read", "grep": "extension:scoped-project-read", "git_read": "extension:scoped-project-read",
    "write": "package:pi-sandbox-control", "edit": "package:pi-sandbox-control", "bash": "package:pi-sandbox-control",
    "post_project_message": "extension:controller-channel", "list_project_messages": "extension:controller-channel",
    "acknowledge_project_message": "extension:controller-channel", "reply_project_message": "extension:controller-channel",
    "request_project_command": "extension:controller-channel", "project_command_status": "extension:controller-channel",
    "inventory_dependency_changes": "extension:controller-channel", "record_dependency_disposition": "extension:controller-channel",
    "record_package_security_review": "extension:controller-channel", "check_package_review_gate": "extension:controller-channel",
    "request_package_operation": "extension:controller-channel", "package_operation_status": "extension:controller-channel",
    "subagent": "package:pi-subagents",
    "subagent_start": "package:pi-subagents", "subagent_status": "package:pi-subagents",
    "subagent_wait": "package:pi-subagents", "subagent_list": "package:pi-subagents",
    "subagent_interrupt": "package:pi-subagents", "subagent_stop": "package:pi-subagents",
    "subagent_resume": "package:pi-subagents", "subagent_steer": "package:pi-subagents",
    "worker_start": "package:pi-subagents",
    "submit_change": "extension:controller-channel", "list_changes": "extension:controller-channel",
    "request_review": "extension:controller-channel", "analyze_integration": "extension:controller-channel",
    "observe_tasks": "extension:controller-channel", "observe_fleet": "extension:controller-channel",
    "observe_messages": "extension:controller-channel", "observe_change_queue": "extension:controller-channel",
    "harness_feedback": "extension:harness-feedback",
    "project_work_index": "extension:controller-channel", "start_investigation": "extension:controller-channel",
    "propose_workstream": "extension:controller-channel", "approve_workstream": "extension:controller-channel",
    "propose_review": "extension:controller-channel", "propose_integration": "extension:controller-channel",
}


class HostSupervisorError(LaunchError):
    pass


def _install_cleanup_signal_handlers() -> tuple[dict[int, Any], dict[str, Any]]:
    """Convert SIGHUP/SIGTERM into a controlled supervisor unwind.

    tmux pane death and `pi-restart` deliver SIGHUP to the supervisor; without
    a handler the process dies and the `finally` cleanup never runs. Raising
    SystemExit from the handler enters the existing `except BaseException`
    path, which kills the child, cleans the exact container, and fails the run
    in durable state before the process exits.
    """

    state = {"started": False, "signum": None}

    def _raise_system_exit(signum: int, _frame: Any) -> None:
        if state["started"]:
            return
        state["started"] = True
        state["signum"] = signum
        raise SystemExit(f"host supervisor received signal {signum}")

    previous: dict[int, Any] = {}
    for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.signal(signum, _raise_system_exit)
        except (OSError, ValueError):
            pass
    return previous, state


def _restore_cleanup_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):
            pass


def _terminate_child(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _regular_file(path: Path, *, executable: bool = False, require_user: bool = True) -> Path:
    value = path.expanduser().absolute()
    current = Path(value.anchor)
    for component in value.parts[1:]:
        current /= component
        if current.is_symlink():
            raise HostSupervisorError("selected resource path must not contain a symlink")
    try:
        info = value.lstat()
    except FileNotFoundError as error:
        raise HostSupervisorError("selected resource is unavailable") from error
    if not stat.S_ISREG(info.st_mode) or (require_user and info.st_uid != os.geteuid()) or (executable and not os.access(value, os.X_OK)):
        raise HostSupervisorError(f"selected resource is not an allowed regular file: {value}")
    return value


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise HostSupervisorError("runtime directory is unsafe")
    os.chmod(path, 0o700)


def ensure_session(path: Path, *, state_root: Path, session_id: str, cwd: Path, timestamp: str | None = None) -> dict[str, Any]:
    state = state_root.resolve(strict=True)
    destination = path.absolute()
    try:
        destination.relative_to(state / "sessions")
    except ValueError as error:
        raise HostSupervisorError("controller session path escapes the state root") from error
    current = state
    for part in destination.parent.relative_to(state).parts:
        current /= part
        _private_directory(current)
    header = {"type": "session", "version": 3, "id": session_id, "timestamp": timestamp or utc_now(), "cwd": str(cwd)}
    body = canonical_json(header).encode("utf-8") + b"\n"
    if not destination.exists() and not destination.is_symlink():
        fd, temporary = tempfile.mkstemp(prefix=".session-", dir=str(destination.parent))
        temporary_path = Path(temporary)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary_path, destination, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            temporary_path.unlink(missing_ok=True)
    info = destination.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise HostSupervisorError("controller session file type, owner, or mode is invalid")
    with destination.open("rb") as stream:
        first = stream.readline(16 * 1024)
    try:
        existing = json.loads(first.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HostSupervisorError("controller session header is invalid") from error
    if not isinstance(existing, dict) or set(existing) != set(header) or existing.get("type") != "session" or existing.get("version") != 3 or existing.get("id") != session_id or existing.get("cwd") != str(cwd):
        raise HostSupervisorError("controller session header identity does not match the conversation")
    timestamp_value = existing.get("timestamp")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00")) if isinstance(timestamp_value, str) and timestamp_value.endswith("Z") else None
    except ValueError:
        parsed_timestamp = None
    if parsed_timestamp is None or parsed_timestamp.utcoffset() != timezone.utc.utcoffset(parsed_timestamp) or canonical_json(existing).encode("utf-8") + b"\n" != first:
        raise HostSupervisorError("controller session header is not canonical Pi v3 JSON")
    return existing


def _load_inventory(build: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    path = Path(build["resource_manifest_path"])
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HostSupervisorError("registered release resource inventory is unreadable") from error
    if not isinstance(value, dict) or value.get("schemaVersion") != 1 or path.name != RESOURCE_INVENTORY:
        raise HostSupervisorError("registered release resource inventory is invalid")
    return path.parent.resolve(strict=True), value


def _resource(stage: Path, relative: str, digest: str, resource_id: str) -> dict[str, str]:
    path = _regular_file(stage / relative, executable=resource_id == "package:pi-core")
    if not path.is_relative_to(stage) or _sha256(path) != digest:
        raise HostSupervisorError("staged role resource digest is invalid")
    return {"resourceId": resource_id, "path": str(path), "digest": digest}


def _package_extension(stage: Path, item: Mapping[str, Any], resource_id: str) -> dict[str, str]:
    relative = item.get("extensionPath")
    digest = item.get("extensionSha256")
    if not isinstance(relative, str) or not isinstance(digest, str):
        raise HostSupervisorError("staged role package has no exact extension entrypoint")
    return _resource(stage, relative, digest, resource_id)


def _launch_selection(store: Any, conversation_id: str, build_id: str) -> dict[str, Any]:
    build = verify_registered_build(store, build_id)
    conversation = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=? AND desired_state='active'", (conversation_id,)).fetchone()
    if conversation is None:
        raise HostSupervisorError("active conversation was not found")
    stage, inventory = _load_inventory(build)
    profiles = {item.get("role"): item for item in inventory.get("hostLaunchProfiles", []) if isinstance(item, dict)}
    profile = profiles.get(conversation["role"])
    if not isinstance(profile, dict) or profile.get("supported") is not True:
        raise HostSupervisorError("conversation role has no implemented host launch profile")
    node = _regular_file(Path(str(inventory.get("nodeExecutable", ""))), executable=True, require_user=False)
    if executable_sha256(node) != inventory.get("nodeExecutableSha256"):
        raise HostSupervisorError("configured Node executable bytes differ from the registered generation")
    core = _resource(stage, inventory["piExecutable"], inventory["piExecutableSha256"], "package:pi-core")
    extensions = {item["resourceId"]: item for item in inventory.get("extensions", []) if isinstance(item, dict) and isinstance(item.get("resourceId"), str)}
    packages = {item["resourceId"]: item for item in inventory.get("packages", []) if isinstance(item, dict) and isinstance(item.get("resourceId"), str)}
    resources = [core]
    for resource_id in profile.get("resources", []):
        if resource_id == "package:pi-core":
            continue
        item = extensions.get(resource_id)
        package = packages.get(resource_id)
        if not isinstance(item, dict) and not isinstance(package, dict):
            raise HostSupervisorError("role launch profile names an unavailable staged resource")
        resources.append(_resource(stage, item["path"], item["sha256"], resource_id) if isinstance(item, dict) else _package_extension(stage, package, resource_id))
    return {"build": build, "conversation": dict(conversation), "stage": stage, "profile": profile, "node": node, "resources": resources}


def _terminalize_temporary_role(store: Any, *, conversation: Mapping[str, Any], state: str, result: Mapping[str, Any], archive: bool = True) -> None:
    role = conversation["role"]
    if role == "investigator":
        from .investigators import complete_conversation_investigation
        complete_conversation_investigation(store, conversation_id=conversation["conversation_id"], state=state, result=result, archive=archive)
    elif role == "reviewer" and archive:
        with store.transaction():
            store.conn.execute(
                "UPDATE conversations SET desired_state='archived',updated_at=?,resource_version=resource_version+1 WHERE conversation_id=? AND desired_state='active'",
                (utc_now(), conversation["conversation_id"]),
            )


def _test_resource(path: str | None, resource_id: str) -> dict[str, str]:
    if not path:
        raise HostSupervisorError("the scripted acceptance profile requires provider and probe paths")
    value = _regular_file(Path(path))
    return {"resourceId": resource_id, "path": str(value), "digest": _sha256(value)}


def _refresh_codex_credential(stored: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Refresh an OpenAI Codex OAuth credential through the provider endpoint.

    The stored access token can be rejected by the gateway before its local
    expiry (server-side rotation). Refreshing here means the sandboxed child
    always receives a token the gateway currently accepts. Returns None when
    the credential is not an OAuth codex credential or refresh is unavailable,
    in which case the caller falls back to the stored credential.
    """
    if stored.get("type") != "oauth" or not isinstance(stored.get("refresh"), str) or not stored["refresh"]:
        return None
    refresh_endpoint = "https://auth.openai.com/oauth/token"
    client_id = "app_EMoamEEZ73f0CkXaXp7hrann"
    import urllib.parse
    import urllib.request
    import urllib.error
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": stored["refresh"],
        "client_id": client_id,
    }).encode("ascii")
    request = urllib.request.Request(
        refresh_endpoint, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, urllib.error.URLError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str) or not payload["access_token"]:
        return None
    refreshed = dict(stored)
    refreshed["access"] = payload["access_token"]
    if isinstance(payload.get("refresh_token"), str) and payload["refresh_token"]:
        refreshed["refresh"] = payload["refresh_token"]
    if isinstance(payload.get("expires_in"), (int, float)) and payload["expires_in"] > 0:
        refreshed["expires"] = int(time.time() * 1000) + int(payload["expires_in"]) * 1000
    return refreshed


def _provision_provider_auth(runtime: Path, model: str) -> None:
    """Copy exactly the selected provider's stored credential into the child's
    private agent directory.

    Providers that authenticate through env keys are forwarded by
    _PROVIDER_ENVIRONMENT. Subscription/OAuth providers such as openai-codex
    store their credential in the host auth.json instead; the child's private
    PI_CODING_AGENT_DIR must receive the same provider entry so the sandboxed
    host Pi process can authenticate without inheriting any host environment.
    Only the selected provider's entry is copied; other credentials never
    leave the host file.
    """
    provider = model.split("/", 1)[0]
    if provider in _PROVIDER_ENVIRONMENT:
        return
    agent_dir = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent")
    host_auth = agent_dir / "auth.json"
    if not host_auth.is_file() or host_auth.is_symlink():
        return
    try:
        credentials = json.loads(host_auth.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    if not isinstance(credentials, dict):
        return
    stored = credentials.get(provider)
    if stored is None or not isinstance(stored, dict):
        return
    if provider == "openai-codex":
        refreshed = _refresh_codex_credential(stored)
        if refreshed is not None:
            stored = refreshed
    elif stored.get("type") == "key":
        # Older auth files used "key"; the installed runtime requires
        # "api_key" for API-key credentials.
        stored = {**stored, "type": "api_key"}
    destination = runtime / "agent" / "auth.json"
    try:
        destination.write_text(
            json.dumps({provider: stored}, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        return
    try:
        destination.chmod(0o600)
    except OSError:
        pass


def _environment(state_root: Path, run_id: str, fd: int, model: str, *, acceptance: bool, manifest_path: Path, interactive: bool = False) -> dict[str, str]:
    runtime = state_root / "runtime" / run_id
    values = {
        "HOME": runtime / "home", "XDG_CONFIG_HOME": runtime / "config",
        "XDG_STATE_HOME": runtime / "state", "XDG_CACHE_HOME": runtime / "cache",
        "XDG_RUNTIME_DIR": runtime / "run", "TMPDIR": runtime / "tmp",
        "PI_CODING_AGENT_DIR": runtime / "agent",
    }
    for path in values.values():
        _private_directory(path)
    if not acceptance:
        _provision_provider_auth(runtime, model)
    env = {key: str(value) for key, value in values.items()}
    term = os.environ.get("TERM", "dumb") if interactive else "dumb"
    env.update({"PATH": os.defpath, "LANG": "C", "LC_ALL": "C", "TERM": term, "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0", CHANNEL_ENVIRONMENT_KEY: str(fd), "PI_RUNTIME_MANIFEST": str(manifest_path)})
    if not acceptance:
        provider = model.split("/", 1)[0]
        for key in _PROVIDER_ENVIRONMENT.get(provider, ()):
            value = os.environ.get(key)
            if value:
                env[key] = value
    return env


def _actual_environment(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    result: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        key, separator, value = item.partition(b"=")
        if not separator:
            raise HostSupervisorError("child environment observation is malformed")
        result[key.decode("utf-8")] = value.decode("utf-8")
    return result


def _actual_executable_digest(pid: int) -> str:
    digest = hashlib.sha256()
    with Path(f"/proc/{pid}/exe").open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _authenticated_reader(store: Any, *, run_id: str, manifest_digest: str) -> tuple[ScopedProjectReader, str]:
    run = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None or run["desired_state"] != "running" or run["observed_state"] != "running" or int(run["resource_version"]) != 1:
        raise ControllerChannelError("authenticated run is stale or terminal")
    conversation = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (run["conversation_id"],)).fetchone()
    project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (run["project_id"],)).fetchone()
    working = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (run["working_copy_id"],)).fetchone()
    if conversation is None or project is None or working is None:
        raise ControllerChannelError("authenticated run binding is incomplete")
    if conversation["desired_state"] != "active" or conversation["observed_state"] != "ready" or int(conversation["resource_version"]) != 1:
        raise ControllerChannelError("authenticated conversation is stale or terminal")
    if project["desired_state"] != "active" or project["observed_state"] != "ready":
        raise ControllerChannelError("authenticated project is stale or terminal")
    if working["desired_state"] != "present" or working["observed_state"] not in {"ready", "dirty"}:
        raise ControllerChannelError("authenticated working-copy scope is stale or unavailable")
    if run["project_id"] != conversation["project_id"] or run["project_id"] != working["project_id"]:
        raise ControllerChannelError("authenticated run crosses project scope")
    if run["working_copy_id"] != working["working_copy_id"]:
        raise ControllerChannelError("authenticated run working-copy scope changed")
    if conversation["role"] == "secretary":
        if conversation["working_copy_id"] is not None or working["kind"] != "primary":
            raise ControllerChannelError("secretary scope is not project-primary")
    elif conversation["working_copy_id"] != working["working_copy_id"]:
        raise ControllerChannelError("temporary role scope differs from its assignment")
    if int(run["expected_working_copy_version"]) != int(working["resource_version"]):
        raise ControllerChannelError("authenticated working-copy resource version is stale")
    if not run["manifest_path"]:
        raise ControllerChannelError("authenticated run manifest is unavailable")
    manifest = require_manifest_active(read_manifest(run["manifest_path"]).manifest)
    if manifest["manifestDigest"] != manifest_digest:
        raise ControllerChannelError("authenticated channel manifest binding is stale")
    expected = {
        "runId": run_id,
        "conversationId": conversation["conversation_id"],
        "role": conversation["role"],
        "projectId": project["project_id"],
        "projectVersion": int(project["resource_version"]),
        "workingCopyId": working["working_copy_id"],
        "workingCopyVersion": int(working["resource_version"]),
        "rootPath": str(Path(working["path"]).absolute()),
        "headOid": run["expected_head_oid"],
        "treeOid": run["expected_tree_oid"],
        "buildId": run["build_id"],
    }
    actual = {
        "runId": manifest["runId"],
        "conversationId": manifest["conversation"]["conversationId"],
        "role": manifest["conversation"]["role"],
        "projectId": manifest["project"]["projectId"],
        "projectVersion": manifest["project"]["resourceVersion"],
        "workingCopyId": manifest["scope"]["workingCopyId"],
        "workingCopyVersion": manifest["scope"]["workingCopyResourceVersion"],
        "rootPath": manifest["scope"]["rootPath"],
        "headOid": manifest["scope"]["headOid"],
        "treeOid": manifest["scope"]["treeOid"],
        "buildId": manifest["installedBuild"]["buildId"],
    }
    if actual != expected or run["expected_head_oid"] != working["expected_head_oid"] or run["expected_tree_oid"] != working["expected_tree_oid"]:
        raise ControllerChannelError("authenticated run manifest or revision binding is stale")
    reader = ScopedProjectReader(
        store,
        project_id=run["project_id"],
        working_copy_id=run["working_copy_id"],
        expected_root=manifest["scope"]["rootPath"],
        expected_head_oid=manifest["scope"]["headOid"],
        expected_tree_oid=manifest["scope"]["treeOid"],
    )
    try:
        reader.assert_revision(clean=conversation["role"] == "reviewer")
    except BaseException:
        reader.close()
        raise
    return reader, str(conversation["role"])


def _authenticated_writer(store: Any, *, run_id: str, manifest_digest: str) -> tuple[dict[str, Any], dict[str, Any]]:
    run = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None or run["desired_state"] != "running" or run["observed_state"] != "running" or run["authority"] != "writer-container" or not run["container_id"]:
        raise ControllerChannelError("authenticated writer run is stale, terminal, or has no container")
    conversation = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (run["conversation_id"],)).fetchone()
    project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (run["project_id"],)).fetchone()
    working = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (run["working_copy_id"],)).fetchone()
    if conversation is None or project is None or working is None:
        raise ControllerChannelError("authenticated writer binding is incomplete")
    if conversation["role"] not in {"personal", "workstream", "integration"} or conversation["authority_profile"] != "writer-container" or conversation["desired_state"] != "active" or conversation["observed_state"] != "ready":
        raise ControllerChannelError("authenticated writer conversation is stale")
    if project["desired_state"] != "active" or project["observed_state"] != "ready" or working["desired_state"] != "present" or working["observed_state"] not in {"ready", "dirty"}:
        raise ControllerChannelError("authenticated writer project or working copy is unavailable")
    if run["project_id"] != conversation["project_id"] or run["project_id"] != working["project_id"] or run["working_copy_id"] != conversation["working_copy_id"]:
        raise ControllerChannelError("authenticated writer crosses its controller assignment")
    if working["active_writer_run_id"] != run_id or int(working["writer_epoch"]) != int(run["writer_epoch"]):
        raise ControllerChannelError("authenticated writer epoch is stale")
    if not run["manifest_path"]:
        raise ControllerChannelError("authenticated writer manifest is unavailable")
    manifest = require_manifest_active(read_manifest(run["manifest_path"]).manifest)
    if manifest["manifestDigest"] != manifest_digest or manifest["runId"] != run_id:
        raise ControllerChannelError("authenticated writer manifest binding is stale")
    expected = {
        "conversationId": conversation["conversation_id"], "projectId": project["project_id"],
        "projectVersion": int(project["resource_version"]), "workingCopyId": working["working_copy_id"],
        "workingCopyVersion": int(working["resource_version"]), "writerEpoch": int(working["writer_epoch"]),
        "rootPath": str(Path(working["path"]).resolve(strict=True)), "containerId": run["container_id"],
    }
    actual = {
        "conversationId": manifest["conversation"]["conversationId"], "projectId": manifest["project"]["projectId"],
        "projectVersion": manifest["project"]["resourceVersion"], "workingCopyId": manifest["workingCopy"]["workingCopyId"],
        "workingCopyVersion": manifest["workingCopy"]["resourceVersion"], "writerEpoch": manifest["workingCopy"]["writerEpoch"],
        "rootPath": manifest["workingCopy"]["hostPath"], "containerId": run["container_id"],
    }
    if actual != expected or run["runtime_spec_hash"] != manifest["toolRuntime"]["specHash"]:
        raise ControllerChannelError("authenticated writer resources changed after manifest creation")
    try:
        observation = inspect_container(str(run["container_id"]))
        attest_container(manifest["toolRuntime"], observation, name="pi-tool-" + run_id.removeprefix("run_"), running=True)
    except DockerRuntimeError as error:
        raise ControllerChannelError(str(error)) from error
    return dict(manifest), dict(run)


def _rpc(store: Any, run_id: str, manifest_digest: str, request: Mapping[str, Any], *, cancellation: threading.Event | None = None, child_context: Mapping[str, Any] | None = None) -> Any:
    if set(request) != {"protocolVersion", "type", "requestId", "operation", "payload"} or request.get("protocolVersion") != PROTOCOL_VERSION or request.get("type") != "request":
        raise ControllerChannelError("runtime request fields do not match the protocol")
    if not isinstance(request.get("operation"), str) or not isinstance(request.get("payload"), dict):
        raise ControllerChannelError("runtime request operation is not allowed")
    payload = dict(request["payload"])
    identity_fields = {"projectId", "conversationId", "runId", "workstreamId", "workingCopyId", "writerGeneration", "writerEpoch", "workingCopyPath", "investigatorRunId"}
    if identity_fields.intersection(payload):
        raise ControllerChannelError("runtime request cannot override authenticated scope")
    channel_operation = str(request["operation"])
    if channel_operation == "writer-tool":
        if set(payload) != {"tool", "arguments"} or not isinstance(payload.get("arguments"), dict):
            raise ControllerChannelError("writer tool request fields are invalid")
        manifest, run = _authenticated_writer(store, run_id=run_id, manifest_digest=manifest_digest)
        tool = payload["tool"]
        try:
            if tool in {"read", "write", "edit"}:
                return execute_file_tool(str(run["container_id"]), str(tool), payload["arguments"], cancellation=cancellation)
            if tool == "bash":
                return execute_shell_tool(str(run["container_id"]), payload["arguments"], cancellation=cancellation)
        except DockerRuntimeError as error:
            raise ControllerChannelError(str(error)) from error
        raise ControllerChannelError("writer tool is not allowed")
    if channel_operation == "scoped-read":
        operation = payload.pop("operation", None)
        reader, _role = _authenticated_reader(store, run_id=run_id, manifest_digest=manifest_digest)
        try:
            if operation == "read" and set(payload) <= {"path", "startLine", "maxLines"}:
                return reader.read(payload.get("path", ""), start_line=payload.get("startLine", 1), max_lines=payload.get("maxLines", 2000))
            if operation == "list" and set(payload) <= {"path", "pattern"}:
                return reader.list(payload.get("path", "."), pattern=payload.get("pattern", "*"))
            if operation == "grep" and set(payload) <= {"path", "pattern"}:
                return reader.grep(payload.get("pattern", ""), payload.get("path", "."))
            if operation == "git" and set(payload) <= {"query", "path", "mode", "limit"}:
                return reader.git(payload.get("query", ""), path=payload.get("path"), mode=payload.get("mode", "revision"), limit=payload.get("limit", 20))
            raise ControllerChannelError("scoped read operation or fields are not allowed")
        finally:
            reader.close()

    run = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    conversation = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (run["conversation_id"],)).fetchone() if run is not None else None
    if run is None or conversation is None or channel_operation not in _ROLE_OPERATIONS.get(str(conversation["role"]), set()):
        raise ControllerChannelError("runtime operation is not granted to the authenticated role")
    reader, _ = _authenticated_reader(store, run_id=run_id, manifest_digest=manifest_digest)
    reader.close()
    writer_epoch = None
    if conversation["role"] in {"personal", "workstream", "integration"}:
        if channel_operation == "writer-tool":
            _manifest, current = _authenticated_writer(store, run_id=run_id, manifest_digest=manifest_digest)
            writer_epoch = int(current["writer_epoch"])
        else:
            current = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (run["working_copy_id"],)).fetchone()
            if current is None or current["active_writer_run_id"] != run_id:
                raise ControllerChannelError("writer run no longer holds the working-copy claim")
            writer_epoch = int(current["writer_epoch"])
    project_id, conversation_id = str(run["project_id"]), str(run["conversation_id"])

    if channel_operation == "project.work-index":
        if set(payload):
            raise ControllerChannelError("work index request fields are invalid")
        from .projects import work_index
        return work_index(store, project_id)

    if channel_operation == "workstream.propose":
        from .pi_workstreams import propose_workstream
        if set(payload) - {"title", "purpose", "targetRef", "knownOverlap", "idempotencyKey"}:
            raise ControllerChannelError("workstream proposal fields are invalid")
        if set(payload) & {"title", "purpose", "idempotencyKey"} != {"title", "purpose", "idempotencyKey"}:
            raise ControllerChannelError("workstream proposal is incomplete")
        return propose_workstream(
            store, project_id=project_id, secretary_conversation_id=conversation_id, secretary_run_id=run_id,
            title=payload["title"], purpose=payload["purpose"],
            target_ref=payload.get("targetRef"), known_overlap=payload.get("knownOverlap"),
            idempotency_key=payload["idempotencyKey"],
        )

    if channel_operation == "workstream.approve":
        from .pi_workstreams import approve_workstream_proposal
        if set(payload) != {"messageId"}:
            raise ControllerChannelError("workstream approval fields are invalid")
        return approve_workstream_proposal(store, message_id=payload["messageId"], actor_id=run_id)

    if channel_operation == "workstream.apply":
        from .pi_workstreams import apply_workstream_proposal
        if set(payload) != {"messageId", "authorizationId"}:
            raise ControllerChannelError("workstream apply fields are invalid")
        return apply_workstream_proposal(store, message_id=payload["messageId"], authorization_id=payload["authorizationId"], actor_id=run_id)

    if channel_operation == "subagent.spawn":
        if set(payload) != {"role", "task", "idempotencyKey"} or payload.get("role") not in READ_ONLY_ROLES or not isinstance(payload.get("task"), str) or not isinstance(payload.get("idempotencyKey"), str):
            raise ControllerChannelError("subagent request fields are invalid")
        if child_context is None:
            raise ControllerChannelError("subagent controller launch context is unavailable")
        from .subagents import run_controller_child
        return run_controller_child(
            store, parent_run_id=run_id, semantic_role=payload["role"], task=payload["task"],
            idempotency_key=f"subagent:{run_id}:{payload['idempotencyKey']}",
            build_id=str(child_context["buildId"]), model=str(child_context["model"]),
            acceptance_test_profile=child_context.get("acceptanceTestProfile"),
            test_provider=child_context.get("testProvider"), test_probe=child_context.get("testProbe"),
            cancellation=cancellation,
        )

    if channel_operation == "subagent.start":
        if set(payload) != {"role", "task", "idempotencyKey"} or payload.get("role") not in READ_ONLY_ROLES or not isinstance(payload.get("task"), str) or not isinstance(payload.get("idempotencyKey"), str):
            raise ControllerChannelError("async subagent request fields are invalid")
        if child_context is None:
            raise ControllerChannelError("subagent controller launch context is unavailable")
        from .subagents import start_child_assignment
        return start_child_assignment(
            store, parent_run_id=run_id, semantic_role=payload["role"], task=payload["task"],
            idempotency_key=f"subagent:{run_id}:{payload['idempotencyKey']}",
            build_id=str(child_context["buildId"]), model=str(child_context["model"]),
            acceptance_test_profile=child_context.get("acceptanceTestProfile"),
            test_provider=child_context.get("testProvider"), test_probe=child_context.get("testProbe"),
        )

    if channel_operation == "subagent.status":
        if set(payload) != {"childRequestId"} or not isinstance(payload.get("childRequestId"), str):
            raise ControllerChannelError("subagent status request fields are invalid")
        from .subagents import child_status
        return child_status(store, parent_run_id=run_id, child_request_id=payload["childRequestId"])

    if channel_operation == "subagent.wait":
        if set(payload) != {"childRequestId", "timeoutSeconds"} or not isinstance(payload.get("childRequestId"), str):
            raise ControllerChannelError("subagent wait request fields are invalid")
        timeout = payload.get("timeoutSeconds", 300)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ControllerChannelError("subagent wait timeout is invalid")
        from .subagents import wait_child_terminal
        return wait_child_terminal(store, parent_run_id=run_id, child_request_id=payload["childRequestId"], timeout=float(timeout))

    if channel_operation == "subagent.list":
        if set(payload):
            raise ControllerChannelError("subagent list request fields are invalid")
        from .subagents import list_child_requests
        return list_child_requests(store, parent_run_id=run_id)

    if channel_operation == "subagent.interrupt":
        if set(payload) != {"childRequestId"} or not isinstance(payload.get("childRequestId"), str):
            raise ControllerChannelError("subagent interrupt request fields are invalid")
        from .subagents import interrupt_child
        return interrupt_child(store, parent_run_id=run_id, child_request_id=payload["childRequestId"])

    if channel_operation == "subagent.stop":
        if set(payload) != {"childRequestId"} or not isinstance(payload.get("childRequestId"), str):
            raise ControllerChannelError("subagent stop request fields are invalid")
        from .subagents import stop_child
        return stop_child(store, parent_run_id=run_id, child_request_id=payload["childRequestId"])

    if channel_operation == "subagent.resume":
        if set(payload) != {"childRequestId"} or not isinstance(payload.get("childRequestId"), str):
            raise ControllerChannelError("subagent resume request fields are invalid")
        if child_context is None:
            raise ControllerChannelError("subagent controller launch context is unavailable")
        from .subagents import resume_child
        return resume_child(
            store, parent_run_id=run_id, child_request_id=payload["childRequestId"],
            build_id=str(child_context["buildId"]), model=str(child_context["model"]),
            acceptance_test_profile=child_context.get("acceptanceTestProfile"),
            test_provider=child_context.get("testProvider"), test_probe=child_context.get("testProbe"),
        )

    if channel_operation == "subagent.steer":
        if set(payload) != {"childRequestId", "message"} or not isinstance(payload.get("childRequestId"), str) or not isinstance(payload.get("message"), str) or not payload["message"]:
            raise ControllerChannelError("subagent steer request fields are invalid")
        child = store.conn.execute("SELECT * FROM child_requests WHERE child_request_id=? AND parent_run_id=?", (payload["childRequestId"], run_id)).fetchone()
        if child is None or child["child_run_id"] is None:
            raise ControllerChannelError("child request is not bound to a run")
        from .messages import post_message
        return post_message(
            store, project_id=project_id, conversation_id=conversation_id, run_id=run_id,
            kind="progress", payload={"steer": payload["message"], "childRequestId": payload["childRequestId"]},
            idempotency_key=f"steer:{run_id}:{payload['childRequestId']}",
        )

    if channel_operation == "worker.start":
        if set(payload) != {"task", "idempotencyKey", "title"} or not isinstance(payload.get("task"), str) or not isinstance(payload.get("idempotencyKey"), str) or not isinstance(payload.get("title"), str):
            raise ControllerChannelError("worker request fields are invalid")
        if child_context is None:
            raise ControllerChannelError("worker controller launch context is unavailable")
        tool_image = child_context.get("toolImage")
        if not isinstance(tool_image, str) or not tool_image:
            raise ControllerChannelError("worker launch requires the controller-selected tool image")
        from .subagents import start_worker_assignment
        return start_worker_assignment(
            store, parent_run_id=run_id, task=payload["task"], title=payload["title"],
            idempotency_key=f"worker:{run_id}:{payload['idempotencyKey']}",
            build_id=str(child_context["buildId"]), model=str(child_context["model"]),
            tool_image=tool_image,
            acceptance_test_profile=child_context.get("acceptanceTestProfile"),
            test_provider=child_context.get("testProvider"), test_probe=child_context.get("testProbe"),
        )

    if channel_operation == "change.submit":
        if set(payload) != {"title", "summary", "targetRef", "captureMode", "selectedPaths", "excludedPaths", "idempotencyKey"} or not isinstance(payload.get("title"), str) or not isinstance(payload.get("summary"), str) or not isinstance(payload.get("targetRef"), str) or not isinstance(payload.get("idempotencyKey"), str):
            raise ControllerChannelError("change submission fields are invalid")
        capture_mode = payload.get("captureMode")
        if capture_mode not in {None, "clean", "dirty"}:
            raise ControllerChannelError("change capture mode is invalid")
        selected = payload.get("selectedPaths")
        excluded = payload.get("excludedPaths")
        if not isinstance(selected, list) or not isinstance(excluded, list) or any(not isinstance(item, str) for item in selected + excluded):
            raise ControllerChannelError("change path policy is invalid")
        from .changes import submit_change
        submission = submit_change(
            store, project_id=project_id, working_copy_id=str(run["working_copy_id"]),
            target_ref=payload["targetRef"], title=payload["title"], summary=payload["summary"],
            capture_mode=capture_mode or "dirty", selected_paths=selected or None, excluded_paths=excluded or None,
            idempotency_key=f"change:{run_id}:{payload['idempotencyKey']}",
            created_by_conversation_id=conversation_id, actor_id=run_id,
        )
        return submission.as_dict()

    if channel_operation == "change.list":
        if set(payload) not in (set(), {"states"}):
            raise ControllerChannelError("change list fields are invalid")
        from .changes import list_changes
        return list_changes(store, project_id=project_id)

    if channel_operation == "review.request":
        if set(payload) != {"changeId", "revision"} or not isinstance(payload.get("changeId"), str) or not isinstance(payload.get("revision"), int) or payload["revision"] < 1:
            raise ControllerChannelError("review request fields are invalid")
        change = store.conn.execute("SELECT project_id FROM changes WHERE change_id=?", (payload["changeId"],)).fetchone()
        if change is None or change["project_id"] != project_id:
            raise ControllerChannelError("review request crosses the authenticated project")
        if child_context is None:
            raise ControllerChannelError("review controller launch context is unavailable")
        from .pi_review import create_review_assignment
        from .subagents import launch_reviewer_detached
        assignment = create_review_assignment(store, change_id=payload["changeId"], revision=payload["revision"])
        prompt = (
            f"You are a read-only reviewer inspecting change {payload['changeId']} revision {payload['revision']} "
            f"at its exact detached snapshot. Return your verdict (accept, changes_requested, or comment) with findings."
        )
        pid = launch_reviewer_detached(
            store, conversation_id=assignment["conversationId"],
            build_id=str(child_context["buildId"]), model=str(child_context["model"]), prompt=prompt,
            acceptance_test_profile=child_context.get("acceptanceTestProfile"),
            test_provider=child_context.get("testProvider"), test_probe=child_context.get("testProbe"),
        )
        return {**assignment, "launched": True, "launcherPid": pid}

    if channel_operation == "integration.analyze":
        if set(payload) != {"changeId", "revision", "targetRef"} or not isinstance(payload.get("changeId"), str) or not isinstance(payload.get("revision"), int) or not isinstance(payload.get("targetRef"), str):
            raise ControllerChannelError("integration analysis fields are invalid")
        change = store.conn.execute("SELECT project_id FROM changes WHERE change_id=?", (payload["changeId"],)).fetchone()
        if change is None or change["project_id"] != project_id:
            raise ControllerChannelError("integration analysis crosses the authenticated project")
        target = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary' AND desired_state='present'", (project_id,)).fetchone()
        if target is None:
            raise ControllerChannelError("integration target working copy is unavailable")
        from .integration import analyze_integration
        return analyze_integration(
            store, project_id=project_id, change_id=payload["changeId"], revision=payload["revision"],
            target_working_copy_id=target["working_copy_id"], target_ref=payload["targetRef"],
        ).as_dict()

    if channel_operation.startswith("message."):
        from .messages import acknowledge_message, list_messages, post_message, reply_message
        if channel_operation == "message.post" and set(payload) <= {"kind", "payload", "idempotencyKey", "requestId", "replyToMessageId"}:
            workstream_row = store.conn.execute("SELECT workstream_id FROM workstreams WHERE conversation_id=?", (conversation_id,)).fetchone()
            workstream_id = workstream_row["workstream_id"] if workstream_row is not None else None
            return post_message(store, project_id=project_id, conversation_id=conversation_id, run_id=run_id, writer_generation=writer_epoch, kind=payload.get("kind"), payload=payload.get("payload"), idempotency_key=payload.get("idempotencyKey"), request_id=payload.get("requestId"), reply_to_message_id=payload.get("replyToMessageId"), workstream_id=workstream_id)
        if channel_operation == "message.list" and set(payload) <= {"states", "limit"}:
            states = payload.get("states")
            return list_messages(store, project_id=project_id, states=set(states) if isinstance(states, list) else None, limit=payload.get("limit", 256))
        if channel_operation == "message.acknowledge" and set(payload) <= {"messageId", "resolve"}:
            return acknowledge_message(store, project_id=project_id, message_id=payload.get("messageId"), resolve=payload.get("resolve", False), conversation_id=conversation_id, run_id=run_id, writer_generation=writer_epoch)
        if channel_operation == "message.reply" and set(payload) == {"targetMessageId", "payload", "idempotencyKey"}:
            workstream_row = store.conn.execute("SELECT workstream_id FROM workstreams WHERE conversation_id=?", (conversation_id,)).fetchone()
            workstream_id = workstream_row["workstream_id"] if workstream_row is not None else None
            return reply_message(store, project_id=project_id, target_message_id=payload["targetMessageId"], conversation_id=conversation_id, run_id=run_id, payload=payload["payload"], idempotency_key=payload["idempotencyKey"], workstream_id=workstream_id, writer_generation=writer_epoch)
        raise ControllerChannelError("message operation fields are invalid")
    if channel_operation.startswith("command."):
        from .command_requests import command_status, request_command
        if channel_operation == "command.request" and set(payload) == {"operation", "purpose"}:
            return request_command(store, project_id=project_id, conversation_id=conversation_id, run_id=run_id, writer_generation=writer_epoch, operation=payload["operation"], purpose=payload["purpose"])
        if channel_operation == "command.status" and set(payload) == {"commandRequestId"}:
            return command_status(store, project_id=project_id, command_request_id=payload["commandRequestId"])
        raise ControllerChannelError("command operation fields are invalid")
    if channel_operation.startswith("dependency.") or channel_operation.startswith("package-review."):
        from .dependencies import inventory_dependencies, package_review_gate, record_package_security_review, set_dependency_disposition
        if channel_operation == "dependency.inventory" and set(payload) <= {"changeId", "revision", "workerReason"} and {"changeId", "revision"} <= set(payload):
            return inventory_dependencies(store, project_id=project_id, change_id=payload["changeId"], revision=payload["revision"], worker_reason=payload.get("workerReason"))
        if channel_operation == "dependency.disposition" and set(payload) == {"dependencyChangeId", "disposition"}:
            dependency = store.conn.execute("SELECT project_id FROM dependency_changes WHERE dependency_change_id=?", (payload["dependencyChangeId"],)).fetchone()
            if dependency is None or dependency["project_id"] != project_id:
                raise ControllerChannelError("dependency disposition crosses the authenticated project")
            return set_dependency_disposition(store, dependency_change_id=payload["dependencyChangeId"], disposition=payload["disposition"])
        if channel_operation == "package-review.record" and set(payload) == {"dependencyChangeId", "evidence", "riskLevel", "recommendation"}:
            return record_package_security_review(store, dependency_change_id=payload["dependencyChangeId"], evidence=payload["evidence"], risk_level=payload["riskLevel"], recommendation=payload["recommendation"], investigator_run_id=run_id)
        if channel_operation == "package-review.gate" and set(payload) == {"changeId", "revision"}:
            change = store.conn.execute("SELECT project_id FROM changes WHERE change_id=?", (payload["changeId"],)).fetchone()
            if change is None or change["project_id"] != project_id:
                raise ControllerChannelError("package gate crosses the authenticated project")
            return package_review_gate(store, change_id=payload["changeId"], revision=payload["revision"])
        raise ControllerChannelError("dependency review operation fields are invalid")
    if channel_operation.startswith("package."):
        from .package_environment import package_status, request_package_operation
        if channel_operation == "package.request" and set(payload) <= {"changeId", "revision", "ecosystem", "action", "packageName", "exactVersion"} and {"changeId", "revision", "ecosystem", "action"} <= set(payload):
            return request_package_operation(store, project_id=project_id, conversation_id=conversation_id, run_id=run_id, writer_generation=writer_epoch, change_id=payload["changeId"], revision=payload["revision"], ecosystem=payload["ecosystem"], action=payload["action"], package_name=payload.get("packageName"), exact_version=payload.get("exactVersion"))
        if channel_operation == "package.status" and set(payload) == {"packageRequestId"}:
            return package_status(store, project_id=project_id, package_request_id=payload["packageRequestId"])
        raise ControllerChannelError("package operation fields are invalid")
    raise ControllerChannelError("runtime request operation is not allowed")


def launch_host_pi(
    store: Any,
    *,
    conversation_id: str,
    build_id: str,
    prompt: str,
    model: str,
    acceptance_test_profile: str | None = None,
    test_provider: str | None = None,
    test_probe: str | None = None,
    handshake_timeout: float = 15.0,
    before_spawn: Callable[[], None] | None = None,
    expected_role: str | None = None,
    tool_image: str | None = None,
    parent_run_id: str | None = None,
    child_source: Mapping[str, Any] | None = None,
    child_request_id: str | None = None,
    child_test_provider: str | None = None,
    cancellation: threading.Event | None = None,
    interactive: bool = False,
) -> int:
    if interactive:
        if acceptance_test_profile is not None:
            raise HostSupervisorError("interactive launches cannot use the acceptance-test profile")
    elif not isinstance(prompt, str) or not prompt or "\x00" in prompt or len(prompt.encode("utf-8")) > 16 * 1024:
        raise HostSupervisorError("prompt is empty or exceeds its bound")
    acceptance = acceptance_test_profile is not None
    if acceptance_test_profile not in {None, ACCEPTANCE_PROFILE}:
        raise HostSupervisorError("unknown acceptance-test profile")
    if acceptance:
        if model != "scripted/scripted-1":
            raise HostSupervisorError("scripted acceptance profile requires its exact test model")
    elif _MODEL.fullmatch(model) is None:
        raise HostSupervisorError("model must be one controlled provider/model identifier")
    selected = _launch_selection(store, conversation_id, build_id)
    if expected_role is not None and selected["conversation"]["role"] != expected_role:
        raise HostSupervisorError("controller conversation role differs from the exact role launcher")
    profile = selected["profile"]
    writer = selected["conversation"]["authority_profile"] == "writer-container"
    if writer:
        if tool_image is None:
            if acceptance:
                tool_image = PINNED_ACCEPTANCE_IMAGE
            else:
                raise HostSupervisorError("writer launch requires one explicit registered digest-pinned tool image")
    elif tool_image is not None:
        raise HostSupervisorError("host-read-only roles cannot select a tool image")
    resources = list(selected["resources"])
    acceptance_resources = []
    if acceptance:
        acceptance_resources = [_test_resource(test_provider, "test:scripted-provider"), _test_resource(test_probe, "test:loaded-resource-probe")]
    elif test_provider is not None or test_probe is not None:
        raise HostSupervisorError("test resources require the explicit acceptance-test profile")
    resource_paths = {item["resourceId"]: item["path"] for item in resources}
    session_path = Path(selected["conversation"]["session_file"])
    if selected["conversation"]["role"] == "secretary":
        working = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary' AND desired_state='present'", (selected["conversation"]["project_id"],)).fetchone()
    else:
        working = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=? AND project_id=? AND desired_state='present'", (selected["conversation"]["working_copy_id"], selected["conversation"]["project_id"])).fetchone()
    if working is None:
        raise HostSupervisorError("controller-derived project scope is unavailable")
    cwd = Path(working["path"]).resolve(strict=True)
    ensure_session(session_path, state_root=Path(store.state_root), session_id=selected["conversation"]["pi_session_id"], cwd=cwd)
    tools = sorted(profile["tools"])
    if interactive:
        argv = [
            str(selected["node"]), resource_paths["package:pi-core"], "--offline", "--no-approve", "--no-extensions",
        ]
    else:
        argv = [
            str(selected["node"]), resource_paths["package:pi-core"], "--mode", "json", "--offline", "--no-approve", "--no-extensions",
        ]
    for item in resources:
        if item["resourceId"].startswith("extension:") or item["resourceId"] in {"package:pi-sandbox-control", "package:pi-subagents"}:
            argv.extend(["-e", item["path"]])
    for item in acceptance_resources:
        argv.extend(["-e", item["path"]])
    if interactive:
        argv.extend([
            "--no-builtin-tools", "--tools", ",".join(tools), "--no-skills", "--no-prompt-templates", "--no-context-files", "--no-themes",
            "--system-prompt", "You are a controller-scoped Pi role.",
            "--model", model, "--session", str(session_path),
        ])
    else:
        argv.extend([
            "--no-builtin-tools", "--tools", ",".join(tools), "--no-skills", "--no-prompt-templates", "--no-context-files", "--no-themes",
            "--system-prompt", "Installed-process deterministic test." if acceptance else "You are a controller-scoped Pi role.",
            "--model", model, "--thinking", "off", "--session", str(session_path), prompt,
        ])
    provider_keys = tuple(key for key in (() if acceptance else _PROVIDER_ENVIRONMENT.get(model.split("/", 1)[0], ())) if os.environ.get(key))
    environment_keys = sorted({
        "HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "TMPDIR", "PI_CODING_AGENT_DIR",
        "PATH", "LANG", "LC_ALL", "TERM", "PI_OFFLINE", "PI_SKIP_VERSION_CHECK", "PI_TELEMETRY", CHANNEL_ENVIRONMENT_KEY,
        "PI_RUNTIME_MANIFEST",
        *provider_keys,
    })
    host_process = {
        "executable": str(selected["node"]), "executableSha256": executable_sha256(selected["node"]), "argv": argv,
        "toolProfile": selected["conversation"]["role"], "environmentKeys": environment_keys,
    }
    requested_run_id = new_id("run") if writer else None
    tool_runtime = None
    if writer:
        project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (selected["conversation"]["project_id"],)).fetchone()
        if project is None:
            raise HostSupervisorError("writer project binding is unavailable")
        tool_runtime = prepare_tool_runtime(
            state_root=store.state_root, run_id=requested_run_id, image_reference=str(tool_image), project=project,
            working_copy=working, build_id=build_id, writer_epoch=int(working["writer_epoch"]) + 1,
        )
    previous_handlers, shutdown_state = _install_cleanup_signal_handlers()
    try:
        prepared = prepare_run(store, conversation_id=conversation_id, build_id=build_id, host_process=host_process, tool_runtime=tool_runtime, owner_pid=os.getpid(), run_id=requested_run_id, parent_run_id=parent_run_id, child_source=child_source)
    except BaseException:
        _restore_cleanup_signal_handlers(previous_handlers)
        raise
    process: subprocess.Popen[bytes] | None = None
    parent_channel: socket.socket | None = None
    child_channel: socket.socket | None = None
    terminalized = False
    temporary_terminalized = False
    temporary_bound = selected["conversation"]["role"] in {"investigator", "reviewer"}
    container_prepared = False
    requests_enabled = False
    try:
        if selected["conversation"]["role"] == "investigator":
            from .investigators import bind_investigation_run
            bind_investigation_run(store, conversation_id=conversation_id, run_id=prepared.run["run_id"])
        if child_request_id is not None:
            from .subagents import bind_child_run
            bind_child_run(store, child_request_id=child_request_id, child_run_id=prepared.run["run_id"])
        if writer:
            create_start_container(store, run_id=prepared.run["run_id"], manifest=prepared.manifest)
            container_prepared = True
        parent_channel, child_channel = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        environment = _environment(Path(store.state_root), prepared.run["run_id"], child_channel.fileno(), model, acceptance=acceptance, manifest_path=prepared.manifest_path, interactive=interactive)
        if sorted(environment) != [key for key in environment_keys if key in environment]:
            raise HostSupervisorError("constructed child environment differs from the manifest allowlist")
        if before_spawn is not None:
            before_spawn()
        if executable_sha256(selected["node"]) != prepared.manifest["hostProcess"]["executableSha256"]:
            raise HostSupervisorError("Node executable changed between prepare and spawn")
        process = subprocess.Popen(argv, cwd=str(cwd), env=environment, stdin=None, stdout=None, stderr=None, shell=False, close_fds=True, pass_fds=(child_channel.fileno(),))
        child_channel.close()
        child_channel = None
        child_identity = process_start_identity(process.pid)
        actual_digest = _actual_executable_digest(process.pid)
        actual_environment = _actual_environment(process.pid)
        if actual_digest != prepared.manifest["hostProcess"]["executableSha256"]:
            raise HostSupervisorError("spawned child executable bytes differ from the prepared manifest")
        if actual_environment != environment:
            raise HostSupervisorError("spawned child environment differs from the empty allowlisted environment")
        observed_environment = {key: "<redacted-provider-credential>" if key in provider_keys else value for key, value in actual_environment.items()}
        loaded_resources = sorted(resources + acceptance_resources, key=lambda item: item["resourceId"])
        def tool_source(name: str) -> str:
            resource_id = _TOOL_RESOURCE.get(name)
            if resource_id == "scoped-or-writer":
                resource_id = "package:pi-sandbox-control" if writer else "extension:scoped-project-read"
            if resource_id is None or resource_id not in resource_paths:
                raise HostSupervisorError(f"active tool has no exact staged resource: {name}")
            return resource_paths[resource_id]
        allowed_operations = sorted(_ROLE_OPERATIONS[selected["conversation"]["role"]])
        expected = {
            "runId": prepared.run["run_id"], "manifestDigest": prepared.manifest["manifestDigest"], "childPid": process.pid,
            "childStartIdentity": child_identity, "role": selected["conversation"]["role"],
            "sessionId": selected["conversation"]["pi_session_id"], "sessionPath": str(session_path), "activeTools": tools,
            "toolSources": [{"name": name, "path": tool_source(name)} for name in tools], "loadedResources": loaded_resources,
        }
        observation = {
            "schemaVersion": 1, "childPid": process.pid, "childStartIdentity": child_identity,
            "executable": str(selected["node"]), "executableSha256": actual_digest, "argv": argv,
            "environment": observed_environment, "resources": resources,
            "acceptance": {"testOnly": True, "profile": acceptance_test_profile, "resources": acceptance_resources} if acceptance else None,
            "handshake": None,
        }
        with store.transaction():
            store.conn.execute("UPDATE runs SET child_pid=?,child_start_identity=?,host_process_observation_json=?,updated_at=? WHERE run_id=?", (process.pid, child_identity, canonical_json(observation), utc_now(), prepared.run["run_id"]))
        send_frame(parent_channel, {"protocolVersion": PROTOCOL_VERSION, "type": "challenge", **expected, "resources": loaded_resources, "allowedOperations": allowed_operations})
        handshake = receive_frame(parent_channel, timeout=handshake_timeout)
        observation["handshake"] = validate_handshake(handshake, expected)
        if writer:
            current_run = store.conn.execute("SELECT container_id FROM runs WHERE run_id=?", (prepared.run["run_id"],)).fetchone()
            if current_run is None or not current_run["container_id"]:
                raise HostSupervisorError("writer container identity disappeared before joint attestation")
            attest_container(prepared.manifest["toolRuntime"], inspect_container(current_run["container_id"]), name="pi-tool-" + prepared.run["run_id"].removeprefix("run_"), running=True)
        attest_run(store, run_id=prepared.run["run_id"], manifest_digest=prepared.manifest["manifestDigest"], observed=observation)
        send_frame(parent_channel, {"protocolVersion": PROTOCOL_VERSION, "type": "startup-accepted", "runId": prepared.run["run_id"], "manifestDigest": prepared.manifest["manifestDigest"]})
        requests_enabled = True
        channel_reader = ChannelReader(parent_channel)
        pending: "queue.SimpleQueue[dict[str, Any]]" = queue.SimpleQueue()
        while process.poll() is None:
            if cancellation is not None and cancellation.is_set():
                raise HostSupervisorError("controller child run was interrupted")
            try:
                request = pending.get_nowait() if not pending.empty() else channel_reader.receive(timeout=0.1)
            except ControllerChannelError as error:
                if "timed out" in str(error):
                    continue
                if "closed" in str(error):
                    parent_channel.close()
                    parent_channel = None
                    break
                raise
            if not isinstance(request, dict) or not isinstance(request.get("requestId"), str) or not isinstance(request.get("operation"), str):
                raise ControllerChannelError("controller request frame is malformed")
            request_id = request["requestId"]
            if not requests_enabled:
                raise ControllerChannelError("runtime requests are disabled")
            cancellation = threading.Event()
            outcome: dict[str, Any] = {}
            def invoke() -> None:
                from .pi_store import PiStore
                request_store = PiStore(store.state_root)
                try:
                    request_store.open()
                    outcome["result"] = _rpc(
                        request_store, prepared.run["run_id"], prepared.manifest["manifestDigest"], request,
                        cancellation=cancellation,
                        child_context={
                            "buildId": build_id, "model": model, "acceptanceTestProfile": acceptance_test_profile,
                            "testProvider": child_test_provider, "testProbe": test_probe,
                            "toolImage": tool_image if tool_image is not None else PINNED_ACCEPTANCE_IMAGE,
                        },
                    )
                except BaseException as error:
                    outcome["error"] = error
                    if os.environ.get("PI_DEBUG_INVOKE_TRACEBACK"):
                        import traceback
                        with open(os.environ["PI_DEBUG_INVOKE_TRACEBACK"], "a") as debug_handle:
                            debug_handle.write(f"run={run_id} op={channel_operation}\n")
                            traceback.print_exception(error, file=debug_handle)
                finally:
                    request_store.close()
            worker = threading.Thread(target=invoke, name=f"pi-tool-{request_id}", daemon=True)
            worker.start()
            while worker.is_alive():
                try:
                    followup = channel_reader.receive(timeout=0.1)
                except ControllerChannelError as error:
                    if "timed out" in str(error):
                        if process.poll() is not None:
                            cancellation.set()
                        continue
                    cancellation.set()
                    worker.join(timeout=5)
                    raise
                if followup.get("type") == "cancel":
                    if set(followup) != {"protocolVersion", "type", "requestId"} or followup.get("protocolVersion") != PROTOCOL_VERSION or followup.get("requestId") != request_id:
                        cancellation.set()
                        worker.join(timeout=5)
                        raise ControllerChannelError("only exact cancellation is allowed while a tool request is active")
                    cancellation.set()
                    continue
                if followup.get("type") == "request" and isinstance(followup.get("requestId"), str) and isinstance(followup.get("operation"), str):
                    pending.put(dict(followup))
                    continue
                cancellation.set()
                worker.join(timeout=5)
                raise ControllerChannelError("only cancellation or an additional request is allowed while a tool request is active")
            worker.join()
            if "error" in outcome:
                error = outcome["error"]
                reply = {"protocolVersion": PROTOCOL_VERSION, "type": "response", "requestId": request_id, "ok": False, "error": str(error)[:1024]}
            else:
                reply = {"protocolVersion": PROTOCOL_VERSION, "type": "response", "requestId": request_id, "ok": True, "result": outcome.get("result")}
            send_frame(parent_channel, reply)
        return_code = int(process.wait())
        requests_enabled = False
        cleanup = cleanup_run_container(store, run_id=prepared.run["run_id"]) if writer else {"absent": True}
        if not cleanup["absent"]:
            fail_run(store, run_id=prepared.run["run_id"], code="CONTAINER_CLEANUP_UNKNOWN", detail=canonical_json(cleanup), needs_attention=True)
            raise HostSupervisorError("writer container cleanup state is unknown")
        if return_code == 0:
            stop_run(store, run_id=prepared.run["run_id"], reason="process-exited", container_absent=writer)
        else:
            fail_run(store, run_id=prepared.run["run_id"], code="PROCESS_EXIT_NONZERO", detail=f"process-failed:{return_code}", release_writer=writer)
        if selected["conversation"]["role"] in {"investigator", "reviewer"}:
            _terminalize_temporary_role(
                store,
                conversation=selected["conversation"],
                state="result" if return_code == 0 else "failed",
                result={"runId": prepared.run["run_id"], "returnCode": return_code},
            )
            temporary_terminalized = True
        terminalized = True
        return return_code
    except BaseException as error:
        requests_enabled = False
        if process is not None and process.poll() is None:
            _terminate_child(process)
        cleanup = {"absent": True}
        if writer:
            try:
                cleanup = cleanup_run_container(store, run_id=prepared.run["run_id"])
            except BaseException as cleanup_error:
                cleanup = {"absent": False, "errors": [str(cleanup_error)[:1024]]}
        interrupted = isinstance(error, (KeyboardInterrupt, SystemExit))
        fail_run(
            store,
            run_id=prepared.run["run_id"],
            code="HOST_SUPERVISOR_INTERRUPTED" if interrupted else "HOST_ATTESTATION_FAILED",
            detail=str(error),
            needs_attention=writer and not cleanup["absent"],
            release_writer=writer and cleanup["absent"],
        )
        if selected["conversation"]["role"] in {"investigator", "reviewer"} and temporary_bound and not temporary_terminalized:
            state = "interrupted" if interrupted else "failed"
            # An interrupt keeps the temporary conversation active so it can be
            # resumed; ordinary failures archive it.
            archive = not interrupted
            _terminalize_temporary_role(store, conversation=selected["conversation"], state=state, result={"runId": prepared.run["run_id"], "error": type(error).__name__, "message": str(error)[:1024]}, archive=archive)
            temporary_terminalized = True
        terminalized = True
        raise
    finally:
        if child_channel is not None:
            child_channel.close()
        if parent_channel is not None:
            parent_channel.close()
        if not terminalized:
            cleanup = {"absent": True}
            if writer:
                try:
                    cleanup = cleanup_run_container(store, run_id=prepared.run["run_id"])
                except BaseException as cleanup_error:
                    cleanup = {"absent": False, "errors": [str(cleanup_error)[:1024]]}
            fail_run(store, run_id=prepared.run["run_id"], code="HOST_SUPERVISOR_INTERRUPTED", detail="host supervisor interrupted", needs_attention=writer and not cleanup["absent"], release_writer=writer and cleanup["absent"])
            if selected["conversation"]["role"] in {"investigator", "reviewer"} and temporary_bound and not temporary_terminalized:
                _terminalize_temporary_role(store, conversation=selected["conversation"], state="interrupted", result={"runId": prepared.run["run_id"], "reason": "host-supervisor-interrupted"})
        prepared.close()
        _restore_cleanup_signal_handlers(previous_handlers)


__all__ = ["ACCEPTANCE_PROFILE", "CHANNEL_ENVIRONMENT_KEY", "HostSupervisorError", "ensure_session", "launch_host_pi"]
