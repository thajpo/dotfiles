from __future__ import annotations

import sqlite3
import unittest

from scripts.pi_control.integration import AuthorizationError, IntegrationNeedsResolution, analyze_integration, integrate
from tests.control_plane.test_integration_analysis import IntegrationAnalysisTests as _Fixture
from scripts.pi_control.store import ControllerStore


class IntegrationCASTests(unittest.TestCase):
    setUp = _Fixture.setUp
    tearDown = _Fixture.tearDown
    _git = _Fixture._git
    _rev = _Fixture._rev
    _candidate = _Fixture._candidate
    _review_and_authorize = _Fixture._review_and_authorize

    def test_fast_forward_cas_creates_rollback_and_merges_only_after_proof(self) -> None:
        with ControllerStore(self.state) as store:
            candidate = self._candidate(store, key="cas")
            analysis = analyze_integration(store, project_id=self.project_id, change_id=candidate.change_id, revision=1, target_working_copy_id=self.wc_id, target_ref="refs/heads/target")
            auth = self._review_and_authorize(store, analysis, context="cas-request")
            result = integrate(store, integration_id=analysis.integration_id, authorization_id=auth["authorizationId"])
            self.assertEqual(result.state, "succeeded")
            self.assertEqual(self._rev("refs/heads/target"), candidate.tip_oid)
            self.assertEqual(self._rev(result.rollback_ref), self.base)
            self.assertEqual(store.conn.execute("SELECT state FROM changes WHERE change_id=?", (candidate.change_id,)).fetchone()[0], "merged")
            self.assertEqual(store.conn.execute("SELECT state FROM authorizations WHERE authorization_id=?", (auth["authorizationId"],)).fetchone()[0], "consumed")
            self.assertEqual(store.conn.execute("SELECT count(*) FROM control_events WHERE event_kind='integration.succeeded'").fetchone()[0], 1)
            with self.assertRaises(AuthorizationError):
                integrate(store, integration_id=analysis.integration_id, authorization_id=auth["authorizationId"])
            self.assertEqual(store.conn.execute("SELECT state FROM authorizations WHERE authorization_id=?", (auth["authorizationId"],)).fetchone()[0], "consumed")
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE authorizations SET state='active',consumed_at=NULL WHERE authorization_id=?", (auth["authorizationId"],))

    def test_crash_after_rollback_or_target_ref_reconciles_without_guessing(self) -> None:
        class Failpoint:
            def __init__(self, boundary: str):
                self.boundary = boundary
            def __call__(self, name: str) -> None:
                if name == self.boundary:
                    raise RuntimeError("injected")

        with ControllerStore(self.state) as store:
            candidate = self._candidate(store, key="crash-cas")
            analysis = analyze_integration(store, project_id=self.project_id, change_id=candidate.change_id, revision=1, target_working_copy_id=self.wc_id, target_ref="refs/heads/target")
            auth = self._review_and_authorize(store, analysis, context="crash-cas-request")
            with self.assertRaises(RuntimeError):
                integrate(store, integration_id=analysis.integration_id, authorization_id=auth["authorizationId"], failpoint=Failpoint("rollback-ref"))
            self.assertEqual(self._rev("refs/heads/target"), self.base)
            recovered = integrate(store, integration_id=analysis.integration_id, authorization_id=auth["authorizationId"])
            self.assertEqual(recovered.result_oid, candidate.tip_oid)

    def test_authorization_cancellation_after_ref_cas_never_records_false_success(self) -> None:
        with ControllerStore(self.state) as store:
            candidate = self._candidate(store, key="cancel-after-cas")
            analysis = analyze_integration(store, project_id=self.project_id, change_id=candidate.change_id, revision=1, target_working_copy_id=self.wc_id, target_ref="refs/heads/target")
            auth = self._review_and_authorize(store, analysis, context="cancel-after-cas-request")

            def cancel_after_target(name: str) -> None:
                if name == "target-ref":
                    store.conn.execute("UPDATE authorizations SET state='cancelled' WHERE authorization_id=?", (auth["authorizationId"],))

            with self.assertRaises(AuthorizationError):
                integrate(store, integration_id=analysis.integration_id, authorization_id=auth["authorizationId"], failpoint=cancel_after_target)
            self.assertEqual(self._rev("refs/heads/target"), candidate.tip_oid)
            self.assertEqual(store.conn.execute("SELECT state FROM integration_attempts WHERE integration_id=?", (analysis.integration_id,)).fetchone()[0], "needs_resolution")
            self.assertEqual(store.conn.execute("SELECT state FROM changes WHERE change_id=?", (candidate.change_id,)).fetchone()[0], "open")
            self.assertEqual(store.conn.execute("SELECT count(*) FROM control_events WHERE event_kind='integration.succeeded'").fetchone()[0], 0)

    def test_checked_out_target_is_refused_and_never_updated(self) -> None:
        with ControllerStore(self.state) as store:
            candidate = self._candidate(store, key="checked-out")
            analysis = analyze_integration(store, project_id=self.project_id, change_id=candidate.change_id, revision=1, target_working_copy_id=self.wc_id, target_ref="refs/heads/main")
            auth = self._review_and_authorize(store, analysis, context="checked-out-request")
            with self.assertRaises(IntegrationNeedsResolution):
                integrate(store, integration_id=analysis.integration_id, authorization_id=auth["authorizationId"])
            self.assertEqual(self._rev("refs/heads/main"), self.base)
            self.assertEqual(store.conn.execute("SELECT state FROM integration_attempts WHERE integration_id=?", (analysis.integration_id,)).fetchone()[0], "needs_resolution")


# Keep the imported fixture out of unittest module discovery.
del _Fixture


if __name__ == "__main__":
    unittest.main()
