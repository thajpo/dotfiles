import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "scripts/pi-secretary-control.py"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Secretary Test")
    git(path, "config", "user.email", "secretary@example.invalid")
    (path / "tracked").write_text("initial\n")
    git(path, "add", "tracked")
    git(path, "commit", "-m", "initial")
    return path


def setup_registry(root: Path, count: int = 2):
    home = root / "home"
    home.mkdir()
    worktrees = root / "worktrees"
    worktrees.mkdir()
    repos = [make_repo(root / f"repo-{index}") for index in range(count)]
    policy = home / ".config/pi/repository-policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({
        "version": 1, "defaultMode": "isolated", "trustedRoots": [str(root)],
        "isolatedRoots": [], "controlPlaneRepositories": [],
        "protectedBranches": ["main", "master"], "worktreeRoot": str(worktrees),
    }))
    policy.chmod(0o600)
    env = os.environ.copy()
    env.update({"HOME": str(home), "XDG_STATE_HOME": str(root / "state")})
    records = []
    for index, repo in enumerate(repos):
        result = subprocess.run(
            [str(CONTROL), "--repository", str(repo), "register", "--alias", f"project-{index}"],
            env=env, text=True, capture_output=True, check=True,
        )
        records.append(json.loads(result.stdout))
    return home, repos, records, env


class SecretaryLauncherTests(unittest.TestCase):
    def test_active_project_selection_is_bounded_ordered_and_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, records, env = setup_registry(root, 5)
            initial = subprocess.run(
                [str(CONTROL), "active-list"], env=env, text=True,
                capture_output=True, check=True,
            )
            initial_records = json.loads(initial.stdout)
            self.assertEqual(len(initial_records), 3)
            self.assertTrue({item["projectId"] for item in initial_records}.issubset(
                {item["projectId"] for item in records},
            ))

            selected_aliases = ["project-4", "project-1", "project-3"]
            selected = subprocess.run(
                [str(CONTROL), "active-set", *sum((["--alias", alias] for alias in selected_aliases), [])],
                env=env, text=True, capture_output=True, check=True,
            )
            self.assertEqual([item["alias"] for item in json.loads(selected.stdout)], selected_aliases)
            reread = subprocess.run(
                [str(CONTROL), "active-list"], env=env, text=True,
                capture_output=True, check=True,
            )
            self.assertEqual([item["alias"] for item in json.loads(reread.stdout)], selected_aliases)
            state = root / "state/pi-secretary/active-projects.json"
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)

    def test_pi_secretary_constructs_fixed_read_only_pi_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, repos, records, env = setup_registry(root, 1)
            agent_dir = home / ".pi/agent"
            for path in [
                agent_dir / "npm/node_modules/@kjrjay/pi-sandbox/index.ts",
                agent_dir / "extensions/secretary/index.ts",
                agent_dir / "extensions/root-session/index.ts",
                agent_dir / "extensions/auto-continue/index.ts",
                agent_dir / "extensions/secretary-subagents/index.ts",
                agent_dir / "extensions/secretary-investigator-git/index.ts",
                agent_dir / "extensions/fast-mode/index.ts",
                agent_dir / "extensions/host-command/index.ts",
                agent_dir / "npm/node_modules/pi-web-access/index.ts",
                agent_dir / "skills/project-status/SKILL.md",
            ]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("placeholder\n")
            output = root / "pi-output.json"
            package = home / ".local/share/pi/core/node_modules/@earendil-works/pi-coding-agent"
            (package / "dist").mkdir(parents=True)
            (package.parent.parent / ".bin").mkdir(parents=True)
            (package / "package.json").write_text('{"name":"@earendil-works/pi-coding-agent","version":"0.83.0"}\n')
            fake_pi = package / "dist/cli.js"
            fake_pi.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
pathlib.Path(os.environ['FAKE_PI_OUTPUT']).write_text(json.dumps({
  'args': sys.argv[1:], 'cwd': os.getcwd(),
  'secretary': {k: os.environ.get(k) for k in ['PI_SECRETARY_PROJECT_ID','PI_SECRETARY_ALIAS','PI_SECRETARY_READ_ONLY']},
}))
""")
            fake_pi.chmod(0o755)
            (package.parent.parent / ".bin/pi").symlink_to(fake_pi)
            env.update({
                "PI_CODING_AGENT_DIR": str(agent_dir), "FAKE_PI_OUTPUT": str(output),
                "PATH": "/usr/local/bin:/usr/bin:/bin",
            })
            result = subprocess.run(
                [str(ROOT / "bin/pi-secretary"), "--internal-launch", "--project-id", records[0]["projectId"]],
                cwd=home, env=env, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            invocation = json.loads(output.read_text())
            self.assertEqual(invocation["cwd"], str(repos[0]))
            args = invocation["args"]
            self.assertEqual(args[args.index("--tools") + 1],
                             "read,grep,find,ls,web_search,fetch_content,get_search_content,source_check,host_command,subagent,subagent_supervisor,secretary_git,secretary_git_write,secretary_git_cleanup,secretary_record_idea,secretary_create_workstream,secretary_open_workstream,secretary_relaunch_workstream,secretary_list_workstreams,secretary_list_attention,secretary_acknowledge_attention,secretary_create_reviewer,secretary_land_reviewed,secretary_create_integration,secretary_cleanup_workstream")
            for flag in ["--no-extensions", "--no-skills", "--no-context-files", "--no-prompt-templates", "--session"]:
                self.assertIn(flag, args)
            self.assertEqual(args.count("-e"), 7)
            self.assertNotIn("--session-id", args)
            session_file = Path(args[args.index("--session") + 1])
            self.assertTrue(session_file.is_file())
            self.assertEqual(session_file.parent.name, "root")
            self.assertNotIn("bash", args[args.index("--tools") + 1])
            self.assertEqual(invocation["secretary"]["PI_SECRETARY_READ_ONLY"], "1")
            self.assertEqual(invocation["secretary"]["PI_SECRETARY_PROJECT_ID"], records[0]["projectId"])

    def test_pi_secretary_herdr_creates_a_parallel_surface_without_tmux(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, records, env = setup_registry(root, 3)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            log = root / "herdr.jsonl"
            herdr = fake_bin / "herdr"
            herdr.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with pathlib.Path(os.environ['FAKE_HERDR_LOG']).open('a') as stream:
    stream.write(json.dumps({'args': args, 'config': os.environ.get('HERDR_CONFIG_PATH')}) + '\\n')
command = args[2:] if len(args) >= 2 and args[:2] == ['--session', 'pi-secretary'] else args
if command == ['workspace', 'list']:
    print(json.dumps({'result': {'workspaces': []}}))
elif command == ['server', 'reload-config']:
    print(json.dumps({'result': {'type': 'config_reload'}}))
elif command[:2] == ['workspace', 'create']:
    label = command[command.index('--label') + 1]
    number = label.rsplit('/', 1)[-1]
    print(json.dumps({'result': {
        'workspace': {'workspace_id': 'w-' + number, 'label': label},
        'tab': {'tab_id': 'w-' + number + ':t1'},
        'root_pane': {'pane_id': 'w-' + number + ':p1',
                      'workspace_id': 'w-' + number,
                      'tab_id': 'w-' + number + ':t1'},
    }}))
elif command[:2] in (['pane', 'rename'], ['pane', 'run']) or command[:2] == ['workspace', 'focus']:
    print(json.dumps({'result': {'type': 'ok'}}))
elif command == []:
    pass
else:
    raise SystemExit('unexpected Herdr command: ' + repr(command))
""")
            herdr.chmod(0o755)
            env.update({"PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
                        "FAKE_HERDR_LOG": str(log)})
            result = subprocess.run([str(ROOT / "bin/pi-secretary"), "--herdr"], cwd=home,
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            creates = [call for call in calls if call["args"][2:4] == ["workspace", "create"]]
            launches = [call for call in calls if call["args"][2:4] == ["pane", "run"]]
            self.assertEqual(len(creates), 3)
            self.assertEqual(len(launches), 3)
            launch_ids = set()
            for call in launches:
                command = call["args"][-1]
                self.assertIn("--internal-launch --project-id", command)
                launch_ids.add(command.rsplit(" ", 1)[-1])
            self.assertEqual(launch_ids, {record["projectId"] for record in records})
            self.assertEqual(calls[-1]["args"], ["--session", "pi-secretary"])
            config = root / "state/pi-secretary/herdr/config.toml"
            self.assertIn("resume_agents_on_restore = false", config.read_text())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_pisec_active_selection_restarts_the_current_herdr_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, _, env = setup_registry(root, 3)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            herdr = fake_bin / "herdr"
            herdr.write_text("""#!/bin/sh
[ "$*" = "session list --json" ] || exit 2
printf '%s\n' '{"sessions":[{"name":"pi-secretary","running":true}]}'
""")
            herdr.chmod(0o755)
            restart_log = root / "restart.log"
            home_bin = home / ".local/bin"
            home_bin.mkdir(parents=True)
            restart = home_bin / "pi-restart"
            restart.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$FAKE_RESTART_LOG\"\n")
            restart.chmod(0o755)
            env.update({"PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
                        "FAKE_RESTART_LOG": str(restart_log)})
            result = subprocess.run(
                [str(ROOT / "bin/pisec"), "activate", "project-2", "project-0"],
                cwd=home, env=env, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("restarting the active Herdr surface", result.stderr)
            self.assertEqual(restart_log.read_text(), "-herdr\n")
            active = subprocess.run(
                [str(CONTROL), "active-list"], env=env, text=True,
                capture_output=True, check=True,
            )
            self.assertEqual([item["alias"] for item in json.loads(active.stdout)],
                             ["project-2", "project-0"])

    def test_pisec_initial_grid_starts_one_direct_command_per_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, records, env = setup_registry(root, 3)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            log = root / "tmux.jsonl"
            tmux = fake_bin / "tmux"
            tmux.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args=sys.argv[1:]
with pathlib.Path(os.environ['FAKE_TMUX_LOG']).open('a') as f: f.write(json.dumps(args)+'\\n')
cmd=args[0]
if cmd=='has-session': raise SystemExit(1)
if cmd=='new-session': print('@1'); raise SystemExit(0)
if cmd=='new-window': print('@2'); raise SystemExit(0)
if cmd=='kill-window': raise SystemExit(0)
if cmd=='rename-window': raise SystemExit(0)
if cmd=='list-panes': print('%1\\t0\\tshell-1\\tsh\\t0\\t'); raise SystemExit(0)
if cmd=='split-window': print('%2'); raise SystemExit(0)
raise SystemExit(0)
""")
            tmux.chmod(0o755)
            ps = fake_bin / "ps"
            ps.write_text("#!/bin/sh\nprintf 'sh\\n'\n")
            ps.chmod(0o755)
            pgrep = fake_bin / "pgrep"
            pgrep.write_text("#!/bin/sh\nexit 1\n")
            pgrep.chmod(0o755)
            env.update({"PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin", "FAKE_TMUX_LOG": str(log),
                        "PI_LAUNCHER_FORCE_DIRECTORY_LOCK": "1"})
            lock_dir = root / "state/pi-secretary/pisec-grid.lock.d"
            lock_dir.parent.mkdir(parents=True, exist_ok=True)
            lock_dir.mkdir()
            release_lock = subprocess.Popen([
                sys.executable, "-c",
                f"import time; time.sleep(0.15); __import__('os').rmdir({str(lock_dir)!r})",
            ])
            result = subprocess.run([str(ROOT / "bin/pisec"), "launch"], cwd=home, env=env, text=True, capture_output=True)
            release_lock.wait(timeout=2)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            launches = [call for call in calls if call[0] == "respawn-pane"]
            self.assertEqual(len(launches), 3)
            for call in launches:
                self.assertEqual(call[1:3], ["-k", "-t"])
                self.assertIn(" --internal-launch --project-id ", call[-1])
            self.assertEqual({record["projectId"] for record in records},
                             {call[-1].rsplit(" ", 1)[-1] for call in launches})
            self.assertEqual(len([call for call in calls if call[0] == "send-keys"]), 0)
            self.assertEqual(len([call for call in calls if call[0] == "split-window"]), 1)
            first_layout = next(i for i, call in enumerate(calls) if call[0] == "select-layout")
            first_split = next(i for i, call in enumerate(calls) if call[0] == "split-window")
            self.assertLess(first_layout, first_split)
            self.assertIn(["select-layout", "-t", "@2", "even-horizontal"], calls)

    def test_pisec_refuses_cross_surface_lock_before_creating_blank_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, records, env = setup_registry(root, 1)
            fake_bin = root / "bin"; fake_bin.mkdir()
            log = root / "tmux.jsonl"
            tmux = fake_bin / "tmux"
            tmux.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
with pathlib.Path(os.environ['FAKE_TMUX_LOG']).open('a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'has-session': raise SystemExit(1)
raise SystemExit('unexpected tmux mutation: ' + repr(sys.argv[1:]))
""")
            tmux.chmod(0o755)
            project_dir = home / ".pi/agent/sessions/secretary" / records[0]["projectId"]
            project_dir.mkdir(parents=True)
            lock_stream = (project_dir / ".active.lock").open("w")
            try:
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                env.update({"PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
                            "FAKE_TMUX_LOG": str(log)})
                result = subprocess.run([str(ROOT / "bin/pisec"), "launch"], cwd=home,
                                        env=env, text=True, capture_output=True)
            finally:
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
                lock_stream.close()
            self.assertEqual(result.returncode, 1)
            self.assertIn("already owned by another secretary surface", result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(calls, [["has-session", "-t", "=pisec"]])

    def test_pisec_open_preserves_live_processes_without_restarting_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, records, env = setup_registry(root, 2)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            log = root / "tmux.jsonl"
            process_map = {}
            for index, record in enumerate(records):
                root_pid, launch_pid, pi_pid = f"root-{index}", f"launch-{index}", f"pi-{index}"
                process_map[root_pid] = {"args": "sh", "children": [launch_pid]}
                process_map[launch_pid] = {
                    "args": f"{ROOT / 'bin/pi-secretary'} --internal-launch --project-id {record['projectId']}",
                    "children": [pi_pid],
                }
                process_map[pi_pid] = {"args": f"/fake/pi --session-id {record['secretarySessionId']}", "children": []}
            process_path = root / "processes.json"
            process_path.write_text(json.dumps(process_map))
            tmux = fake_bin / "tmux"
            tmux.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args=sys.argv[1:]
with pathlib.Path(os.environ['FAKE_TMUX_LOG']).open('a') as f: f.write(json.dumps(args)+'\\n')
if args[0]=='has-session': raise SystemExit(0)
if args[0]=='list-windows': print('@1\\tprojects'); raise SystemExit(0)
if args[0]=='kill-session': raise SystemExit(0)
if args[0]=='new-window': print('@9'); raise SystemExit(0)
if args[0]=='split-window': print('%3'); raise SystemExit(0)
if args[0]=='kill-window': raise SystemExit(0)
if args[0]=='rename-window': raise SystemExit(0)
if args[0]=='list-panes':
 print('%1\\t0\\troot-0\\tsh\\t0\\tchanged-title')
 print('%2\\t1\\troot-1\\tsh\\t0\\tother-title')
 raise SystemExit(0)
raise SystemExit(0)
""")
            tmux.chmod(0o755)
            ps = fake_bin / "ps"
            ps.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
value=json.loads(pathlib.Path(os.environ['FAKE_PROCESSES']).read_text()).get(sys.argv[-1])
if value is None: raise SystemExit(1)
print(value['args'])
""")
            ps.chmod(0o755)
            pgrep = fake_bin / "pgrep"
            pgrep.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
value=json.loads(pathlib.Path(os.environ['FAKE_PROCESSES']).read_text()).get(sys.argv[-1])
if value is None or not value['children']: raise SystemExit(1)
print('\\n'.join(value['children']))
""")
            pgrep.chmod(0o755)
            env.update({"PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
                        "FAKE_TMUX_LOG": str(log), "FAKE_PROCESSES": str(process_path),
                        "TMUX": "/tmp/fake-tmux,1,0"})
            result = subprocess.run([str(ROOT / "bin/pisec"), "open"], cwd=home,
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertFalse(any(call[0] in {"send-keys", "split-window", "new-window", "kill-window"}
                                 for call in calls))
            self.assertIn(["switch-client", "-t", "=pisec:@1"], calls)
            titles = [call[-1] for call in calls if call[0] == "select-pane"]
            self.assertEqual(titles, [])

    def test_pisec_rejects_wrong_window_and_extra_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, _, env = setup_registry(root, 1)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            tmux = fake_bin / "tmux"
            tmux.write_text("""#!/bin/sh
case "$1" in
  has-session) exit 0;;
  list-windows) printf '@9\\tother\\n'; exit 0;;
  *) exit 99;;
esac
""")
            tmux.chmod(0o755)
            env["PATH"] = f"{fake_bin}:/usr/local/bin:/usr/bin:/bin"
            result = subprocess.run([str(ROOT / "bin/pisec"), "open"], cwd=home, env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unexpected window", result.stderr)
            arity = subprocess.run([str(ROOT / "bin/pisec"), "open", "extra"], cwd=home, env=env, text=True, capture_output=True)
            self.assertEqual(arity.returncode, 2)
            self.assertIn("usage:", arity.stderr)


if __name__ == "__main__":
    unittest.main()
