#!/usr/bin/env python3
"""Build the guarded personal Pi surface in the named ``pi-personal`` Herdr session."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, NoReturn


SESSION = "pi-personal"
WORKSPACE_PREFIX = "personal/"
SHELL_NAMES = {"bash", "zsh", "sh", "dash", "fish", "nu", "ksh"}


class PersonalHerdrError(RuntimeError):
    pass


def fail(message: str) -> NoReturn:
    raise PersonalHerdrError(message)


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        fail(f"invalid Herdr {label}")
    return value


def _run(command: list[str], *, env: dict[str, str], label: str,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, env=env, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        fail(f"{label} failed: {detail[:500]}")
    return result


def _herdr_json(herdr: str, args: list[str], *, env: dict[str, str], label: str) -> dict[str, Any]:
    result = _run([herdr, "--session", SESSION, *args], env=env, label=label)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        fail(f"Herdr {label} returned invalid JSON: {error}")
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        fail(f"Herdr {label} returned an invalid response")
    return payload["result"]


def _herdr_ok(herdr: str, args: list[str], *, env: dict[str, str], label: str) -> None:
    _run([herdr, "--session", SESSION, *args], env=env, label=label)


def _ensure_directory(path: Path, label: str) -> None:
    if path.is_symlink():
        fail(f"{label} must not be a symlink")
    if path.exists() and not path.is_dir():
        fail(f"{label} is not a directory")
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _ensure_config(state_root: Path, env: dict[str, str]) -> None:
    herdr_root = state_root / "herdr"
    _ensure_directory(herdr_root, "personal Herdr state directory")
    config = herdr_root / "config.toml"
    if config.is_symlink():
        fail("personal Herdr config must not be a symlink")
    expected = (
        "# Managed by pi-personal --herdr.\n"
        "onboarding = false\n"
        "[session]\n"
        "resume_agents_on_restore = false\n"
    )
    if config.exists():
        if not config.is_file():
            fail("personal Herdr config is not a regular file")
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
            fail("personal Herdr config must disable native agent restore")
    else:
        config.write_text(expected, encoding="utf-8")
    os.chmod(config, 0o600)
    env["HERDR_CONFIG_PATH"] = str(config)
    env.pop("HERDR_SOCKET_PATH", None)
    env.pop("HERDR_SESSION", None)


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
    records: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            fail("Herdr pane list contains an invalid pane")
        _require_string(item.get("pane_id"), "pane id")
        _require_string(item.get("workspace_id"), "pane workspace id")
        _require_string(item.get("tab_id"), "pane tab id")
        records.append(item)
    return records


def _process_tokens(process: dict[str, Any]) -> list[str]:
    argv = process.get("argv")
    if isinstance(argv, list) and argv and all(isinstance(item, str) for item in argv):
        return list(argv)
    cmdline = process.get("cmdline")
    if isinstance(cmdline, str):
        try:
            return shlex.split(cmdline)
        except ValueError:
            return []
    return []


def _is_shell(process: dict[str, Any]) -> bool:
    name = process.get("name")
    if isinstance(name, str) and Path(name).name in SHELL_NAMES:
        tokens = _process_tokens(process)
        return len(tokens) <= 1
    tokens = _process_tokens(process)
    return len(tokens) == 1 and Path(tokens[0]).name in SHELL_NAMES


def _launcher_tokens(process: dict[str, Any]) -> list[str]:
    tokens = _process_tokens(process)
    if not tokens:
        return []
    if Path(tokens[0]).name in SHELL_NAMES:
        if len(tokens) < 2 or tokens[1] == "-c":
            return []
        tokens = tokens[1:]
    if tokens and Path(tokens[0]).name == "env":
        index = 1
        while index < len(tokens) and "=" in tokens[index] and not tokens[index].startswith("="):
            index += 1
        tokens = tokens[index:]
    return tokens


def _is_role_launcher(process: dict[str, Any], role: dict[str, str]) -> bool:
    tokens = _launcher_tokens(process)
    if not tokens:
        return False
    try:
        executable = Path(tokens[0]).resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    if executable != Path(role["launcher"]):
        return False
    try:
        session_index = tokens.index("--session-id")
    except ValueError:
        return False
    if session_index + 1 >= len(tokens) or tokens[session_index + 1] != role["session_id"]:
        return False
    if role["name"] == "pi-host":
        try:
            directory_index = tokens.index("--session-dir")
        except ValueError:
            return False
        return (directory_index + 1 < len(tokens) and
                tokens[directory_index + 1] == role["session_dir"])
    return True


def _pane_processes(herdr: str, pane_id: str, *, env: dict[str, str]) -> list[dict[str, Any]]:
    # Immediately after native shell-only restore, Herdr may expose the pane
    # before its foreground shell process has been observed. Retry only that
    # empty startup state; malformed process data still fails immediately.
    for attempt in range(40):
        result = _herdr_json(herdr, ["pane", "process-info", "--pane", pane_id], env=env,
                             label=f"inspect personal pane {pane_id}")
        process_info = result.get("process_info")
        processes = process_info.get("foreground_processes") if isinstance(process_info, dict) else None
        if not isinstance(processes, list) or not all(isinstance(item, dict) for item in processes):
            fail(f"Herdr pane {pane_id} returned invalid process information")
        if processes or attempt == 39:
            return processes
        time.sleep(0.05)
    return []


def _pane_state(herdr: str, pane: dict[str, Any], role: dict[str, str],
                *, env: dict[str, str]) -> str:
    pane_id = _require_string(pane.get("pane_id"), "pane id")
    # Herdr's stable pane cwd remains the launch repository while a guarded
    # root Pi may intentionally move its foreground process into its managed
    # worktree. Require the stable cwd here; an idle shell is checked against
    # its foreground cwd below before it can be relaunched.
    cwd = pane.get("cwd") or pane.get("foreground_cwd")
    if not isinstance(cwd, str) or Path(cwd).resolve(strict=False) != Path(role["cwd"]):
        fail(f"personal pane {pane_id} has the wrong working directory")
    agent = pane.get("agent")
    if agent not in (None, "", "pi"):
        fail(f"personal pane {pane_id} contains unexpected agent {agent!r}")
    processes = _pane_processes(herdr, pane_id, env=env)
    if any(_is_role_launcher(process, role) for process in processes):
        return "live"
    if agent == "pi" or any(Path(str(process.get("name", ""))).name == "pi" for process in processes):
        fail(f"personal pane {pane_id} contains Pi without its managed launcher")
    if processes and all(_is_shell(process) for process in processes):
        foreground_cwd = pane.get("foreground_cwd") or cwd
        if (not isinstance(foreground_cwd, str) or
                Path(foreground_cwd).resolve(strict=False) != Path(role["cwd"])):
            fail(f"personal shell pane {pane_id} has the wrong working directory")
        return "shell"
    fail(f"personal pane {pane_id} is not an idle shell or verified personal Pi")


def _workspace_is_idle(herdr: str, workspace_id: str, *, env: dict[str, str]) -> bool:
    panes = _panes(_herdr_json(herdr, ["pane", "list", "--workspace", workspace_id], env=env,
                                label=f"inspect personal workspace {workspace_id}"))
    if not panes:
        return False
    for pane in panes:
        processes = _pane_processes(herdr, _require_string(pane.get("pane_id"), "pane id"), env=env)
        if not processes or not all(_is_shell(process) for process in processes):
            return False
    return True


def _surface_command(role: dict[str, str]) -> str:
    command = ["env", "PI_ROOT_PROFILE=personal", "PI_PERSONAL_BACKEND=herdr",
               role["launcher"]]
    if role["name"] == "pi-host":
        command.extend(["--session-dir", role["session_dir"]])
    command.extend(["--session-id", role["session_id"]])
    return shlex.join(command)


def _expected_groups(layout: str, roles: list[dict[str, str]]) -> list[tuple[str, list[dict[str, str]]]]:
    if layout == "mobile":
        return [(f"{WORKSPACE_PREFIX}{role['name']}", [role]) for role in roles]
    return [
        (f"{WORKSPACE_PREFIX}personal-1", roles[:2]),
        (f"{WORKSPACE_PREFIX}personal-2", roles[2:]),
    ]


def _ensure_group(herdr: str, label: str, roles: list[dict[str, str]],
                  workspaces: list[dict[str, Any]], *, env: dict[str, str]) -> str:
    matches = [workspace for workspace in workspaces if workspace.get("label") == label]
    if len(matches) > 1:
        fail(f"multiple personal Herdr workspaces exist for {label}")
    if matches:
        workspace = matches[0]
        workspace_id = _require_string(workspace.get("workspace_id"), "workspace id")
        panes = _panes(_herdr_json(herdr, ["pane", "list", "--workspace", workspace_id], env=env,
                                    label=f"inspect personal workspace {label}"))
        by_label = {pane.get("label"): pane for pane in panes}
        expected_labels = {f"{WORKSPACE_PREFIX}{role['name']}" for role in roles}
        if len(panes) != len(roles) or set(by_label) != expected_labels:
            fail(f"personal Herdr workspace {label} has an unexpected pane layout; run pi-restart")
        for role in roles:
            pane = by_label[f"{WORKSPACE_PREFIX}{role['name']}"]
            state = _pane_state(herdr, pane, role, env=env)
            if state == "shell":
                _herdr_ok(herdr, ["pane", "run", pane["pane_id"], _surface_command(role)], env=env,
                          label=f"launch personal role {role['name']}")
        return workspace_id

    result = _herdr_json(
        herdr,
        ["workspace", "create", "--cwd", roles[0]["cwd"], "--label", label, "--no-focus",
         "--env", "PI_PERSONAL_BACKEND=herdr", "--env", "HERDR_ENV=1"],
        env=env, label=f"create personal workspace {label}",
    )
    workspace = result.get("workspace")
    root_pane = result.get("root_pane")
    if not isinstance(workspace, dict) or not isinstance(root_pane, dict):
        fail(f"Herdr workspace create for {label} omitted its root pane")
    workspace_id = _require_string(workspace.get("workspace_id"), "workspace id")
    if workspace.get("label") != label or root_pane.get("workspace_id") != workspace_id:
        fail(f"Herdr workspace create for {label} returned the wrong identity")
    panes = [root_pane]
    if len(roles) == 2:
        split = _herdr_json(
            herdr,
            ["pane", "split", root_pane["pane_id"], "--direction", "right", "--ratio", "0.5",
             "--cwd", roles[1]["cwd"], "--no-focus"],
            env=env, label=f"split personal workspace {label}",
        )
        pane = split.get("pane")
        if not isinstance(pane, dict) or pane.get("workspace_id") != workspace_id:
            fail(f"Herdr split for {label} returned the wrong pane")
        panes.append(pane)
    for role, pane in zip(roles, panes, strict=True):
        pane_id = _require_string(pane.get("pane_id"), "pane id")
        _herdr_ok(herdr, ["pane", "rename", pane_id, f"{WORKSPACE_PREFIX}{role['name']}"], env=env,
                  label=f"label personal role {role['name']}")
        _herdr_ok(herdr, ["pane", "run", pane_id, _surface_command(role)], env=env,
                  label=f"launch personal role {role['name']}")
    return workspace_id


def _ensure_server(herdr: str, *, env: dict[str, str], log_path: Path) -> dict[str, Any]:
    try:
        result = _herdr_json(herdr, ["workspace", "list"], env=env,
                             label="inspect personal Herdr session")
    except PersonalHerdrError as first_error:
        if log_path.is_symlink():
            fail("personal Herdr server log must not be a symlink")
        _ensure_directory(log_path.parent, "personal Herdr log directory")
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        log = os.fdopen(fd, "a", encoding="utf-8", buffering=1)
        try:
            process = subprocess.Popen(
                [herdr, "--session", SESSION, "server"], env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as error:
            log.close()
            fail(f"cannot start personal Herdr server: {error}")
        deadline = time.monotonic() + 15
        last_error = str(first_error)
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    fail(f"personal Herdr server exited with status {process.returncode}")
                try:
                    return _herdr_json(herdr, ["workspace", "list"], env=env,
                                       label="wait for personal Herdr server")
                except PersonalHerdrError as error:
                    last_error = str(error)
                    time.sleep(0.1)
        finally:
            log.close()
        fail(f"personal Herdr server did not become ready: {last_error}")
    _herdr_json(herdr, ["server", "reload-config"], env=env,
                label="reload personal Herdr config")
    return result


def _refuse_tmux_owner(env: dict[str, str]) -> None:
    tmux = shutil.which("tmux", path=env.get("PATH"))
    if not tmux:
        return
    result = _run([tmux, "has-session", "-t", "=pi-personal"], env=env,
                  label="inspect tmux personal surface", check=False)
    if result.returncode == 0:
        fail("tmux pi-personal is running; use pi-restart -herdr to switch safely")
    if result.returncode != 1:
        fail("cannot inspect the tmux personal surface")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--herdr-bin", required=True)
    parser.add_argument("--pi-session-launcher", required=True)
    parser.add_argument("--pi-host-launcher", required=True)
    parser.add_argument("--mobile", "-mobile", action="store_true")
    parser.add_argument("--no-attach", action="store_true")
    parser.add_argument("--focus-role", choices=("mlre-transition", "financials", "dotfiles", "pi-host"))
    args = parser.parse_args()

    herdr = str(Path(args.herdr_bin).resolve(strict=True))
    pi_session = str(Path(args.pi_session_launcher).resolve(strict=True))
    pi_host = str(Path(args.pi_host_launcher).resolve(strict=True))
    for executable, label in ((herdr, "Herdr"), (pi_session, "personal Pi"), (pi_host, "pi-host")):
        if not Path(executable).is_file() or not os.access(executable, os.X_OK):
            fail(f"{label} launcher is not executable")

    env = os.environ.copy()
    _refuse_tmux_owner(env)
    directories = {
        "mlre-transition": Path(env.get("PI_PERSONAL_MLRE_DIR", "")).expanduser().resolve(strict=True),
        "financials": Path(env.get("PI_PERSONAL_FINANCIALS_DIR", "")).expanduser().resolve(strict=True),
        "dotfiles": Path(env.get("PI_PERSONAL_DOTFILES_DIR", "")).expanduser().resolve(strict=True),
        "pi-host": Path.home().resolve(strict=True),
    }
    agent_dir = Path(env.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent"))).expanduser()
    host_session_dir = agent_dir / "sessions" / "host-maintenance" / "pi-personal-host"
    roles = [
        {"name": "mlre-transition", "cwd": str(directories["mlre-transition"]),
         "session_id": "personal-mlre-transition", "launcher": pi_session, "session_dir": ""},
        {"name": "financials", "cwd": str(directories["financials"]),
         "session_id": "personal-financials", "launcher": pi_session, "session_dir": ""},
        {"name": "dotfiles", "cwd": str(directories["dotfiles"]),
         "session_id": "personal-dotfiles", "launcher": pi_session, "session_dir": ""},
        {"name": "pi-host", "cwd": str(directories["pi-host"]),
         "session_id": "personal-host", "launcher": pi_host, "session_dir": str(host_session_dir)},
    ]
    layout = "mobile" if args.mobile else "desktop"
    groups = _expected_groups(layout, roles)

    state_base = Path(env.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))).expanduser()
    state_root = state_base / "pi-personal"
    _ensure_directory(state_root, "personal state directory")
    _ensure_config(state_root, env)
    lock_path = state_root / "herdr-launch.lock"
    if lock_path.is_symlink():
        fail("personal Herdr launch lock must not be a symlink")
    log_path = state_root / "herdr" / "server.log"

    with lock_path.open("a+") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        workspaces = _workspace_records(_ensure_server(herdr, env=env, log_path=log_path))
        if any(not str(workspace.get("label", "")).startswith(WORKSPACE_PREFIX)
               for workspace in workspaces):
            fail("pi-personal Herdr session contains an unrelated workspace")
        expected_labels = {label for label, _roles in groups}
        retained: list[dict[str, Any]] = []
        for workspace in workspaces:
            label = _require_string(workspace.get("label"), "workspace label")
            workspace_id = _require_string(workspace.get("workspace_id"), "workspace id")
            if label in expected_labels:
                retained.append(workspace)
                continue
            if not _workspace_is_idle(herdr, workspace_id, env=env):
                fail(f"personal layout differs while {label} is live; run pi-restart")
            _herdr_ok(herdr, ["workspace", "close", workspace_id], env=env,
                      label=f"close old personal workspace {label}")
        workspaces = retained

        workspace_by_role: dict[str, str] = {}
        first_workspace = ""
        for label, group_roles in groups:
            workspace_id = _ensure_group(herdr, label, group_roles, workspaces, env=env)
            if not first_workspace:
                first_workspace = workspace_id
            for role in group_roles:
                workspace_by_role[role["name"]] = workspace_id
            if not any(item.get("workspace_id") == workspace_id for item in workspaces):
                workspaces.append({"workspace_id": workspace_id, "label": label})
        focus_workspace = workspace_by_role.get(args.focus_role or "", first_workspace)
        if focus_workspace:
            _herdr_ok(herdr, ["workspace", "focus", focus_workspace], env=env,
                      label="focus personal Herdr workspace")

    if args.no_attach:
        return 0
    os.execve(herdr, [herdr, "--session", SESSION], env)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PersonalHerdrError as error:
        print(f"pi-personal --herdr: {error}", file=sys.stderr)
        raise SystemExit(1)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"pi-personal --herdr: {error}", file=sys.stderr)
        raise SystemExit(1)
