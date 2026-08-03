import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PiPersonalTests(unittest.TestCase):
    def test_creates_two_pane_windows_and_is_idempotent(self):
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
            tmux.write_text(r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with pathlib.Path(os.environ["FAKE_TMUX_LOG"]).open("a") as stream:
    stream.write(json.dumps(args) + "\n")
cmd = args[0]
existing = os.environ.get("FAKE_TMUX_EXISTING") == "1"
target = args[args.index("-t") + 1] if "-t" in args else ""
if cmd == "has-session": raise SystemExit(0 if existing else 1)
if cmd == "new-session": print("@1"); raise SystemExit(0)
if cmd == "new-window": print("@2"); raise SystemExit(0)
if cmd == "list-windows": print("@1\tpersonal-1\n@2\tpersonal-2"); raise SystemExit(0)
if cmd == "list-panes":
    if args[-1] == "#{pane_id}":
        print("%1" if target == "@1" else "%3")
    elif existing:
        groups = {
            "@1": [("mlre-transition", "personal-mlre-transition"), ("financials", "personal-financials")],
            "@2": [("dotfiles", "personal-dotfiles"), ("pi-host", "personal-host")],
        }
        start = 1 if target == "@1" else 3
        for offset, (title, session_id) in enumerate(groups[target]):
            index = start + offset
            dead = 1 if os.environ.get("FAKE_TMUX_DEAD") == title else 0
            if os.environ.get("FAKE_TMUX_NO_ROLES") == "1":
                print(f"%{index}|π - changed|{dead}||/restored/path|exec launcher --session-id {session_id}")
            else:
                print(f"%{index}|{title}|{dead}|{title}||")
    else:
        print("%1|mlre-transition|0|mlre-transition")
    raise SystemExit(0)
if cmd == "split-window":
    counter = pathlib.Path(os.environ["FAKE_TMUX_COUNTER"])
    value = int(counter.read_text()) + 1 if counter.exists() else 2
    counter.write_text(str(value))
    print(f"%{value}")
    raise SystemExit(0)
raise SystemExit(0)
''')
            tmux.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
                "PI_LAUNCHER_FORCE_DIRECTORY_LOCK": "1",
                "FAKE_TMUX_LOG": str(log),
                "FAKE_TMUX_COUNTER": str(counter),
            })
            lock_dir = home / ".local/state/pi/personal.lock.d"
            lock_dir.parent.mkdir(parents=True, exist_ok=True)
            lock_dir.mkdir()
            release_lock = subprocess.Popen([
                sys.executable, "-c",
                f"import time; time.sleep(0.15); __import__('os').rmdir({str(lock_dir)!r})",
            ])
            result = subprocess.run(
                [str(ROOT / "bin/pi-personal"), "--ensure"],
                cwd=home, env=env, text=True, capture_output=True,
            )
            release_lock.wait(timeout=2)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len([call for call in calls if call[0] == "new-session"]), 1)
            splits = [call for call in calls if call[0] == "split-window"]
            self.assertEqual(len(splits), 2)
            self.assertEqual(len([call for call in calls if call[0] == "new-window"]), 1)
            commands = [call[-1] for call in calls if call[0] in {"new-session", "new-window", "split-window"}]
            for session_id in ["personal-mlre-transition", "personal-financials", "personal-dotfiles", "personal-host"]:
                self.assertTrue(any(session_id in command for command in commands), session_id)
            self.assertTrue(any("--session-dir" in command and "pi-personal-host" in command for command in commands))
            self.assertEqual(len([call for call in calls if call[0] == "select-layout"]), 2)
            self.assertTrue(all(call[-1] == "even-horizontal" for call in calls if call[0] == "select-layout"))
            self.assertNotIn("tiled", {call[-1] for call in calls if call[0] == "select-layout"})
            self.assertIn(["set-option", "-w", "-t", "@1", "pane-border-status", "top"], calls)
            self.assertFalse(any(call[0] in {"attach-session", "switch-client"} for call in calls))

            log.write_text("")
            env["FAKE_TMUX_EXISTING"] = "1"
            second = subprocess.run(
                [str(ROOT / "bin/pi-personal"), "--ensure"],
                cwd=home, env=env, text=True, capture_output=True,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertFalse(any(call[0] in {"new-session", "split-window"} for call in calls))

            log.write_text("")
            env["FAKE_TMUX_NO_ROLES"] = "1"
            recovered = subprocess.run(
                [str(ROOT / "bin/pi-personal"), "--ensure"],
                cwd=home, env=env, text=True, capture_output=True,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertFalse(any(call[0] in {"new-session", "split-window", "respawn-pane"} for call in calls))
            configured_roles = [
                call[-1] for call in calls
                if len(call) >= 6 and call[:3] == ["set-option", "-p", "-t"] and call[4] == "@pi_personal_role"
            ]
            self.assertEqual(set(configured_roles), {"mlre-transition", "financials", "dotfiles", "pi-host"})

            log.write_text("")
            env.pop("FAKE_TMUX_NO_ROLES")
            env["FAKE_TMUX_DEAD"] = "financials"
            restarted = subprocess.run(
                [str(ROOT / "bin/pi-personal"), "--ensure"],
                cwd=home, env=env, text=True, capture_output=True,
            )
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            respawns = [call for call in calls if call[0] == "respawn-pane"]
            self.assertEqual(len(respawns), 1)
            self.assertIn("%2", respawns[0])
            self.assertIn("personal-financials", respawns[0][-1])


if __name__ == "__main__":
    unittest.main()
