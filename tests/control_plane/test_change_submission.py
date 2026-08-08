from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from scripts.pi_control.changes import ChangeIntegrityError, ChangeSelectionRequired, submit_change, get_change, list_changes
from scripts.pi_control.errors import ConstraintError, IdempotencyConflictError
from scripts.pi_control.git_adapter import observe_repository
from scripts.pi_control.store import ControllerStore


class ChangeSubmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.state = self.root / "state"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "Change Test")
        self._git("config", "user.email", "change@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-qm", "base")
        self.project_id = "prj_" + "1" * 32
        self.working_copy_id = "wc_" + "2" * 32
        self.conversation_id = "conv_" + "3" * 32
        self._seed_store()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str) -> None:
        env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Change Test", "GIT_AUTHOR_EMAIL": "change@example.invalid",
            "GIT_COMMITTER_NAME": "Change Test", "GIT_COMMITTER_EMAIL": "change@example.invalid",
        }
        subprocess.run(["git", *args], cwd=self.repo, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def _seed_store(self) -> None:
        with ControllerStore(self.state) as store:
            store.register_build("build", source_tree_hash="tree", artifact_manifest_hash="manifest", pi_version="pi", package_lock_hash="lock", status="active")
            store.conn.execute(
                "INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.project_id, "p", str(self.repo / ".git"), 1, 1, str(self.repo), "sha1", "trusted", "policy", "active", "ready", 1, "t", "t"),
            )
            store.conn.execute(
                "INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,effective_mode,desired_state,observed_state,writer_epoch,resource_version,controller_owned,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.working_copy_id, self.project_id, "primary", "primary", "personal", str(self.repo), "trusted-live", "present", "ready", 0, 1, 1, "t", "t"),
            )
            store.conn.execute(
                "INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.conversation_id, self.project_id, self.working_copy_id, "personal", "personal", "personal-session", str(self.root / "session.jsonl"), "active", "ready", 1, "t", "t"),
            )

    def _submit(self, store: ControllerStore, **kwargs):
        return submit_change(
            store, project_id=self.project_id, working_copy_id=self.working_copy_id,
            target_ref="refs/heads/main", title="Update", summary="A bounded change",
            created_by_conversation_id=self.conversation_id, idempotency_key="change-request-" + kwargs.pop("key", "one"), **kwargs,
        )

    def _index_digest(self) -> str:
        output = subprocess.check_output(["git", "rev-parse", "--git-path", "index"], cwd=self.repo, text=True).strip()
        path = Path(output)
        if not path.is_absolute():
            path = self.repo / path
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_clean_branch_tip_submission_is_immutable_and_source_unchanged(self) -> None:
        before = observe_repository(self.repo, include_worktrees=False)
        index_before = self._index_digest()
        with ControllerStore(self.state) as store:
            result = self._submit(store)
            self.assertEqual(result.capture_mode, "branch-tip")
            self.assertEqual(result.base_oid, before.head_oid)
            self.assertEqual(result.tip_oid, before.head_oid)
            self.assertEqual(result.tree_oid, before.tree_oid)
            row = store.conn.execute("SELECT state,current_revision FROM changes WHERE change_id=?", (result.change_id,)).fetchone()
            self.assertEqual(tuple(row), ("open", 1))
            self.assertEqual(store.conn.execute("SELECT count(*) FROM control_events WHERE event_kind='change.submitted'").fetchone()[0], 1)
            self.assertEqual(get_change(store, result.change_id)["revision"]["ref_name"], result.ref_name)
            self.assertEqual([item["change_id"] for item in list_changes(store, project_id=self.project_id)], [result.change_id])
        after = observe_repository(self.repo, include_worktrees=False)
        self.assertEqual(after.head_oid, before.head_oid)
        self.assertEqual(after.status_hash, before.status_hash)
        self.assertEqual(self._index_digest(), index_before)
        self.assertEqual(subprocess.check_output(["git", "rev-parse", result.ref_name], cwd=self.repo, text=True).strip(), result.tip_oid)

    def test_dirty_submission_uses_temp_index_and_explicit_selection(self) -> None:
        (self.repo / "tracked.txt").write_text("task delta\n", encoding="utf-8")
        (self.repo / "unselected.txt").write_text("pre-existing\n", encoding="utf-8")
        before = observe_repository(self.repo, include_worktrees=False)
        index_before = self._index_digest()
        with ControllerStore(self.state) as store:
            result = self._submit(store, capture_mode="dirty", selected_paths=["tracked.txt"], excluded_paths=["unselected.txt"], key="dirty")
            self.assertEqual(result.capture_mode, "temporary-index")
            self.assertIn("tracked.txt", result.changed_paths)
            self.assertIn("unselected.txt", result.excluded_paths)
        after = observe_repository(self.repo, include_worktrees=False)
        self.assertEqual(after.head_oid, before.head_oid)
        self.assertEqual(after.status_hash, before.status_hash)
        self.assertEqual(self._index_digest(), index_before)
        self.assertEqual(subprocess.check_output(["git", "show", f"{result.ref_name}:tracked.txt"], cwd=self.repo, text=True), "task delta\n")
        with self.assertRaises(subprocess.CalledProcessError):
            subprocess.run(["git", "cat-file", "-e", f"{result.ref_name}:unselected.txt"], cwd=self.repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_dirty_requires_explicit_selection_and_overlap_is_not_silent(self) -> None:
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        with ControllerStore(self.state) as store:
            with self.assertRaises(ChangeSelectionRequired):
                self._submit(store, capture_mode="dirty", key="missing-selection")
            with self.assertRaises(ChangeSelectionRequired):
                self._submit(store, capture_mode="dirty", selected_paths=["tracked.txt"], excluded_paths=["tracked.txt"], key="overlap")
            with self.assertRaises(ChangeSelectionRequired):
                self._submit(store, capture_mode="clean", key="clean-dirty")

    def test_idempotent_retry_and_conflicting_request(self) -> None:
        with ControllerStore(self.state) as store:
            first = self._submit(store, key="same")
            second = self._submit(store, key="same")
            self.assertEqual(first.as_dict(), second.as_dict())
            with self.assertRaises(IdempotencyConflictError):
                submit_change(store, project_id=self.project_id, working_copy_id=self.working_copy_id, target_ref="refs/heads/main", title="Different", summary="Different", idempotency_key="change-request-same")
            self.assertEqual(store.conn.execute("SELECT count(*) FROM change_revisions").fetchone()[0], 1)
            self.assertEqual(store.conn.execute("SELECT count(*) FROM control_events WHERE event_kind='change.submitted'").fetchone()[0], 1)

    def test_retry_after_ref_publication_reuses_exact_revision(self) -> None:
        class Failpoint:
            def __init__(self):
                self.hit = False
            def __call__(self, name: str) -> None:
                if name == "after-ref" and not self.hit:
                    self.hit = True
                    raise RuntimeError("injected")

        failpoint = Failpoint()
        with ControllerStore(self.state) as store:
            with self.assertRaises(RuntimeError):
                self._submit(store, key="crash", failpoint=failpoint)
            self.assertEqual(store.conn.execute("SELECT count(*) FROM change_revisions").fetchone()[0], 0)
            result = self._submit(store, key="crash")
            self.assertEqual(store.conn.execute("SELECT count(*) FROM change_revisions").fetchone()[0], 1)
            self.assertEqual(result.revision, 1)

    def test_revision_and_lifecycle_state_commit_atomically(self) -> None:
        with ControllerStore(self.state) as store:
            store.conn.execute(
                "CREATE TRIGGER reject_change_transition BEFORE UPDATE ON changes "
                "BEGIN SELECT RAISE(ABORT, 'injected transition failure'); END"
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transition failure"):
                self._submit(store, key="atomic")
            self.assertEqual(store.conn.execute("SELECT count(*) FROM change_revisions").fetchone()[0], 0)
            self.assertEqual(store.conn.execute("SELECT state FROM changes").fetchone()[0], "draft")
            self.assertEqual(store.conn.execute("SELECT state FROM operations").fetchone()[0], "applying")
            store.conn.execute("DROP TRIGGER reject_change_transition")
            result = self._submit(store, key="atomic")
            self.assertEqual(result.revision, 1)
            self.assertEqual(tuple(store.conn.execute("SELECT state,current_revision FROM changes").fetchone()), ("open", 1))
            self.assertEqual(store.conn.execute("SELECT state FROM operations").fetchone()[0], "succeeded")

    def test_retry_ref_is_not_finalized_after_source_moves(self) -> None:
        class Failpoint:
            def __call__(self, name: str) -> None:
                if name == "after-ref":
                    raise RuntimeError("injected")

        with ControllerStore(self.state) as store:
            with self.assertRaises(RuntimeError):
                self._submit(store, key="moved", failpoint=Failpoint())
            (self.repo / "tracked.txt").write_text("moved\n", encoding="utf-8")
            with self.assertRaises(ChangeIntegrityError):
                self._submit(store, key="moved")
            self.assertEqual(store.conn.execute("SELECT count(*) FROM change_revisions").fetchone()[0], 0)

    def test_invalid_ref_path_and_unselected_source_are_rejected(self) -> None:
        with ControllerStore(self.state) as store:
            with self.assertRaises(Exception):
                submit_change(store, project_id=self.project_id, working_copy_id=self.working_copy_id, target_ref="refs/heads/../evil", title="x", summary="x", idempotency_key="bad-ref")
            with self.assertRaises(Exception):
                self._submit(store, capture_mode="dirty", selected_paths=["../escape"], key="bad-path")


if __name__ == "__main__":
    unittest.main()
