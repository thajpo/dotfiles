from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import scripts.pi_control.integration as integration_module
from scripts.pi_control.changes import submit_change
from scripts.pi_control.integration import IntegrationNeedsResolution, analyze_integration, integrate
from tests.control_plane.test_integration_analysis import IntegrationAnalysisTests as _Fixture
from scripts.pi_control.store import ControllerStore


class IntegrationRecoveryTests(unittest.TestCase):
    setUp = _Fixture.setUp
    tearDown = _Fixture.tearDown
    _git = _Fixture._git
    _rev = _Fixture._rev
    _candidate = _Fixture._candidate
    _review_and_authorize = _Fixture._review_and_authorize

    def test_non_fast_forward_uses_separate_worktree_and_submits_new_result(self) -> None:
        with ControllerStore(self.state) as store:
            candidate = self._candidate(store, key="non-ff")
            (self.repo / "target-only.txt").write_text("target\n", encoding="utf-8")
            self._git("add", "target-only.txt")
            self._git("commit", "-qm", "target divergence")
            target_tip = self._rev("HEAD")
            self._git("update-ref", "refs/heads/target", target_tip, self.base)
            analysis = analyze_integration(store, project_id=self.project_id, change_id=candidate.change_id, revision=1, target_working_copy_id=self.wc_id, target_ref="refs/heads/target")
            self.assertEqual(analysis.strategy, "integration-worktree")
            auth = self._review_and_authorize(store, analysis, context="non-ff-request")
            result = integrate(store, integration_id=analysis.integration_id, authorization_id=auth["authorizationId"])
            self.assertEqual(result.state, "succeeded")
            self.assertIsNotNone(result.result_change_id)
            self.assertEqual(self._rev("refs/heads/target"), target_tip)
            self.assertEqual(self._rev(candidate.ref_name), candidate.tip_oid)
            revision = store.conn.execute("SELECT * FROM change_revision_inputs WHERE result_change_id=?", (result.result_change_id,)).fetchone()
            self.assertEqual(revision["input_change_id"], candidate.change_id)
            self.assertEqual(store.conn.execute("SELECT state FROM changes WHERE change_id=?", (candidate.change_id,)).fetchone()[0], "open")

    def test_integration_lock_rejects_symlinked_lock_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir()
            redirected = Path(temporary) / "redirected"
            redirected.mkdir()
            (state / "locks").symlink_to(redirected, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                with integration_module._exclusive_locks(state, "prj_" + "1" * 32, "wc_" + "2" * 32):
                    pass
            self.assertEqual(list(redirected.iterdir()), [])

    def test_non_fast_forward_rejects_symlinked_integration_worktree_root(self) -> None:
        with ControllerStore(self.state) as store:
            candidate = self._candidate(store, key="worktree-root-symlink")
            (self.repo / "target-only.txt").write_text("target\n", encoding="utf-8")
            self._git("add", "target-only.txt")
            self._git("commit", "-qm", "target divergence")
            target_tip = self._rev("HEAD")
            self._git("update-ref", "refs/heads/target", target_tip, self.base)
            analysis = analyze_integration(store, project_id=self.project_id, change_id=candidate.change_id, revision=1, target_working_copy_id=self.wc_id, target_ref="refs/heads/target")
            auth = self._review_and_authorize(store, analysis, context="worktree-root-symlink-request")
            redirected = self.root / "redirected-integration-worktrees"
            redirected.mkdir(mode=0o700)
            (self.state / "integration-worktrees").symlink_to(redirected, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                integration_module.integrate(store, integration_id=analysis.integration_id, authorization_id=auth["authorizationId"])
            self.assertEqual(list(redirected.iterdir()), [])
            self.assertEqual(self._rev("refs/heads/target"), target_tip)

    def test_retry_recovers_after_result_change_before_worktree_removal(self) -> None:
        with ControllerStore(self.state) as store:
            candidate = self._candidate(store, key="recover-after-result")
            (self.repo / "target-only.txt").write_text("target\n", encoding="utf-8")
            self._git("add", "target-only.txt")
            self._git("commit", "-qm", "target divergence")
            target_tip = self._rev("HEAD")
            self._git("update-ref", "refs/heads/target", target_tip, self.base)
            analysis = analyze_integration(store, project_id=self.project_id, change_id=candidate.change_id, revision=1, target_working_copy_id=self.wc_id, target_ref="refs/heads/target")
            auth = self._review_and_authorize(store, analysis, context="recover-result-request")
            original_git = integration_module._git

            def fail_worktree_remove(cwd, args, **kwargs):
                if args[:2] == ["worktree", "remove"]:
                    raise RuntimeError("simulated crash after durable result publication")
                return original_git(cwd, args, **kwargs)

            with mock.patch.object(integration_module, "_git", side_effect=fail_worktree_remove):
                with self.assertRaises(RuntimeError):
                    integration_module.integrate(store, integration_id=analysis.integration_id, authorization_id=auth["authorizationId"])
            self.assertEqual(store.conn.execute("SELECT state FROM integration_attempts WHERE integration_id=?", (analysis.integration_id,)).fetchone()[0], "planned")
            result_id = integration_module._result_change_id(analysis.integration_id)
            self.assertIsNotNone(store.conn.execute("SELECT 1 FROM changes WHERE change_id=?", (result_id,)).fetchone())
            result = integration_module.integrate(store, integration_id=analysis.integration_id, authorization_id=auth["authorizationId"])
            self.assertEqual(result.state, "succeeded")
            self.assertEqual(result.result_change_id, result_id)
            self.assertFalse((self.state / "integration-worktrees" / analysis.integration_id).exists())

    def test_conflict_preserves_candidate_and_retains_integration_worktree(self) -> None:
        with ControllerStore(self.state) as store:
            (self.repo / "base.txt").write_text("candidate version\n", encoding="utf-8")
            candidate = submit_change(store, project_id=self.project_id, working_copy_id=self.wc_id, target_ref="refs/heads/target", title="Conflict", summary="conflict candidate", capture_mode="dirty", selected_paths=["base.txt"], excluded_paths=[], idempotency_key="conflict", created_by_conversation_id=self.conv_id, actor_type="personal", actor_id="personal")
            (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
            (self.repo / "base.txt").write_text("target version\n", encoding="utf-8")
            self._git("add", "base.txt")
            self._git("commit", "-qm", "target conflict")
            target_tip = self._rev("HEAD")
            self._git("update-ref", "refs/heads/target", target_tip, self.base)
            analysis = analyze_integration(store, project_id=self.project_id, change_id=candidate.change_id, revision=1, target_working_copy_id=self.wc_id, target_ref="refs/heads/target")
            auth = self._review_and_authorize(store, analysis, context="conflict-request")
            with self.assertRaises(IntegrationNeedsResolution):
                integrate(store, integration_id=analysis.integration_id, authorization_id=auth["authorizationId"])
            self.assertEqual(self._rev("refs/heads/target"), target_tip)
            self.assertEqual(self._rev(candidate.ref_name), candidate.tip_oid)
            self.assertEqual(store.conn.execute("SELECT state FROM integration_attempts WHERE integration_id=?", (analysis.integration_id,)).fetchone()[0], "needs_resolution")


# Keep the imported fixture out of unittest module discovery.
del _Fixture


if __name__ == "__main__":
    unittest.main()
