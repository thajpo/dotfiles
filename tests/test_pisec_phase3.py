from pathlib import Path
import hashlib
import tempfile
import unittest

from scripts.pisec.access import authorize_apply_access_grant, authorize_apply_access_revoke, effective_runtime_scope, prepare_access_grant, prepare_access_revoke
from scripts.pisec.adapters import AdapterRegistry
from scripts.pisec.broker import BrokerDispatcher
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import register_project
from scripts.pisec.secretary import ensure_secretary
from scripts.pisec.workstreams import authorize_apply_workstream, prepare_workstream
from tests.pisec_fixture import FixtureGitObjects, FixtureHarness, FixtureWorkspace, make_repo


class ScopeAwareFixtureHarness(FixtureHarness):
    def desired_generation(self, scope):
        data_dirs = tuple(sorted(str(value) for value in scope.get("dataDirs", [])))
        return hashlib.sha256(("fixture-generation:" + str(scope["executionProfile"]) + ":" + repr(data_dirs)).encode()).hexdigest()


class Phase3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        make_repo(self.repo)
        self.store = PiStore(self.root / "state")
        self.harness = ScopeAwareFixtureHarness(self.root)
        self.workspace = FixtureWorkspace(self.root, self.store)
        registry = AdapterRegistry()
        registry.register_harness(self.harness)
        registry.register_workspace(self.workspace)
        self.dispatcher = BrokerDispatcher(
            lambda: PiStore(self.root / "state"),
            registry=registry,
            harness=self.harness,
            workspace=self.workspace,
            git_objects=FixtureGitObjects(),
        )
        self.project = register_project(self.store, self.repo, default_ref="main")
        ensure_secretary(self.store, self.project["project_id"], self.harness, self.workspace)
        task_packet = {"schemaVersion": 1, "outcome": "Exercise Phase 3.", "boundaries": ["Test only."], "acceptance": ["Phase 3 passes."], "openQuestions": [], "evidence": ["Test output."]}
        prepared = prepare_workstream(
            self.store,
            project_id=self.project["project_id"],
            title="Phase 3 worker",
            purpose="Exercise the broker contracts.",
            brief="Exercise the broker contracts without unrelated changes.",
            task_packet=task_packet,
            idempotency_key="phase3-worker",
            harness=self.harness,
            workspace=self.workspace,
            work_root=self.root / "worktrees",
            object_root=self.root / "objects",
        )
        applied = authorize_apply_workstream(self.store, scope=prepared["approvalScope"], harness=self.harness, workspace=self.workspace, git_objects=FixtureGitObjects())
        self.worker_id = str(applied["workstream"]["workstream_id"])
        binding = self.store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.worker_id,)).fetchone()
        self.auth = {
            "workstreamId": self.worker_id,
            "runtimeInstanceId": binding["runtime_instance_id"],
            "surfaceId": binding["workspace_surface_id"],
            "token": Path(binding["launch_secret_path"]).read_text().strip(),
        }

    def tearDown(self):
        self.dispatcher.stop_background()
        self.store.close()
        self.temp.cleanup()

    def test_help_routes_to_existing_durable_records(self):
        clarification = self.dispatcher.dispatch("runtime", "help.request", {**self.auth, "kind": "clarification", "summary": "Need a decision", "details": "Which parser contract is authoritative?", "requestedAction": "Answer the contract question.", "blocking": True, "evidence": [], "idempotencyKey": "help-clarification"})
        self.assertEqual(clarification["recordType"], "coordination")
        self.assertEqual(clarification["request"]["kind"], "clarification")

        access = self.dispatcher.dispatch("runtime", "help.request", {**self.auth, "kind": "access", "summary": "Need fixture data", "details": "The worker cannot read the approved fixture directory.", "requestedAction": "Approve a read-only grant.", "blocking": True, "evidence": [{"path": "fixture"}], "idempotencyKey": "help-access"})
        self.assertEqual(access["recordType"], "issue")
        self.assertEqual(access["request"]["category"], "access")

    def test_tool_failure_event_is_bounded_and_durable(self):
        result = self.dispatcher.dispatch("runtime", "runtime.tool_failure", {**self.auth, "toolName": "pisec_request_help", "failureCode": "tool_error"})
        self.assertTrue(result["recorded"])
        event = self.store.conn.execute("SELECT kind,payload_json FROM events WHERE event_id=?", (result["eventId"],)).fetchone()
        self.assertEqual(event["kind"], "runtime.tool_failed")
        self.assertEqual(event["payload_json"], '{"failureCode":"tool_error","toolName":"pisec_request_help"}')

    def test_grant_and_revoke_refresh_the_target_binding(self):
        external = self.root / "external"
        external.mkdir()
        prepared = prepare_access_grant(self.store, project_id=self.project["project_id"], subject_kind="workstream", workstream_id=self.worker_id, path=str(external), issue_id=None, idempotency_key="grant-external")
        applied = authorize_apply_access_grant(self.store, scope=prepared["approvalScope"], harness=self.harness, workspace=self.workspace)
        self.assertEqual(applied["refresh"]["failed"], [])
        binding = self.store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.worker_id,)).fetchone()
        self.assertEqual(binding["refresh_pending"], 0)
        self.assertIn(str(external), effective_runtime_scope(self.store, {**dict(binding), "kind": "worker", "project_id": self.project["project_id"]})["dataDirs"])

        revoke = prepare_access_revoke(self.store, project_id=self.project["project_id"], grant_id=prepared["grant"]["grant_id"], idempotency_key="revoke-external")
        revoked = authorize_apply_access_revoke(self.store, scope=revoke["approvalScope"], harness=self.harness, workspace=self.workspace)
        self.assertEqual(revoked["refresh"]["failed"], [])
        binding = self.store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.worker_id,)).fetchone()
        self.assertEqual(binding["refresh_pending"], 0)
        self.assertNotIn(str(external), effective_runtime_scope(self.store, {**dict(binding), "kind": "worker", "project_id": self.project["project_id"]})["dataDirs"])

    def test_busy_binding_does_not_leave_a_phantom_refresh_pending_flag(self):
        external = self.root / "busy-external"
        external.mkdir()
        self.store.conn.execute("UPDATE runtime_bindings SET observed_state='working' WHERE workstream_id=?", (self.worker_id,))
        prepared = prepare_access_grant(self.store, project_id=self.project["project_id"], subject_kind="workstream", workstream_id=self.worker_id, path=str(external), issue_id=None, idempotency_key="grant-busy")
        applied = authorize_apply_access_grant(self.store, scope=prepared["approvalScope"], harness=self.harness, workspace=self.workspace)
        self.assertEqual(len(applied["refresh"]["pending"]), 1)
        self.assertEqual(self.store.conn.execute("SELECT refresh_pending FROM runtime_bindings WHERE workstream_id=?", (self.worker_id,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
