from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.pisec.adapters import AdapterRegistry
from scripts.pisec.broker import BrokerDispatcher
from scripts.pisec.models import AuthorizationError, ConflictError, NeedsAttentionError
from scripts.pisec.pi_store import PiStore
from tests.pisec_fixture import FixtureGitObjects, FixtureHarness, FixtureWorkspace, make_repo


class FixtureAdapterBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        make_repo(self.repo)
        self.harness = FixtureHarness(self.root)
        self.workspace = FixtureWorkspace(self.root)
        self.git_objects = FixtureGitObjects()
        self.registry = AdapterRegistry()
        self.registry.register_harness(self.harness)
        self.registry.register_workspace(self.workspace)
        self.dispatcher = BrokerDispatcher(
            lambda: PiStore(self.root / "state"),
            registry=self.registry,
            harness=self.harness,
            workspace=self.workspace,
            git_objects=self.git_objects,
        )

    def tearDown(self) -> None:
        self.dispatcher.stop_background()
        self.temp.cleanup()

    def _binding_auth(self, workstream_id: str, *, instance: str | None = None) -> dict[str, str]:
        with PiStore(self.root / "state") as store:
            binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone())
        return {
            "workstreamId": workstream_id,
            "runtimeInstanceId": instance or str(binding["runtime_instance_id"]),
            "surfaceId": str(binding["workspace_surface_id"]),
            "token": Path(str(binding["launch_secret_path"])).read_text().strip(),
        }

    def test_registry_and_broker_keep_product_adapters_at_boundary(self):
        with self.assertRaises(NeedsAttentionError):
            self.registry.resolve_harness("missing-harness")
        self.assertEqual(self.registry.resolve_workspace("fixture-workspace"), self.workspace)

        project = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "defaultRef": "main"})
        self.assertEqual(set(project), {"project_id", "display_name", "default_ref", "secretary_workstream_id", "created_at", "updated_at"})
        self.assertNotIn("repository_path", project)

        ensured = self.dispatcher.dispatch("admin", "secretary.ensure", {"project": project["project_id"]})
        self.assertEqual(ensured["workstream"]["kind"], "secretary")
        self.assertNotIn("launch_secret_path", json.dumps(ensured))
        secretary_id = str(ensured["workstream"]["workstream_id"])
        with PiStore(self.root / "state") as store:
            secretary_binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (secretary_id,)).fetchone())
        secretary_token = Path(str(secretary_binding["launch_secret_path"])).read_text().strip()

        task_packet = {
            "schemaVersion": 1,
            "outcome": "Fixture adapter behavior is verified.",
            "boundaries": ["Use only the approved scope."],
            "acceptance": ["The broker routes through adapters."],
            "openQuestions": [],
            "evidence": ["Fixture boundary test."],
        }
        prepared = self.dispatcher.dispatch(
            "secretary",
            "workstream.prepare",
            {
                "authToken": secretary_token,
                "title": "Exercise fixture adapters",
                "purpose": "Verify the adapter boundary",
                "brief": "Use the exact approved scope and no product-specific core calls.",
                "taskPacket": task_packet,
                "idempotencyKey": "fixture-boundary-1",
            },
        )
        scope = prepared["approvalScope"]
        self.assertEqual(scope["harnessId"], "fixture-harness")
        self.assertEqual(scope["workspaceAdapterId"], "fixture-workspace")
        self.assertNotIn("privateGitObjectDir", scope)
        self.assertNotIn("gitCommonObjectDir", scope)

        applied = self.dispatcher.dispatch("secretary", "workstream.authorize_apply", {"authToken": secretary_token, "approvalScope": scope})
        worker_id = str(applied["workstream"]["workstream_id"])
        self.assertIn(worker_id, self.harness.launched)
        self.assertTrue(self.workspace.prompts)
        self.assertNotIn("harness_home", json.dumps(applied))

        worker_auth = self._binding_auth(worker_id, instance="fixture-runtime-next")
        report_base = {
            **worker_auth,
            "seq": 1,
            "event": "session_start",
            "state": "working",
            "nativeSessionKind": "id",
            "nativeSessionValue": "fixture-session-id",
            "startSource": "startup",
        }
        self.assertTrue(self.dispatcher.dispatch("runtime", "runtime.report", report_base)["accepted"])
        self.assertTrue(self.dispatcher.dispatch("runtime", "runtime.report", {**report_base, "seq": 2, "event": "lifecycle", "nativeSessionKind": None, "nativeSessionValue": None})["accepted"])
        self.assertTrue(self.dispatcher.dispatch("runtime", "runtime.report", {**report_base, "seq": 3, "event": "session_shutdown", "state": "stopped", "nativeSessionKind": None, "nativeSessionValue": None})["accepted"])
        with self.assertRaises(ConflictError):
            self.dispatcher.dispatch("runtime", "runtime.report", {**report_base, "seq": 2, "event": "lifecycle", "nativeSessionKind": None, "nativeSessionValue": None})
        with self.assertRaises(AuthorizationError):
            self.dispatcher.dispatch("runtime", "task.get", {**worker_auth, "token": "x" * 48})

        request = self.dispatcher.dispatch(
            "runtime",
            "research.request",
            {
                **worker_auth,
                "idempotencyKey": "fixture-research-1",
                "request": {
                    "kind": "research",
                    "summary": "Need public adapter documentation",
                    "question": "What boundary was exercised?",
                    "context": "The fixture worker used the broker.",
                    "attempted": ["Local fixture inspection"],
                    "candidateSources": ["https://example.com/docs"],
                    "blocking": True,
                },
            },
        )
        self.assertIsInstance(request["request_id"], str)
        self.assertTrue(request["request_id"])
        self.dispatcher.dispatch("admin", "system.reconcile", {"event": "fixture", "payload": {"ok": True}})

        self.dispatcher.dispatch("secretary", "workstream.complete", {"authToken": secretary_token, "workstreamId": worker_id})
        retired = self.dispatcher.dispatch("secretary", "workstream.retire", {"authToken": secretary_token, "workstreamId": worker_id})
        self.assertEqual(retired["desired_state"], "retired")
        cleaned = self.dispatcher.dispatch("admin", "workstream.cleanup", {"workstreamId": worker_id, "confirm": worker_id})
        self.assertEqual(cleaned["operation"]["state"], "succeeded")
        self.assertNotIn("private_git_object_dir", json.dumps(cleaned))
        self.assertNotIn("launch_secret_path", json.dumps(cleaned))


if __name__ == "__main__":
    unittest.main()
