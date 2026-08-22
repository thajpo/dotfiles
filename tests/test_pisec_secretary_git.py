import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import scripts.pisec.secretary_git as secretary_git_module
from scripts.pisec.models import ConflictError, InvalidRequestError, NeedsAttentionError, ScopeMismatchError
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import register_project
from scripts.pisec.secretary_git import apply_workstream_merge, git_status, inspect_workstream_changes, prepare_workstream_merge, push_branch
from scripts.pisec.workflow import submit_completion
from scripts.pisec.secretary import ensure_secretary
from scripts.pisec.workstreams import authorize_apply_workstream, complete_workstream, prepare_workstream
from tests.pisec_fixture import FixtureGitObjects, FixtureHarness, FixtureWorkspace


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
        ensure_secretary(store, project["project_id"], harness, workspace)
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
        authorize_apply_workstream(
            store,
            scope=scope,
            harness=harness,
            workspace=workspace,
            git_objects=FixtureGitObjects(),
        )
        workstream = store.conn.execute("SELECT worktree_path FROM workstreams WHERE workstream_id=?", (scope["workstreamId"],)).fetchone()
        worktree = Path(workstream["worktree_path"])
        git(worktree, "config", "user.name", "Pisec Worker")
        git(worktree, "config", "user.email", "worker@example.invalid")
        common_objects = Path(project["git_common_dir"]) / "objects"
        private_objects = Path(scope["privateGitObjectDir"])
        for path in (private_objects, private_objects / "info", private_objects / "pack"):
            path.chmod(0o700)
        (private_objects / "info" / "alternates").chmod(0o600)
        (worktree / "feature.txt").write_text("implemented\n")
        git_with_objects(worktree, private_objects, common_objects, "add", "feature.txt")
        git_with_objects(worktree, private_objects, common_objects, "commit", "-qm", "implement feature")
        source_commit = git_with_objects(worktree, private_objects, common_objects, "rev-parse", "HEAD").lower()
        binding = store.conn.execute("SELECT runtime_instance_id FROM runtime_bindings WHERE workstream_id=?", (scope["workstreamId"],)).fetchone()
        task_packet = store.conn.execute("SELECT packet_sha256 FROM task_packets WHERE workstream_id=?", (scope["workstreamId"],)).fetchone()
        completion = submit_completion(
            store,
            workstream_id=scope["workstreamId"],
            runtime_instance_id=binding["runtime_instance_id"],
            packet={
                "acceptance": [{"criterion": "Fast-forward merge succeeds.", "status": "passed", "evidence": ["Fixture commit."]}],
                "verification": [{"command": "fixture verification", "result": "passed"}],
                "sourceCommit": source_commit,
                "taskPacketSha256": task_packet["packet_sha256"],
                "changedSurfaces": ["fixture"],
                "residualRisk": "none",
            },
        )
        complete_workstream(store, project["project_id"], scope["workstreamId"], completion["packet_sha256"], workspace)
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
            with self.assertRaises(InvalidRequestError):
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

    def push_fixture(self, root: Path):
        remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", "--initial-branch=main", str(remote)], check=True)
        repo = root / "push-repo"
        make_repo(repo)
        git(repo, "config", "--local", "remote.origin.url", str(remote))
        git(remote, "fetch", "-q", str(repo), "refs/heads/main:refs/heads/main")
        git(repo, "switch", "-q", "-c", "research/topic")
        (repo / "research.txt").write_text("published base\n")
        git(repo, "add", "research.txt")
        git(repo, "commit", "-qm", "published branch")
        git(remote, "fetch", "-q", str(repo), "refs/heads/research/topic:refs/heads/research/topic")
        remote_oid = git(repo, "rev-parse", "HEAD")
        for index in range(3):
            (repo / "research.txt").write_text(f"local {index}\n")
            git(repo, "add", "research.txt")
            git(repo, "commit", "-qm", f"local {index}")
        local_oid = git(repo, "rev-parse", "HEAD")
        store = PiStore(root / "push-state")
        self.addCleanup(store.close)
        project = register_project(store, repo, default_ref="main")
        return store, project, repo, remote, remote_oid, local_oid

    def test_existing_non_default_branch_pushes_fast_forward_and_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            store, project, repo, remote, remote_oid, local_oid = self.push_fixture(root)
            real_run_git = secretary_git_module._run_git
            pushed_commands = []

            def simulate_push(path, *args, **kwargs):
                if args[0] != "push":
                    return real_run_git(path, *args, **kwargs)
                pushed_commands.append(args)
                remote_path = Path(args[-2])
                target_ref = args[-1].split(":", 1)[1]
                real_run_git(
                    remote_path,
                    "fetch",
                    "--quiet",
                    str(repo),
                    f"refs/heads/research/topic:{target_ref}",
                )
                return 0, "simulated push"

            with patch.dict(os.environ, {"HOME": str(home)}), patch.object(secretary_git_module, "_run_git", side_effect=simulate_push):
                result = push_branch(
                    store,
                    project["project_id"],
                    branch="research/topic",
                    expected_local_oid=local_oid,
                    expected_remote_oid=remote_oid,
                )
                replay = push_branch(
                    store,
                    project["project_id"],
                    branch="research/topic",
                    expected_local_oid=local_oid,
                    expected_remote_oid=remote_oid,
                )
            self.assertEqual(len(pushed_commands), 1)
            self.assertIn(f"--force-with-lease=refs/heads/research/topic:{remote_oid}", pushed_commands[0])
            self.assertTrue(result["pushed"])
            self.assertFalse(result["reused"])
            self.assertTrue(replay["reused"])
            self.assertEqual(git(remote, "rev-parse", "refs/heads/research/topic"), local_oid)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='project.git_pushed'").fetchone()[0], 1)

    def test_autonomous_push_refuses_default_branch_and_origin_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            store, project, repo, _remote, remote_oid, local_oid = self.push_fixture(root)
            main_oid = git(repo, "rev-parse", "refs/heads/main")
            with patch.dict(os.environ, {"HOME": str(home)}):
                with self.assertRaisesRegex(InvalidRequestError, "default branch"):
                    push_branch(
                        store,
                        project["project_id"],
                        branch="main",
                        expected_local_oid=main_oid,
                        expected_remote_oid=main_oid,
                    )
                other_remote = root / "other.git"
                subprocess.run(["git", "init", "--bare", "-q", "--initial-branch=main", str(other_remote)], check=True)
                git(repo, "config", "--local", "remote.origin.url", str(other_remote))
                with self.assertRaisesRegex(NeedsAttentionError, "origin remote drifted"):
                    push_branch(
                        store,
                        project["project_id"],
                        branch="research/topic",
                        expected_local_oid=local_oid,
                        expected_remote_oid=remote_oid,
                    )

    def test_autonomous_push_refuses_non_fast_forward_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            store, project, _repo, remote, _remote_oid, local_oid = self.push_fixture(root)
            competing = root / "competing"
            subprocess.run(["git", "clone", "-q", str(remote), str(competing)], check=True)
            git(competing, "config", "user.name", "Competing Writer")
            git(competing, "config", "user.email", "competing@example.invalid")
            git(competing, "switch", "-q", "research/topic")
            (competing / "remote.txt").write_text("remote advance\n")
            git(competing, "add", "remote.txt")
            git(competing, "commit", "-qm", "remote advance")
            advanced_remote_oid = git(competing, "rev-parse", "HEAD")
            git(remote, "fetch", "-q", str(competing), "refs/heads/research/topic:refs/heads/research/topic")
            with patch.dict(os.environ, {"HOME": str(home)}):
                with self.assertRaisesRegex(ConflictError, "not a fast-forward"):
                    push_branch(
                        store,
                        project["project_id"],
                        branch="research/topic",
                        expected_local_oid=local_oid,
                        expected_remote_oid=advanced_remote_oid,
                    )

if __name__ == "__main__":
    unittest.main()
