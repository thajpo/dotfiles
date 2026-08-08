"""Phase 3 working-copy inventory and observe-only reconciliation tests."""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from scripts.pi_control.locks import shared_observation_lock
from scripts.pi_control.project_policy import load_policy
from tests.control_plane.helpers import git_run
from scripts.pi_control.reconcile import inventory_working_copies, reconcile_observe_only, register_project
from scripts.pi_control.store import ControllerStore
from tests.control_plane.helpers import DisposableEnvironment, snapshot_git_repository


def policy_for(root: Path, default: str = "trusted"):
    return load_policy({
        "version": 1,
        "defaultMode": default,
        "trustedRoots": [str(root)] if default == "trusted" else [],
        "isolatedRoots": [str(root)] if default == "isolated" else [],
        "controlPlaneRepositories": [],
        "protectedBranches": ["main"],
        "worktreeRoot": str(root / "worktrees"),
    })


class WorkingCopyReconcileTests(unittest.TestCase):
    def test_linked_worktree_is_verified_and_unmanaged_observation_is_not_adopted(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            with ControllerStore(fixture.state_home / "pi-control") as store:
                project = register_project(store, fixture.repo, "fixture", policy=policy_for(fixture.root))
                inventory = inventory_working_copies(store, project["project_id"], policy=policy_for(fixture.root))
                self.assertEqual(len(inventory["worktrees"]), 2)
                linked = [item for item in inventory["worktrees"] if item["path"].endswith("worktrees/linked")][0]
                self.assertFalse(linked["managed"])
                self.assertFalse(linked["controller_owned"])
                self.assertTrue(linked["common_dir_matches"])
                self.assertEqual(linked["effective_mode"], "trusted-live")

    def test_dirty_and_missing_worktree_states_are_observations(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            with ControllerStore(fixture.state_home / "pi-control") as store:
                project = register_project(store, fixture.repo, "fixture", policy=policy_for(fixture.root))
                linked = fixture.worktree_root / "linked"
                (linked / "untracked.txt").write_text("dirty\n", encoding="utf-8")
                inventory = inventory_working_copies(store, project["project_id"], policy=policy_for(fixture.root))
                item = [row for row in inventory["worktrees"] if row["path"] == str(linked.resolve())][0]
                self.assertEqual(item["state"], "dirty")
                shutil.rmtree(linked)
                inventory = inventory_working_copies(store, project["project_id"], policy=policy_for(fixture.root))
                item = [row for row in inventory["worktrees"] if row["path"] == str(linked.resolve())][0]
                self.assertEqual(item["state"], "missing")
                self.assertIsNone(item["effective_mode"])

    def test_stored_head_tree_branch_anchors_classify_managed_drift(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            with ControllerStore(fixture.state_home / "pi-control") as store:
                project = register_project(store, fixture.repo, "fixture", policy=policy_for(fixture.root))
                (fixture.repo / "anchor-change.txt").write_text("changed\n", encoding="utf-8")
                git_run(["add", "anchor-change.txt"], cwd=fixture.repo, fixture_root=fixture.root)
                git_run(["commit", "-m", "advance"], cwd=fixture.repo, fixture_root=fixture.root)
                inventory = inventory_working_copies(store, project["project_id"], policy=policy_for(fixture.root))
                primary = next(row for row in inventory["worktrees"] if row["path"] == str(fixture.repo.resolve()))
                self.assertEqual(primary["state"], "drifted")
                self.assertFalse(primary["anchors_match"])
                reconcile_observe_only(store, project["project_id"], policy=policy_for(fixture.root))
                self.assertEqual(store.conn.execute("SELECT observed_state FROM projects WHERE project_id=?", (project["project_id"],)).fetchone()[0], "drifted")

    def test_observe_only_reconcile_persists_rows_and_events_but_does_not_touch_git(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            with ControllerStore(fixture.state_home / "pi-control") as store:
                project = register_project(store, fixture.repo, "fixture", policy=policy_for(fixture.root))
                before = snapshot_git_repository(fixture.repo)
                result = reconcile_observe_only(store, project["project_id"], policy=policy_for(fixture.root))
                after = snapshot_git_repository(fixture.repo)
                self.assertEqual(before, after)
                self.assertEqual(result["state"], "ready")
                self.assertGreaterEqual(store.conn.execute("SELECT count(*) FROM control_events").fetchone()[0], 2)
                self.assertEqual(store.conn.execute("SELECT observed_state FROM projects WHERE project_id=?", (project["project_id"],)).fetchone()[0], "ready")

    def test_observation_lock_is_shared_and_secure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            root.mkdir(mode=0o700)
            with shared_observation_lock(root, "project-test"):
                with shared_observation_lock(root, "project-test"):
                    self.assertTrue((root / "locks" / "project-test.lock").exists())

    def test_read_only_observation_lock_rejects_symlinked_lock_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            root.mkdir(mode=0o700)
            redirected = Path(temporary) / "redirected"
            redirected.mkdir(mode=0o700)
            (root / "locks").symlink_to(redirected, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                with shared_observation_lock(root, "project-test", create=False):
                    pass
            self.assertEqual(list(redirected.iterdir()), [])

    def test_unrelated_repository_does_not_appear_as_a_working_copy(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            unrelated = fixture.root / "unrelated"
            unrelated.mkdir()
            git_run(["init", "--initial-branch=main"], cwd=unrelated, fixture_root=fixture.root)
            (unrelated / "file").write_text("other\n", encoding="utf-8")
            git_run(["config", "user.name", "Fixture"], cwd=unrelated, fixture_root=fixture.root)
            git_run(["config", "user.email", "fixture@example.invalid"], cwd=unrelated, fixture_root=fixture.root)
            git_run(["add", "file"], cwd=unrelated, fixture_root=fixture.root)
            git_run(["commit", "-m", "initial"], cwd=unrelated, fixture_root=fixture.root)
            with ControllerStore(fixture.state_home / "pi-control") as store:
                project = register_project(store, fixture.repo, "fixture", policy=policy_for(fixture.root))
                paths = {row["path"] for row in inventory_working_copies(store, project["project_id"], policy=policy_for(fixture.root))["worktrees"]}
                self.assertNotIn(str(unrelated.resolve()), paths)


if __name__ == "__main__":
    unittest.main()
