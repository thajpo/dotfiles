import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.pisec.adapters import AdapterRegistry, AgentObservation, RuntimeProcessObservation
from scripts.pisec.access import authorize_apply_project_permissions, prepare_project_permissions
from scripts.pisec.broker import BrokerDispatcher
from scripts.pisec.events import append_event_in_transaction
from scripts.pisec.operations import create_operation
from scripts.pisec.pi_store import PiStore
from scripts.pisec.refresh import _RECOVERABLE_STOPPED_REFRESH_ERRORS, _binding_scope, _recover_stopped_refresh_attention, _reset_stale_refresh_for_new_session, _reserve_refresh
from scripts.pisec.runtime import reset_codex_session_in_transaction
from scripts.pisec.models import ConflictError
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

    def test_stopped_refresh_recovery_keeps_supported_error_families(self):
        self.assertIn("runtime process identity became ambiguous during refresh", _RECOVERABLE_STOPPED_REFRESH_ERRORS)
        self.assertIn("runtime did not stop gracefully", _RECOVERABLE_STOPPED_REFRESH_ERRORS)

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

    def test_reconcile_closes_pre_stop_failure_after_newer_authenticated_refresh(self):
        with PiStore(self.root / "state") as store:
            operation, _ = create_operation(
                store,
                kind="runtime.refresh",
                project_id=self.project_id,
                workstream_id=self.workstream_id,
                idempotency_key="legacy-pre-stop-refresh",
                request={"workstreamId": self.workstream_id, "desiredGenerationSha256": "a" * 64},
                state="needs_attention",
                step="pre_stop_attention",
            )
            store.conn.execute(
                "UPDATE operations SET error_code='runtime_refresh_staging_failed',error_message='plugin snapshot contains a device or socket' WHERE operation_id=?",
                (operation.operation_id,),
            )

        refreshed = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})
        self.assertTrue(refreshed["ok"])
        reconciled = self.dispatcher.dispatch("admin", "system.reconcile", {})

        recovered = next(item for item in reconciled["resumed"] if item.get("operationId") == operation.operation_id)
        self.assertEqual(recovered["state"], "failed")
        self.assertTrue(recovered["recovered"])
        with PiStore(self.root / "state") as store:
            row = store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone()
            self.assertEqual((row["state"], row["step"], row["error_code"]), ("failed", "superseded", "superseded_by_successful_refresh"))
            event = store.conn.execute(
                "SELECT kind,payload_json FROM events WHERE operation_id=? ORDER BY sequence DESC LIMIT 1",
                (operation.operation_id,),
            ).fetchone()
            self.assertEqual(event["kind"], "runtime.refresh_superseded")
            self.assertEqual(json.loads(event["payload_json"])["supersededByOperationId"], recovered["supersededByOperationId"])

    def test_reconcile_keeps_pre_stop_failure_without_newer_success(self):
        refreshed = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})
        self.assertTrue(refreshed["ok"])
        with PiStore(self.root / "state") as store:
            operation, _ = create_operation(
                store,
                kind="runtime.refresh",
                project_id=self.project_id,
                workstream_id=self.workstream_id,
                idempotency_key="latest-pre-stop-refresh",
                request={"workstreamId": self.workstream_id, "desiredGenerationSha256": "b" * 64},
                state="needs_attention",
                step="pre_stop_attention",
            )
            store.conn.execute(
                "UPDATE operations SET error_code='runtime_refresh_staging_failed',error_message='staging failed' WHERE operation_id=?",
                (operation.operation_id,),
            )

        reconciled = self.dispatcher.dispatch("admin", "system.reconcile", {})

        self.assertFalse(any(item.get("operationId") == operation.operation_id for item in reconciled["resumed"]))
        with PiStore(self.root / "state") as store:
            row = store.conn.execute("SELECT state,step FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone()
            self.assertEqual(tuple(row), ("needs_attention", "pre_stop_attention"))

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

    def test_refresh_reservation_rechecks_workstream_lifecycle_state(self):
        with PiStore(self.root / "state") as store:
            binding = dict(
                store.conn.execute(
                    "SELECT r.*,w.kind,w.project_id FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=?",
                    (self.workstream_id,),
                ).fetchone()
            )
            store.conn.execute(
                "UPDATE workstreams SET provisioning_state='needs_attention',attention_reason='unrelated lifecycle attention' WHERE workstream_id=?",
                (self.workstream_id,),
            )
            with self.assertRaises(ConflictError):
                _reserve_refresh(store, binding, str(binding["desired_generation_sha256"]), None)
            current = store.conn.execute(
                "SELECT refresh_pending,refresh_operation_id,launch_generation_sha256 FROM runtime_bindings WHERE workstream_id=?",
                (self.workstream_id,),
            ).fetchone()
            self.assertEqual(tuple(current), (0, None, None))

    def test_refresh_reservation_rechecks_project_lifecycle_state(self):
        with PiStore(self.root / "state") as store:
            binding = dict(
                store.conn.execute(
                    "SELECT r.*,w.kind,w.project_id FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=?",
                    (self.workstream_id,),
                ).fetchone()
            )
            store.conn.execute(
                "UPDATE projects SET active=0,lifecycle_attention_reason='project lifecycle changed' WHERE project_id=?",
                (self.project_id,),
            )
            with self.assertRaises(ConflictError):
                _reserve_refresh(store, binding, str(binding["desired_generation_sha256"]), None)
            current = store.conn.execute(
                "SELECT refresh_pending,refresh_operation_id,launch_generation_sha256 FROM runtime_bindings WHERE workstream_id=?",
                (self.workstream_id,),
            ).fetchone()
            self.assertEqual(tuple(current), (0, None, None))

    def test_pre_stop_staging_failure_preserves_idle_binding_for_retry(self):
        with patch.object(self.harness, "stage_profile", side_effect=RuntimeError("staging failed before stop")):
            failed = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})

        self.assertFalse(failed["ok"])
        self.assertEqual(len(failed["failed"]), 1)
        self.assertFalse(any(call[0] == "stop" for call in self.workspace.calls))
        with PiStore(self.root / "state") as store:
            binding = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            operation = store.conn.execute(
                "SELECT * FROM operations WHERE workstream_id=? AND kind='runtime.refresh'",
                (self.workstream_id,),
            ).fetchone()
            self.assertEqual(binding["observed_state"], "idle")
            self.assertEqual(binding["refresh_pending"], 0)
            self.assertIsNone(binding["refresh_operation_id"])
            self.assertIsNone(binding["launch_generation_sha256"])
            self.assertEqual(operation["state"], "needs_attention")
            self.assertEqual(operation["step"], "pre_stop_attention")
            store.conn.execute(
                "UPDATE workstreams SET attention_reason='secretary ensure requires attention' WHERE workstream_id=?",
                (self.workstream_id,),
            )

        def launch_without_attestation(surface_id, name, agent_kind):
            self.workspace.calls.append(("start", (surface_id, name, agent_kind)))
            self.workspace.agents[name] = AgentObservation(name, surface_id, True, "working")
            self.workspace.runtime_states.pop(surface_id, None)
            return {"started": True, "name": name, "surfaceId": surface_id}

        with patch.object(self.workspace, "start_agent", side_effect=launch_without_attestation):
            retried = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})

        self.assertTrue(retried["ok"])
        self.assertEqual(retried["upgraded"], [])
        self.assertEqual([item["workstreamId"] for item in retried["pending"]], [self.workstream_id])
        self.assertEqual(retried["pending"][0]["state"], "startup_in_progress")
        with PiStore(self.root / "state") as store:
            binding = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            operation = store.conn.execute(
                "SELECT * FROM operations WHERE workstream_id=? AND kind='runtime.refresh'",
                (self.workstream_id,),
            ).fetchone()
            generation = str(binding["launch_generation_sha256"])
            self.assertEqual(binding["observed_state"], "starting")
            self.assertEqual(binding["refresh_pending"], 1)
            self.assertEqual(operation["state"], "applying")
            self.assertEqual(operation["step"], "reserved")

        report = self.dispatcher.dispatch(
            "runtime",
            "runtime.report",
            {
                "workstreamId": self.workstream_id,
                "runtimeInstanceId": "refresh-retry-runtime",
                "seq": 1,
                "event": "session_start",
                "reason": None,
                "state": "idle",
                "nativeSessionKind": "path",
                "nativeSessionValue": self.identity[3],
                "startSource": "startup",
                "surfaceId": self.identity[2],
                "token": self.secretary_token,
                "generation": generation,
            },
        )
        self.assertTrue(report["accepted"])
        settled = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})
        self.assertEqual([item["workstreamId"] for item in settled["skipped"]], [self.workstream_id])
        with PiStore(self.root / "state") as store:
            binding = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            workstream = store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            operation = store.conn.execute(
                "SELECT * FROM operations WHERE workstream_id=? AND kind='runtime.refresh'",
                (self.workstream_id,),
            ).fetchone()
            self.assertEqual(binding["observed_state"], "idle")
            self.assertEqual(binding["applied_generation_sha256"], binding["desired_generation_sha256"])
            self.assertEqual(binding["refresh_pending"], 0)
            self.assertEqual(workstream["provisioning_state"], "bound")
            self.assertEqual(operation["state"], "succeeded")
            self.assertEqual(operation["step"], "verified")

    def test_post_stop_refresh_attention_cannot_retry_as_success(self):
        with patch.object(self.harness, "activate_profile", side_effect=RuntimeError("activation failed after stop")):
            failed = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})

        self.assertFalse(failed["ok"])
        self.assertEqual(len([call for call in self.workspace.calls if call[0] == "stop"]), 1)
        with PiStore(self.root / "state") as store:
            binding = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            operation = store.conn.execute(
                "SELECT * FROM operations WHERE workstream_id=? AND kind='runtime.refresh'",
                (self.workstream_id,),
            ).fetchone()
            self.assertEqual(binding["refresh_pending"], 1)
            self.assertEqual(binding["observed_state"], "error")
            self.assertEqual(operation["state"], "needs_attention")
            self.assertEqual(operation["step"], "attention")

        call_count = len(self.workspace.calls)
        retried = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})
        self.assertFalse(retried["ok"])
        self.assertEqual(len(self.workspace.calls), call_count)
        with PiStore(self.root / "state") as store:
            operation = store.conn.execute(
                "SELECT state,step FROM operations WHERE workstream_id=? AND kind='runtime.refresh'",
                (self.workstream_id,),
            ).fetchone()
            self.assertEqual(tuple(operation), ("needs_attention", "attention"))

    def test_refresh_waits_through_transient_shutdown_identity_ambiguity(self):
        original_observe_runtime = self.workspace.observe_runtime
        transient_unknowns = 1

        def observe_runtime(surface_id, process_identity):
            nonlocal transient_unknowns
            observation = original_observe_runtime(surface_id, process_identity)
            if observation.state == "stopped" and transient_unknowns:
                transient_unknowns -= 1
                return RuntimeProcessObservation("unknown", "launcher is still exiting")
            return observation

        with patch.object(self.workspace, "observe_runtime", side_effect=observe_runtime):
            result = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["upgraded"]), 1)
        self.assertEqual(result["failed"], [])
        self.assertEqual(transient_unknowns, 0)

    def test_stopped_graceful_shutdown_failure_is_compensated_before_retry(self):
        with PiStore(self.root / "state") as store:
            binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone())
            desired = str(binding["desired_generation_sha256"])
            operation, _ = create_operation(
                store,
                kind="runtime.refresh",
                project_id=self.project_id,
                workstream_id=self.workstream_id,
                idempotency_key=f"runtime.refresh:{self.workstream_id}:{desired}",
                request={"workstreamId": self.workstream_id, "desiredGenerationSha256": desired},
                state="needs_attention",
                step="attention",
            )
            store.conn.execute(
                "UPDATE operations SET error_code='runtime_refresh_failed',error_message='runtime did not stop gracefully' WHERE operation_id=?",
                (operation.operation_id,),
            )
            store.conn.execute(
                "UPDATE runtime_bindings SET refresh_pending=1,refresh_operation_id=?,refresh_started_at='2026-08-27T00:00:00Z',launch_generation_sha256=?,runtime_instance_id='old-runtime',report_seq=1,observed_state='error',applied_generation_sha256=? WHERE workstream_id=?",
                (operation.operation_id, desired, desired, self.workstream_id),
            )
            store.conn.execute(
                "UPDATE workstreams SET provisioning_state='needs_attention',attention_reason='runtime did not stop gracefully' WHERE workstream_id=?",
                (self.workstream_id,),
            )
        self.workspace.stop_runtime(self.identity[2])

        def launch_without_attestation(surface_id, name, agent_kind):
            self.workspace.calls.append(("start", (surface_id, name, agent_kind)))
            self.workspace.agents[name] = AgentObservation(name, surface_id, True, "working")
            self.workspace.runtime_states.pop(surface_id, None)
            return {"started": True, "name": name, "surfaceId": surface_id}

        with patch.object(self.workspace, "start_agent", side_effect=launch_without_attestation):
            result = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})

        self.assertTrue(result["ok"])
        self.assertEqual(result["upgraded"], [])
        self.assertEqual([item["workstreamId"] for item in result["pending"]], [self.workstream_id])
        with PiStore(self.root / "state") as store:
            binding = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            operation_id = binding["refresh_operation_id"]
            generation = str(binding["launch_generation_sha256"])
            self.assertEqual(binding["refresh_pending"], 1)
            self.assertEqual(binding["observed_state"], "starting")
        report = self.dispatcher.dispatch(
            "runtime",
            "runtime.report",
            {
                "workstreamId": self.workstream_id,
                "runtimeInstanceId": "recovered-refresh-runtime",
                "seq": 1,
                "event": "session_start",
                "reason": None,
                "state": "idle",
                "nativeSessionKind": "path",
                "nativeSessionValue": self.identity[3],
                "startSource": "startup",
                "surfaceId": self.identity[2],
                "token": self.secretary_token,
                "generation": generation,
            },
        )
        self.assertTrue(report["accepted"])
        with PiStore(self.root / "state") as store:
            binding = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            operation = store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            self.assertEqual(binding["applied_generation_sha256"], binding["desired_generation_sha256"])
            self.assertEqual(binding["refresh_pending"], 0)
            self.assertEqual(operation["state"], "succeeded")

    def test_stopped_legacy_omp_backup_failure_is_compensated(self):
        with PiStore(self.root / "state") as store:
            binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone())
            desired = str(binding["desired_generation_sha256"])
            operation, _ = create_operation(
                store,
                kind="runtime.refresh",
                project_id=self.project_id,
                workstream_id=self.workstream_id,
                idempotency_key=f"runtime.refresh:{self.workstream_id}:{desired}",
                request={"workstreamId": self.workstream_id, "desiredGenerationSha256": desired},
                state="needs_attention",
                step="attention",
            )
            artifacts = __import__("json").loads(str(binding["adapter_artifacts_json"]))
            artifacts["generationSha256"] = desired
            store.conn.execute(
                "UPDATE operations SET error_code='runtime_refresh_failed',error_message='OMP generated backup contains an unsupported file' WHERE operation_id=?",
                (operation.operation_id,),
            )
            store.conn.execute(
                "UPDATE runtime_bindings SET adapter_artifacts_json=?,refresh_pending=1,refresh_operation_id=?,refresh_started_at='2026-08-27T00:00:00Z',launch_generation_sha256=?,runtime_instance_id=NULL,report_seq=0,observed_state='error',applied_generation_sha256=? WHERE workstream_id=?",
                (__import__("json").dumps(artifacts, sort_keys=True, separators=(",", ":")), operation.operation_id, desired, "a" * 64, self.workstream_id),
            )
            store.conn.execute(
                "UPDATE workstreams SET provisioning_state='needs_attention',attention_reason='OMP generated backup contains an unsupported file' WHERE workstream_id=?",
                (self.workstream_id,),
            )
            current = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone())
            recovered = _recover_stopped_refresh_attention(store, current, dict(store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone()), runtime_state="stopped")

            self.assertTrue(recovered)
            binding = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            operation = store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone()
            self.assertEqual(binding["applied_generation_sha256"], "a" * 64)
            self.assertIsNone(binding["launch_generation_sha256"])
            self.assertEqual(binding["refresh_pending"], 0)
            self.assertEqual(operation["state"], "applying")
            self.assertEqual(operation["step"], "reserved")

    def test_stopped_refresh_retries_materialized_launch_after_newer_generation(self):
        with PiStore(self.root / "state") as store:
            binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone())
            requested_generation = "f" * 64
            old_generation = "b" * 64
            operation, _ = create_operation(
                store,
                kind="runtime.refresh",
                project_id=self.project_id,
                workstream_id=self.workstream_id,
                idempotency_key=f"runtime.refresh:{self.workstream_id}:{requested_generation}",
                request={"workstreamId": self.workstream_id, "desiredGenerationSha256": requested_generation},
                state="needs_attention",
                step="attention",
            )
            artifacts = json.loads(str(binding["adapter_artifacts_json"]))
            artifacts["generationSha256"] = old_generation
            store.conn.execute(
                "UPDATE operations SET error_code='runtime_refresh_failed',error_message='runtime binding is already reserved by another refresh' WHERE operation_id=?",
                (operation.operation_id,),
            )
            store.conn.execute(
                "UPDATE runtime_bindings SET adapter_artifacts_json=?,refresh_pending=1,refresh_operation_id=?,refresh_started_at='2026-08-27T00:00:00Z',launch_generation_sha256=?,runtime_instance_id=NULL,report_seq=0,observed_state='error',applied_generation_sha256=? WHERE workstream_id=?",
                (json.dumps(artifacts, sort_keys=True, separators=(",", ":")), operation.operation_id, old_generation, "a" * 64, self.workstream_id),
            )
            store.conn.execute(
                "UPDATE workstreams SET provisioning_state='needs_attention',attention_reason='runtime binding is already reserved by another refresh' WHERE workstream_id=?",
                (self.workstream_id,),
            )
            current = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone())
            recovered = _recover_stopped_refresh_attention(store, current, dict(store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone()), runtime_state="stopped")

            self.assertTrue(recovered)
            self.assertEqual(
                tuple(store.conn.execute("SELECT desired_generation_sha256,applied_generation_sha256,refresh_pending,refresh_operation_id,launch_generation_sha256 FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()),
                (binding["desired_generation_sha256"], "a" * 64, 0, None, None),
            )

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

    def test_targeted_runtime_ensure_restarts_current_generation_without_refresh_reservation(self):
        with PiStore(self.root / "state") as store:
            store.conn.execute(
                "UPDATE runtime_bindings SET applied_generation_sha256=desired_generation_sha256,launch_generation_sha256=NULL,refresh_pending=0,refresh_operation_id=NULL,refresh_started_at=NULL,observed_state='idle' WHERE workstream_id=?",
                (self.workstream_id,),
            )
        self.workspace.stop_runtime(self.identity[2])

        def launch_without_attestation(surface_id, name, agent_kind):
            self.workspace.calls.append(("start", (surface_id, name, agent_kind)))
            self.workspace.agents[name] = AgentObservation(name, surface_id, True, "working")
            self.workspace.runtime_states.pop(surface_id, None)
            return {"started": True, "name": name, "surfaceId": surface_id}

        with patch.object(self.workspace, "start_agent", side_effect=launch_without_attestation):
            started = self.dispatcher.dispatch(
                "admin",
                "runtime.ensure",
                {"workstreamId": self.workstream_id, "waitSeconds": 0},
            )

        self.assertEqual(started["action"], "startup_in_progress")
        self.assertEqual(started["state"], "starting")
        with PiStore(self.root / "state") as store:
            binding = store.conn.execute(
                "SELECT * FROM runtime_bindings WHERE workstream_id=?",
                (self.workstream_id,),
            ).fetchone()
            generation = str(binding["applied_generation_sha256"])
            self.assertEqual(binding["observed_state"], "starting")
            self.assertIsNone(binding["launch_generation_sha256"])
            self.assertEqual(binding["refresh_pending"], 0)
            self.assertIsNone(binding["refresh_operation_id"])
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM operations WHERE workstream_id=? AND kind='runtime.refresh'",
                    (self.workstream_id,),
                ).fetchone()[0],
                0,
            )

        reported = self.dispatcher.dispatch(
            "runtime",
            "runtime.report",
            {
                "workstreamId": self.workstream_id,
                "runtimeInstanceId": "ensure-restart-runtime",
                "seq": 1,
                "event": "session_start",
                "reason": None,
                "state": "idle",
                "nativeSessionKind": "path",
                "nativeSessionValue": self.identity[3],
                "startSource": "startup",
                "surfaceId": self.identity[2],
                "token": self.secretary_token,
                "generation": generation,
            },
        )
        self.assertTrue(reported["accepted"])
        with PiStore(self.root / "state") as store:
            binding = store.conn.execute(
                "SELECT observed_state,runtime_instance_id,session_start_event_sequence FROM runtime_bindings WHERE workstream_id=?",
                (self.workstream_id,),
            ).fetchone()
            self.assertEqual(binding["observed_state"], "idle")
            self.assertEqual(binding["runtime_instance_id"], "ensure-restart-runtime")
            self.assertIsNotNone(binding["session_start_event_sequence"])

    def test_targeted_runtime_ensure_can_reset_a_stopped_codex_session(self):
        with PiStore(self.root / "state") as store:
            store.conn.execute("UPDATE runtime_bindings SET applied_generation_sha256=desired_generation_sha256,launch_generation_sha256=NULL,refresh_pending=0,refresh_operation_id=NULL,refresh_started_at=NULL,observed_state='stopped' WHERE workstream_id=?", (self.workstream_id,))
            binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone())
            session = Path(binding["harness_home"]) / "sessions" / "retained.jsonl"
            session.write_text("retained\n")
            session.chmod(0o600)
            store.conn.execute("UPDATE runtime_bindings SET native_session_kind='path',native_session_value=? WHERE workstream_id=?", (str(session), self.workstream_id))
            binding.update(kind="worker", harness_id="codex", project_id=self.project_id)
            with store.transaction():
                reset_codex_session_in_transaction(store.conn, binding)
            binding = store.conn.execute("SELECT native_session_kind,native_session_value FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            self.assertIsNone(binding["native_session_kind"])
            self.assertIsNone(binding["native_session_value"])
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM events WHERE workstream_id=? AND kind='runtime.session_reset'", (self.workstream_id,)).fetchone()[0], 1)

    def test_session_reset_compensates_only_a_stopped_materialized_refresh(self):
        with PiStore(self.root / "state") as store:
            binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone())
            desired = str(binding["desired_generation_sha256"])
            applied_generation = "e" * 64
            launch_generation = "f" * 64
            operation, _ = create_operation(
                store,
                kind="runtime.refresh",
                project_id=self.project_id,
                workstream_id=self.workstream_id,
                idempotency_key=f"runtime.refresh:{self.workstream_id}:{desired}:reset",
                request={"workstreamId": self.workstream_id, "desiredGenerationSha256": desired},
                state="needs_attention",
                step="attention",
            )
            artifacts = json.loads(str(binding["adapter_artifacts_json"]))
            artifacts["generationSha256"] = applied_generation
            store.conn.execute(
                "UPDATE runtime_bindings SET adapter_artifacts_json=?,refresh_pending=1,refresh_operation_id=?,refresh_started_at='2026-08-27T00:00:00Z',applied_generation_sha256=?,launch_generation_sha256=?,observed_state='error' WHERE workstream_id=?",
                (json.dumps(artifacts, sort_keys=True, separators=(",", ":")), operation.operation_id, applied_generation, launch_generation, self.workstream_id),
            )
            store.conn.execute(
                "UPDATE workstreams SET kind='worker',execution_profile='worker-default',provisioning_state='needs_attention',attention_reason='runtime binding is already reserved by another refresh' WHERE workstream_id=?",
                (self.workstream_id,),
            )
            binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone())
            binding.update(kind="worker", harness_id="codex", project_id=self.project_id)
            _reset_stale_refresh_for_new_session(store, binding)
            current = store.conn.execute("SELECT refresh_pending,refresh_operation_id,launch_generation_sha256,observed_state FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            self.assertEqual(tuple(current), (0, None, None, "stopped"))
            self.assertEqual(tuple(store.conn.execute("SELECT state,step,error_code FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone()), ("failed", "superseded", "runtime_session_reset"))

    def test_session_reset_compensates_reset_residue_after_generation_changes(self):
        with PiStore(self.root / "state") as store:
            binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone())
            old_generation = "a" * 64
            desired = str(binding["desired_generation_sha256"])
            operation, _ = create_operation(
                store,
                kind="runtime.refresh",
                project_id=self.project_id,
                workstream_id=self.workstream_id,
                idempotency_key=f"runtime.refresh:{self.workstream_id}:{old_generation}:reset-residue",
                request={"workstreamId": self.workstream_id, "desiredGenerationSha256": old_generation},
                state="failed",
                step="superseded",
            )
            store.conn.execute(
                "UPDATE operations SET error_code='runtime_session_reset',error_message='refresh reservation was compensated by an explicit stopped-worker session reset' WHERE operation_id=?",
                (operation.operation_id,),
            )
            artifacts = json.loads(str(binding["adapter_artifacts_json"]))
            artifacts["generationSha256"] = old_generation
            store.conn.execute(
                "UPDATE runtime_bindings SET adapter_artifacts_json=?,refresh_pending=1,refresh_operation_id=?,refresh_started_at='2026-08-27T00:00:00Z',launch_generation_sha256=?,desired_generation_sha256=?,observed_state='error' WHERE workstream_id=?",
                (json.dumps(artifacts, sort_keys=True, separators=(",", ":")), operation.operation_id, old_generation, desired, self.workstream_id),
            )
            store.conn.execute(
                "UPDATE workstreams SET kind='worker',execution_profile='worker-default',provisioning_state='bound',attention_reason=NULL WHERE workstream_id=?",
                (self.workstream_id,),
            )
            binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone())
            binding.update(kind="worker", harness_id="codex", project_id=self.project_id)
            _reset_stale_refresh_for_new_session(store, binding)
            current = store.conn.execute("SELECT refresh_pending,refresh_operation_id,launch_generation_sha256,observed_state FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            self.assertEqual(tuple(current), (0, None, None, "stopped"))
            self.assertEqual(tuple(store.conn.execute("SELECT state,step,error_code FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone()), ("failed", "superseded", "runtime_session_reset"))

    def test_binding_scope_injects_project_data_dirs(self):
        data = self.repo / "data"
        data.mkdir()
        with PiStore(self.root / "state") as store:
            prepared = prepare_project_permissions(
                store,
                project_id=self.project_id,
                data_dirs=[str(data)],
                external_domains=[],
                issue_id=None,
                idempotency_key="refresh-binding-scope-data-dir",
            )
            authorize_apply_project_permissions(
                store,
                approval_scope=prepared["approvalScope"],
                harness_resolver=lambda _workstream_id: self.harness,
                surface_resolver=lambda _harness_id: self.harness.current_runtime_surface(),
                workspace=self.workspace,
                actor="secretary",
            )
            binding = store.conn.execute("SELECT r.*,w.kind,w.project_id FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=?", (self.workstream_id,)).fetchone()
            scope = _binding_scope(store, binding, self.harness)
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
