from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.pisec.adapters import AdapterRegistry
from scripts.pisec.broker import BrokerDispatcher
from scripts.pisec.models import AuthorizationError, ConflictError, NeedsAttentionError
from scripts.pisec.pi_store import PiStore
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace, make_repo


class FixtureAdapterBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        make_repo(self.repo)
        self.harness = FixtureHarness(self.root)
        self.workspace = FixtureWorkspace(self.root)
        self.registry = AdapterRegistry()
        self.registry.register_harness(self.harness)
        self.registry.register_workspace(self.workspace)
        self.dispatcher = BrokerDispatcher(
            lambda: PiStore(self.root / "state"),
            registry=self.registry,
            harness=self.harness,
            workspace=self.workspace,
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
            "generation": str(binding["launch_generation_sha256"] or binding["applied_generation_sha256"]),
        }

    def test_registry_and_broker_keep_product_adapters_at_boundary(self):
        with self.assertRaises(NeedsAttentionError):
            self.registry.resolve_harness("missing-harness")
        self.assertEqual(self.registry.resolve_workspace("fixture-workspace"), self.workspace)

        project = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "defaultRef": "main"})
        self.assertEqual(set(project), {"project_id", "display_name", "default_ref", "data_dirs", "external_domains", "secretary_workstream_id", "coordination_mode", "active", "lifecycle_attention_reason", "deactivated_at", "created_at", "updated_at"})
        self.assertNotIn("repository_path", project)

        ensured = self.dispatcher.dispatch("admin", "project.open", {"project": project["project_id"]})
        self.assertTrue(ensured["focused"])
        self.assertFalse(ensured["reused"])
        self.assertEqual(ensured["workstream"]["kind"], "secretary")
        self.assertNotIn("launch_secret_path", json.dumps(ensured))
        reopened = self.dispatcher.dispatch("admin", "project.open", {"project": project["project_id"]})
        self.assertTrue(reopened["focused"])
        self.assertTrue(reopened["reused"])
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
            "generation": worker_auth["generation"],
            "seq": 1,
            "event": "session_start",
            "state": "working",
            "nativeSessionKind": "id",
            "nativeSessionValue": "fixture-session-id",
            "reason": None,
            "startSource": "startup",
        }
        self.assertTrue(self.dispatcher.dispatch("runtime", "runtime.report", report_base)["accepted"])
        selected_session = self.root / "harness" / worker_id / "sessions" / "selected.jsonl"
        selected_session.write_text("selected\n")
        selected_session.chmod(0o600)
        self.assertTrue(self.dispatcher.dispatch("runtime", "session.switch.prepare", {**worker_auth, "reason": "resume", "targetSessionFile": str(selected_session)})["prepared"])
        with self.assertRaises((NeedsAttentionError, ValueError)):
            self.dispatcher.dispatch("runtime", "session.switch.prepare", {**worker_auth, "reason": "resume", "targetSessionFile": str(self.root / "escape.jsonl")})
        self.assertTrue(self.dispatcher.dispatch("runtime", "runtime.report", {**report_base, "seq": 2, "event": "lifecycle", "reason": "resume", "state": "idle", "nativeSessionKind": "path", "nativeSessionValue": str(selected_session)})["accepted"])
        self.assertTrue(self.dispatcher.dispatch("runtime", "runtime.report", {**report_base, "seq": 3, "event": "lifecycle", "nativeSessionKind": None, "nativeSessionValue": None})["accepted"])
        self.assertTrue(self.dispatcher.dispatch("runtime", "runtime.report", {**report_base, "seq": 4, "event": "session_shutdown", "state": "stopped", "nativeSessionKind": None, "nativeSessionValue": None})["accepted"])
        with self.assertRaises(ConflictError):
            self.dispatcher.dispatch("runtime", "runtime.report", {**report_base, "seq": 3, "event": "lifecycle", "nativeSessionKind": None, "nativeSessionValue": None})
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

        with PiStore(self.root / "state") as store:
            task = store.conn.execute("SELECT packet_sha256 FROM task_packets WHERE workstream_id=?", (worker_id,)).fetchone()
            workstream = store.conn.execute("SELECT worktree_path FROM workstreams WHERE workstream_id=?", (worker_id,)).fetchone()
        source_commit = subprocess.run(
            ["git", "-C", str(workstream["worktree_path"]), "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        completion = self.dispatcher.dispatch(
            "runtime",
            "workstream.completion.submit",
            {
                **worker_auth,
                "completionPacket": {
                    "acceptance": [{"criterion": "The broker routes through adapters.", "status": "passed", "evidence": ["Fixture boundary test passed."]}],
                    "verification": [{"command": "python3 -m unittest tests.test_pisec_adapters", "result": "passed"}],
                    "sourceCommit": source_commit,
                    "taskPacketSha256": task["packet_sha256"],
                    "changedSurfaces": ["fixture adapters"],
                    "residualRisk": "none",
                },
            },
        )
        prepared_acceptance = self.dispatcher.dispatch(
            "secretary",
            "workstream.accept.prepare",
            {"authToken": secretary_token, "workstreamId": worker_id},
        )
        accepted = self.dispatcher.dispatch(
            "secretary",
            "workstream.accept.apply",
            {"authToken": secretary_token, "approvalScope": prepared_acceptance["approvalScope"]},
        )
        self.dispatcher.dispatch("admin", "system.reconcile", {"event": "integration", "payload": {"ok": True}})
        with PiStore(self.root / "state") as store:
            retired = store.conn.execute("SELECT desired_state FROM workstreams WHERE workstream_id=?", (worker_id,)).fetchone()
            self.assertEqual(retired["desired_state"], "retired")
            self.assertEqual(store.conn.execute("SELECT state FROM integration_jobs WHERE integration_id=?", (accepted["integration"]["integration_id"],)).fetchone()["state"], "integrated")
            self.assertIsNone(store.conn.execute("SELECT 1 FROM runtime_bindings WHERE workstream_id=?", (worker_id,)).fetchone())

    def test_project_mode_change_requires_first_mate_and_backfills_fleet_scope(self):
        project = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo)})
        self.dispatcher.dispatch("admin", "project.open", {"project": project["project_id"]})
        with self.assertRaises(NeedsAttentionError):
            self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "coordinationMode": "fleet"})
        self.dispatcher.dispatch("admin", "first_mate.ensure", {"project": project["project_id"]})
        for name, agent in list(self.workspace.agents.items()):
            self.workspace.agents[name] = type(agent)(agent.name, agent.surface_id, agent.identity_usable, "idle")
        changed = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "coordinationMode": "fleet"})
        self.assertEqual(changed["coordination_mode"], "fleet")
        restored = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "coordinationMode": "project"})
        self.assertEqual(restored["coordination_mode"], "project")

    def test_first_mate_retries_a_recoverable_needs_attention_saga(self):
        project = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo)})
        self.dispatcher.dispatch("admin", "project.open", {"project": project["project_id"]})
        with PiStore(self.root / "state") as store:
            first = self.dispatcher.dispatch("admin", "first_mate.ensure", {"project": project["project_id"]})
            workstream_id = first["workstream"]["workstream_id"]
            store.conn.execute(
                "UPDATE operations SET state='needs_attention',step='committed',error_code='effect_mismatch',error_message='transient identity mismatch' WHERE workstream_id=? AND kind='first_mate.ensure'",
                (workstream_id,),
            )
            store.conn.execute(
                "UPDATE workstreams SET provisioning_state='needs_attention',attention_reason='transient identity mismatch' WHERE workstream_id=?",
                (workstream_id,),
            )
        retried = self.dispatcher.dispatch("admin", "first_mate.ensure", {"project": project["project_id"]})
        self.assertFalse(retried["reused"])
        self.assertEqual(retried["workstream"]["provisioning_state"], "bound")


if __name__ == "__main__":
    unittest.main()
