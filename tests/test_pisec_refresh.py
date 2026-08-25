from pathlib import Path
import tempfile
import unittest

from scripts.pisec.adapters import AdapterRegistry
from scripts.pisec.broker import BrokerDispatcher
from scripts.pisec.events import append_event_in_transaction
from scripts.pisec.operations import create_operation
from scripts.pisec.pi_store import PiStore
from scripts.pisec.refresh import _binding_scope
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace, make_repo


class RuntimeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        make_repo(self.repo)
        self.harness = FixtureHarness(self.root)
        self.workspace = FixtureWorkspace(self.root)
        registry = AdapterRegistry()
        registry.register_harness(self.harness)
        registry.register_workspace(self.workspace)
        self.dispatcher = BrokerDispatcher(
            lambda: PiStore(self.root / "state"),
            registry=registry,
            harness=self.harness,
            workspace=self.workspace,
        )
        project = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "defaultRef": "main"})
        self.project_id = project["project_id"]
        opened = self.dispatcher.dispatch("admin", "project.open", {"project": self.project_id})
        self.workstream_id = opened["workstream"]["workstream_id"]
        with PiStore(self.root / "state") as store:
            binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone())
            self.secretary_token = Path(binding["launch_secret_path"]).read_text().strip()
            session = Path(binding["harness_home"]) / "sessions" / "retained.jsonl"
            session.write_text("retained session\n")
            session.chmod(0o600)
            store.conn.execute(
                "UPDATE runtime_bindings SET native_session_kind='path',native_session_value=?,applied_generation_sha256=NULL,observed_state='idle' WHERE workstream_id=?",
                (str(session), self.workstream_id),
            )
            self.identity = (binding["workspace_id"], binding["workspace_view_id"], binding["workspace_surface_id"], str(session))

    def tearDown(self):
        self.dispatcher.stop_background()
        self.temp.cleanup()

    def test_refresh_preserves_identity_and_session_then_is_idempotent(self):
        first = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})
        self.assertTrue(first["ok"])
        self.assertEqual(len(first["upgraded"]), 1)
        upgraded = first["upgraded"][0]
        self.assertEqual(set(upgraded), {"project", "workstreamId", "harnessId", "generation"})
        with PiStore(self.root / "state") as store:
            binding = store.conn.execute("SELECT workspace_id,workspace_view_id,workspace_surface_id,native_session_value FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            self.assertEqual(tuple(binding), self.identity)
        self.assertEqual(Path(self.identity[3]).read_text(), "retained session\n")
        stop_count = len([call for call in self.workspace.calls if call[0] == "stop"])
        second = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})
        self.assertEqual(second["upgraded"], [])
        self.assertEqual(len(second["skipped"]), 1)
        self.assertEqual(len([call for call in self.workspace.calls if call[0] == "stop"]), stop_count)

    def test_secretary_refresh_is_limited_to_its_project(self):
        other_repo = self.root / "other-repo"
        make_repo(other_repo)
        other_project = self.dispatcher.dispatch("admin", "project.register", {"path": str(other_repo), "defaultRef": "main"})
        other_opened = self.dispatcher.dispatch("admin", "project.open", {"project": other_project["project_id"]})
        other_workstream_id = other_opened["workstream"]["workstream_id"]
        with PiStore(self.root / "state") as store:
            store.conn.execute(
                "UPDATE runtime_bindings SET applied_generation_sha256=?,observed_state='idle' WHERE workstream_id=?",
                ("a" * 64, other_workstream_id),
            )
        result = self.dispatcher.dispatch(
            "secretary",
            "project.refresh",
            {"authToken": self.secretary_token, "waitSeconds": 0},
        )
        self.assertTrue(result["ok"])
        self.assertEqual([item["workstreamId"] for item in result["upgraded"]], [self.workstream_id])
        with PiStore(self.root / "state") as store:
            other_binding = store.conn.execute(
                "SELECT applied_generation_sha256 FROM runtime_bindings WHERE workstream_id=?",
                (other_workstream_id,),
            ).fetchone()
            self.assertEqual(other_binding["applied_generation_sha256"], "a" * 64)

    def test_refresh_recovers_stopped_stale_needs_attention_binding(self):
        with PiStore(self.root / "state") as store:
            row = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            store.conn.execute(
                "UPDATE runtime_bindings SET observed_state='stopped',applied_generation_sha256=? WHERE workstream_id=?",
                ("a" * 64, self.workstream_id),
            )
            store.conn.execute(
                "UPDATE workstreams SET provisioning_state='needs_attention',attention_reason='workspace runtime is missing' WHERE workstream_id=?",
                (self.workstream_id,),
            )
            self.assertNotEqual("a" * 64, row["desired_generation_sha256"])
        self.workspace.stop_runtime(self.identity[2])
        first = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})
        self.assertTrue(first["ok"])
        self.assertEqual(len(first["upgraded"]), 1)
        upgraded = first["upgraded"][0]
        self.assertEqual(set(upgraded), {"project", "workstreamId", "harnessId", "generation"})
        with PiStore(self.root / "state") as store:
            binding = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            workstream = store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            self.assertEqual(binding["applied_generation_sha256"], binding["desired_generation_sha256"])
            self.assertEqual(workstream["provisioning_state"], "bound")

    def test_targeted_runtime_ensure_is_idempotent_and_starts_only_one_binding(self):
        with PiStore(self.root / "state") as store:
            store.conn.execute(
                "UPDATE runtime_bindings SET applied_generation_sha256=desired_generation_sha256,refresh_pending=0,observed_state='idle' WHERE workstream_id=?",
                (self.workstream_id,),
            )
        already = self.dispatcher.dispatch("admin", "runtime.ensure", {"workstreamId": self.workstream_id})
        self.assertEqual(already["action"], "already_live")
        self.workspace.stop_runtime(self.identity[2])
        started = self.dispatcher.dispatch("admin", "runtime.ensure", {"workstreamId": self.workstream_id})
        self.assertEqual(started["action"], "started")
        self.assertEqual(
            self.dispatcher.dispatch("admin", "runtime.ensure", {"workstreamId": self.workstream_id})["action"],
            "already_live",
        )

    def test_binding_scope_injects_project_data_dirs(self):
        data = self.repo / "data"
        data.mkdir()
        self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "dataDirs": [str(data)]})
        with PiStore(self.root / "state") as store:
            binding = store.conn.execute("SELECT r.*,w.kind,w.project_id FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=?", (self.workstream_id,)).fetchone()
            scope = _binding_scope(store, binding)
            self.assertIn(str(data.resolve()), scope["dataDirs"])

    def test_working_runtime_is_pending_and_not_interrupted(self):
        with PiStore(self.root / "state") as store:
            store.conn.execute("UPDATE runtime_bindings SET observed_state='working' WHERE workstream_id=?", (self.workstream_id,))
        result = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})
        self.assertEqual(result["upgraded"], [])
        self.assertEqual(result["pending"][0]["state"], "working")
        self.assertFalse(any(call[0] == "stop" for call in self.workspace.calls))

    def test_session_attestation_completes_refresh_operation(self):
        with PiStore(self.root / "state") as store:
            binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone())
            operation, _ = create_operation(
                store,
                kind="runtime.refresh",
                project_id=self.project_id,
                workstream_id=self.workstream_id,
                idempotency_key="refresh-attestation-completes",
                request={"workstreamId": self.workstream_id, "desiredGenerationSha256": binding["desired_generation_sha256"]},
                state="applying",
                step="reserved",
            )
            store.conn.execute(
                "UPDATE runtime_bindings SET refresh_pending=1,refresh_operation_id=?,refresh_started_at='2026-08-25T00:00:00Z',launch_generation_sha256=?,runtime_instance_id=NULL,report_seq=0,session_start_event_sequence=NULL,session_start_report_seq=NULL,session_started_at=NULL,observed_state='starting' WHERE workstream_id=?",
                (operation.operation_id, binding["desired_generation_sha256"], self.workstream_id),
            )
            payload = {
                "workstreamId": self.workstream_id,
                "runtimeInstanceId": "attested-refresh-runtime",
                "seq": 1,
                "event": "session_start",
                "reason": None,
                "state": "idle",
                "nativeSessionKind": "path",
                "nativeSessionValue": self.identity[3],
                "startSource": "startup",
                "surfaceId": self.identity[2],
                "token": self.secretary_token,
                "generation": binding["desired_generation_sha256"],
            }
        result = self.dispatcher.dispatch("runtime", "runtime.report", payload)
        self.assertTrue(result["accepted"])
        with PiStore(self.root / "state") as store:
            operation = store.conn.execute("SELECT state,step FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone()
            self.assertEqual(tuple(operation), ("succeeded", "verified"))

    def test_reconcile_recovers_current_refresh_operation(self):
        with PiStore(self.root / "state") as store:
            binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone())
            operation, _ = create_operation(
                store,
                kind="runtime.refresh",
                project_id=self.project_id,
                workstream_id=self.workstream_id,
                idempotency_key="refresh-reconcile-recovers",
                request={"workstreamId": self.workstream_id, "desiredGenerationSha256": binding["desired_generation_sha256"]},
                state="applying",
                step="reserved",
            )
            event = append_event_in_transaction(
                store.conn,
                kind="runtime.session_started",
                project_id=self.project_id,
                workstream_id=self.workstream_id,
                payload={
                    "runtimeInstanceId": "recovered-refresh-runtime",
                    "generationSha256": binding["desired_generation_sha256"],
                    "reportSeq": 1,
                },
            )
            store.conn.execute(
                "UPDATE runtime_bindings SET refresh_pending=0,refresh_operation_id=NULL,refresh_started_at=NULL,launch_generation_sha256=NULL,applied_generation_sha256=desired_generation_sha256,runtime_instance_id='recovered-refresh-runtime',report_seq=1,session_start_event_sequence=?,session_start_report_seq=1,session_started_at='2026-08-25T00:00:00Z',observed_state='idle' WHERE workstream_id=?",
                (event["sequence"], self.workstream_id),
            )
        result = self.dispatcher.startup_reconcile()
        self.assertIn({"operationId": operation.operation_id, "state": "succeeded", "recovered": True}, result["resumed"])

    def test_reconcile_terminalizes_superseded_refresh_operation(self):
        with PiStore(self.root / "state") as store:
            binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone())
            operation, _ = create_operation(
                store,
                kind="runtime.refresh",
                project_id=self.project_id,
                workstream_id=self.workstream_id,
                idempotency_key="refresh-reconcile-superseded",
                request={"workstreamId": self.workstream_id, "desiredGenerationSha256": "a" * 64},
                state="applying",
                step="reserved",
            )
            store.conn.execute(
                "UPDATE runtime_bindings SET refresh_pending=0,refresh_operation_id=NULL,refresh_started_at=NULL,launch_generation_sha256=NULL,applied_generation_sha256=desired_generation_sha256,observed_state='idle' WHERE workstream_id=?",
                (self.workstream_id,),
            )
        result = self.dispatcher.startup_reconcile()
        self.assertIn({"operationId": operation.operation_id, "state": "failed", "recovered": True}, result["resumed"])
        with PiStore(self.root / "state") as store:
            row = store.conn.execute("SELECT state,error_code FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone()
            self.assertEqual(tuple(row), ("failed", "superseded_by_newer_refresh"))


if __name__ == "__main__":
    unittest.main()
