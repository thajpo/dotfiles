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
    target.parent.mkdir(parents=True)
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


class WorkspacePreparationTests(unittest.TestCase):
    def prepare_home(self, root: Path, branch="main"):
        home = root / "home"
        home.mkdir()
        repo = make_repo(root / "source", branch)
        write_policy(home, repo, control=True)
        return home, repo

    def test_protected_dirty_checkout_is_refused_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, repo = self.prepare_home(Path(tmp))
            (repo / "untracked.txt").write_text("human work\n")
            before = git(repo, "status", "--porcelain=v1", "--untracked-files=all")
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                with self.assertRaises(ws.WorkspaceError):
                    ws.prepare(repo, os.getpid())
            self.assertEqual(git(repo, "status", "--porcelain=v1", "--untracked-files=all"), before)
            self.assertFalse((home / ".local/share/pi/worktrees/repo").exists())

    def test_protected_clean_checkout_creates_linked_task_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, repo = self.prepare_home(Path(tmp))
            original_head = git(repo, "rev-parse", "HEAD")
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                prepared = ws.prepare(repo, os.getpid())
            route = json.loads(Path(prepared["route"]).read_text())
            task = Path(prepared["worktree"])
            self.assertNotEqual(task, repo)
            self.assertTrue(str(task).startswith(str(home / ".local/share/pi/worktrees")))
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
            self.assertEqual(stat.S_IMODE(route_path.stat().st_mode), 0o600)
            self.assertEqual(route["capabilityHash"], hashlib.sha256(prepared["capability"].encode()).hexdigest())
            self.assertNotIn(prepared["capability"], route_path.read_text())
            context = Path(route["hostContext"]).read_text()
            self.assertNotIn("TOP_SECRET_SENTINEL", context)
            self.assertNotIn("do-not-copy", context)
            self.assertIn("Workspace mode: trusted-live", context)


if __name__ == "__main__":
    unittest.main()
