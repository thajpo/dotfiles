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
            self.assertEqual(invocation["args"], ["hello"])
            self.assertEqual(invocation["env"]["PI_TASK_MODE"], "trusted-live")
            self.assertTrue(Path(invocation["env"]["PI_TASK_ROUTE_FILE"]).is_file())
            self.assertNotIn("hijacked", result.stderr)

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
            self.assertEqual(json.loads(output.read_text())["args"], ["--", "--sandbox-target=current"])

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

    def test_pi_host_refuses_nested_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, _, _, env = setup_home(Path(tmp))
            env["PI_TASK_ROUTE_FILE"] = "/tmp/fake-route"
            result = subprocess.run([str(ROOT / "bin/pi-host")], cwd=home, env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing nested invocation", result.stderr)

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
