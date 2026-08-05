import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pi_personal_herdr_orchestrator", ROOT / "scripts/pi-personal-herdr.py",
)
personal_herdr = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(personal_herdr)


def setup_personal_home(root: Path):
    home = root / "home"
    directories = [
        home / "Projects/mlre-transition",
        home / "Projects/investing/investment-os",
        home / "dotfiles",
        home / ".config/dotfiles",
    ]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    (home / ".config/dotfiles/machine.env").write_text(
        'PI_PERSONAL_MLRE_DIR="${HOME}/Projects/mlre-transition"\n'
        'PI_PERSONAL_FINANCIALS_DIR="${HOME}/Projects/investing/investment-os"\n'
        'PI_PERSONAL_DOTFILES_DIR="${HOME}/dotfiles"\n'
    )
    return home


def install_fake_commands(root: Path):
    fake_bin = root / "bin"
    fake_bin.mkdir()
    log = root / "herdr.jsonl"
    tmux = fake_bin / "tmux"
    tmux.write_text("#!/bin/sh\nexit ${FAKE_TMUX_STATUS:-1}\n")
    tmux.chmod(0o755)
    herdr = fake_bin / "herdr"
    herdr.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with pathlib.Path(os.environ['FAKE_HERDR_LOG']).open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
command = args[2:] if args[:2] == ['--session', 'pi-personal'] else args
log = pathlib.Path(os.environ['FAKE_HERDR_LOG'])
prior = [json.loads(line) for line in log.read_text().splitlines()[:-1]]
if command == ['workspace', 'list']:
    if os.environ.get('FAKE_RESTORED') == '1':
        labels = ['mlre-transition', 'financials', 'dotfiles', 'pi-host']
        print(json.dumps({'result': {'workspaces': [
          {'workspace_id': 'w' + str(i + 1), 'label': 'personal/' + label}
          for i, label in enumerate(labels)
        ]}}))
    else:
        print(json.dumps({'result': {'workspaces': []}}))
elif command == ['server', 'reload-config']:
    print(json.dumps({'result': {'type': 'ok'}}))
elif command[:2] == ['workspace', 'create']:
    number = 1 + sum(1 for item in prior if item[2:4] == ['workspace', 'create'])
    wid = 'w' + str(number)
    label = command[command.index('--label') + 1]
    print(json.dumps({'result': {
      'workspace': {'workspace_id': wid, 'label': label},
      'tab': {'tab_id': wid + ':t1'},
      'root_pane': {'pane_id': wid + ':p1', 'workspace_id': wid, 'tab_id': wid + ':t1'},
    }}))
elif command[:2] == ['pane', 'split']:
    root_pane = command[2]
    wid = root_pane.split(':', 1)[0]
    print(json.dumps({'result': {'pane': {
      'pane_id': wid + ':p2', 'workspace_id': wid, 'tab_id': wid + ':t1',
    }}}))
elif command[:2] == ['pane', 'list']:
    wid = command[-1]
    index = int(wid[1:]) - 1
    labels = ['mlre-transition', 'financials', 'dotfiles', 'pi-host']
    paths = [
      pathlib.Path(os.environ['HOME']) / 'Projects/mlre-transition',
      pathlib.Path(os.environ['HOME']) / 'Projects/investing/investment-os',
      pathlib.Path(os.environ['HOME']) / 'dotfiles',
      pathlib.Path(os.environ['HOME']),
    ]
    print(json.dumps({'result': {'panes': [{
      'pane_id': wid + ':p1', 'workspace_id': wid, 'tab_id': wid + ':t1',
      'label': 'personal/' + labels[index], 'cwd': str(paths[index]),
      'foreground_cwd': str(paths[index]),
    }]}}))
elif command[:2] == ['pane', 'process-info']:
    pane = command[-1]
    seen = sum(1 for item in prior if item[2:4] == ['pane', 'process-info'] and item[-1] == pane)
    processes = [] if seen == 0 else [{'argv': ['/bin/bash'], 'cmdline': '/bin/bash', 'name': 'bash'}]
    print(json.dumps({'result': {'process_info': {'foreground_processes': processes}}}))
elif command[:2] in (['pane', 'rename'], ['pane', 'run']) or command[:2] == ['workspace', 'focus']:
    pass
else:
    raise SystemExit('unexpected Herdr command: ' + repr(command))
""")
    herdr.chmod(0o755)
    return fake_bin, log


class PersonalHerdrTests(unittest.TestCase):
    def run_launcher(self, root: Path, *args: str, tmux_status: int = 1, restored: bool = False):
        home = setup_personal_home(root)
        fake_bin, log = install_fake_commands(root)
        env = os.environ.copy()
        env.update({
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
            "XDG_STATE_HOME": str(root / "state"),
            "FAKE_HERDR_LOG": str(log),
            "FAKE_TMUX_STATUS": str(tmux_status),
            "FAKE_RESTORED": "1" if restored else "0",
        })
        result = subprocess.run(
            [str(ROOT / "bin/pi-personal-herdr"), *args], cwd=home,
            env=env, text=True, capture_output=True,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls, root / "state/pi-personal/herdr/config.toml"

    def test_desktop_creates_two_spaces_with_two_managed_panes_each(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, calls, config = self.run_launcher(root, "--no-attach")
            self.assertEqual(result.returncode, 0, result.stderr)
            creates = [call for call in calls if call[2:4] == ["workspace", "create"]]
            splits = [call for call in calls if call[2:4] == ["pane", "split"]]
            runs = [call for call in calls if call[2:4] == ["pane", "run"]]
            self.assertEqual(len(creates), 2)
            self.assertEqual(len(splits), 2)
            self.assertEqual(len(runs), 4)
            labels = [call[call.index("--label") + 1] for call in creates]
            self.assertEqual(labels, ["personal/personal-1", "personal/personal-2"])
            commands = [call[-1] for call in runs]
            for session_id in (
                "personal-mlre-transition", "personal-financials",
                "personal-dotfiles", "personal-host",
            ):
                self.assertTrue(any(session_id in command for command in commands), session_id)
            self.assertTrue(any("pi-host" in command and "--session-dir" in command for command in commands))
            self.assertIn("resume_agents_on_restore = false", config.read_text())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_mobile_creates_one_space_per_personal_conversation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, calls, _ = self.run_launcher(root, "-mobile", "--no-attach")
            self.assertEqual(result.returncode, 0, result.stderr)
            creates = [call for call in calls if call[2:4] == ["workspace", "create"]]
            self.assertEqual(len(creates), 4)
            self.assertFalse(any(call[2:4] == ["pane", "split"] for call in calls))
            self.assertEqual(len([call for call in calls if call[2:4] == ["pane", "run"]]), 4)
            self.assertEqual(
                [call[call.index("--label") + 1] for call in creates],
                [
                    "personal/mlre-transition", "personal/financials",
                    "personal/dotfiles", "personal/pi-host",
                ],
            )

    def test_restored_empty_process_snapshot_retries_then_relaunches_shells(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, calls, _ = self.run_launcher(
                root, "-mobile", "--no-attach", restored=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(any(call[2:4] == ["workspace", "create"] for call in calls))
            self.assertEqual(len([call for call in calls if call[2:4] == ["pane", "run"]]), 4)
            for pane in ("w1:p1", "w2:p1", "w3:p1", "w4:p1"):
                inspections = [call for call in calls
                               if call[2:4] == ["pane", "process-info"] and call[-1] == pane]
                self.assertEqual(len(inspections), 2)

    def test_live_root_pi_may_use_its_managed_worktree_as_foreground_cwd(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            repository.mkdir()
            launcher = str((ROOT / "bin/pi-tmux-session").resolve())
            role = {
                "name": "mlre-transition", "cwd": str(repository),
                "launcher": launcher, "session_id": "personal-mlre-transition",
                "session_dir": "",
            }
            pane = {
                "pane_id": "w1:p1", "cwd": str(repository),
                "foreground_cwd": str(Path(temporary) / "managed-worktree"),
                "agent": "pi",
            }
            process = {
                "argv": [launcher, "--session-id", "personal-mlre-transition"],
                "cmdline": f"{launcher} --session-id personal-mlre-transition",
                "name": "bash",
            }
            with mock.patch.object(personal_herdr, "_pane_processes", return_value=[process]):
                state = personal_herdr._pane_state("/fake/herdr", pane, role, env={})
            self.assertEqual(state, "live")

    def test_refuses_a_live_tmux_personal_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, calls, _ = self.run_launcher(root, "--no-attach", tmux_status=0)
            self.assertEqual(result.returncode, 1)
            self.assertIn("pi-restart -herdr", result.stderr)
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
