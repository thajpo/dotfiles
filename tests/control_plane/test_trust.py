"""Phase 3 project-based trust and policy tests."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.project_policy import PolicyError, load_policy
from scripts.pi_control.reconcile import inventory_working_copies, register_project
from scripts.pi_control.store import ControllerStore
from tests.control_plane.helpers import DisposableEnvironment


class TrustTests(unittest.TestCase):
    def test_linked_worktree_trust_follows_registered_project_not_storage_path(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            policy = load_policy({
                "version": 1, "defaultMode": "trusted", "trustedRoots": [str(fixture.repo)],
                "isolatedRoots": [], "controlPlaneRepositories": [], "protectedBranches": [],
                "worktreeRoot": str(fixture.worktree_root),
            })
            with ControllerStore(fixture.state_home / "pi-control") as store:
                project = register_project(store, fixture.repo, "trusted", policy=policy)
                rows = inventory_working_copies(store, project["project_id"], policy=policy)["worktrees"]
                linked = next(row for row in rows if row["path"].endswith("worktrees/linked"))
                self.assertEqual(project["trust_mode"], "trusted")
                self.assertEqual(linked["effective_mode"], "trusted-live")
                self.assertTrue(linked["common_dir_matches"])

    def test_isolated_project_cannot_be_broadened_by_requested_mode(self):
        policy = load_policy({
            "version": 1, "defaultMode": "isolated", "trustedRoots": [], "isolatedRoots": [],
            "controlPlaneRepositories": [], "protectedBranches": [], "worktreeRoot": "~/worktrees",
        })
        self.assertEqual(policy.effective_mode("isolated"), "isolated")
        self.assertEqual(policy.effective_mode("isolated", "read-only"), "read-only")
        with self.assertRaises(PolicyError):
            policy.effective_mode("isolated", "trusted-live")

    def test_unknown_fields_and_version_fail_closed_and_hash_is_stable(self):
        base = {
            "version": 1, "defaultMode": "isolated", "trustedRoots": [], "isolatedRoots": [],
            "controlPlaneRepositories": [], "protectedBranches": ["main"], "worktreeRoot": "~/worktrees",
        }
        one = load_policy(base)
        two = load_policy(dict(base))
        self.assertEqual(one.policy_hash, two.policy_hash)
        with self.assertRaises(PolicyError):
            load_policy({**base, "unknown": True})
        with self.assertRaises(PolicyError):
            load_policy({**base, "version": 2})

    def test_path_only_does_not_create_a_second_project_identity(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            policy = load_policy({
                "version": 1, "defaultMode": "isolated", "trustedRoots": [],
                "isolatedRoots": [str(fixture.root)], "controlPlaneRepositories": [],
                "protectedBranches": [], "worktreeRoot": str(fixture.worktree_root),
            })
            with ControllerStore(fixture.state_home / "pi-control") as store:
                first = register_project(store, fixture.repo, "one", policy=policy)
                other = fixture.root / "other"
                other.mkdir()
                from tests.control_plane.helpers import git_run
                git_run(["init", "--initial-branch=main"], cwd=other, fixture_root=fixture.root)
                second = register_project(store, other, "two", policy=policy)
                self.assertNotEqual(first["project_id"], second["project_id"])
                self.assertEqual(store.conn.execute("SELECT count(*) FROM projects").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
