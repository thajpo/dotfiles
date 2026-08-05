import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MobileLauncherTests(unittest.TestCase):
    def test_pi_personal_mobile_uses_one_window_per_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            for directory in [
                home / "Projects/mlre-transition",
                home / "Projects/investing/investment-os",
                home / "dotfiles",
                home / ".local/bin",
                home / ".config/dotfiles",
            ]:
                directory.mkdir(parents=True, exist_ok=True)
            (home / ".config/dotfiles/machine.env").write_text(
                'PI_PERSONAL_MLRE_DIR="${HOME}/Projects/mlre-transition"\n'
                'PI_PERSONAL_FINANCIALS_DIR="${HOME}/Projects/investing/investment-os"\n'
                'PI_PERSONAL_DOTFILES_DIR="${HOME}/dotfiles"\n'
            )
            for launcher in ["pi-tmux-session", "pi-host"]:
                path = home / ".local/bin" / launcher
                path.write_text("#!/bin/sh\nexit 0\n")
                path.chmod(0o755)

            fake_bin = root / "bin"
            fake_bin.mkdir()
            log = root / "tmux.jsonl"
            counter = root / "counter"
            tmux = fake_bin / "tmux"
            tmux.write_text(
                """#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with pathlib.Path(os.environ['FAKE_TMUX_LOG']).open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
cmd = args[0]
if cmd == 'has-session': raise SystemExit(1)
if cmd in {'new-session', 'new-window'}:
    value = int(pathlib.Path(os.environ['FAKE_TMUX_COUNTER']).read_text()) if pathlib.Path(os.environ['FAKE_TMUX_COUNTER']).exists() else 0
    value += 1
    pathlib.Path(os.environ['FAKE_TMUX_COUNTER']).write_text(str(value))
    print('@' + str(value))
    raise SystemExit(0)
if cmd == 'list-panes': print('%1'); raise SystemExit(0)
if cmd == 'split-window': raise SystemExit(9)
raise SystemExit(0)
"""
            )
            tmux.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
                "PI_LAUNCHER_FORCE_DIRECTORY_LOCK": "1",
                "FAKE_TMUX_LOG": str(log),
                "FAKE_TMUX_COUNTER": str(counter),
            })
            result = subprocess.run(
                [str(ROOT / "bin/pi-personal"), "--mobile", "--ensure"],
                cwd=home, env=env, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len([call for call in calls if call[0] == "new-session"]), 1)
            self.assertEqual(len([call for call in calls if call[0] == "new-window"]), 3)
            self.assertFalse(any(call[0] == "split-window" for call in calls))
            self.assertFalse(any(call[0] == "select-layout" for call in calls))
            starts = [call[-1] for call in calls if call[0] in {"new-session", "new-window"}]
            for session_id in [
                "personal-mlre-transition", "personal-financials",
                "personal-dotfiles", "personal-host",
            ]:
                self.assertTrue(any(session_id in command for command in starts), session_id)

    def test_pisec_mobile_uses_one_window_per_secretary(self):
        from tests.test_pi_secretary_launchers import setup_registry

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _, records, env = setup_registry(root, 3)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            log = root / "tmux.jsonl"
            tmux = fake_bin / "tmux"
            tmux.write_text(
                """#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with pathlib.Path(os.environ['FAKE_TMUX_LOG']).open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
cmd = args[0]
if cmd == 'has-session': raise SystemExit(1)
if cmd == 'new-session': print('@1'); raise SystemExit(0)
if cmd == 'new-window': print('@' + str(2 + int(sum(1 for line in pathlib.Path(os.environ['FAKE_TMUX_LOG']).read_text().splitlines() if 'new-window' in line)))); raise SystemExit(0)
if cmd == 'list-panes': print('%1\\t0\\tshell\\tsh\\t0\\t'); raise SystemExit(0)
if cmd == 'split-window': raise SystemExit(9)
raise SystemExit(0)
"""
            )
            tmux.chmod(0o755)
            ps = fake_bin / "ps"
            ps.write_text("#!/bin/sh\nprintf 'sh\\n'\n")
            ps.chmod(0o755)
            pgrep = fake_bin / "pgrep"
            pgrep.write_text("#!/bin/sh\nexit 1\n")
            pgrep.chmod(0o755)
            env.update({
                "PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
                "FAKE_TMUX_LOG": str(log),
                "PI_LAUNCHER_FORCE_DIRECTORY_LOCK": "1",
            })
            result = subprocess.run(
                [str(ROOT / "bin/pisec"), "--mobile", "launch"],
                cwd=home, env=env, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len([call for call in calls if call[0] == "new-window"]), 3)
            self.assertFalse(any(call[0] == "split-window" for call in calls))
            self.assertFalse(any(call[0] == "select-layout" for call in calls))
            launches = [call for call in calls if call[0] == "respawn-pane"]
            self.assertEqual(len(launches), len(records))
            self.assertFalse(any(call[0] == "send-keys" for call in calls))

    def test_pidev_mobile_starts_a_single_full_width_pi_pane(self):
        from tests.test_pi_launchers import make_git_repo, make_tmux_fixture

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = make_git_repo(root)
            home, fake_bin, log, state_path, env = make_tmux_fixture(root)
            result = subprocess.run(
                [str(ROOT / "bin/pidev"), "-mobile"],
                cwd=repo, env=env, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len([call for call in calls if call[0] == "new-session"]), 1)
            self.assertFalse(any(call[0] == "split-window" for call in calls))
            command = next(call[-1] for call in calls if call[0] == "new-session")
            self.assertIn("--mobile", command)
            state = json.loads(state_path.read_text())
            sessions = list(state["sessions"].values())
            self.assertEqual(len(sessions), 1)
            self.assertEqual(len(sessions[0]["windows"]), 1)
            self.assertEqual(len(sessions[0]["windows"][0]["panes"]), 1)


if __name__ == "__main__":
    unittest.main()
