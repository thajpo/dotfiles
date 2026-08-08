from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from scripts.pi_control.changes import submit_change
from scripts.pi_control.errors import ConstraintError, IdempotencyConflictError, InvalidRequestError
from scripts.pi_control.integration import AnalysisStaleError, IntegrationNeedsResolution, analyze_integration, authorize_integration, integrate
from scripts.pi_control.reviews import request_review, submit_review
from scripts.pi_control.run_manifest import capability_hash
from scripts.pi_control.store import ControllerStore


class IntegrationAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "Integration Test")
        self._git("config", "user.email", "integration@example.invalid")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "base.txt")
        self._git("commit", "-qm", "base")
        self.base = self._rev("HEAD")
        self._git("branch", "target", self.base)
        self.project_id = "prj_" + "1" * 32
        self.wc_id = "wc_" + "2" * 32
        self.conv_id = "conv_" + "3" * 32
        self.review_wc_id = "wc_" + "4" * 32
        self.review_conv_id = "conv_" + "5" * 32
        self.review_run_id = "run_" + "6" * 32
        self.review_secret = "review-secret-" + "a" * 48
        self.state = self.root / "state"
        with ControllerStore(self.state) as store:
            store.register_build("build", source_tree_hash="tree", artifact_manifest_hash="manifest", pi_version="pi", package_lock_hash="lock", status="active")
            store.conn.execute("INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (self.project_id, "p", str(self.repo / ".git"), 1, 1, str(self.repo), "sha1", "trusted", "policy", "active", "ready", 1, "t", "t"))
            store.conn.execute("INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,branch_ref,effective_mode,desired_state,observed_state,writer_epoch,resource_version,controller_owned,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (self.wc_id, self.project_id, "primary", "primary", "personal", str(self.repo), "refs/heads/main", "trusted-live", "present", "ready", 0, 1, 1, "t", "t"))
            store.conn.execute("INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,branch_ref,effective_mode,desired_state,observed_state,writer_epoch,resource_version,controller_owned,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (self.review_wc_id, self.project_id, "review", "review", "review", str(self.root / "review"), None, "read-only", "present", "ready", 0, 1, 1, "t", "t"))
            store.conn.execute("INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (self.conv_id, self.project_id, self.wc_id, "personal", "personal", "personal-session", str(self.root / "session.jsonl"), "active", "ready", 1, "t", "t"))
            store.conn.execute("INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (self.review_conv_id, self.project_id, self.review_wc_id, "review", "review", "review-session", str(self.root / "review.jsonl"), "active", "ready", 1, "t", "t"))
            store.create_run(run_id=self.review_run_id, conversation_id=self.review_conv_id, project_id=self.project_id, working_copy_id=self.review_wc_id, authority="read-only", runtime_spec_hash="runtime", build_id="build", capability_hash=capability_hash(self.review_secret))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str) -> None:
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_AUTHOR_NAME": "Integration Test", "GIT_AUTHOR_EMAIL": "integration@example.invalid", "GIT_COMMITTER_NAME": "Integration Test", "GIT_COMMITTER_EMAIL": "integration@example.invalid"}
        subprocess.run(["git", *args], cwd=self.repo, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def _rev(self, ref: str) -> str:
        return subprocess.check_output(["git", "rev-parse", ref], cwd=self.repo, text=True).strip()

    def _candidate(self, store: ControllerStore, key: str = "candidate"):
        (self.repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
        result = submit_change(store, project_id=self.project_id, working_copy_id=self.wc_id, target_ref="refs/heads/target", title="Candidate", summary="candidate change", capture_mode="dirty", selected_paths=["candidate.txt"], excluded_paths=[], idempotency_key=key, created_by_conversation_id=self.conv_id, actor_type="personal", actor_id="personal")
        (self.repo / "candidate.txt").unlink()
        return result

    def _review_and_authorize(self, store: ControllerStore, analysis, *, context: str = "integration-request"):
        review = request_review(store, change_id=analysis.change_id, revision=analysis.revision, reviewer_conversation_id=self.review_conv_id, reviewer_run_id=self.review_run_id, reviewer_actor_id="reviewer", reviewer_capability_secret=self.review_secret, evidence={"integrationId": analysis.integration_id, "analysisDigest": analysis.analysis_digest, "targetOid": analysis.target_oid})
        submit_review(store, review_id=review.review_id, verdict="accept", summary="approved", evidence={"integrationId": analysis.integration_id, "analysisDigest": analysis.analysis_digest, "targetOid": analysis.target_oid}, reviewer_run_id=self.review_run_id, reviewer_actor_id="reviewer", reviewer_capability_secret=self.review_secret)
        expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        return authorize_integration(store, integration_id=analysis.integration_id, actor_id="user", request_context_id=context, expires_at=expiry, review_id=review.review_id)

    def test_analysis_binds_exact_candidate_target_and_preserves_source(self) -> None:
        with ControllerStore(self.state) as store:
            candidate = self._candidate(store)
            before = self._rev("HEAD")
            analysis = analyze_integration(store, project_id=self.project_id, change_id=candidate.change_id, revision=1, target_working_copy_id=self.wc_id, target_ref="refs/heads/target")
            self.assertEqual(analysis.relation, "fast-forward")
            self.assertEqual(analysis.target_oid, self.base)
            self.assertEqual(analysis.candidate_tip_oid, candidate.tip_oid)
            self.assertEqual(self._rev("HEAD"), before)
            self.assertEqual(store.conn.execute("SELECT state FROM integration_attempts WHERE integration_id=?", (analysis.integration_id,)).fetchone()[0], "planned")

    def test_review_receipt_rejects_wrong_reviewer_capability(self) -> None:
        with ControllerStore(self.state) as store:
            candidate = self._candidate(store, key="review-auth")
            analysis = analyze_integration(store, project_id=self.project_id, change_id=candidate.change_id, revision=1, target_working_copy_id=self.wc_id, target_ref="refs/heads/target")
            evidence = {"integrationId": analysis.integration_id, "analysisDigest": analysis.analysis_digest, "targetOid": analysis.target_oid}
            review = request_review(store, change_id=candidate.change_id, revision=1, reviewer_conversation_id=self.review_conv_id, reviewer_run_id=self.review_run_id, reviewer_actor_id="reviewer", reviewer_capability_secret=self.review_secret, evidence=evidence)
            replay = request_review(store, change_id=candidate.change_id, revision=1, reviewer_conversation_id=self.review_conv_id, reviewer_run_id=self.review_run_id, reviewer_actor_id="reviewer", reviewer_capability_secret=self.review_secret, evidence=evidence, review_id=review.review_id)
            self.assertEqual(replay.review_id, review.review_id)
            with self.assertRaises(IdempotencyConflictError):
                request_review(store, change_id=candidate.change_id, revision=1, reviewer_conversation_id=self.review_conv_id, reviewer_run_id=self.review_run_id, reviewer_actor_id="reviewer", reviewer_capability_secret=self.review_secret, evidence={"different": True}, review_id=review.review_id)
            with self.assertRaises(ConstraintError):
                submit_review(store, review_id=review.review_id, verdict="accept", evidence=evidence, reviewer_run_id=self.review_run_id, reviewer_actor_id="reviewer", reviewer_capability_secret="wrong-secret-" + "b" * 48)
            with self.assertRaises(ConstraintError):
                submit_review(store, review_id=review.review_id, verdict="accept", evidence=evidence, reviewer_run_id=self.review_run_id, reviewer_actor_id="other", reviewer_capability_secret=self.review_secret)
            submit_review(store, review_id=review.review_id, verdict="accept", evidence=evidence, reviewer_run_id=self.review_run_id, reviewer_actor_id="reviewer", reviewer_capability_secret=self.review_secret)
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE reviews SET verdict='changes_requested' WHERE review_id=?", (review.review_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("DELETE FROM reviews WHERE review_id=?", (review.review_id,))
            auth = authorize_integration(store, integration_id=analysis.integration_id, actor_id="user", request_context_id="review-auth-context", expires_at="2099-01-01T00:00:00Z", review_id=review.review_id)
            self.assertEqual(auth["scope"]["reviewId"], review.review_id)
            replay = authorize_integration(store, integration_id=analysis.integration_id, actor_id="user", request_context_id="review-auth-context", expires_at="2099-01-01T00:00:00Z", review_id=review.review_id)
            self.assertEqual(replay["authorizationId"], auth["authorizationId"])
            with self.assertRaises(IdempotencyConflictError):
                authorize_integration(store, integration_id=analysis.integration_id, actor_id="user", request_context_id="review-auth-context", expires_at="2099-01-02T00:00:00Z", review_id=review.review_id)
            with self.assertRaises(InvalidRequestError):
                authorize_integration(store, integration_id=analysis.integration_id, actor_id="user", request_context_id="naive-expiry", expires_at="2099-01-01T00:00:00", review_id=review.review_id)
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE authorizations SET expires_at='2099-02-01T00:00:00Z' WHERE authorization_id=?", (auth["authorizationId"],))

    def test_review_and_authorization_are_exact_and_expire_bound(self) -> None:
        with ControllerStore(self.state) as store:
            candidate = self._candidate(store, key="review")
            analysis = analyze_integration(store, project_id=self.project_id, change_id=candidate.change_id, revision=1, target_working_copy_id=self.wc_id, target_ref="refs/heads/target")
            auth = self._review_and_authorize(store, analysis)
            self.assertEqual(auth["scope"]["analysisDigest"], analysis.analysis_digest)
            with self.assertRaises(Exception):
                authorize_integration(store, integration_id=analysis.integration_id, actor_id="user", request_context_id="replayed", expires_at="2000-01-01T00:00:00Z")

    def test_target_movement_invalidates_analysis_before_mutation(self) -> None:
        with ControllerStore(self.state) as store:
            candidate = self._candidate(store, key="moved")
            analysis = analyze_integration(store, project_id=self.project_id, change_id=candidate.change_id, revision=1, target_working_copy_id=self.wc_id, target_ref="refs/heads/target")
            auth = self._review_and_authorize(store, analysis, context="moved-request")
            (self.repo / "target.txt").write_text("target moved\n", encoding="utf-8")
            self._git("add", "target.txt")
            self._git("commit", "-qm", "move target")
            moved = self._rev("HEAD")
            self._git("update-ref", "refs/heads/target", moved, self.base)
            with self.assertRaises(AnalysisStaleError):
                integrate(store, integration_id=analysis.integration_id, authorization_id=auth["authorizationId"])
            self.assertEqual(self._rev("refs/heads/target"), moved)

    def test_dirty_or_checked_out_target_is_refused_without_target_mutation(self) -> None:
        with ControllerStore(self.state) as store:
            candidate = self._candidate(store, key="dirty-target")
            analysis = analyze_integration(store, project_id=self.project_id, change_id=candidate.change_id, revision=1, target_working_copy_id=self.wc_id, target_ref="refs/heads/target")
            auth = self._review_and_authorize(store, analysis, context="dirty-target-request")
            (self.repo / "unrelated.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaises(IntegrationNeedsResolution):
                integrate(store, integration_id=analysis.integration_id, authorization_id=auth["authorizationId"])
            self.assertEqual(self._rev("refs/heads/target"), self.base)


if __name__ == "__main__":
    unittest.main()
