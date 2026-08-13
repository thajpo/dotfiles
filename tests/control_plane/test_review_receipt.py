"""Greenfield-store review receipt unit tests for P8 exact-revision binding."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.pi_control.changes import submit_change, submit_change_revision
from scripts.pi_control.errors import ConstraintError, IdempotencyConflictError
from scripts.pi_control.pi_review import create_review_assignment
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.models import new_id, utc_now
from scripts.pi_control.projects import register_project
from scripts.pi_control.reviews import ReviewError, request_review, submit_review


def git(path: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Receipt Fixture",
        "GIT_AUTHOR_EMAIL": "receipt@example.invalid",
        "GIT_COMMITTER_NAME": "Receipt Fixture",
        "GIT_COMMITTER_EMAIL": "receipt@example.invalid",
    }
    return subprocess.run(
        ["git", "-C", str(path), *args], env=env, check=True,
        stdout=subprocess.PIPE, text=True,
    ).stdout.strip()


def _reviewer_run(
    store: PiStore,
    *,
    conversation_id: str,
    project_id: str,
    working_copy_id: str,
    desired_state: str,
    observed_state: str,
    authority: str = "host-read-only",
) -> dict:
    op = store.create_operation(
        idempotency_key="review.receipt:" + new_id("op"),
        kind="run.prepare",
        resource_type="run",
        resource_id=new_id("run"),
        actor_type="controller",
        request={"test": True},
    )
    run_id = op.resource_id
    now = utc_now()
    store.conn.execute(
        """INSERT INTO runs(
            run_id,operation_id,conversation_id,project_id,working_copy_id,
            authority,desired_state,observed_state,runtime_spec_hash,build_id,
            channel_binding_hash,resource_version,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, op.operation_id, conversation_id, project_id, working_copy_id,
         authority, "running", "created",
         "runtime-spec", "build-id",
         "sha256:" + "a" * 64, 1, now, now),
    )
    def _transition(to_observed: str, *, mark_running: bool = False) -> None:
        store.conn.execute(
            "UPDATE runs SET observed_state=?,updated_at=? WHERE run_id=?",
            (to_observed, utc_now(), run_id),
        )
        if mark_running:
            store.conn.execute(
                "UPDATE runs SET desired_state='stopped',ended_at=?,updated_at=? WHERE run_id=?",
                (utc_now(), utc_now(), run_id),
            )
    _transition("preparing")
    _transition("ready")
    if observed_state == "running":
        _transition("running")
    elif observed_state == "stopped":
        _transition("running")
        store.conn.execute(
            "UPDATE runs SET desired_state='stopped',observed_state='stopped',ended_at=?,updated_at=? WHERE run_id=?",
            (utc_now(), utc_now(), run_id),
        )
    elif observed_state == "failed":
        _transition("running")
        store.conn.execute(
            "UPDATE runs SET desired_state='stopped',observed_state='failed',ended_at=?,updated_at=? WHERE run_id=?",
            (utc_now(), utc_now(), run_id),
        )
    elif observed_state == "lost":
        _transition("running")
        store.conn.execute(
            "UPDATE runs SET desired_state='stopped',observed_state='lost',ended_at=?,updated_at=? WHERE run_id=?",
            (utc_now(), utc_now(), run_id),
        )
    elif observed_state == "needs_attention":
        _transition("running")
        store.conn.execute(
            "UPDATE runs SET desired_state='stopped',observed_state='needs_attention',ended_at=?,updated_at=? WHERE run_id=?",
            (utc_now(), utc_now(), run_id),
        )
    else:
        _transition("running")
    return dict(store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())


class GreenfieldReviewReceiptTests(unittest.TestCase):
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
        self.store = PiStore(self.root / "state").open()
        self.project = register_project(self.store, self.repository)
        self.working = self.store.conn.execute(
            "SELECT * FROM working_copies WHERE project_id=?",
            (self.project["project_id"],),
        ).fetchone()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    # -- receipt binding: run state acceptance --------------------------------

    def test_review_request_accepts_active_run(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="active run receipt",
            summary="receipt from active run",
            idempotency_key="receipt-active-run",
        )
        assignment = create_review_assignment(
            self.store, change_id=submission.change_id, revision=submission.revision,
        )
        run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="running",
            observed_state="running",
        )
        review = request_review(
            self.store,
            change_id=submission.change_id,
            revision=submission.revision,
            reviewer_conversation_id=assignment["conversationId"],
            reviewer_run_id=run["run_id"],
            reviewer_actor_id="reviewer",
            evidence={"source": "test"},
        )
        self.assertEqual(review.review_id[:7], "review_")
        self.assertEqual(review.change_id, submission.change_id)
        self.assertEqual(review.revision, submission.revision)
        self.assertEqual(review.state, "requested")

    def test_review_request_accepts_stopped_run(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="stopped run receipt",
            summary="receipt from cleanly stopped run",
            idempotency_key="receipt-stopped-run",
        )
        assignment = create_review_assignment(
            self.store, change_id=submission.change_id, revision=submission.revision,
        )
        run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="stopped",
            observed_state="stopped",
        )
        review = request_review(
            self.store,
            change_id=submission.change_id,
            revision=submission.revision,
            reviewer_conversation_id=assignment["conversationId"],
            reviewer_run_id=run["run_id"],
            reviewer_actor_id="reviewer",
            evidence={"source": "test"},
        )
        self.assertEqual(review.state, "requested")

    def test_review_request_rejects_failed_run(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="failed run receipt",
            summary="receipt from failed run rejected",
            idempotency_key="receipt-failed-run",
        )
        assignment = create_review_assignment(
            self.store, change_id=submission.change_id, revision=submission.revision,
        )
        run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="stopped",
            observed_state="failed",
        )
        with self.assertRaises(ConstraintError):
            request_review(
                self.store,
                change_id=submission.change_id,
                revision=submission.revision,
                reviewer_conversation_id=assignment["conversationId"],
                reviewer_run_id=run["run_id"],
                reviewer_actor_id="reviewer",
                evidence={"source": "test"},
            )

    def test_review_request_rejects_lost_run(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="lost run receipt",
            summary="receipt from lost run rejected",
            idempotency_key="receipt-lost-run",
        )
        assignment = create_review_assignment(
            self.store, change_id=submission.change_id, revision=submission.revision,
        )
        run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="stopped",
            observed_state="lost",
        )
        with self.assertRaises(ConstraintError):
            request_review(
                self.store,
                change_id=submission.change_id,
                revision=submission.revision,
                reviewer_conversation_id=assignment["conversationId"],
                reviewer_run_id=run["run_id"],
                reviewer_actor_id="reviewer",
                evidence={"source": "test"},
            )

    def test_review_request_rejects_needs_attention_run(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="needs-attention run receipt",
            summary="receipt from needs-attention run rejected",
            idempotency_key="receipt-na-run",
        )
        assignment = create_review_assignment(
            self.store, change_id=submission.change_id, revision=submission.revision,
        )
        run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="stopped",
            observed_state="needs_attention",
        )
        with self.assertRaises(ConstraintError):
            request_review(
                self.store,
                change_id=submission.change_id,
                revision=submission.revision,
                reviewer_conversation_id=assignment["conversationId"],
                reviewer_run_id=run["run_id"],
                reviewer_actor_id="reviewer",
                evidence={"source": "test"},
            )

    # -- receipt binding: exact revision and staleness ------------------------

    def test_review_request_binds_exact_revision(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="exact revision binding",
            summary="receipt binds exactly the current revision",
            idempotency_key="receipt-exact-revision",
        )
        assignment = create_review_assignment(
            self.store, change_id=submission.change_id, revision=submission.revision,
        )
        run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="stopped",
            observed_state="stopped",
        )
        review = request_review(
            self.store,
            change_id=submission.change_id,
            revision=submission.revision,
            reviewer_conversation_id=assignment["conversationId"],
            reviewer_run_id=run["run_id"],
            reviewer_actor_id="reviewer",
            evidence={"source": "test"},
        )
        self.assertEqual(review.revision, submission.revision)
        self.assertIsNotNone(review.reviewer_source.get("tipOid"))
        self.assertEqual(review.reviewer_source["tipOid"], submission.tip_oid)

    def test_review_request_rejects_old_revision(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="old revision rejected",
            summary="receipt for a superseded revision is rejected",
            idempotency_key="receipt-old-revision",
        )
        assignment = create_review_assignment(
            self.store, change_id=submission.change_id, revision=submission.revision,
        )
        run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="stopped",
            observed_state="stopped",
        )
        (self.repository / "README").write_text("superseding content\n", encoding="utf-8")
        git(self.repository, "add", "README")
        git(self.repository, "commit", "-qm", "create revision 2")
        superseding = submit_change_revision(
            self.store,
            change_id=submission.change_id,
            title="superseding revision",
            summary="new revision makes old one stale",
            capture_mode="clean",
            idempotency_key="receipt-supersede-revision",
        )
        self.assertEqual(superseding.revision, submission.revision + 1)
        with self.assertRaises(ConstraintError):
            request_review(
                self.store,
                change_id=submission.change_id,
                revision=submission.revision,
                reviewer_conversation_id=assignment["conversationId"],
                reviewer_run_id=run["run_id"],
                reviewer_actor_id="reviewer",
                evidence={"source": "test"},
            )

    # -- submit_review with stopped run ---------------------------------------

    def test_review_submit_accepts_active_run(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="submit while active",
            summary="submit receipt while reviewer is still running",
            idempotency_key="receipt-submit-active",
        )
        assignment = create_review_assignment(
            self.store, change_id=submission.change_id, revision=submission.revision,
        )
        run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="running",
            observed_state="running",
        )
        review = request_review(
            self.store,
            change_id=submission.change_id,
            revision=submission.revision,
            reviewer_conversation_id=assignment["conversationId"],
            reviewer_run_id=run["run_id"],
            reviewer_actor_id="reviewer",
            evidence={"source": "test"},
        )
        submitted = submit_review(
            self.store,
            review_id=review.review_id,
            verdict="accept",
            summary="approved while running",
            evidence={"confirmed": True},
            reviewer_run_id=run["run_id"],
            reviewer_actor_id="reviewer",
        )
        self.assertEqual(submitted.state, "submitted")
        self.assertEqual(submitted.verdict, "accept")

    # -- negative tests -------------------------------------------------------

    def test_review_request_rejects_wrong_conversation_role(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="wrong role rejected",
            summary="conversation with non-reviewer role fails",
            idempotency_key="receipt-wrong-role",
        )
        non_review_wc = new_id("wc")
        now = utc_now()
        self.store.conn.execute(
            "INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,git_dir,expected_head_oid,expected_tree_oid,effective_mode,desired_state,observed_state,writer_epoch,resource_version,controller_owned,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (non_review_wc, self.project["project_id"], "non-review", "review", "review", str(self.root / "non-review"), None, "0" * 40, "0" * 40, "read-only", "present", "ready", 0, 1, 1, now, now),
        )
        non_review_conv = new_id("conv")
        self.store.conn.execute(
            "INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,authority_profile,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (non_review_conv, self.project["project_id"], non_review_wc, "investigator", "host-read-only", "non-review-conv", "session", "/nonexistent", "active", "ready", 1, now, now),
        )
        run = _reviewer_run(
            self.store,
            conversation_id=non_review_conv,
            project_id=self.project["project_id"],
            working_copy_id=non_review_wc,
            desired_state="stopped",
            observed_state="stopped",
        )
        with self.assertRaises(ConstraintError):
            request_review(
                self.store,
                change_id=submission.change_id,
                revision=submission.revision,
                reviewer_conversation_id=non_review_conv,
                reviewer_run_id=run["run_id"],
                reviewer_actor_id="reviewer",
                evidence={"source": "test"},
            )

    def test_review_submit_rejects_cancelled_review(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="cancelled review",
            summary="submit on cancelled review fails",
            idempotency_key="receipt-cancelled",
        )
        assignment = create_review_assignment(
            self.store, change_id=submission.change_id, revision=submission.revision,
        )
        run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="stopped",
            observed_state="stopped",
        )
        review = request_review(
            self.store,
            change_id=submission.change_id,
            revision=submission.revision,
            reviewer_conversation_id=assignment["conversationId"],
            reviewer_run_id=run["run_id"],
            reviewer_actor_id="reviewer",
            evidence={"source": "test"},
        )
        self.store.conn.execute(
            "UPDATE reviews SET state='cancelled' WHERE review_id=?", (review.review_id,),
        )
        with self.assertRaises(ReviewError):
            submit_review(
                self.store,
                review_id=review.review_id,
                verdict="accept",
                evidence={"confirmed": True},
                reviewer_run_id=run["run_id"],
                reviewer_actor_id="reviewer",
            )

    def test_review_submit_rejects_actor_mismatch(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="actor mismatch",
            summary="submit with different actor fails",
            idempotency_key="receipt-actor-mismatch",
        )
        assignment = create_review_assignment(
            self.store, change_id=submission.change_id, revision=submission.revision,
        )
        run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="stopped",
            observed_state="stopped",
        )
        review = request_review(
            self.store,
            change_id=submission.change_id,
            revision=submission.revision,
            reviewer_conversation_id=assignment["conversationId"],
            reviewer_run_id=run["run_id"],
            reviewer_actor_id="original-reviewer",
            evidence={"source": "test"},
        )
        with self.assertRaises(ConstraintError):
            submit_review(
                self.store,
                review_id=review.review_id,
                verdict="accept",
                evidence={"confirmed": True},
                reviewer_run_id=run["run_id"],
                reviewer_actor_id="different-actor",
            )

    def test_review_submit_rejects_run_id_mismatch(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="run mismatch",
            summary="submit with different run ID fails",
            idempotency_key="receipt-run-mismatch",
        )
        assignment = create_review_assignment(
            self.store, change_id=submission.change_id, revision=submission.revision,
        )
        run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="stopped",
            observed_state="stopped",
        )
        other_run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="stopped",
            observed_state="stopped",
        )
        review = request_review(
            self.store,
            change_id=submission.change_id,
            revision=submission.revision,
            reviewer_conversation_id=assignment["conversationId"],
            reviewer_run_id=run["run_id"],
            reviewer_actor_id="reviewer",
            evidence={"source": "test"},
        )
        with self.assertRaises(ConstraintError):
            submit_review(
                self.store,
                review_id=review.review_id,
                verdict="accept",
                evidence={"confirmed": True},
                reviewer_run_id=other_run["run_id"],
                reviewer_actor_id="reviewer",
            )

    def test_review_submit_accepts_stopped_run(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="submit after stop",
            summary="submit receipt from a stopped reviewer run",
            idempotency_key="receipt-submit-stopped",
        )
        assignment = create_review_assignment(
            self.store, change_id=submission.change_id, revision=submission.revision,
        )
        run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="stopped",
            observed_state="stopped",
        )
        review = request_review(
            self.store,
            change_id=submission.change_id,
            revision=submission.revision,
            reviewer_conversation_id=assignment["conversationId"],
            reviewer_run_id=run["run_id"],
            reviewer_actor_id="reviewer",
            evidence={"source": "test"},
        )
        submitted = submit_review(
            self.store,
            review_id=review.review_id,
            verdict="accept",
            summary="approved",
            findings="looks good",
            evidence={"confirmed": True},
            reviewer_run_id=run["run_id"],
            reviewer_actor_id="reviewer",
        )
        self.assertEqual(submitted.state, "submitted")
        self.assertEqual(submitted.verdict, "accept")
        self.assertIsNotNone(submitted.submitted_at)

    # -- idempotency ----------------------------------------------------------

    def test_review_request_idempotent_replay(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="idempotent receipt",
            summary="same receipt request returns same record",
            idempotency_key="receipt-idempotent",
        )
        assignment = create_review_assignment(
            self.store, change_id=submission.change_id, revision=submission.revision,
        )
        run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="stopped",
            observed_state="stopped",
        )
        first = request_review(
            self.store,
            change_id=submission.change_id,
            revision=submission.revision,
            reviewer_conversation_id=assignment["conversationId"],
            reviewer_run_id=run["run_id"],
            reviewer_actor_id="reviewer",
            evidence={"source": "test"},
            review_id="review_" + "a" * 32,
        )
        second = request_review(
            self.store,
            change_id=submission.change_id,
            revision=submission.revision,
            reviewer_conversation_id=assignment["conversationId"],
            reviewer_run_id=run["run_id"],
            reviewer_actor_id="reviewer",
            evidence={"source": "test"},
            review_id="review_" + "a" * 32,
        )
        self.assertEqual(first.review_id, second.review_id)
        self.assertEqual(first.state, second.state)

    def test_review_request_conflict_on_different_evidence(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="conflict evidence",
            summary="same id with different evidence fails",
            idempotency_key="receipt-conflict",
        )
        assignment = create_review_assignment(
            self.store, change_id=submission.change_id, revision=submission.revision,
        )
        run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="stopped",
            observed_state="stopped",
        )
        request_review(
            self.store,
            change_id=submission.change_id,
            revision=submission.revision,
            reviewer_conversation_id=assignment["conversationId"],
            reviewer_run_id=run["run_id"],
            reviewer_actor_id="reviewer",
            evidence={"source": "original"},
            review_id="review_" + "b" * 32,
        )
        with self.assertRaises(IdempotencyConflictError):
            request_review(
                self.store,
                change_id=submission.change_id,
                revision=submission.revision,
                reviewer_conversation_id=assignment["conversationId"],
                reviewer_run_id=run["run_id"],
                reviewer_actor_id="reviewer",
                evidence={"source": "different"},
                review_id="review_" + "b" * 32,
            )


    # -- dependency review digest ---------------------------------------------

    def test_review_request_stores_dependency_digest(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="dependency digest",
            summary="receipt with dependency review digest",
            idempotency_key="receipt-dep-digest",
        )
        assignment = create_review_assignment(
            self.store, change_id=submission.change_id, revision=submission.revision,
        )
        run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="stopped",
            observed_state="stopped",
        )
        dep_digest = "sha256:" + "c" * 64
        review = request_review(
            self.store,
            change_id=submission.change_id,
            revision=submission.revision,
            reviewer_conversation_id=assignment["conversationId"],
            reviewer_run_id=run["run_id"],
            reviewer_actor_id="reviewer",
            evidence={"source": "test"},
            dependency_review_digest=dep_digest,
        )
        self.assertEqual(review.dependency_review_digest, dep_digest)
        row = self.store.conn.execute(
            "SELECT dependency_review_digest FROM reviews WHERE review_id=?",
            (review.review_id,),
        ).fetchone()
        self.assertEqual(row[0], dep_digest)

    # -- writer-container conversation rejected --------------------------------

    def test_review_request_rejects_writer_container_run(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="writer run rejected",
            summary="writer-container run cannot create a review",
            idempotency_key="receipt-writer-run",
        )
        writer_wc = new_id("wc")
        now = utc_now()
        self.store.conn.execute(
            "INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,git_dir,expected_head_oid,expected_tree_oid,effective_mode,desired_state,observed_state,writer_epoch,resource_version,controller_owned,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (writer_wc, self.project["project_id"], "writer-wc", "primary", "personal", str(self.root / "writer-wc"), None, "0" * 40, "0" * 40, "isolated", "present", "ready", 0, 1, 1, now, now),
        )
        writer_conv = new_id("conv")
        self.store.conn.execute(
            "INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,authority_profile,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (writer_conv, self.project["project_id"], writer_wc, "personal", "writer-container", "writer-conv", "session", "/nonexistent", "active", "ready", 1, now, now),
        )
        op = self.store.create_operation(
            idempotency_key="review.receipt:writer:" + new_id("op"),
            kind="run.prepare",
            resource_type="run",
            resource_id=new_id("run"),
            actor_type="controller",
            request={"test": True},
        )
        run_id = op.resource_id
        self.store.conn.execute(
            "INSERT INTO runs(run_id,operation_id,conversation_id,project_id,working_copy_id,authority,desired_state,observed_state,runtime_spec_hash,build_id,channel_binding_hash,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, op.operation_id, writer_conv, self.project["project_id"], writer_wc, "writer-container", "running", "created", "runtime-spec", "build-id", "sha256:" + "a" * 64, 1, now, now),
        )
        with self.assertRaises(ConstraintError):
            request_review(
                self.store,
                change_id=submission.change_id,
                revision=submission.revision,
                reviewer_conversation_id=writer_conv,
                reviewer_run_id=run_id,
                reviewer_actor_id="reviewer",
                evidence={"source": "test"},
            )

    # -- review does not create integration authority --------------------------

    def test_review_does_not_grant_integration_authority(self) -> None:
        submission = submit_change(
            self.store,
            project_id=self.project["project_id"],
            working_copy_id=self.working["working_copy_id"],
            target_ref="refs/heads/main",
            title="no integration auth",
            summary="submitted review does not create integration authorization",
            idempotency_key="receipt-no-auth",
        )
        assignment = create_review_assignment(
            self.store, change_id=submission.change_id, revision=submission.revision,
        )
        run = _reviewer_run(
            self.store,
            conversation_id=assignment["conversationId"],
            project_id=self.project["project_id"],
            working_copy_id=assignment["workingCopyId"],
            desired_state="stopped",
            observed_state="stopped",
        )
        review = request_review(
            self.store,
            change_id=submission.change_id,
            revision=submission.revision,
            reviewer_conversation_id=assignment["conversationId"],
            reviewer_run_id=run["run_id"],
            reviewer_actor_id="reviewer",
            evidence={"source": "test"},
        )
        submit_review(
            self.store,
            review_id=review.review_id,
            verdict="accept",
            summary="approved",
            evidence={"confirmed": True},
            reviewer_run_id=run["run_id"],
            reviewer_actor_id="reviewer",
        )
        integration_attempts = self.store.conn.execute(
            "SELECT count(*) FROM integration_attempts",
        ).fetchone()[0]
        self.assertEqual(integration_attempts, 0)
        authorizations = self.store.conn.execute(
            "SELECT count(*) FROM authorizations",
        ).fetchone()[0]
        self.assertEqual(authorizations, 0)


if __name__ == "__main__":
    unittest.main()
