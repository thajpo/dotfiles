from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def make_start_fixture(root: Path):
    home = root / "home"
    fake_bin = root / "bin"
    home_bin = home / ".local/bin"
    home_bin.mkdir(parents=True)
    fake_bin.mkdir()
    log = root / "calls.log"

    tmux = fake_bin / "tmux"
    tmux.write_text("""#!/bin/sh
printf 'tmux %s\n' "$*" >> "$PI_START_LOG"
[ "$1" != has-session ]
""")
    tmux.chmod(0o755)
    herdr = fake_bin / "herdr"
    herdr.write_text("#!/bin/sh\nprintf '%s\\n' '{\"sessions\":[]}'\n")
    herdr.chmod(0o755)
    launchers = {
        "pi-personal": "personal",
        "pi-personal-herdr": "personal-herdr",
        "pisec": "pisec",
        "pi-secretary": "secretary",
        "pidev": "pidev",
        "pi-restart": "restart",
    }
    for name, label in launchers.items():
        path = home_bin / name
        path.write_text(f"#!/bin/sh\nprintf '{label} %s\\n' \"$*\" >> \"$PI_START_LOG\"\n")
        path.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "PI_START_LOG": str(log),
    })
    for key in ("TMUX", "PI_TMUX_LAYOUT", "DISPLAY", "WAYLAND_DISPLAY"):
        env.pop(key, None)
    return home, fake_bin, home_bin, log, env


class PiRestartTests(unittest.TestCase):
    def test_pi_start_all_defaults_to_desktop_tmux_and_starts_both(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, log, env = make_start_fixture(root)
            result = subprocess.run(
                [str(ROOT / "bin/pi-start"), "all"], env=env,
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text().splitlines()
            self.assertIn("personal --ensure", calls)
            self.assertIn("pisec ", calls)
            self.assertFalse(any(line.startswith("personal-herdr ") for line in calls))
            self.assertFalse(any(line.startswith("secretary ") for line in calls))

    def test_pi_start_all_mobile_uses_tmux_for_personal_and_secretary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, log, env = make_start_fixture(root)
            result = subprocess.run(
                [str(ROOT / "bin/pi-start"), "all", "-mobile"], env=env,
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text().splitlines()
            self.assertIn("personal --ensure --mobile", calls)
            self.assertIn("pisec --mobile", calls)
            self.assertFalse(any(line.startswith("personal-herdr ") for line in calls))

    def test_pi_start_clears_controller_scope_from_process_and_tmux(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, fake_bin, home_bin, log, env = make_start_fixture(root)
            (home_bin / "pi-personal").write_text("""#!/bin/sh
printf 'personal project=%s runtime=%s args=%s\n' "${PI_SYSTEM_PROJECT_ID-unset}" "${PI_RUNTIME_CAPABILITY-unset}" "$*" >> "$PI_START_LOG"
""")
            (home_bin / "pi-personal").chmod(0o755)
            tmux = fake_bin / "tmux"
            tmux.write_text("""#!/bin/sh
printf 'tmux %s\n' "$*" >> "$PI_START_LOG"
if [ "$1" = show-environment ]; then
  printf '%s\n' 'PI_SYSTEM_PROJECT_ID=prj_stale' 'PI_RUNTIME_CAPABILITY=stale'
  exit 0
fi
[ "$1" != has-session ]
""")
            tmux.chmod(0o755)
            env.update({"PI_SYSTEM_PROJECT_ID": "prj_" + "1" * 32, "PI_SYSTEM_STATE_ROOT": "/stale", "PI_RUNTIME_CAPABILITY": "stale"})
            result = subprocess.run([str(ROOT / "bin/pi-start"), "personal"], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text().splitlines()
            self.assertIn("personal project=unset runtime=unset args=--ensure", calls)
            self.assertIn("tmux set-environment -gu PI_SYSTEM_PROJECT_ID", calls)
            self.assertIn("tmux set-environment -gu PI_RUNTIME_CAPABILITY", calls)
            self.assertLess(calls.index("tmux set-environment -gu PI_SYSTEM_PROJECT_ID"), calls.index("tmux new-session -d -s main -n shell -c " + str(root / "home") + " exec bash"))

    def test_pi_start_fails_closed_when_tmux_scope_cannot_be_cleared(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, fake_bin, _, log, env = make_start_fixture(root)
            tmux = fake_bin / "tmux"
            tmux.write_text("""#!/bin/sh
printf 'tmux %s\n' "$*" >> "$PI_START_LOG"
if [ "$1" = show-environment ]; then printf '%s\n' 'PI_SYSTEM_PROJECT_ID=prj_stale'; exit 0; fi
if [ "$1" = set-environment ]; then exit 1; fi
[ "$1" != has-session ]
""")
            tmux.chmod(0o755)
            result = subprocess.run([str(ROOT / "bin/pi-start"), "personal"], env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot clear stale tmux controller scope", result.stderr)
            self.assertFalse(any(line.startswith("personal ") for line in log.read_text().splitlines()))

    def test_pi_start_all_mobile_herdr_starts_two_named_surfaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, log, env = make_start_fixture(root)
            env["PI_START_NO_ATTACH"] = "1"
            result = subprocess.run(
                [str(ROOT / "bin/pi-start"), "-mobile", "-herdr", "all"], env=env,
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text().splitlines()
            self.assertIn("personal-herdr --mobile --no-attach", calls)
            self.assertIn("secretary --herdr --no-attach", calls)
            self.assertFalse(any(line.startswith("personal ") for line in calls))
            self.assertFalse(any(line.startswith("pisec ") for line in calls))
            self.assertFalse(any(line.startswith("tmux new-session") for line in calls))

    def test_flags_are_per_invocation_not_persisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, log, env = make_start_fixture(root)
            first_env = env.copy()
            first_env["PI_START_NO_ATTACH"] = "1"
            first = subprocess.run(
                [str(ROOT / "bin/pi-start"), "all", "-mobile", "-herdr"],
                env=first_env, text=True, capture_output=True,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            log.write_text("")
            second = subprocess.run(
                [str(ROOT / "bin/pi-start"), "all"], env=env,
                text=True, capture_output=True,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            calls = log.read_text().splitlines()
            self.assertIn("personal --ensure", calls)
            self.assertIn("pisec ", calls)
            self.assertFalse(any("--mobile" in line or "herdr" in line for line in calls
                                 if not line.startswith("tmux ")))

    def test_pi_start_all_rebuilds_instead_of_overlaying_cross_backend_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, fake_bin, _, log, env = make_start_fixture(root)
            tmux = fake_bin / "tmux"
            tmux.write_text("""#!/bin/sh
printf 'tmux %s\n' "$*" >> "$PI_START_LOG"
if [ "$1" = has-session ] && [ "$3" = =pi-personal ]; then exit 0; fi
exit 1
""")
            tmux.chmod(0o755)
            result = subprocess.run(
                [str(ROOT / "bin/pi-start"), "all", "-herdr"], env=env,
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("rebuilding the managed grid", result.stderr)
            self.assertIn("restart -herdr", log.read_text().splitlines())

    def test_pi_start_all_rebuilds_a_different_tmux_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, fake_bin, _, log, env = make_start_fixture(root)
            tmux = fake_bin / "tmux"
            tmux.write_text("""#!/bin/sh
printf 'tmux %s\n' "$*" >> "$PI_START_LOG"
if [ "$1" = has-session ] && [ "$3" = =pi-personal ]; then exit 0; fi
if [ "$1" = list-windows ] && [ "$3" = =pi-personal ]; then
  printf 'personal-1\t2\npersonal-2\t2\n'
  exit 0
fi
exit 1
""")
            tmux.chmod(0o755)
            result = subprocess.run(
                [str(ROOT / "bin/pi-start"), "all", "-mobile"], env=env,
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("tmux personal is desktop, not mobile", result.stderr)
            self.assertIn("restart -mobile", log.read_text().splitlines())

    def test_pi_start_all_rebuilds_a_different_herdr_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, fake_bin, _, log, env = make_start_fixture(root)
            herdr = fake_bin / "herdr"
            herdr.write_text("""#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if args == ['session', 'list', '--json']:
    print(json.dumps({'sessions': [{'name': 'pi-personal', 'running': True}]}))
elif args == ['--session', 'pi-personal', 'workspace', 'list']:
    print(json.dumps({'result': {'workspaces': [
      {'label': 'personal/personal-1'}, {'label': 'personal/personal-2'},
    ]}}))
else:
    raise SystemExit(2)
""")
            herdr.chmod(0o755)
            result = subprocess.run(
                [str(ROOT / "bin/pi-start"), "all", "-mobile", "-herdr"],
                env=env, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Herdr personal is desktop, not mobile", result.stderr)
            self.assertIn("restart -mobile -herdr", log.read_text().splitlines())

    def test_pi_start_all_keeps_a_matching_tmux_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, fake_bin, _, log, env = make_start_fixture(root)
            tmux = fake_bin / "tmux"
            tmux.write_text("""#!/bin/sh
printf 'tmux %s\n' "$*" >> "$PI_START_LOG"
if [ "$1" = has-session ] && { [ "$3" = =pi-personal ] || [ "$3" = =main ]; }; then exit 0; fi
if [ "$1" = list-windows ] && [ "$3" = =pi-personal ]; then
  printf 'personal-1\t1\npersonal-2\t1\npersonal-3\t1\npersonal-4\t1\n'
  exit 0
fi
exit 1
""")
            tmux.chmod(0o755)
            result = subprocess.run(
                [str(ROOT / "bin/pi-start"), "all", "-mobile"], env=env,
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text().splitlines()
            self.assertNotIn("restart -mobile", calls)
            self.assertIn("personal --ensure --mobile", calls)
            self.assertIn("pisec --mobile", calls)

    def test_restart_defaults_to_tmux_desktop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"; fake_bin = root / "bin"; home_bin = home / ".local/bin"
            home_bin.mkdir(parents=True); fake_bin.mkdir()
            log = root / "calls.log"
            tmux = fake_bin / "tmux"
            tmux.write_text("#!/bin/sh\nprintf 'tmux %s\\n' \"$*\" >> \"$PI_RESTART_LOG\"\n")
            tmux.chmod(0o755)
            pi_start = home_bin / "pi-start"
            pi_start.write_text("#!/bin/sh\nprintf 'pi-start %s\\n' \"$*\" >> \"$PI_RESTART_LOG\"\n")
            pi_start.chmod(0o755)
            env = os.environ.copy()
            env.update({"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin",
                        "PI_RESTART_LOG": str(log)})
            env.pop("TMUX", None)
            result = subprocess.run([str(ROOT / "bin/pi-restart")], env=env,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("backend=tmux layout=desktop", result.stderr)
            self.assertEqual(log.read_text().splitlines(), ["tmux kill-server", "pi-start all"])

    def test_restart_does_not_forward_controller_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"; fake_bin = root / "bin"; home_bin = home / ".local/bin"
            home_bin.mkdir(parents=True); fake_bin.mkdir()
            log = root / "calls.log"
            tmux = fake_bin / "tmux"
            tmux.write_text("#!/bin/sh\nprintf 'tmux %s\\n' \"$*\" >> \"$PI_RESTART_LOG\"\n")
            tmux.chmod(0o755)
            pi_start = home_bin / "pi-start"
            pi_start.write_text("""#!/bin/sh
printf 'pi-start project=%s state=%s runtime=%s args=%s\n' "${PI_SYSTEM_PROJECT_ID-unset}" "${PI_SYSTEM_STATE_ROOT-unset}" "${PI_RUNTIME_CAPABILITY-unset}" "$*" >> "$PI_RESTART_LOG"
""")
            pi_start.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin", "PI_RESTART_LOG": str(log),
                "PI_SYSTEM_PROJECT_ID": "prj_" + "1" * 32, "PI_SYSTEM_STATE_ROOT": "/stale",
                "PI_RUNTIME_CAPABILITY": "stale",
            })
            env.pop("TMUX", None)
            result = subprocess.run([str(ROOT / "bin/pi-restart"), "--no-attach"], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("pi-start project=unset state=unset runtime=unset args=all", log.read_text().splitlines())

    def test_restart_stops_both_herdr_sessions_and_forwards_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"; fake_bin = root / "bin"; home_bin = home / ".local/bin"
            home_bin.mkdir(parents=True); fake_bin.mkdir()
            log = root / "calls.log"
            state = root / "sessions.json"
            state.write_text(json.dumps(["pi-personal", "pi-secretary"]))
            tmux = fake_bin / "tmux"
            tmux.write_text("#!/bin/sh\nprintf 'tmux %s\\n' \"$*\" >> \"$PI_RESTART_LOG\"\n")
            tmux.chmod(0o755)
            herdr = fake_bin / "herdr"
            herdr.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
state = pathlib.Path(os.environ['PI_HERDR_STATE'])
names = json.loads(state.read_text())
args = sys.argv[1:]
if args == ['session', 'list', '--json']:
    print(json.dumps({'sessions': [{'name': name, 'running': True} for name in names]}))
elif len(args) == 4 and args[:2] == ['session', 'stop'] and args[3] == '--json':
    name = args[2]
    names.remove(name)
    state.write_text(json.dumps(names))
    with pathlib.Path(os.environ['PI_RESTART_LOG']).open('a') as stream: stream.write('herdr stop ' + name + '\\n')
else:
    raise SystemExit(2)
""")
            herdr.chmod(0o755)
            pi_start = home_bin / "pi-start"
            pi_start.write_text("#!/bin/sh\nprintf 'pi-start %s\\n' \"$*\" >> \"$PI_RESTART_LOG\"\n")
            pi_start.chmod(0o755)
            env = os.environ.copy()
            env.update({"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin",
                        "PI_RESTART_LOG": str(log), "PI_HERDR_STATE": str(state)})
            env.pop("TMUX", None)
            result = subprocess.run(
                [str(ROOT / "bin/pi-restart"), "-mobile", "-herdr"], env=env,
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("backend=herdr layout=mobile", result.stderr)
            self.assertEqual(log.read_text().splitlines(), [
                "herdr stop pi-personal", "herdr stop pi-secretary",
                "tmux kill-server", "pi-start all -mobile -herdr",
            ])

    def test_internal_handoff_clears_surface_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"; fake_bin = root / "bin"; home_bin = home / ".local/bin"
            home_bin.mkdir(parents=True); fake_bin.mkdir()
            log = root / "calls.log"
            state = root / "sessions.json"
            state.write_text(json.dumps(["pi-personal"]))
            tmux = fake_bin / "tmux"
            tmux.write_text("#!/bin/sh\nprintf 'tmux %s\\n' \"$*\" >> \"$PI_RESTART_LOG\"\n")
            tmux.chmod(0o755)
            herdr = fake_bin / "herdr"
            herdr.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
state = pathlib.Path(os.environ['PI_HERDR_STATE'])
names = json.loads(state.read_text())
args = sys.argv[1:]
if args == ['session', 'list', '--json']:
 print(json.dumps({'sessions': [{'name': n, 'running': True} for n in names]}))
elif args[:2] == ['session', 'stop']:
 names.remove(args[2]); state.write_text(json.dumps(names))
else: raise SystemExit(2)
""")
            herdr.chmod(0o755)
            pi_start = home_bin / "pi-start"
            pi_start.write_text("""#!/bin/sh
printf 'pi-start personal=%s secretary=%s args=%s\n' "${PI_PERSONAL_BACKEND-unset}" "${PI_SECRETARY_BACKEND-unset}" "$*" >> "$PI_RESTART_LOG"
""")
            pi_start.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin",
                "PI_RESTART_LOG": str(log), "PI_HERDR_STATE": str(state),
                "PI_RESTART_HANDOFF": "1", "PI_PERSONAL_BACKEND": "herdr",
                "PI_SECRETARY_BACKEND": "herdr", "HERDR_ENV": "1",
                "HERDR_SESSION": "pi-personal", "HERDR_WORKSPACE_ID": "w1",
                "TMUX": f"/tmp/tmux-{os.getuid()}/pi-restart-test,1,0",
            })
            result = subprocess.run(
                [str(ROOT / "bin/pi-restart"), "--internal-handoff", "-herdr"],
                env=env, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("pi-start personal=unset secretary=unset args=all -herdr",
                          log.read_text().splitlines())


if __name__ == "__main__":
    unittest.main()
