import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pi_workspace", ROOT / "scripts/pi-workspace.py")
ws = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(ws)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def make_repo(root: Path, branch: str = "main") -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", branch)
    git(repo, "config", "user.name", "Pi Test")
    git(repo, "config", "user.email", "pi-test@example.invalid")
    (repo / "tracked.txt").write_text("start\n")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "initial")
    return repo.resolve()


def write_policy(home: Path, repo: Path, *, trusted=True, isolated=False, control=False) -> Path:
    policy = {
        "version": 1,
        "defaultMode": "isolated",
        "trustedRoots": [str(repo)] if trusted else [],
        "isolatedRoots": [str(repo)] if isolated else [],
        "controlPlaneRepositories": [str(repo)] if control else [],
        "protectedBranches": ["main", "master"],
        "worktreeRoot": str(home / ".local/share/pi/worktrees"),
    }
    target = home / ".config/pi/repository-policy.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(policy))
    target.chmod(0o600)
    return target


class WorkspacePolicyTests(unittest.TestCase):
    def test_routing_precedence_and_unknown_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            authored_root = root / "authored"
            external_root = root / "external"
            authored_root.mkdir()
            external_root.mkdir()
            authored = make_repo(authored_root)
            external = make_repo(external_root)
            control = make_repo(root / "control")
            unknown = make_repo(root / "unknown")
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps({
                "version": 1,
                "defaultMode": "isolated",
                "trustedRoots": [str(authored_root)],
                "isolatedRoots": [str(external_root)],
                "controlPlaneRepositories": [str(control)],
                "protectedBranches": ["main", "master"],
                "worktreeRoot": str(root / "worktrees"),
            }))
            policy_path.chmod(0o600)
            policy = ws.load_policy(policy_path, fail_closed=False)
            self.assertEqual(ws.classify(authored, policy), ("trusted-live", False))
            self.assertEqual(ws.classify(external, policy), ("isolated", False))
            self.assertEqual(ws.classify(control, policy), ("trusted-live", True))
            self.assertEqual(ws.classify(unknown, policy), ("isolated", False))

    def test_symlink_classification_uses_real_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = make_repo(root)
            alias = root / "alias"
            alias.symlink_to(repo, target_is_directory=True)
            policy_path = write_policy(root / "home", repo)
            policy = ws.load_policy(policy_path, fail_closed=False)
            self.assertEqual(ws.classify(alias.resolve(), policy)[0], "trusted-live")

    def test_missing_malformed_or_unsafe_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = ws.load_policy(root / "missing.json")
            self.assertFalse(missing["policyValid"])
            self.assertEqual(missing["defaultMode"], "isolated")
            malformed = root / "malformed.json"
            malformed.write_text("{")
            malformed.chmod(0o600)
            self.assertFalse(ws.load_policy(malformed)["policyValid"])
            malformed.write_text("{}")
            malformed.chmod(0o644)
            self.assertFalse(ws.load_policy(malformed)["policyValid"])

    def test_repository_local_policy_cannot_broaden_trust(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = make_repo(root)
            (repo / ".pi").mkdir()
            (repo / ".pi/repository-policy.json").write_text(json.dumps({"defaultMode": "trusted-live"}))
            policy = ws.load_policy(root / "does-not-exist")
            self.assertEqual(ws.classify(repo, policy)[0], "isolated")


class HostContextTests(unittest.TestCase):
    def test_gpu_scan_uses_complete_lspci_output(self):
        output = "00:00.0 Host bridge: Example\n03:00.0 VGA compatible controller: Example GPU\n"
        result = subprocess.CompletedProcess(["lspci"], 0, stdout=output)
        with mock.patch.object(ws.subprocess, "run", return_value=result):
            self.assertEqual(ws.gpu_description(), "03:00.0 VGA compatible controller: Example GPU")


class WorkspacePreparationTests(unittest.TestCase):
    def prepare_home(self, root: Path, branch="main"):
        home = root / "home"
        home.mkdir()
        repo = make_repo(root / "source", branch)
        write_policy(home, repo, control=True)
        return home, repo

    def test_read_only_prepare_uses_dirty_protected_checkout_without_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, repo = self.prepare_home(Path(tmp))
            (repo / "human.txt").write_text("dirty\n")
            before = git(repo, "status", "--porcelain=v1", "--untracked-files=all")
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                prepared = ws.prepare(repo, os.getpid(), read_only=True)
            self.assertEqual(Path(prepared["worktree"]), repo)
            self.assertTrue(prepared["readOnly"])
            self.assertEqual(git(repo, "status", "--porcelain=v1", "--untracked-files=all"), before)
            self.assertFalse((home / ".local/share/pi/worktrees/repo").exists())

    def test_protected_dirty_checkout_is_refused_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, repo = self.prepare_home(Path(tmp))
            # This is an ordinary trusted repository, not the harness control
            # plane. Dirty protected checkouts must still fail closed.
            write_policy(home, repo, control=False)
            (repo / "untracked.txt").write_text("human work\n")
            before = git(repo, "status", "--porcelain=v1", "--untracked-files=all")
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                with self.assertRaises(ws.WorkspaceError):
                    ws.prepare(repo, os.getpid())
            self.assertEqual(git(repo, "status", "--porcelain=v1", "--untracked-files=all"), before)
            self.assertFalse((home / ".local/share/pi/worktrees/repo").exists())

    def test_dirty_control_plane_checkout_stays_live_for_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, repo = self.prepare_home(Path(tmp))
            (repo / "launcher-fix.txt").write_text("in progress\n")
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                prepared = ws.prepare(repo, os.getpid())
            self.assertEqual(Path(prepared["worktree"]), repo)
            self.assertEqual(prepared["mode"], "trusted-live")
            self.assertTrue(prepared["controlPlane"])
            self.assertEqual(git(repo, "status", "--porcelain=v1", "--untracked-files=all"), "?? launcher-fix.txt")

    def test_protected_clean_checkout_creates_linked_task_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, repo = self.prepare_home(Path(tmp))
            original_head = git(repo, "rev-parse", "HEAD")
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                prepared = ws.prepare(repo, os.getpid())
            route = json.loads(Path(prepared["route"]).read_text())
            task = Path(prepared["worktree"])
            self.assertNotEqual(task, repo)
            self.assertTrue(task.is_relative_to((home / ".local/share/pi/worktrees").resolve()))
            self.assertRegex(route["branch"], r"^pi/")
            self.assertEqual(git(repo, "branch", "--show-current"), "main")
            self.assertEqual(git(repo, "rev-parse", "HEAD"), original_head)
            self.assertEqual(git(task, "rev-parse", "HEAD"), original_head)
            git(repo, "worktree", "remove", "--force", str(task))
            git(repo, "branch", "-D", route["branch"])

    def test_nonprotected_dirty_checkout_is_used_live_and_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, repo = self.prepare_home(Path(tmp), branch="feature")
            (repo / "tracked.txt").write_text("dirty\n")
            (repo / "untracked.txt").write_text("untracked\n")
            before = git(repo, "status", "--porcelain=v1", "--untracked-files=all")
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                prepared = ws.prepare(repo, os.getpid())
            self.assertEqual(Path(prepared["worktree"]), repo)
            self.assertEqual(git(repo, "status", "--porcelain=v1", "--untracked-files=all"), before)

    def test_route_permissions_capability_and_context_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, repo = self.prepare_home(Path(tmp), branch="feature")
            with mock.patch.dict(os.environ, {"HOME": str(home), "TOP_SECRET_SENTINEL": "do-not-copy"}, clear=False):
                prepared = ws.prepare(repo, os.getpid())
            route_path = Path(prepared["route"])
            route = json.loads(route_path.read_text())
            context_path = Path(route["hostContext"])
            git_config_path = Path(route["gitConfig"])
            self.assertEqual(stat.S_IMODE(route_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(context_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(git_config_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(context_path.parent.stat().st_mode), 0o700)
            self.assertEqual(git(git_config_path.parent, "config", "--file", str(git_config_path), "--get", "user.name"), "Pi Test")
            self.assertEqual(git(git_config_path.parent, "config", "--file", str(git_config_path), "--get", "user.email"), "pi-test@example.invalid")
            self.assertEqual(git(git_config_path.parent, "config", "--file", str(git_config_path), "--name-only", "--list"), "user.name\nuser.email")
            self.assertEqual(route["capabilityHash"], hashlib.sha256(prepared["capability"].encode()).hexdigest())
            self.assertNotIn(prepared["capability"], route_path.read_text())
            context = context_path.read_text()
            self.assertNotIn("TOP_SECRET_SENTINEL", context)
            self.assertNotIn("do-not-copy", context)
            self.assertIn("Workspace mode: trusted-live", context)

    def test_concurrent_tasks_keep_distinct_context_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, repo = self.prepare_home(Path(tmp), branch="feature")
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                first = ws.prepare(repo, os.getpid())
                first_route = json.loads(Path(first["route"]).read_text())
                first_path = Path(first_route["hostContext"])
                first_content = first_path.read_text()
                second = ws.prepare(repo, os.getpid())
            second_route = json.loads(Path(second["route"]).read_text())
            second_path = Path(second_route["hostContext"])
            self.assertNotEqual(first_path, second_path)
            self.assertNotEqual(Path(first_route["gitConfig"]), Path(second_route["gitConfig"]))
            self.assertEqual(first_path.read_text(), first_content)
            self.assertNotEqual(first_content, second_path.read_text())
            self.assertTrue(Path(first_route["gitConfig"]).is_file())
            self.assertTrue(Path(second_route["gitConfig"]).is_file())
            self.assertTrue((home / ".pi/agent/generated/HOST_CONTEXT.md").is_file())

    def test_missing_git_identity_fails_before_task_route_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, repo = self.prepare_home(root, branch="main")
            git(repo, "config", "--unset-all", "user.name")
            git(repo, "config", "--unset-all", "user.email")
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                with self.assertRaisesRegex(ws.WorkspaceError, "host Git identity is missing user.name"):
                    ws.prepare(repo, os.getpid())
            self.assertFalse((home / ".pi/agent/generated").exists())
            self.assertFalse((home / ".local/share/pi/runtime/pi-tasks").exists())
            self.assertFalse((home / ".local/share/pi/worktrees/repo").exists())

    def test_isolated_route_receives_minimal_git_identity_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            repo = make_repo(root / "source", branch="feature")
            write_policy(home, repo, trusted=False)
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                prepared = ws.prepare(repo, os.getpid())
            route = json.loads(Path(prepared["route"]).read_text())
            git_config_path = Path(route["gitConfig"])
            self.assertEqual(route["mode"], "isolated")
            self.assertEqual(stat.S_IMODE(git_config_path.stat().st_mode), 0o600)
            self.assertEqual(git(git_config_path.parent, "config", "--file", str(git_config_path), "--name-only", "--list"), "user.name\nuser.email")

    def test_generated_identity_supports_an_ordinary_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, repo = self.prepare_home(root, branch="feature")
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                prepared = ws.prepare(repo, os.getpid())
            route = json.loads(Path(prepared["route"]).read_text())
            git(repo, "config", "--unset-all", "user.name")
            git(repo, "config", "--unset-all", "user.email")
            (repo / "identity.txt").write_text("identity\n")
            git(repo, "add", "identity.txt")
            env = os.environ.copy()
            env.update({"HOME": str(home), "GIT_CONFIG_GLOBAL": route["gitConfig"], "GIT_CONFIG_NOSYSTEM": "1"})
            result = subprocess.run(["git", "commit", "-m", "identity"], cwd=repo, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            identity = git(repo, "show", "-s", "--format=%an%n%ae%n%cn%n%ce")
            self.assertEqual(identity.splitlines(), ["Pi Test", "pi-test@example.invalid", "Pi Test", "pi-test@example.invalid"])


if __name__ == "__main__":
    unittest.main()
