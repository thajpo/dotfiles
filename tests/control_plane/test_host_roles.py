"""P4 controller-created investigator and exact reviewer assignment tests."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from scripts.pi_control.changes import submit_change
from scripts.pi_control.greenfield_review import ReviewAssignmentError, create_review_assignment
from scripts.pi_control.greenfield_store import GreenfieldStore
from scripts.pi_control.investigators import start_investigation
from scripts.pi_control.models import new_id, utc_now
from scripts.pi_control.projects import register_project
from scripts.pi_control.scoped_read import ScopedProjectReader


def git(path: Path, *args: str) -> str:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Role Fixture",
        "GIT_AUTHOR_EMAIL": "roles@example.invalid",
        "GIT_COMMITTER_NAME": "Role Fixture",
        "GIT_COMMITTER_EMAIL": "roles@example.invalid",
    }
    return subprocess.run(["git", "-C", str(path), *args], env=environment, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()


class HostRoleAssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        git(self.repository, "init", "-q", "-b", "main")
        (self.repository / "README").write_text("revision one\n", encoding="utf-8")
        git(self.repository, "add", "README")
        git(self.repository, "commit", "-qm", "one")
        (self.repository / "README").write_text("revision two\n", encoding="utf-8")
        git(self.repository, "add", "README")
        git(self.repository, "commit", "-qm", "two")
        self.store = GreenfieldStore(self.root / "state").open()
        self.project = register_project(self.store, self.repository)
        self.working = self.store.conn.execute("SELECT * FROM working_copies WHERE project_id=?", (self.project["project_id"],)).fetchone()

    def tearDown(self) -> None:
        self.store.close()
        reviews = self.root / "state/reviews"
        if reviews.exists():
            for parent, directories, files in os.walk(reviews):
                for name in directories:
                    os.chmod(Path(parent) / name, 0o700)
                for name in files:
                    path = Path(parent) / name
                    if not path.is_symlink():
                        os.chmod(path, 0o600)
        self.temporary.cleanup()

    def test_investigator_is_controller_assigned_without_thread_or_synthetic_run(self) -> None:
        assignment = start_investigation(self.store, project_id=self.project["project_id"], purpose="bounded inspection")
        self.assertIsNone(assignment["run_id"])
        self.assertEqual(assignment["state"], "running")
        self.assertEqual(assignment["working_copy_id"], self.working["working_copy_id"])
        conversation = self.store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (assignment["conversation_id"],)).fetchone()
        self.assertEqual((conversation["role"], conversation["authority_profile"], conversation["working_copy_id"], conversation["desired_state"], conversation["observed_state"]), ("investigator", "host-read-only", self.working["working_copy_id"], "active", "ready"))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM runs WHERE conversation_id=?", (conversation["conversation_id"],)).fetchone()[0], 0)

    def test_reviewer_snapshot_remains_exact_after_branch_movement(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="review fixture",
            summary="exact revision",
            idempotency_key="p4-review-fixture",
        )
        assignment = create_review_assignment(self.store, change_id=submission.change_id, revision=submission.revision)
        snapshot = Path(assignment["path"])
        self.assertTrue(assignment["readOnly"] and assignment["detached"])
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode) & 0o222, 0)
        with self.assertRaises(PermissionError):
            (snapshot / "forbidden").write_text("no\n", encoding="utf-8")
        (self.repository / "README").write_text("branch moved\n", encoding="utf-8")
        git(self.repository, "add", "README")
        git(self.repository, "commit", "-qm", "three")
        self.assertNotEqual(git(self.repository, "rev-parse", "HEAD"), assignment["tipOid"])
        with ScopedProjectReader(self.store, project_id=self.project["project_id"], working_copy_id=assignment["workingCopyId"]) as reader:
            reader.assert_revision(clean=True)
            self.assertEqual(reader.read("README")["lines"], ["revision two"])
            shown = reader.git("show", path="README")
            self.assertEqual(shown["revision"], assignment["tipOid"])
            self.assertEqual(shown["output"], "revision two\n")
        conversation = self.store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (assignment["conversationId"],)).fetchone()
        self.assertEqual((conversation["role"], conversation["authority_profile"], conversation["working_copy_id"]), ("reviewer", "host-read-only", assignment["workingCopyId"]))
        with self.assertRaises(ReviewAssignmentError):
            create_review_assignment(self.store, change_id=submission.change_id, revision=submission.revision + 1)

    def test_integration_result_change_can_be_reviewed(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="result fixture",
            summary="controller-created integration result",
            idempotency_key="p4-result-fixture",
        )
        # Simulate a controller-created integration result change: no source
        # working copy, revision captured as integration-result with its own ref.
        result_id = new_id("chg")
        result_ref = f"refs/pi/changes/{result_id}/1"
        git(self.repository, "update-ref", result_ref, submission.tip_oid)
        now = utc_now()
        self.store.conn.execute(
            "INSERT INTO changes(change_id,project_id,source_working_copy_id,title,summary,target_ref,baseline_oid,baseline_tree_oid,baseline_state_json,state,current_revision,resource_version,created_at,updated_at,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (result_id, self.project["project_id"], None, "Integration result", "awaiting fresh authorization", "refs/heads/main", submission.tip_oid, submission.tree_oid, '{"captureMode":"integration-result"}', "open", 1, 1, now, now, now),
        )
        self.store.conn.execute(
            "INSERT INTO change_revisions(change_id,revision,base_oid,tip_oid,tree_oid,source_head_oid,capture_mode,ref_name,changed_paths_json,diffstat_json,verification_json,provenance_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (result_id, 1, submission.base_oid, submission.tip_oid, submission.tree_oid, submission.tip_oid, "integration-result", result_ref, "[]", "{}", '{"refVerified":true}', '{"inputs":[]}', now),
        )
        assignment = create_review_assignment(self.store, change_id=result_id, revision=1)
        snapshot = Path(assignment["path"])
        self.assertTrue(assignment["readOnly"] and assignment["detached"])
        self.assertEqual(assignment["tipOid"], submission.tip_oid)
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode) & 0o222, 0)
        with ScopedProjectReader(self.store, project_id=self.project["project_id"], working_copy_id=assignment["workingCopyId"]) as reader:
            reader.assert_revision(clean=True)
            self.assertEqual(reader.read("README")["lines"], ["revision two"])
        conversation = self.store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (assignment["conversationId"],)).fetchone()
        self.assertEqual(conversation["role"], "reviewer")


if __name__ == "__main__":
    unittest.main()
