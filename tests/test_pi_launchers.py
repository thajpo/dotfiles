import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def setup_home(root: Path):
    home = root / "home"
    home.mkdir()
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "feature")
    git(repo, "config", "user.name", "Pi Test")
    git(repo, "config", "user.email", "pi-test@example.invalid")
    (repo / "file.txt").write_text("start\n")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-m", "initial")
    policy = home / ".config/pi/repository-policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({
        "version": 1,
        "defaultMode": "isolated",
        "trustedRoots": [str(repo.resolve())],
        "isolatedRoots": [],
        "controlPlaneRepositories": [],
        "protectedBranches": ["main", "master"],
        "worktreeRoot": str(home / ".local/share/pi/worktrees"),
    }))
    policy.chmod(0o600)
    fake_dir = home / ".local/share/pi/bin"
    fake_dir.mkdir(parents=True)
    fake = fake_dir / "pi"
    fake.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
out = pathlib.Path(os.environ['FAKE_PI_OUTPUT'])
out.write_text(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd(), 'env': {k:v for k,v in os.environ.items() if k.startswith('PI_TASK_') or k == 'PI_SUBAGENTS_WORKTREE_DIR'}}))
""")
    fake.chmod(0o755)
    output = root / "fake-output.json"
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{ROOT / 'bin'}:{repo}:{fake_dir}:/usr/local/bin:/usr/bin:/bin",
        "FAKE_PI_OUTPUT": str(output),
    })
    return home, repo, output, env


def make_tmux_fixture(root: Path):
    """Create a deterministic tmux/process facade for launcher state tests."""
    fake_bin = root / "fake-bin"
    fake_bin.mkdir()
    log = root / "tmux.jsonl"
    state_path = root / "tmux-state.json"
    home = root / "home"
    (home / ".pi/agent/tmux-sessions").mkdir(parents=True)
    tmux = fake_bin / "tmux"
    tmux.write_text(r'''#!/usr/bin/env python3
import fcntl, json, os, pathlib, sys
state_path = pathlib.Path(os.environ["FAKE_TMUX_STATE"])
state_path.touch()
lock = state_path.open("r+")
fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
state = json.loads(state_path.read_text()) if state_path.stat().st_size else {"sessions": {}, "next": 1}
args = sys.argv[1:]
command = args[0] if args else ""
with pathlib.Path(os.environ["FAKE_TMUX_LOG"]).open("a") as stream:
    stream.write(json.dumps(args) + "\n")
if command in set(filter(None, os.environ.get("FAKE_TMUX_FAIL", "").split(","))):
    raise SystemExit(2)
def value(flag, default=None):
    try: return args[args.index(flag) + 1]
    except (ValueError, IndexError): return default
def session_name(target):
    target = (target or "").removeprefix("=")
    return target.split(":", 1)[0]
def find_window(target):
    if target and target.startswith("@"):
        for session in state["sessions"].values():
            for window in session["windows"]:
                if window["id"] == target: return session, window
        return None, None
    session = state["sessions"].get(session_name(target))
    if not session: return None, None
    suffix = (target or "").split(":", 1)[1] if ":" in (target or "") else None
    if suffix:
        for window in session["windows"]:
            if window["id"] == suffix or window["name"] == suffix: return session, window
    return session, session["windows"][0] if session["windows"] else (session, None)
def find_pane(target):
    if target and target.startswith("%"):
        for session in state["sessions"].values():
            for window in session["windows"]:
                for pane in window["panes"]:
                    if pane["id"] == target: return session, window, pane
        return None, None, None
    session, window = find_window(target)
    return session, window, window["panes"][0] if window and window["panes"] else None
def render(fmt, window=None, pane=None):
    values = {"window_id": window.get("id", "") if window else "", "window_name": window.get("name", "") if window else "", "pane_id": pane.get("id", "") if pane else "", "pane_index": str(pane.get("index", "")) if pane else "", "pane_pid": pane.get("pid", "") if pane else "", "pane_current_command": pane.get("command", "") if pane else ""}
    for key, item in values.items(): fmt = fmt.replace("#{" + key + "}", item)
    return fmt
def save(): state_path.write_text(json.dumps(state))
def pane(window, command_line):
    p = {"id": "%" + str(state["next"]), "index": len(window["panes"]), "pid": "shell", "command": "bash", "args": command_line, "children": []}
    state["next"] += 1
    if "nvim" in command_line: p.update(pid="nvim-" + p["id"], command="nvim", args="nvim")
    else: p.update(pid=p["id"], command="pi")
    window["panes"].append(p)
    return p
if command == "has-session": raise SystemExit(0 if session_name(value("-t")) in state["sessions"] else 1)
if command in {"new-session", "new-window"}:
    name = value("-s") if command == "new-session" else session_name(value("-t"))
    if command == "new-session":
        if name in state["sessions"]: raise SystemExit(2)
        session = state["sessions"][name] = {"windows": []}
    else:
        session = state["sessions"].get(name)
        if not session: raise SystemExit(2)
    window = {"id": "@" + str(state["next"]), "name": value("-n"), "panes": []}; state["next"] += 1
    pane(window, args[-1]); session["windows"].append(window); save()
    if "-P" in args: print(render(value("-F", "#{window_id}"), window))
    raise SystemExit(0)
if command in {"list-windows", "list-panes"}:
    session, window = find_window(value("-t"))
    if not session or (command == "list-panes" and not window): raise SystemExit(2)
    if command == "list-windows":
        for item in session["windows"]: print(render(value("-F"), item))
    else:
        for item in window["panes"]: print(render(value("-F"), window, item))
    raise SystemExit(0)
if command == "rename-session":
    old = session_name(value("-t")); session = state["sessions"].pop(old); state["sessions"][args[-1]] = session; save(); raise SystemExit(0)
if command == "rename-window":
    _, window = find_window(value("-t"))
    if not window: raise SystemExit(2)
    window["name"] = args[-1]; save(); raise SystemExit(0)
if command == "move-window":
    source, window = find_window(value("-s")); destination = state["sessions"].get(session_name(value("-t")))
    if not source or not window or not destination: raise SystemExit(2)
    source["windows"].remove(window); destination["windows"].append(window)
    if not source["windows"]:
        for name, item in list(state["sessions"].items()):
            if item is source: del state["sessions"][name]
    save(); raise SystemExit(0)
if command == "split-window":
    target = value("-t"); session, window, target_pane = find_pane(target)
    if not window: raise SystemExit(2)
    new = pane(window, args[-1])
    if "-b" in args and target_pane:
        index = target_pane["index"]
        for item in window["panes"]:
            if item is not new and item["index"] >= index: item["index"] += 1
        new["index"] = index
        window["panes"].sort(key=lambda item: item["index"])
    save()
    if "-P" in args: print(render(value("-F", "#{pane_id}"), window, new))
    raise SystemExit(0)
if command == "display-message":
    _, window, item = find_pane(value("-t")); print(render(args[-1], window, item)); raise SystemExit(0)
if command == "send-keys":
    _, window, item = find_pane(value("-t"))
    if not item: raise SystemExit(2)
    item.update(pid=item["id"], command="pi", args=" ".join(args[args.index("-t") + 2:-1])); save(); raise SystemExit(0)
if command == "swap-pane":
    _, window, _ = find_pane(value("-s")); source = value("-s"); target = value("-t")
    if window:
        a = next(item for item in window["panes"] if item["id"] == source); b = next(item for item in window["panes"] if item["id"] == target)
        a["index"], b["index"] = b["index"], a["index"]; window["panes"].sort(key=lambda item: item["index"]); save()
    raise SystemExit(0)
if command in {"select-pane", "switch-client", "attach-session"}: raise SystemExit(0)
raise SystemExit(0)
''')
    tmux.chmod(0o755)
    ps = fake_bin / "ps"
    ps.write_text(r'''#!/usr/bin/env python3
import json, os, pathlib, sys
if os.environ.get("FAKE_PS_FAIL") == "1": raise SystemExit(2)
state = json.loads(pathlib.Path(os.environ["FAKE_TMUX_STATE"]).read_text()); pid = sys.argv[-1]
for session in state["sessions"].values():
  for window in session["windows"]:
    for pane in window["panes"]:
      if pane["pid"] == pid: print(pane.get("args", pane["command"])); raise SystemExit(0)
raise SystemExit(1)
''')
    ps.chmod(0o755)
    pgrep = fake_bin / "pgrep"
    pgrep.write_text(r'''#!/usr/bin/env python3
import json, os, pathlib, sys
if os.environ.get("FAKE_PGREP_FAIL") == "1": raise SystemExit(2)
state = json.loads(pathlib.Path(os.environ["FAKE_TMUX_STATE"]).read_text()); pid = sys.argv[-1]
for session in state["sessions"].values():
  for window in session["windows"]:
    for pane in window["panes"]:
      if pane["pid"] == pid:
        children = pane.get("children", [])
        if children: print("\n".join(children)); raise SystemExit(0)
        raise SystemExit(1)
raise SystemExit(2)
''')
    pgrep.chmod(0o755)
    uuidgen = fake_bin / "uuidgen"
    uuidgen.write_text("#!/bin/sh\nprintf '%s\\n' \"$PPID\" >> \"$FAKE_UUID_LOG\"\nprintf 'fixture-session-id-%s\\n' \"$PPID\"\n")
    uuidgen.chmod(0o755)
    env = os.environ.copy()
    env.update({"HOME": str(home), "PI_CODING_AGENT_DIR": str(home / ".pi/agent"), "PATH": f"{fake_bin}:{ROOT / 'bin'}:{env['PATH']}", "FAKE_TMUX_LOG": str(log), "FAKE_TMUX_STATE": str(state_path), "FAKE_UUID_LOG": str(root / "uuid.log")})
    return home, fake_bin, log, state_path, env


def make_git_repo(root: Path):
    repo = root / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Pi Test")
    git(repo, "config", "user.email", "pi-test@example.invalid")
    (repo / "file").write_text("x\n")
    git(repo, "add", "file")
    git(repo, "commit", "-m", "initial")
    return repo


def launcher_ids(repo: Path):
    worktree_hash = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()
    common_hash = hashlib.sha256(str((repo / ".git").resolve()).encode()).hexdigest()
    return (f"pi-project-{worktree_hash[:12]}", f"pi-project-{common_hash[:12]}", worktree_hash, common_hash)


def seed_session(state_path: Path, name: str, windows):
    state_path.write_text(json.dumps({"sessions": {name: {"windows": windows}}, "next": 100}))


def window(window_id, name, panes):
    return {"id": window_id, "name": name, "panes": panes}


def pane(pane_id, index, pid, command, args):
    return {"id": pane_id, "index": index, "pid": pid, "command": command, "args": args, "children": []}


class LauncherTests(unittest.TestCase):
    def test_normal_launcher_selects_policy_and_ignores_malicious_repo_pi(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, repo, output, env = setup_home(Path(tmp))
            malicious = repo / "pi"
            malicious.write_text("#!/bin/sh\necho hijacked >&2; exit 99\n")
            malicious.chmod(0o755)
            result = subprocess.run([str(ROOT / "bin/pi"), "hello"], cwd=repo, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            invocation = json.loads(output.read_text())
            self.assertEqual(invocation["cwd"], str(repo.resolve()))
            args = invocation["args"]
            self.assertEqual(args[-1], "hello")
            self.assertIn("--session-dir", args)
            self.assertIn("--session", args)
            self.assertTrue(Path(args[args.index("--session") + 1]).is_file())
            self.assertEqual(invocation["env"]["PI_TASK_MODE"], "trusted-live")
            self.assertTrue(Path(invocation["env"]["PI_TASK_ROUTE_FILE"]).is_file())
            self.assertNotIn("hijacked", result.stderr)

    def test_installed_launchers_use_control_plane_helper_when_repo_path_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            installed = home / ".local/bin"
            control = home / ".local/share/pi/control"
            core = home / ".local/share/pi/core/node_modules/.bin"
            installed.mkdir(parents=True)
            control.mkdir(parents=True)
            core.mkdir(parents=True)
            output = root / "pi-output.json"
            fake_pi = core / "pi"
            fake_pi.write_text("""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
Path(os.environ["FAKE_PI_OUTPUT"]).write_text(json.dumps(sys.argv[1:]))
""")
            fake_pi.chmod(0o755)
            helper = control / "pi-workspace.py"
            helper.write_text("""#!/usr/bin/env python3
import os
import sys
if sys.argv[1] != "resolve-pi":
    raise SystemExit(2)
print(os.environ["FAKE_PI"])
""")
            helper.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "FAKE_PI": str(fake_pi),
                "FAKE_PI_OUTPUT": str(output),
            })
            for key in list(env):
                if key.startswith("PI_TASK_") or key.startswith("PI_SUBAGENT_"):
                    env.pop(key)

            for name, arguments in (("pi", ["--help"]), ("pi-host", ["--help"])):
                launcher = installed / name
                launcher.write_text((ROOT / "bin" / name).read_text())
                launcher.chmod(0o755)
                result = subprocess.run(
                    [str(launcher), *arguments], cwd=home, env=env, text=True, capture_output=True,
                )
                self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")
                self.assertNotIn(".local/scripts/pi-workspace.py", result.stderr)
                invocation = json.loads(output.read_text())
                if name == "pi":
                    self.assertEqual(invocation, arguments)
                else:
                    for flag in ["--no-context-files", "--no-extensions", "--no-skills", "--no-prompt-templates", "--help"]:
                        self.assertIn(flag, invocation)

    def test_workspace_and_trust_selector_flags_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, repo, _, env = setup_home(Path(tmp))
            for option in ["--no-sandbox", "--sandbox-target=current", "--approve", "-a", "--no-approve", "-na", "--no-extensions", "--extension=./evil.ts"]:
                result = subprocess.run([str(ROOT / "bin/pi"), option], cwd=repo, env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 2, option)
                self.assertIn("host-policy owned", result.stderr)

    def test_double_dash_treats_following_selector_text_as_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, repo, output, env = setup_home(Path(tmp))
            result = subprocess.run([str(ROOT / "bin/pi"), "--", "--sandbox-target=current"], cwd=repo, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(output.read_text())["args"]
            self.assertEqual(args[-2:], ["--", "--sandbox-target=current"])
            self.assertIn("--session", args)

    def test_pi_host_banner_fresh_session_and_disabled_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, _, output, env = setup_home(Path(tmp))
            result = subprocess.run([str(ROOT / "bin/pi-host"), "maintenance"], cwd=home, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("UNSANDBOXED HOST MODE", result.stderr)
            invocation = json.loads(output.read_text())
            args = invocation["args"]
            for flag in ["--no-context-files", "--no-extensions", "--no-skills", "--no-prompt-templates", "--session-dir", "--name"]:
                self.assertIn(flag, args)
            session_dir = Path(args[args.index("--session-dir") + 1])
            self.assertTrue(session_dir.is_dir())
            self.assertEqual(invocation["env"], {})

    def test_pi_host_accepts_only_stable_host_maintenance_session_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, _, output, env = setup_home(Path(tmp))
            session_dir = home / ".pi/agent/sessions/host-maintenance/pi-personal-host"
            result = subprocess.run(
                [str(ROOT / "bin/pi-host"), "--session-dir", str(session_dir), "--session-id", "personal-host"],
                cwd=home, env=env, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(output.read_text())["args"]
            self.assertEqual(args.count("--session-dir"), 1)
            self.assertEqual(Path(args[args.index("--session-dir") + 1]), session_dir)
            self.assertIn("personal-host", args)

            rejected = subprocess.run(
                [str(ROOT / "bin/pi-host"), "--session-dir", str(home / "outside")],
                cwd=home, env=env, text=True, capture_output=True,
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("host-maintenance session root", rejected.stderr)

            nested = subprocess.run(
                [str(ROOT / "bin/pi-host"), "--session-dir", str(session_dir / "nested")],
                cwd=home, env=env, text=True, capture_output=True,
            )
            self.assertEqual(nested.returncode, 2)

            outside = home / "outside-session"
            outside.mkdir()
            linked = session_dir.parent / "linked"
            linked.symlink_to(outside, target_is_directory=True)
            symlinked = subprocess.run(
                [str(ROOT / "bin/pi-host"), "--session-dir", str(linked)],
                cwd=home, env=env, text=True, capture_output=True,
            )
            self.assertEqual(symlinked.returncode, 2)
            self.assertIn("cannot be a symlink", symlinked.stderr)

    def test_pi_host_refuses_nested_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, _, _, env = setup_home(Path(tmp))
            env["PI_TASK_ROUTE_FILE"] = "/tmp/fake-route"
            result = subprocess.run([str(ROOT / "bin/pi-host")], cwd=home, env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing nested invocation", result.stderr)

    def test_pidev_separates_same_named_repositories_in_tmux(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            tmux_log = root / "tmux.jsonl"
            tmux_state = root / "tmux-state.json"
            fake_tmux = fake_bin / "tmux"
            fake_tmux.write_text("""#!/usr/bin/env python3
import fcntl
import json
import os
import pathlib
import sys

state_path = pathlib.Path(os.environ["FAKE_TMUX_STATE"])
state_path.touch()
lock = state_path.open("r+")
fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
state = json.loads(state_path.read_text()) if state_path.stat().st_size else {"sessions": {}, "next": 1}
args = sys.argv[1:]
command = args[0] if args else ""
with pathlib.Path(os.environ["FAKE_TMUX_LOG"]).open("a") as stream:
    stream.write(json.dumps(args) + "\\n")
if command in set(filter(None, os.environ.get("FAKE_TMUX_FAIL", "").split(","))):
    raise SystemExit(2)

def value(flag, default=None):
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return default

def session_name(target):
    target = target or ""
    if target.startswith("="):
        target = target[1:]
    return target.split(":", 1)[0]

def find_window(target):
    if target and target.startswith("@"):
        for session in state["sessions"].values():
            for window in session["windows"]:
                if window["id"] == target:
                    return session, window
    session = state["sessions"].get(session_name(target))
    if not session:
        return None, None
    suffix = (target or "").split(":", 1)[1] if ":" in (target or "") else None
    if suffix:
        for window in session["windows"]:
            if window["id"] == suffix or window["name"] == suffix:
                return session, window
    return session, session["windows"][0] if session["windows"] else None

def find_pane(target):
    if target and target.startswith("%"):
        for session in state["sessions"].values():
            for window in session["windows"]:
                for pane in window["panes"]:
                    if pane["id"] == target:
                        return session, window, pane
    session, window = find_window(target)
    return session, window, window["panes"][0] if window and window["panes"] else None

def render(fmt, session=None, window=None, pane=None):
    values = {
        "window_id": window.get("id", "") if window else "",
        "window_name": window.get("name", "") if window else "",
        "pane_id": pane.get("id", "") if pane else "",
        "pane_index": str(pane.get("index", "")) if pane else "",
        "pane_pid": pane.get("pid", "") if pane else "",
        "pane_current_command": pane.get("command", "") if pane else "",
    }
    for key, item in values.items():
        fmt = fmt.replace("#{" + key + "}", item)
    return fmt

def save():
    state_path.write_text(json.dumps(state))

if command == "has-session":
    raise SystemExit(0 if session_name(value("-t")) in state["sessions"] else 1)
if command == "new-session":
    name = value("-s")
    window = {"id": "@" + str(state["next"]), "name": value("-n"), "panes": []}
    state["next"] += 1
    window["panes"].append({"id": "%" + str(state["next"]), "index": 0, "pid": "nvim", "command": "nvim", "args": "nvim", "children": []})
    state["next"] += 1
    state["sessions"][name] = {"windows": [window]}
    save()
    raise SystemExit(0)
if command == "new-window":
    session = state["sessions"].get(session_name(value("-t")))
    if not session:
        raise SystemExit(2)
    window = {"id": "@" + str(state["next"]), "name": value("-n"), "panes": []}
    state["next"] += 1
    window["panes"].append({"id": "%" + str(state["next"]), "index": 0, "pid": "nvim", "command": "nvim", "args": "nvim", "children": []})
    state["next"] += 1
    session["windows"].append(window)
    save()
    if "-P" in args:
        print(render(value("-F", "#{window_id}"), session, window))
    raise SystemExit(0)
if command == "list-windows":
    session = state["sessions"].get(session_name(value("-t")))
    for window in (session or {}).get("windows", []):
        print(render(value("-F", "#{window_id}"), session, window))
    raise SystemExit(0)
if command == "list-panes":
    session, window = find_window(value("-t"))
    for pane in (window or {}).get("panes", []):
        print(render(value("-F", "#{pane_id}"), session, window, pane))
    raise SystemExit(0)
if command == "rename-session":
    old = session_name(value("-t"))
    session = state["sessions"].pop(old)
    state["sessions"][args[-1]] = session
    save()
    raise SystemExit(0)
if command == "rename-window":
    session, window = find_window(value("-t"))
    if not window:
        raise SystemExit(2)
    window["name"] = args[-1]
    save()
    raise SystemExit(0)
if command == "move-window":
    source_session, window = find_window(value("-s"))
    destination = state["sessions"].get(session_name(value("-t")))
    if not source_session or not window or not destination:
        raise SystemExit(2)
    source_session["windows"].remove(window)
    destination["windows"].append(window)
    save()
    raise SystemExit(0)
if command == "split-window":
    target = value("-t")
    session, window, _ = find_pane(target) if target and target.startswith("%") else (*find_window(target), None)
    if not window:
        raise SystemExit(2)
    command_line = args[-1]
    pane = {"id": "%" + str(state["next"]), "index": len(window["panes"]), "pid": "shell", "command": "bash", "args": command_line, "children": []}
    state["next"] += 1
    if "nvim" in command_line:
        pane["pid"], pane["command"] = "nvim", "nvim"
    else:
        pane["pid"], pane["command"] = pane["id"], "pi"
    window["panes"].append(pane)
    save()
    if "-P" in args:
        print(render(value("-F", "#{pane_id}"), session, window, pane))
    raise SystemExit(0)
if command == "display-message":
    session, window, pane = find_pane(value("-t"))
    print(render(value("-F", args[-1]), session, window, pane))
    raise SystemExit(0)
if command == "send-keys":
    session, window, pane = find_pane(value("-t"))
    if not pane:
        raise SystemExit(2)
    pane["pid"], pane["command"], pane["args"] = pane["id"], "pi", " ".join(args[args.index("-t") + 2:-1])
    save()
    raise SystemExit(0)
if command == "swap-pane":
    _, window, _ = find_pane(value("-s"))
    _, target_window, _ = find_pane(value("-t"))
    if window and window is target_window:
        source = next(p for p in window["panes"] if p["id"] == value("-s"))
        target = next(p for p in window["panes"] if p["id"] == value("-t"))
        source_index, target_index = source["index"], target["index"]
        source["index"], target["index"] = target_index, source_index
        window["panes"].sort(key=lambda p: p["index"])
        save()
    raise SystemExit(0)
if command in {"select-pane", "switch-client", "attach-session"}:
    raise SystemExit(0)
raise SystemExit(0)
""")
            fake_tmux.chmod(0o755)
            fake_ps = fake_bin / "ps"
            fake_ps.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
if os.environ.get("FAKE_PS_FAIL") == "1":
    raise SystemExit(2)
state = json.loads(pathlib.Path(os.environ["FAKE_TMUX_STATE"]).read_text())
pid = sys.argv[-1]
for session in state["sessions"].values():
    for window in session["windows"]:
        for pane in window["panes"]:
            if pane["pid"] == pid:
                print(pane.get("args", pane["command"]))
                raise SystemExit(0)
raise SystemExit(1)
""")
            fake_ps.chmod(0o755)
            fake_pgrep = fake_bin / "pgrep"
            fake_pgrep.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
if os.environ.get("FAKE_PGREP_FAIL") == "1":
    raise SystemExit(2)
state = json.loads(pathlib.Path(os.environ["FAKE_TMUX_STATE"]).read_text())
parent = sys.argv[-1]
for session in state["sessions"].values():
    for window in session["windows"]:
        for pane in window["panes"]:
            if pane["pid"] == parent:
                children = pane.get("children", [])
                if children:
                    print("\\n".join(children))
                    raise SystemExit(0)
                raise SystemExit(1)
raise SystemExit(2)
""")
            fake_pgrep.chmod(0o755)
            fake_uuidgen = fake_bin / "uuidgen"
            fake_uuidgen.write_text("#!/bin/sh\nprintf 'test-session-id-%s\n' \"$PPID\"\n")
            fake_uuidgen.chmod(0o755)

            repositories = []
            for parent in ("first", "second"):
                repo = root / parent / "service"
                repo.mkdir(parents=True)
                git(repo, "init", "-b", "feature")
                git(repo, "config", "user.name", "Pi Test")
                git(repo, "config", "user.email", "pi-test@example.invalid")
                (repo / "file.txt").write_text("start\\n")
                git(repo, "add", "file.txt")
                git(repo, "commit", "-m", "initial")
                repositories.append(repo)

            home = root / "home"
            home.mkdir()
            state_dir = home / ".pi/agent/tmux-sessions"
            state_dir.mkdir(parents=True)
            first_hash = hashlib.sha256(str(repositories[0].resolve()).encode()).hexdigest()
            (state_dir / f"{repositories[0].name}-{first_hash}.session-id").write_text("exact-old-state-id\n")
            encoded = str(repositories[1].resolve()).lstrip("/").replace("/", "-")
            pi_dir = home / ".pi/agent/sessions" / f"--{encoded}--"
            pi_dir.mkdir(parents=True)
            (pi_dir / "oldest.jsonl").write_text('{"id":"jsonl-old-id"}\n')
            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "PATH": f"{fake_bin}:{ROOT / 'bin'}:{env['PATH']}",
                "FAKE_TMUX_LOG": str(tmux_log),
                "FAKE_TMUX_STATE": str(tmux_state),
            })
            for repo in repositories:
                result = subprocess.run([str(ROOT / "bin/pidev")], cwd=repo, env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            # Reopening and a branch switch are idempotent: one path/hash
            # window and one stable project session remain in place.
            result = subprocess.run([str(ROOT / "bin/pidev")], cwd=repositories[0], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            before_branch = json.loads(tmux_state.read_text())
            first_session = next(session for name, session in before_branch["sessions"].items() if name.startswith("pi-service-"))
            first_window_name = first_session["windows"][0]["name"]
            subprocess.run(["git", "checkout", "-b", "changed"], cwd=repositories[0], env=env, check=True, capture_output=True)
            result = subprocess.run([str(ROOT / "bin/pidev")], cwd=repositories[0], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            after_branch = json.loads(tmux_state.read_text())
            first_session = next(session for name, session in after_branch["sessions"].items() if name.startswith("pi-service-"))
            self.assertEqual(len(first_session["windows"]), 1)
            self.assertEqual(first_session["windows"][0]["name"], first_window_name)
            linked = root / "linked-service"
            subprocess.run(["git", "worktree", "add", "-b", "linked", str(linked)], cwd=repositories[0], env=env, check=True, capture_output=True)
            result = subprocess.run([str(ROOT / "bin/pidev")], cwd=linked, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            # Restoration repair is scoped to the exact worktree window.
            state = json.loads(tmux_state.read_text())
            linked_window = next(window for session in state["sessions"].values() for window in session["windows"] if window["name"].startswith("w-linked-service-"))
            linked_window["panes"] = [pane for pane in linked_window["panes"] if pane["command"] == "nvim"]
            tmux_state.write_text(json.dumps(state))
            result = subprocess.run([str(ROOT / "bin/pidev")], cwd=linked, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads(tmux_state.read_text())
            linked_window = next(window for session in state["sessions"].values() for window in session["windows"] if window["name"].startswith("w-linked-service-"))
            pi_pane = next(pane for pane in linked_window["panes"] if pane["command"] == "pi")
            pi_pane["pid"], pi_pane["command"], pi_pane["args"] = "shell", "bash", "bash"
            tmux_state.write_text(json.dumps(state))
            result = subprocess.run([str(ROOT / "bin/pidev")], cwd=linked, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

            before_failed_query = tmux_state.read_text()
            env["FAKE_TMUX_FAIL"] = "list-windows"
            failed_query = subprocess.run([str(ROOT / "bin/pidev")], cwd=linked, env=env, text=True, capture_output=True)
            self.assertNotEqual(failed_query.returncode, 0)
            self.assertEqual(tmux_state.read_text(), before_failed_query)
            env.pop("FAKE_TMUX_FAIL")

            calls = [json.loads(line) for line in tmux_log.read_text().splitlines()]
            format_queries = [args[args.index("-F") + 1] for args in calls if "-F" in args and args[0] in {"list-windows", "list-panes"}]
            self.assertTrue(format_queries)
            self.assertTrue(all("\t" in fmt for fmt in format_queries))
            new_sessions = [args for args in calls if args and args[0] == "new-session"]
            self.assertEqual(len(new_sessions), 2)
            session_targets = [args[args.index("-s") + 1] for args in new_sessions]
            self.assertEqual(len(set(session_targets)), 2)
            self.assertTrue(all(target.startswith("pi-service-") for target in session_targets))
            new_windows = [args for args in calls if args and args[0] == "new-window"]
            self.assertEqual(len(new_windows), 1)
            self.assertEqual(len([args for args in calls if args and args[0] == "send-keys"]), 1)
            self.assertEqual(new_windows[0][new_windows[0].index("-t") + 1], "=" + session_targets[0])
            self.assertTrue(new_windows[0][new_windows[0].index("-n") + 1].startswith("w-linked-service-"))
            state_ids = {path.read_text().strip() for path in (home / ".pi/agent/tmux-sessions").glob("*.session-id")}
            self.assertEqual(len(state_ids), 3)
            pi_commands = [pane["args"] for session in json.loads(tmux_state.read_text())["sessions"].values() for window in session["windows"] for pane in window["panes"] if pane["command"] == "pi"]
            self.assertEqual(len(pi_commands), 3)
            for command in pi_commands:
                self.assertEqual(command.count("--session-id"), 1)
                self.assertIn(next(session_id for session_id in state_ids if session_id in command), command)

            working_directories = [args[args.index("-c") + 1] for args in calls if "-c" in args]
            self.assertEqual(working_directories, [str(repo.resolve()) for repo in repositories for _ in range(2)] + [str(linked.resolve()) for _ in range(3)])
            state_files = sorted((home / ".pi/agent/tmux-sessions").glob("*.session-id"))
            self.assertEqual(len(state_files), 3)
            self.assertEqual(len({path.read_text().strip() for path in state_files}), 3)
            self.assertIn("exact-old-state-id", {path.read_text().strip() for path in state_files})
            self.assertIn("jsonl-old-id", {path.read_text().strip() for path in state_files})
            self.assertTrue(all(path.stat().st_mode & 0o077 == 0 for path in state_files))

    def test_legacy_editor_is_renamed_in_place_without_restarting_pi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, log, state_path, env = make_tmux_fixture(root)
            repo = make_git_repo(root)
            legacy, project, worktree_hash, _ = launcher_ids(repo)
            state_file = home / ".pi/agent/tmux-sessions" / f"project-{worktree_hash}.session-id"
            state_file.write_text("old-state-id\n")
            managed = f"{ROOT / 'bin/pidev'} --launch --session-id old-state-id"
            seed_session(state_path, legacy, [window("@1", "editor", [pane("%1", 0, "nvim-pid", "nvim", "nvim"), pane("%2", 1, "pi-pid", "pi", managed)])])
            result = subprocess.run([str(ROOT / "bin/pidev")], cwd=repo, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertIn(["rename-session", "-t", "=" + legacy, project], calls)
            self.assertIn(["rename-window", "-t", "@1", f"w-project-{worktree_hash[:12]}"], calls)
            self.assertNotIn("move-window", [call[0] for call in calls])
            state = json.loads(state_path.read_text())
            self.assertEqual(set(state["sessions"]), {project})
            self.assertEqual(state["sessions"][project]["windows"][0]["name"], f"w-project-{worktree_hash[:12]}")
            self.assertEqual(state_file.read_text(), "old-state-id\n")

    def test_legacy_idle_shell_is_safe_and_started_without_splitting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, log, state_path, env = make_tmux_fixture(root)
            repo = make_git_repo(root)
            legacy, project, worktree_hash, _ = launcher_ids(repo)
            (home / ".pi/agent/tmux-sessions" / f"project-{worktree_hash}.session-id").write_text("old-state-id\n")
            source = window("@1", "editor", [pane("%1", 0, "nvim-pid", "nvim", "nvim"), pane("%2", 1, "shell-pid", "bash", "bash")])
            seed_session(state_path, legacy, [source])
            result = subprocess.run([str(ROOT / "bin/pidev")], cwd=repo, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertIn(["rename-session", "-t", "=" + legacy, project], calls)
            self.assertTrue(any(call[0] == "send-keys" and call[2] == "%2" for call in calls))
            self.assertNotIn("split-window", [call[0] for call in calls])

    def test_legacy_editor_moves_into_existing_project_without_restarting_pi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, log, state_path, env = make_tmux_fixture(root)
            repo = make_git_repo(root)
            legacy, project, worktree_hash, _ = launcher_ids(repo)
            (home / ".pi/agent/tmux-sessions" / f"project-{worktree_hash}.session-id").write_text("old-state-id\n")
            destination = window("@2", "other", [pane("%3", 0, "nvim-other", "nvim", "nvim")])
            managed = f"{ROOT / 'bin/pidev'} --launch --session-id old-state-id"
            source = window("@1", "editor", [pane("%1", 0, "nvim-pid", "nvim", "nvim"), pane("%2", 1, "pi-pid", "pi", managed)])
            state_path.write_text(json.dumps({"sessions": {legacy: {"windows": [source]}, project: {"windows": [destination]}}, "next": 100}))
            result = subprocess.run([str(ROOT / "bin/pidev")], cwd=repo, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertIn(["move-window", "-s", "@1", "-t", "=" + project + ":"], calls)
            self.assertIn(["rename-window", "-t", "@1", f"w-project-{worktree_hash[:12]}"], calls)
            state = json.loads(state_path.read_text())
            self.assertNotIn(legacy, state["sessions"])
            self.assertEqual(len(state["sessions"][project]["windows"]), 2)
            self.assertEqual(sum(window["name"] == f"w-project-{worktree_hash[:12]}" for window in state["sessions"][project]["windows"]), 1)

    def test_unsafe_legacy_shapes_refuse_without_tmux_or_state_mutation(self):
        for shape in ("extra-window", "different-pi", "unknown-pane"):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                home, _, log, state_path, env = make_tmux_fixture(root)
                repo = make_git_repo(root)
                legacy, _, worktree_hash, _ = launcher_ids(repo)
                state_file = home / ".pi/agent/tmux-sessions" / f"project-{worktree_hash}.session-id"
                state_file.write_text("old-state-id\n")
                panes = [pane("%1", 0, "nvim-pid", "nvim", "nvim")]
                managed = str(ROOT / "bin/pidev")
                if shape == "different-pi": panes.append(pane("%2", 1, "pi-pid", "pi", f"{managed} --launch --session-id other-id"))
                elif shape == "unknown-pane": panes.append(pane("%2", 1, "worker-pid", "python", "python worker"))
                else: panes.append(pane("%2", 1, "pi-pid", "pi", f"{managed} --launch --session-id old-state-id"))
                windows = [window("@1", "editor", panes)]
                if shape == "extra-window": windows.append(window("@3", "extra", [pane("%4", 0, "shell-pid", "bash", "bash")]))
                seed_session(state_path, legacy, windows)
                before = state_path.read_bytes()
                result = subprocess.run([str(ROOT / "bin/pidev")], cwd=repo, env=env, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(state_path.read_bytes(), before)
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertFalse(any(call[0] in {"rename-session", "rename-window", "move-window", "split-window", "send-keys"} for call in calls))

    def test_unknown_process_in_existing_workstream_refuses_without_repair_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, log, state_path, env = make_tmux_fixture(root)
            repo = make_git_repo(root)
            _, project, worktree_hash, _ = launcher_ids(repo)
            state_file = home / ".pi/agent/tmux-sessions" / f"project-{worktree_hash}.session-id"
            state_file.write_text("stable-id\n")
            target = window("@2", f"w-project-{worktree_hash[:12]}", [pane("%1", 0, "nvim-pid", "nvim", "nvim"), pane("%2", 1, "worker-pid", "python", "python worker")])
            seed_session(state_path, project, [target])
            before = state_path.read_bytes()
            result = subprocess.run([str(ROOT / "bin/pidev")], cwd=repo, env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(state_path.read_bytes(), before)
            state = json.loads(state_path.read_text())
            self.assertEqual(state["sessions"][project]["windows"][0]["panes"][1]["command"], "python")
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertFalse(any(call[0] in {"send-keys", "split-window", "swap-pane", "rename-window", "move-window"} for call in calls))

    def test_session_id_text_in_unrelated_process_does_not_impersonate_pi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, log, state_path, env = make_tmux_fixture(root)
            repo = make_git_repo(root)
            _, project, worktree_hash, _ = launcher_ids(repo)
            state_file = home / ".pi/agent/tmux-sessions" / f"project-{worktree_hash}.session-id"
            state_file.write_text("stable-id\n")
            impostor = pane("%2", 1, "worker-pid", "python",
                            "python worker.py --session-id stable-id")
            target = window("@2", f"w-project-{worktree_hash[:12]}", [
                pane("%1", 0, "nvim-pid", "nvim", "nvim"), impostor])
            seed_session(state_path, project, [target])
            before = state_path.read_bytes()
            result = subprocess.run([str(ROOT / "bin/pidev")], cwd=repo, env=env,
                                    text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(state_path.read_bytes(), before)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertFalse(any(call[0] in {"send-keys", "split-window", "swap-pane"}
                                 for call in calls))

    def test_arbitrary_pi_basename_cannot_impersonate_managed_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, log, state_path, env = make_tmux_fixture(root)
            repo = make_git_repo(root)
            _, project, worktree_hash, _ = launcher_ids(repo)
            (home / ".pi/agent/tmux-sessions" /
             f"project-{worktree_hash}.session-id").write_text("stable-id\n")
            target = window("@2", f"w-project-{worktree_hash[:12]}", [
                pane("%1", 0, "nvim-pid", "nvim", "nvim"),
                pane("%2", 1, "fake-pi", "pi", "/tmp/pi --session-id stable-id")])
            seed_session(state_path, project, [target])
            result = subprocess.run([str(ROOT / "bin/pidev")], cwd=repo, env=env,
                                    text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertFalse(any(call[0] in {"send-keys", "split-window", "swap-pane"}
                                 for call in calls))

    def test_reversed_known_two_pane_layout_is_swapped_to_nvim_left(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, log, state_path, env = make_tmux_fixture(root)
            repo = make_git_repo(root)
            _, project, worktree_hash, _ = launcher_ids(repo)
            (home / ".pi/agent/tmux-sessions" / f"project-{worktree_hash}.session-id").write_text("stable-id\n")
            managed = f"{ROOT / 'bin/pidev'} --launch --session-id stable-id"
            target = window("@2", f"w-project-{worktree_hash[:12]}", [pane("%2", 0, "pi-pid", "pi", managed), pane("%1", 1, "nvim-pid", "nvim", "nvim")])
            seed_session(state_path, project, [target])
            result = subprocess.run([str(ROOT / "bin/pidev")], cwd=repo, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertIn(["swap-pane", "-s", "%1", "-t", "%2"], calls)
            panes = json.loads(state_path.read_text())["sessions"][project]["windows"][0]["panes"]
            self.assertEqual([(item["index"], item["command"]) for item in panes], [(0, "nvim"), (1, "pi")])

    def test_concurrent_same_worktree_launches_share_one_workspace_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, _, state_path, env = make_tmux_fixture(root)
            repo = make_git_repo(root)
            env["PI_LAUNCHER_FORCE_DIRECTORY_LOCK"] = "1"
            first = subprocess.Popen([str(ROOT / "bin/pidev")], cwd=repo, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            second = subprocess.Popen([str(ROOT / "bin/pidev")], cwd=repo, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            first_out, first_err = first.communicate(timeout=10)
            second_out, second_err = second.communicate(timeout=10)
            self.assertEqual(first.returncode, 0, first_err)
            self.assertEqual(second.returncode, 0, second_err)
            _, project, worktree_hash, _ = launcher_ids(repo)
            state = json.loads(state_path.read_text())
            self.assertEqual(set(state["sessions"]), {project})
            self.assertEqual(len(state["sessions"][project]["windows"]), 1)
            self.assertEqual(len(state["sessions"][project]["windows"][0]["panes"]), 2)
            self.assertEqual(sum(p["command"] == "pi" for p in state["sessions"][project]["windows"][0]["panes"]), 1)
            self.assertEqual(len(list((home / ".pi/agent/tmux-sessions").glob("*.session-id"))), 1)
            persisted = (home / ".pi/agent/tmux-sessions" / f"project-{worktree_hash}.session-id").read_text().strip()
            self.assertRegex(persisted, r"^fixture-session-id-\d+$")
            self.assertEqual(Path(env["FAKE_UUID_LOG"]).read_text().splitlines(), [persisted.rsplit("-", 1)[1]])

    def test_concurrent_linked_worktrees_share_project_with_distinct_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, _, state_path, env = make_tmux_fixture(root)
            repo = make_git_repo(root)
            env["PI_LAUNCHER_FORCE_DIRECTORY_LOCK"] = "1"
            linked = root / "linked"
            git(repo, "worktree", "add", "-b", "linked", str(linked))
            launches = [subprocess.Popen([str(ROOT / "bin/pidev")], cwd=path, env=env,
                                         text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        for path in (repo, linked)]
            for launch in launches:
                _, stderr = launch.communicate(timeout=10)
                self.assertEqual(launch.returncode, 0, stderr)
            _, project, _, _ = launcher_ids(repo)
            state = json.loads(state_path.read_text())
            self.assertEqual(set(state["sessions"]), {project})
            self.assertEqual(len(state["sessions"][project]["windows"]), 2)
            self.assertEqual(sum(pane["command"] == "pi"
                                 for window in state["sessions"][project]["windows"]
                                 for pane in window["panes"]), 2)
            session_ids = {path.read_text().strip()
                           for path in (home / ".pi/agent/tmux-sessions").glob("*.session-id")}
            self.assertEqual(len(session_ids), 2)
            self.assertEqual(len(Path(env["FAKE_UUID_LOG"]).read_text().splitlines()), 2)

    def test_pidev_host_workstream_identity_seeds_exact_session_and_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, log, state_path, env = make_tmux_fixture(root)
            repo = make_git_repo(root)
            brief = root / "brief.md"
            brief.write_text("# Assigned brief\n")
            env.update({"PI_PIDEV_SESSION_ID": "ws-abcdef0123456789",
                        "PI_PIDEV_WORKSTREAM_ID": "assigned-work",
                        "PI_PIDEV_PROJECT_ID": "a" * 64,
                        "PI_PIDEV_BRIEF_PATH": str(brief),
                        "PI_PIDEV_CONTROL": str(ROOT / "scripts/pi-secretary-control.py")})
            result = subprocess.run([str(ROOT / "bin/pidev")], cwd=repo, env=env,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            _, project, worktree_hash, _ = launcher_ids(repo)
            state_file = home / ".pi/agent/tmux-sessions" / f"project-{worktree_hash}.session-id"
            self.assertEqual(state_file.read_text().strip(), "ws-abcdef0123456789")
            panes = json.loads(state_path.read_text())["sessions"][project]["windows"][0]["panes"]
            command = next(pane["args"] for pane in panes if pane["command"] == "pi")
            self.assertIn("PI_WORKSTREAM_ID=assigned-work", command)
            self.assertIn("PI_WORKSTREAM_BRIEF_PATH=", command)
            self.assertEqual(command.count("--session-id"), 1)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            window_id = json.loads(state_path.read_text())["sessions"][project]["windows"][0]["id"]
            self.assertIn(["select-window", "-t", window_id], calls)

    def test_pidev_review_window_is_read_only_and_resurrection_keeps_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, log, state_path, env = make_tmux_fixture(root)
            repo = make_git_repo(root)
            env.update({"PI_PIDEV_SESSION_ID": "rv-abcdef0123456789",
                        "PI_PIDEV_REVIEW_PROJECT_ID": "b" * 64,
                        "PI_PIDEV_REVIEW_REQUEST_ID": "rr-review"})
            result = subprocess.run([str(ROOT / "bin/pidev")], cwd=repo, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertTrue(any("exec nvim -R -M" in call for call in calls if call))
            state = json.loads(state_path.read_text())
            pane_args = [pane["args"] for session in state["sessions"].values() for window in session["windows"] for pane in window["panes"]]
            review_command = next(value for value in pane_args if "--review-request-id" in value)
            self.assertIn("--review-project-id", review_command)
            self.assertIn("rr-review", review_command)
            self.assertIn("rv-abcdef0123456789", review_command)

    def test_pidev_rejects_subagent_launch_bypass_before_tmux(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            tmux = fake_bin / "tmux"
            tmux.write_text("#!/bin/sh\necho unexpected tmux >&2\nexit 99\n")
            tmux.chmod(0o755)
            env = os.environ.copy()
            env.update({"PATH": f"{fake_bin}:{env['PATH']}", "PI_SUBAGENT_CHILD": "1"})
            result = subprocess.run([str(ROOT / "bin/pidev"), "--launch", "--session-id", "stable-id"], cwd=root, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("refusing ordinary invocation", result.stderr)
            self.assertNotIn("unexpected tmux", result.stderr)

    def test_pidev_rejects_ordinary_subagent_invocation_before_tmux(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            tmux = fake_bin / "tmux"
            tmux.write_text("#!/bin/sh\necho unexpected tmux >&2\nexit 99\n")
            tmux.chmod(0o755)
            env = os.environ.copy()
            env.update({"PATH": f"{fake_bin}:{env['PATH']}", "PI_SUBAGENT_CHILD": "1"})
            result = subprocess.run([str(ROOT / "bin/pidev")], cwd=root, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("refusing ordinary invocation", result.stderr)
            self.assertNotIn("unexpected tmux", result.stderr)

    def test_installer_refuses_activation_when_docker_daemon_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            fake_bin = root / "bin"
            home.mkdir()
            fake_bin.mkdir()
            docker = fake_bin / "docker"
            docker.write_text("#!/bin/sh\nexit 1\n")
            docker.chmod(0o755)
            env = os.environ.copy()
            env.update({"HOME": str(home), "PATH": f"{fake_bin}:{env['PATH']}", "PI_HARNESS_ONLY": "1"})
            result = subprocess.run([str(ROOT / "install.sh")], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing partial Pi harness activation", result.stderr)
            self.assertFalse((home / ".local/share/pi/core").exists())
            self.assertFalse((home / ".pi/agent").exists())


if __name__ == "__main__":
    unittest.main()
