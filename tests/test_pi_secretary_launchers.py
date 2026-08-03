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
                             "read,grep,find,ls,host_command,subagent,secretary_git,secretary_git_write,secretary_git_cleanup,secretary_record_idea,secretary_create_workstream,secretary_open_workstream,secretary_list_workstreams,secretary_list_attention,secretary_acknowledge_attention,secretary_create_reviewer,secretary_land_reviewed,secretary_create_integration,secretary_cleanup_workstream")
            for flag in ["--no-extensions", "--no-skills", "--no-context-files", "--no-prompt-templates", "--session"]:
                self.assertIn(flag, args)
            self.assertEqual(args.count("-e"), 6)
            self.assertNotIn("--session-id", args)
            session_file = Path(args[args.index("--session") + 1])
            self.assertTrue(session_file.is_file())
            self.assertEqual(session_file.parent.name, "root")
            self.assertNotIn("bash", args[args.index("--tools") + 1])
            self.assertEqual(invocation["secretary"]["PI_SECRETARY_READ_ONLY"], "1")
            self.assertEqual(invocation["secretary"]["PI_SECRETARY_PROJECT_ID"], records[0]["projectId"])

    def test_pisec_initial_grid_sends_one_quoted_command_per_project(self):
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
if cmd=='list-panes': print('%1\\t0\\tshell-1\\tsh\\t'); raise SystemExit(0)
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
            sends = [call for call in calls if call[0] == "send-keys"]
            self.assertEqual(len(sends), 3)
            for call in sends:
                self.assertEqual(len(call), 5)
                self.assertEqual(call[-1], "C-m")
                self.assertIn(" --internal-launch --project-id ", call[3])
            self.assertEqual({record["projectId"] for record in records},
                             {call[3].rsplit(" ", 1)[-1] for call in sends})
            self.assertEqual(len([call for call in calls if call[0] == "split-window"]), 1)
            first_layout = next(i for i, call in enumerate(calls) if call[0] == "select-layout")
            first_split = next(i for i, call in enumerate(calls) if call[0] == "split-window")
            self.assertLess(first_layout, first_split)
            self.assertIn(["select-layout", "-t", "@2", "even-horizontal"], calls)

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
 print('%1\\t0\\troot-0\\tsh\\tchanged-title')
 print('%2\\t1\\troot-1\\tsh\\tother-title')
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
