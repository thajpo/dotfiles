#!/usr/bin/env python3
"""Launch the guarded project-secretary surface inside a dedicated Herdr session.

This is deliberately separate from the tmux ``pisec`` launcher. Herdr owns the
terminal/workspace layout here, while ``bin/pi-secretary`` remains the only
process that may construct a secretary Pi invocation.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any, NoReturn


SESSION = "pi-secretary"
WORKSPACE_PREFIX = "secretary/"
SHELL_NAMES = {"bash", "zsh", "sh", "dash", "fish", "nu", "ksh"}


class HerdrSecretaryError(RuntimeError):
    pass


def fail(message: str) -> NoReturn:
    raise HerdrSecretaryError(message)


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        fail(f"invalid Herdr {label}")
    return value


def _json_result(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        fail(f"Herdr {label} returned an invalid JSON response")
    return payload["result"]


def _run(command: list[str], *, env: dict[str, str], label: str,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, env=env, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip() or "no diagnostic")
        raise HerdrSecretaryError(f"{label} failed: {detail}")
    return result


def _herdr_json(herdr: str, args: list[str], *, env: dict[str, str], label: str) -> dict[str, Any]:
    result = _run([herdr, "--session", SESSION, *args], env=env, label=label)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        fail(f"Herdr {label} returned invalid JSON: {error}")
    return _json_result(payload, label)


def _herdr_ok(herdr: str, args: list[str], *, env: dict[str, str], label: str) -> None:
    """Run a Herdr command whose CLI intentionally returns no response body."""
    _run([herdr, "--session", SESSION, *args], env=env, label=label)


def _ensure_directory(path: Path, label: str) -> None:
    if path.is_symlink():
        fail(f"{label} must not be a symlink")
    if path.exists() and not path.is_dir():
        fail(f"{label} is not a directory")
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _ensure_config(state_root: Path, env: dict[str, str]) -> Path:
    """Create a private Herdr config that keeps native agent restore disabled.

    Native Pi restore starts ``pi --session ...`` directly. That would bypass
    the secretary wrapper's fixed extension/tool boundary, so this surface
    intentionally restores panes as shells and relaunches them through the
    wrapper on the next ``pi-secretary --herdr`` invocation.
    """
    herdr_root = state_root / "herdr"
    _ensure_directory(herdr_root, "Herdr secretary state directory")
    config = herdr_root / "config.toml"
    if config.is_symlink():
        fail("Herdr secretary config must not be a symlink")
    if config.exists():
        if not config.is_file():
            fail("Herdr secretary config is not a regular file")
        text = config.read_text(encoding="utf-8")
        section = None
        restore_disabled = False
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if section == "session" and line.startswith("resume_agents_on_restore") and "=" in line:
                restore_disabled = line.split("=", 1)[1].strip() == "false"
        if not restore_disabled:
            fail("Herdr secretary config must keep session native restore disabled")
    else:
        config.write_text(
            "# Managed by pi-secretary --herdr.\n"
            "# Native restore would bypass the guarded secretary launcher.\n"
            "onboarding = false\n"
            "[session]\n"
            "resume_agents_on_restore = false\n",
            encoding="utf-8",
        )
    os.chmod(config, 0o600)
    env["HERDR_CONFIG_PATH"] = str(config)
    env["PI_SECRETARY_HERDR_CONFIG"] = str(config)
    # An inherited socket/session selector must not redirect setup to an outer
    # Herdr instance when this command is launched from another terminal pane.
    env.pop("HERDR_SOCKET_PATH", None)
    env.pop("HERDR_SESSION", None)
    return config


def _load_registry(control: Path, env: dict[str, str]) -> list[dict[str, str]]:
    result = _run([sys.executable, str(control), "registry-list"], env=env,
                  label="secretary registry lookup")
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        fail(f"secretary registry returned invalid JSON: {error}")
    if not isinstance(records, list) or not records:
        fail("no registered secretary projects")
    validated: list[dict[str, str]] = []
    aliases: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            fail("secretary registry contains a non-object record")
        project_id = _require_string(record.get("projectId"), "project id")
        if len(project_id) != 64 or any(char not in "0123456789abcdef" for char in project_id):
            fail("secretary registry contains an invalid project id")
        alias = _require_string(record.get("alias"), "project alias")
        repository = _require_string(record.get("primaryRepository"), "repository")
        repository_path = Path(repository).resolve(strict=True)
        if alias in aliases:
            fail(f"duplicate secretary alias: {alias}")
        aliases.add(alias)
        validated.append({"projectId": project_id, "alias": alias,
                          "primaryRepository": str(repository_path)})
    return validated


def _workspace_label(alias: str) -> str:
    return f"{WORKSPACE_PREFIX}{alias}"


def _surface_command(launcher: Path, worker_launcher: Path, project_id: str, *, herdr: Path,
                     config: str | None, workspace_id: str, tab_id: str,
                     pane_id: str) -> str:
    assignments = [
        "PI_SECRETARY_BACKEND=herdr",
        f"PI_SECRETARY_HERDR_BIN={herdr}",
        f"PI_SECRETARY_HERDR_WORKER={worker_launcher}",
        "HERDR_ENV=1",
        f"HERDR_WORKSPACE_ID={workspace_id}",
        f"HERDR_TAB_ID={tab_id}",
        f"HERDR_PANE_ID={pane_id}",
    ]
    if config:
        assignments.append(f"HERDR_CONFIG_PATH={config}")
    return shlex.join(["env", *assignments, str(launcher), "--internal-launch", "--project-id", project_id])


def _workspace_records(result: dict[str, Any]) -> list[dict[str, Any]]:
    value = result.get("workspaces")
    if not isinstance(value, list):
        fail("Herdr workspace list omitted workspaces")
    records: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            fail("Herdr workspace list contains an invalid workspace")
        _require_string(item.get("workspace_id"), "workspace id")
        _require_string(item.get("label"), "workspace label")
        records.append(item)
    return records


def _panes(result: dict[str, Any]) -> list[dict[str, Any]]:
    value = result.get("panes")
    if not isinstance(value, list):
        fail("Herdr pane list omitted panes")
    panes: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            fail("Herdr pane list contains an invalid pane")
        _require_string(item.get("pane_id"), "pane id")
        _require_string(item.get("workspace_id"), "pane workspace id")
        _require_string(item.get("tab_id"), "pane tab id")
        panes.append(item)
    return panes


def _canonical_reported_cwd(pane: dict[str, Any]) -> Path | None:
    for key in ("cwd", "foreground_cwd"):
        value = pane.get(key)
        if isinstance(value, str) and value:
            try:
                return Path(value).resolve(strict=False)
            except (OSError, RuntimeError):
                return None
    return None


def _process_tokens(process: dict[str, Any]) -> list[str]:
    argv = process.get("argv")
    if isinstance(argv, list) and all(isinstance(item, str) for item in argv):
        return list(argv)
    cmdline = process.get("cmdline")
    if isinstance(cmdline, str):
        try:
            return shlex.split(cmdline)
        except ValueError:
            return []
    return []


def _is_shell_process(process: dict[str, Any]) -> bool:
    name = process.get("name")
    if isinstance(name, str) and Path(name).name in SHELL_NAMES:
        return True
    tokens = _process_tokens(process)
    return bool(tokens and Path(tokens[0]).name in SHELL_NAMES)


def _secretary_lock_is_held(project_id: str, env: dict[str, str]) -> bool:
    agent_dir = Path(env.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser()
    project_dir = agent_dir / "sessions" / "secretary" / project_id
    directory_lock = project_dir / ".active.lock.d"
    if directory_lock.is_symlink():
        fail("secretary active lock directory must not be a symlink")
    if directory_lock.exists():
        if not directory_lock.is_dir():
            fail("secretary active lock directory is not a directory")
        return True
    lock_path = project_dir / ".active.lock"
    if lock_path.is_symlink():
        fail("secretary active lock must not be a symlink")
    if not lock_path.exists():
        return False
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _process_executable(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    first = Path(tokens[0]).name
    if first == "env":
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token == "--":
                index += 1
                break
            if token.startswith("-"):
                return None
            if "=" in token:
                index += 1
                continue
            break
        return tokens[index] if index < len(tokens) else None
    if first in SHELL_NAMES:
        if len(tokens) > 1 and tokens[1] != "-c":
            return tokens[1]
        return None
    return tokens[0]


def _is_secretary_launcher(process: dict[str, Any], launcher: Path, project_id: str) -> bool:
    tokens = _process_tokens(process)
    executable = _process_executable(tokens)
    if executable is None:
        return False
    try:
        if Path(executable).resolve(strict=False) != launcher:
            return False
    except (OSError, RuntimeError):
        return False
    try:
        id_index = tokens.index("--project-id")
    except ValueError:
        return False
    return id_index + 1 < len(tokens) and tokens[id_index + 1] == project_id and "--internal-launch" in tokens


def _existing_pane_state(herdr: str, pane: dict[str, Any], project: dict[str, str],
                         launcher: Path, *, workspace_id: str, tab_id: str,
                         env: dict[str, str]) -> str:
    pane_id = _require_string(pane.get("pane_id"), "pane id")
    if pane.get("workspace_id") != workspace_id or pane.get("tab_id") != tab_id:
        fail(f"Herdr secretary pane {pane_id} is bound to the wrong workspace or tab")
    cwd = _canonical_reported_cwd(pane)
    repository = Path(project["primaryRepository"])
    if cwd != repository:
        fail(f"Herdr secretary pane {pane_id} has the wrong working directory")

    # The per-project lock is held for the entire guarded Pi lifetime. It is
    # stronger than Herdr's generic `agent == pi` label: native restore would
    # start bare `pi --session ...` without acquiring this lock. If another
    # surface owns the lock, require proof that this exact pane owns it rather
    # than treating a stale Herdr Pi label as live.
    lock_held = _secretary_lock_is_held(project["projectId"], env)
    agent = pane.get("agent")
    if agent not in (None, "") and agent != "pi":
        fail(f"Herdr secretary pane {pane_id} contains unexpected agent {agent!r}")

    result = _herdr_json(herdr, ["pane", "process-info", "--pane", pane_id], env=env,
                         label=f"inspect Herdr pane {pane_id}")
    process_info = result.get("process_info")
    if not isinstance(process_info, dict):
        fail(f"Herdr pane {pane_id} returned invalid process information")
    processes = process_info.get("foreground_processes")
    if not isinstance(processes, list):
        fail(f"Herdr pane {pane_id} omitted foreground process information")
    if any(isinstance(item, dict) and _is_secretary_launcher(item, launcher, project["projectId"])
           for item in processes):
        return "live"
    if agent == "pi":
        fail(f"Herdr secretary pane {pane_id} contains Pi without the guarded secretary launcher")
    if lock_held:
        fail(f"project {project['alias']} is owned by another secretary surface")
    if processes and all(isinstance(item, dict) and _is_shell_process(item) for item in processes):
        return "shell"
    fail(f"Herdr secretary pane {pane_id} is not an idle shell or verified secretary")


def _ensure_project(herdr: str, project: dict[str, str], workspaces: list[dict[str, Any]],
                    launcher: Path, worker_launcher: Path, *, env: dict[str, str]) -> tuple[str, bool]:
    alias = project["alias"]
    label = _workspace_label(alias)
    matches = [item for item in workspaces if item.get("label") == label]
    if len(matches) > 1:
        fail(f"multiple Herdr secretary workspaces exist for {alias}")

    herdr_path = Path(herdr)
    config = env.get("HERDR_CONFIG_PATH")
    if matches:
        workspace = matches[0]
        workspace_id = _require_string(workspace.get("workspace_id"), "workspace id")
        pane_result = _herdr_json(herdr, ["pane", "list", "--workspace", workspace_id], env=env,
                                  label=f"inspect Herdr workspace {label}")
        panes = _panes(pane_result)
        labeled = [pane for pane in panes if pane.get("label") == label]
        if len(labeled) != 1:
            fail(f"Herdr secretary workspace {label} does not have exactly one labeled pane")
        pane = labeled[0]
        pane_id = _require_string(pane.get("pane_id"), "pane id")
        if pane.get("workspace_id") != workspace_id:
            fail(f"Herdr secretary workspace {label} returned a pane from another workspace")
        tab_id = _require_string(pane.get("tab_id"), "tab id")
        state = _existing_pane_state(herdr, pane, project, launcher,
                                     workspace_id=workspace_id, tab_id=tab_id, env=env)
        if state == "shell":
            command = _surface_command(launcher, worker_launcher, project["projectId"], herdr=herdr_path,
                                       config=config, workspace_id=workspace_id,
                                       tab_id=tab_id, pane_id=pane_id)
            _herdr_ok(herdr, ["pane", "run", pane_id, command], env=env,
                      label=f"launch secretary {alias}")
        return workspace_id, False

    if _secretary_lock_is_held(project["projectId"], env):
        fail(f"project {alias} is already owned by the tmux secretary surface; stop it before switching to Herdr")

    create_args = ["workspace", "create", "--cwd", project["primaryRepository"],
                   "--label", label, "--no-focus",
                   "--env", "PI_SECRETARY_BACKEND=herdr",
                   "--env", f"PI_SECRETARY_HERDR_BIN={herdr}",
                   "--env", f"PI_SECRETARY_HERDR_WORKER={worker_launcher}",
                   "--env", "HERDR_ENV=1"]
    if config:
        create_args.extend(["--env", f"HERDR_CONFIG_PATH={config}"])
    result = _herdr_json(herdr, create_args, env=env,
                         label=f"create Herdr workspace {label}")
    workspace = result.get("workspace")
    root_pane = result.get("root_pane")
    if not isinstance(workspace, dict) or not isinstance(root_pane, dict):
        fail(f"Herdr workspace create for {label} omitted its root pane")
    workspace_id = _require_string(workspace.get("workspace_id"), "workspace id")
    if _require_string(workspace.get("label"), "workspace label") != label:
        fail(f"Herdr workspace create for {label} returned the wrong workspace label")
    pane_id = _require_string(root_pane.get("pane_id"), "pane id")
    if root_pane.get("workspace_id") != workspace_id:
        fail(f"Herdr workspace create for {label} returned a root pane from another workspace")
    tab_value = root_pane.get("tab_id")
    if not tab_value and isinstance(result.get("tab"), dict):
        tab_value = result["tab"].get("tab_id")
    tab_id = _require_string(tab_value, "tab id")
    _herdr_ok(herdr, ["pane", "rename", pane_id, label], env=env,
              label=f"label Herdr secretary pane {alias}")
    command = _surface_command(launcher, worker_launcher, project["projectId"], herdr=herdr_path,
                               config=config, workspace_id=workspace_id,
                               tab_id=tab_id, pane_id=pane_id)
    _herdr_ok(herdr, ["pane", "run", pane_id, command], env=env,
              label=f"launch secretary {alias}")
    return workspace_id, True


def _recover_herdr_workstreams(control: Path, records: list[dict[str, str]], *, env: dict[str, str]) -> None:
    """Repair restored Herdr worker panes without migrating tmux workers.

    Native Herdr Pi restore is disabled because it would start a bare `pi`.
    Restored shells are therefore relaunched only through the controller's
    exact runtime identity path. A tmux-pinned workstream is left untouched;
    switching surfaces never performs an implicit migration.
    """
    for project in records:
        result = _run([sys.executable, str(control), "project-workstreams",
                       "--project-id", project["projectId"]], env=env,
                      label=f"inspect workstreams for {project['alias']}")
        try:
            workstreams = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            fail(f"workstream registry returned invalid JSON for {project['alias']}")
        if not isinstance(workstreams, list):
            fail(f"workstream registry returned an invalid list for {project['alias']}")
        for workstream in workstreams:
            if not isinstance(workstream, dict):
                fail(f"workstream registry contains an invalid record for {project['alias']}")
            backend = workstream.get("backend", "tmux")
            if backend == "tmux":
                continue
            if backend != "herdr" or workstream.get("closedAt") is not None:
                if backend not in {"tmux", "herdr"}:
                    fail(f"workstream has an invalid presentation backend for {project['alias']}")
                continue
            workstream_id = _require_string(workstream.get("workstreamId"), "workstream id")
            command = [sys.executable, str(control), "focus-workstream",
                       "--project-id", project["projectId"], "--workstream-id", workstream_id]
            deadline = time.monotonic() + 10.0
            while True:
                attempt = _run(command, env=env, label=f"recover Herdr workstream {workstream_id}",
                                check=False)
                if attempt.returncode == 0:
                    break
                detail = attempt.stderr.strip() or attempt.stdout.strip() or "no diagnostic"
                # A just-launched secretary shell can take a moment to expose
                # its wrapper argv. Retry only that startup observation; every
                # other identity or launch error remains fail-closed.
                if ("not owned by the guarded secretary launcher" not in detail or
                        time.monotonic() >= deadline):
                    raise HerdrSecretaryError(
                        f"recover Herdr workstream {workstream_id} failed: {detail}")
                time.sleep(0.1)


def _ensure_server(herdr: str, *, env: dict[str, str], log_path: Path) -> dict[str, Any]:
    try:
        result = _herdr_json(herdr, ["workspace", "list"], env=env,
                             label="inspect Herdr secretary session")
    except HerdrSecretaryError as first_error:
        if log_path.is_symlink():
            fail("Herdr server log must not be a symlink")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists() and not log_path.is_file():
            fail("Herdr server log is not a regular file")
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        log = os.fdopen(fd, "a", encoding="utf-8", buffering=1)
        try:
            process = subprocess.Popen(
                [herdr, "--session", SESSION, "server"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as error:
            log.close()
            fail(f"cannot start Herdr secretary server: {error}")
        deadline = time.monotonic() + 15.0
        last_error = str(first_error)
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.flush()
                    detail = log_path.read_text(encoding="utf-8", errors="replace").strip()
                    fail(f"Herdr secretary server exited with status {process.returncode}: {detail or last_error}")
                try:
                    return _herdr_json(herdr, ["workspace", "list"], env=env,
                                       label="wait for Herdr secretary server")
                except HerdrSecretaryError as error:
                    last_error = str(error)
                    time.sleep(0.1)
        finally:
            log.close()
        fail(f"Herdr secretary server did not become ready: {last_error}")
    _herdr_json(herdr, ["server", "reload-config"], env=env,
                label="reload Herdr secretary config")
    return result


def _lock_path(state_root: Path) -> Path:
    path = state_root / "herdr-launch.lock"
    if path.is_symlink():
        fail("Herdr secretary launch lock must not be a symlink")
    state_root.mkdir(parents=True, exist_ok=True)
    os.chmod(state_root, 0o700)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="launch the Pi secretary Herdr surface")
    parser.add_argument("--control", required=True)
    parser.add_argument("--launcher", required=True)
    parser.add_argument("--worker-launcher", required=True)
    parser.add_argument("--herdr-bin", required=True)
    args = parser.parse_args()

    control = Path(args.control).resolve(strict=True)
    launcher = Path(args.launcher).resolve(strict=True)
    worker_launcher = Path(args.worker_launcher).resolve(strict=True)
    herdr_path = Path(args.herdr_bin).resolve(strict=True)
    if not control.is_file() or not os.access(control, os.R_OK):
        fail("secretary control helper is not readable")
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        fail("secretary launcher is not executable")
    if not worker_launcher.is_file() or not os.access(worker_launcher, os.X_OK):
        fail("Herdr workstream launcher is not executable")
    if not herdr_path.is_file() or not os.access(herdr_path, os.X_OK):
        fail("Herdr executable is not executable")

    base_env = os.environ.copy()
    records = _load_registry(control, base_env)
    state_base = Path(base_env.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))).expanduser()
    state_root = state_base / "pi-secretary"
    _ensure_directory(state_root, "secretary state directory")
    env = base_env.copy()
    _ensure_config(state_root, env)
    env["PI_SECRETARY_BACKEND"] = "herdr"
    env["PI_SECRETARY_HERDR_BIN"] = str(herdr_path)
    env["PI_SECRETARY_HERDR_WORKER"] = str(worker_launcher)
    lock_path = _lock_path(state_root)
    log_path = state_root / "herdr" / "server.log"

    with lock_path.open("a+") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        workspaces_result = _ensure_server(str(herdr_path), env=env, log_path=log_path)
        workspaces = _workspace_records(workspaces_result)
        expected_labels = {_workspace_label(item["alias"]) for item in records}
        for item in workspaces:
            label = item.get("label")
            if isinstance(label, str) and label.startswith(WORKSPACE_PREFIX) and label not in expected_labels:
                fail(f"Herdr contains an unregistered secretary workspace: {label}")
        workspace_ids: list[str] = []
        for project in records:
            workspace_id, _created = _ensure_project(
                str(herdr_path), project, workspaces, launcher, worker_launcher, env=env,
            )
            workspace_ids.append(workspace_id)
            if not any(item.get("workspace_id") == workspace_id for item in workspaces):
                workspaces.append({"workspace_id": workspace_id, "label": _workspace_label(project["alias"])})
        _recover_herdr_workstreams(control, records, env=env)
        focused = any(item.get("focused") is True for item in workspaces if item.get("label") in expected_labels)
        if not focused and workspace_ids:
            _herdr_ok(str(herdr_path), ["workspace", "focus", workspace_ids[0]], env=env,
                      label="focus first Herdr secretary workspace")

    os.execve(str(herdr_path), [str(herdr_path), "--session", SESSION], env)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HerdrSecretaryError as error:
        print(f"pi-secretary --herdr: {error}", file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError) as error:
        print(f"pi-secretary --herdr: {error}", file=sys.stderr)
        raise SystemExit(1)
