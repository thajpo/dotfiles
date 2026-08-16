import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.pisec.models import ConflictError, ScopeMismatchError
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import register_project
from scripts.pisec.secretary_git import apply_workstream_merge, git_status, inspect_workstream_changes, prepare_workstream_merge
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace
from scripts.pisec.workstreams import prepare_workstream


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, text=True, capture_output=True).stdout.strip()


def git_with_objects(path: Path, private: Path, common: Path, *args: str) -> str:
    environment = os.environ.copy()
    environment.update({
        "GIT_OBJECT_DIRECTORY": str(private),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(common),
    })
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        text=True,
        capture_output=True,
        env=environment,
    ).stdout.strip()


def make_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    path.chmod(0o755)
    git(path, "config", "user.name", "Pisec Test")
    (path / ".git" / "objects").chmod(0o700)
    (path / ".git" / "objects" / "pack").chmod(0o700)
    git(path, "config", "user.email", "pisec@example.invalid")
    (path / "README").write_text("base\n")
    git(path, "add", "README")
    git(path, "commit", "-qm", "base")


class SecretaryGitTests(unittest.TestCase):
    def fixture(self, root: Path):
        repo = root / "repo"
        make_repo(repo)
        store = PiStore(root / "state")
        self.addCleanup(store.close)
        harness = FixtureHarness(root)
        workspace = FixtureWorkspace(root, store)
        project = register_project(store, repo, default_ref="main")
        proposal = prepare_workstream(
            store,
            project_id=project["project_id"],
            title="Worker",
            purpose="Exercise merge",
            brief="Change the fixture",
            task_packet={"schemaVersion": 1, "outcome": "The worker change is merged.", "boundaries": ["Change the fixture only."], "acceptance": ["Fast-forward merge succeeds."], "openQuestions": [], "evidence": ["Fixture commit."]},
            idempotency_key="secretary-git",
            target_ref="main",
            harness=harness,
            workspace=workspace,
            work_root=root / "worktrees",
            object_root=root / "objects",
        )
        scope = json.loads(store.conn.execute("SELECT result_json FROM operations WHERE workstream_id=?", (proposal["workstream"]["workstream_id"],)).fetchone()[0])
        worktree = Path(scope["worktreePath"])
        git(repo, "worktree", "add", "-q", "-b", scope["branchName"], str(worktree), scope["baseCommitOid"])
        git(worktree, "config", "user.name", "Pisec Worker")
        git(worktree, "config", "user.email", "worker@example.invalid")
        common_objects = Path(project["git_common_dir"]) / "objects"
        private_objects = Path(scope["privateGitObjectDir"])
        (private_objects / "info").mkdir(parents=True)
        (private_objects / "pack").mkdir()
        for path in (private_objects, private_objects / "info", private_objects / "pack"):
            path.chmod(0o700)
        (private_objects / "info" / "alternates").write_text(str(common_objects) + "\n")
        (private_objects / "info" / "alternates").chmod(0o600)
        (worktree / "feature.txt").write_text("implemented\n")
        git_with_objects(worktree, private_objects, common_objects, "add", "feature.txt")
        git_with_objects(worktree, private_objects, common_objects, "commit", "-qm", "implement feature")
        return store, project, scope, repo, worktree, private_objects

    def test_inspection_and_exact_fast_forward_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, project, scope, repo, _worktree, private_objects = self.fixture(Path(tmp))
            status = git_status(store, project["project_id"])
            self.assertEqual(status["branch"], "main")
            self.assertTrue(status["clean"])
            hidden_source = subprocess.run(
                ["git", "-C", str(repo), "cat-file", "-e", f"{scope['branchName']}^{{commit}}"],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(hidden_source.returncode, 0)

            changes = inspect_workstream_changes(store, project["project_id"], scope["workstreamId"])
            self.assertTrue(changes["fastForwardReady"])
            self.assertIn("implement feature", changes["commits"])
            self.assertIn("feature.txt", changes["diffStat"])
            self.assertIn("+implemented", changes["patch"])

            approval_scope = prepare_workstream_merge(store, project["project_id"], scope["workstreamId"])
            self.assertEqual(approval_scope["strategy"], "ff-only")
            result = apply_workstream_merge(store, project["project_id"], approval_scope)
            self.assertTrue(result["merged"])
            self.assertFalse(result["reused"])
            self.assertEqual(git(repo, "rev-parse", "HEAD"), approval_scope["sourceCommitOid"])
            git(repo, "cat-file", "-e", f"{approval_scope['sourceCommitOid']}^{{commit}}")
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='project.git_merged'").fetchone()[0], 1)

            replay = apply_workstream_merge(store, project["project_id"], approval_scope)
            self.assertTrue(replay["reused"])
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='project.git_merged'").fetchone()[0], 1)
            private_objects.rename(private_objects.with_name("objects-disabled"))
            self.assertEqual(git(repo, "status", "--short"), "")
            self.assertEqual(git(repo, "show", "-s", "--format=%H", "HEAD"), approval_scope["sourceCommitOid"])

    def test_merge_refuses_dirty_target_and_stale_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, project, scope, repo, _worktree, _private_objects = self.fixture(Path(tmp))
            approval_scope = prepare_workstream_merge(store, project["project_id"], scope["workstreamId"])
            (repo / "dirty.txt").write_text("dirty\n")
            with self.assertRaisesRegex(ConflictError, "dirty"):
                apply_workstream_merge(store, project["project_id"], approval_scope)
            (repo / "dirty.txt").unlink()

            stale = dict(approval_scope)
            stale["sourceCommitOid"] = "0" * 40
            with self.assertRaises(ScopeMismatchError):
                apply_workstream_merge(store, project["project_id"], stale)
            self.assertEqual(git(repo, "rev-parse", "HEAD"), approval_scope["targetCommitOid"])

    def test_merge_refuses_non_fast_forward_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, project, scope, repo, _worktree, _private_objects = self.fixture(Path(tmp))
            (repo / "target.txt").write_text("advanced\n")
            git(repo, "add", "target.txt")
            git(repo, "commit", "-qm", "advance target")
            with self.assertRaisesRegex(ConflictError, "fast-forward"):
                prepare_workstream_merge(store, project["project_id"], scope["workstreamId"])


if __name__ == "__main__":
    unittest.main()
