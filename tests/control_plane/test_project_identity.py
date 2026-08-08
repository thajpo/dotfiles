"""Phase 3 Git/project identity and read-only observation tests."""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from scripts.pi_control.git_adapter import GitObservationError, _parse_worktree_list, observe_repository, run_git
from scripts.pi_control.project_policy import load_policy
from scripts.pi_control.reconcile import inspect_project, observe_project, plan_rebind, register_project
from scripts.pi_control.store import ControllerStore
from tests.control_plane.helpers import DisposableEnvironment, snapshot_git_repository


def fixture_policy(root: Path, *, default: str = "isolated"):
    return load_policy({
        "version": 1,
        "defaultMode": default,
        "trustedRoots": [str(root)],
        "isolatedRoots": [],
        "controlPlaneRepositories": [],
        "protectedBranches": ["main", "master"],
        "worktreeRoot": str(root / "worktrees"),
    })


class ProjectIdentityTests(unittest.TestCase):
    def test_primary_identity_is_canonical_and_observation_is_read_only(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            before = snapshot_git_repository(fixture.repo)
            observation = observe_repository(fixture.repo)
            self.assertEqual(observation.object_format, "sha1")
            self.assertEqual(len(observation.head_oid), 40)
            self.assertEqual(observation.common_dir, str((fixture.repo / ".git").resolve()))
            self.assertEqual(observation.device_inode, ((fixture.repo / ".git").stat().st_dev, (fixture.repo / ".git").stat().st_ino))
            self.assertFalse(observation.dirty)
            after = snapshot_git_repository(fixture.repo)
            self.assertEqual(before, after)

    def test_registration_assigns_random_project_and_is_idempotent_by_common_dir(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            policy = fixture_policy(fixture.root, default="trusted")
            with ControllerStore(fixture.state_home / "pi-control") as store:
                first = register_project(store, fixture.repo, "fixture", policy=policy)
                second = register_project(store, fixture.repo, "renamed", policy=policy)
                self.assertEqual(first["project_id"], second["project_id"])
                self.assertEqual(first["trust_mode"], "trusted")
                self.assertEqual(store.conn.execute("SELECT count(*) FROM projects").fetchone()[0], 1)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM control_events").fetchone()[0], 1)

    def test_dangerous_local_config_is_rejected_before_git_observation(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            config = fixture.repo / ".git" / "config"
            original = config.read_text(encoding="utf-8")
            marker = fixture.root / "config-marker"
            hook = fixture.root / "config-hook.sh"
            hook.write_text(f"#!/bin/sh\nprintf invoked > {marker}\n", encoding="utf-8")
            hook.chmod(0o700)
            config.write_text(original + f"\n[core]\n\tfsmonitor = {hook}\n", encoding="utf-8")
            try:
                with self.assertRaises(GitObservationError):
                    observe_repository(fixture.repo)
                self.assertFalse(marker.exists())
            finally:
                config.write_text(original, encoding="utf-8")

    def test_adapter_failure_is_error_not_ready_or_missing(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            with mock.patch("scripts.pi_control.git_adapter.run_git", side_effect=GitObservationError("adapter unavailable", kind="adapter-unavailable")):
                observation = observe_project(fixture.repo, fixture_policy(fixture.root))
            self.assertEqual(observation.state, "error")

    def test_unexpected_worktree_failure_is_not_classified_ready(self):
        import subprocess
        from scripts.pi_control import git_adapter
        real_run = git_adapter.run_git
        def failing_worktree(cwd, args, **kwargs):
            if list(args)[:2] == ["worktree", "list"]:
                return subprocess.CompletedProcess(["git", *args], 1, "", "permission denied")
            return real_run(cwd, args, **kwargs)
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            with mock.patch("scripts.pi_control.git_adapter.run_git", side_effect=failing_worktree):
                observation = observe_project(fixture.repo, fixture_policy(fixture.root))
            self.assertEqual(observation.state, "error")

    def test_unexpected_status_failure_is_not_classified_ready(self):
        import subprocess
        from scripts.pi_control import git_adapter
        real_run = git_adapter.run_git
        def failing_status(cwd, args, **kwargs):
            if list(args)[:1] == ["status"]:
                return subprocess.CompletedProcess(["git", *args], 1, "", "permission denied")
            return real_run(cwd, args, **kwargs)
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            with mock.patch("scripts.pi_control.git_adapter.run_git", side_effect=failing_status):
                observation = observe_project(fixture.repo, fixture_policy(fixture.root))
            self.assertEqual(observation.state, "error")

    def test_symlink_repository_path_refuses_ambiguous_identity(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            alias = fixture.root / "repo-alias"
            alias.symlink_to(fixture.repo, target_is_directory=True)
            with self.assertRaises(GitObservationError):
                observe_repository(alias)

    def test_move_requires_explicit_rebind_but_preserves_identity_proof(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            policy = fixture_policy(fixture.root)
            with ControllerStore(fixture.state_home / "pi-control") as store:
                project = register_project(store, fixture.repo, "fixture", policy=policy)
                moved = fixture.root / "moved-repository"
                fixture.repo.rename(moved)
                plan = plan_rebind(store, project["project_id"], moved, policy=policy)
                self.assertEqual(plan["proof"], "explicit-intent-required")
                self.assertTrue(plan["requires_user_intent"])
                self.assertTrue(plan["source_unchanged"])
                self.assertEqual(store.conn.execute("SELECT primary_checkout FROM projects WHERE project_id=?", (project["project_id"],)).fetchone()[0], str(fixture.repo))

    def test_read_only_inspect_cannot_create_or_change_sqlite_files(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            root = fixture.state_home / "pi-control"
            policy = fixture_policy(fixture.root)
            with ControllerStore(root) as store:
                project = register_project(store, fixture.repo, "fixture", policy=policy)
            before = {item.name: (item.stat().st_mode, item.read_bytes()) for item in root.iterdir() if item.is_file()}
            with ControllerStore(root, read_only=True) as store:
                self.assertEqual(store.list_projects()[0]["project_id"], project["project_id"])
                self.assertEqual(inspect_project(store, project["project_id"], policy=policy)["state"], "ready")
            after = {item.name: (item.stat().st_mode, item.read_bytes()) for item in root.iterdir() if item.is_file()}
            self.assertEqual(before, after)
            self.assertFalse((root / "locks").exists())

    def test_symlinked_git_worktree_output_is_classified_unsafe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            rows = _parse_worktree_list(
                f"worktree {alias}\nHEAD {'a' * 40}\nbranch refs/heads/main\n\n",
                object_format="sha1",
                common_dir=root,
            )
            self.assertEqual(rows[0].state, "error")
            self.assertFalse(rows[0].exists)

    def test_copied_clone_with_matching_anchors_is_not_auto_rebound(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            policy = fixture_policy(fixture.root)
            with ControllerStore(fixture.state_home / "pi-control") as store:
                project = register_project(store, fixture.repo, "fixture", policy=policy)
                copied = fixture.root / "copied"
                shutil.copytree(fixture.repo, copied)
                plan = plan_rebind(store, project["project_id"], copied, policy=policy)
                self.assertEqual(plan["proof"], "copied-clone-or-unproven")
                self.assertTrue(plan["requires_user_intent"])
                self.assertTrue(plan["anchor_matches"])

    def test_git_environment_and_command_surface_are_read_only(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            with self.assertRaises(GitObservationError):
                run_git(fixture.repo, ["checkout", "main"])
            observation = observe_project(fixture.repo, fixture_policy(fixture.root))
            self.assertEqual(observation.state, "ready")


if __name__ == "__main__":
    unittest.main()
