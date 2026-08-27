from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.pisec.adapters import AdapterRegistry, AgentObservation
from scripts.pisec.broker import BrokerDispatcher, BrokerService
from scripts.pisec.models import AuthorizationError, ConflictError, NeedsAttentionError
from scripts.pisec.pi_store import PiStore
from scripts.pisec.protocol import request
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

    def test_public_project_lifecycle_can_repeat_after_reopen(self):
        project = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "defaultRef": "main"})
        service = BrokerService(self.dispatcher, runtime_root=self.root / "runtime")
        service.start()
        runtime_counter = 0

        def public_start_agent(surface_id, name, agent_kind):
            nonlocal runtime_counter
            runtime_counter += 1
            runtime_instance = f"public-lifecycle-runtime-{runtime_counter}"
            self.workspace.agents[name] = AgentObservation(name, surface_id, True, "working")
            self.workspace.runtime_states.pop(surface_id, None)
            with PiStore(self.root / "state") as store:
                row = store.conn.execute(
                    "SELECT r.*,w.project_id FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workspace_surface_id=?",
                    (surface_id,),
                ).fetchone()
                self.assertIsNotNone(row)
                payload = {
                    "workstreamId": str(row["workstream_id"]),
                    "runtimeInstanceId": runtime_instance,
                    "seq": 1,
                    "event": "session_start",
                    "reason": None,
                    "state": "idle",
                    "nativeSessionKind": None,
                    "nativeSessionValue": None,
                    "startSource": "startup",
                    "surfaceId": surface_id,
                    "token": Path(str(row["launch_secret_path"])).read_text().strip(),
                    "generation": str(row["launch_generation_sha256"] or row["applied_generation_sha256"]),
                }
            self.assertTrue(request(service.paths["runtime"], "runtime.report", payload)["accepted"])
            return {"started": True, "name": name, "surfaceId": surface_id}

        self.workspace.start_agent = public_start_agent
        try:
            opened = self.dispatcher.dispatch("admin", "project.open", {"project": project["project_id"]})
            secretary_id = str(opened["workstream"]["workstream_id"])

            first = self.dispatcher.dispatch(
                "admin",
                "project.deactivate",
                {"project": project["project_id"], "confirm": project["project_id"]},
            )
            self.assertFalse(first["reused"])
            self.assertFalse(first["project"]["active"])
            self.workspace.agents.clear()

            reopened = self.dispatcher.dispatch("admin", "project.open", {"project": project["project_id"]})
            self.assertEqual(reopened["workstream"]["workstream_id"], secretary_id)
            self.assertTrue(reopened["project"]["active"])

            second = self.dispatcher.dispatch(
                "admin",
                "project.deactivate",
                {"project": project["project_id"], "confirm": project["project_id"]},
            )
            self.assertFalse(second["reused"])
            self.assertFalse(second["project"]["active"])
            with PiStore(self.root / "state") as store:
                self.assertEqual(
                    store.conn.execute(
                        "SELECT COUNT(*) FROM events WHERE project_id=? AND kind='project.deactivated'",
                        (project["project_id"],),
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    store.conn.execute(
                        "SELECT COUNT(*) FROM workstreams WHERE project_id=? AND kind='secretary'",
                        (project["project_id"],),
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    store.conn.execute(
                        "SELECT COUNT(*) FROM operations WHERE project_id=? AND kind='project.deactivate' AND state='succeeded'",
                        (project["project_id"],),
                    ).fetchone()[0],
                    2,
                )
        finally:
            service.stop()

    def test_project_permission_apply_runs_through_secretary_scope(self):
        data = self.repo / "approved-data"
        data.mkdir()
        project = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "defaultRef": "main"})
        opened = self.dispatcher.dispatch("admin", "project.open", {"project": project["project_id"]})
        secretary_id = str(opened["workstream"]["workstream_id"])
        with PiStore(self.root / "state") as store:
            binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (secretary_id,)).fetchone())
        token = Path(str(binding["launch_secret_path"])).read_text().strip()
        prepared = self.dispatcher.dispatch("secretary", "project.permissions.prepare", {"authToken": token, "dataDirs": [str(data)], "externalDomains": [], "idempotencyKey": "fixture-permissions-1"})
        applied = self.dispatcher.dispatch("secretary", "project.permissions.apply", {"authToken": token, "approvalScope": prepared["approvalScope"]})
        self.assertEqual(applied["operation"]["state"], "succeeded")
        self.assertEqual(applied["operation"]["step"], "committed")
        self.assertEqual(applied["refresh"]["failed"], [])

    def test_project_mode_change_requires_first_mate_and_backfills_fleet_scope(self):
        project = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo)})
        self.dispatcher.dispatch("admin", "project.open", {"project": project["project_id"]})
        with self.assertRaises(NeedsAttentionError):
            self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "coordinationMode": "fleet"})
        self.dispatcher.dispatch("admin", "first_mate.ensure", {"project": project["project_id"]})
        for name, agent in list(self.workspace.agents.items()):
            self.workspace.agents[name] = type(agent)(agent.name, agent.surface_id, agent.identity_usable, "idle")
        with PiStore(self.root / "state") as store:
            secretary_id = str(store.conn.execute("SELECT secretary_workstream_id FROM projects WHERE project_id=?", (project["project_id"],)).fetchone()[0])
            secretary_surface = str(store.conn.execute("SELECT workspace_surface_id FROM runtime_bindings WHERE workstream_id=?", (secretary_id,)).fetchone()[0])
        for name, agent in list(self.workspace.agents.items()):
            if agent.surface_id == secretary_surface:
                self.workspace.agents[name] = AgentObservation(self.harness.manifest.agent_kind, agent.surface_id, agent.identity_usable, agent.state)
        changed = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "coordinationMode": "fleet"})
        self.assertEqual(changed["coordination_mode"], "fleet")
        restored = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "coordinationMode": "project"})
        self.assertEqual(restored["coordination_mode"], "project")

    def test_public_mode_transition_moves_secretary_surface_and_durable_identity(self):
        project = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo)})
        self.dispatcher.dispatch("admin", "project.open", {"project": project["project_id"]})
        self.dispatcher.dispatch("admin", "first_mate.ensure", {"project": project["project_id"]})
        with PiStore(self.root / "state") as store:
            secretary_id = str(store.conn.execute("SELECT secretary_workstream_id FROM projects WHERE project_id=?", (project["project_id"],)).fetchone()[0])
            secretary_binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (secretary_id,)).fetchone())
            first_mate_workspace = str(store.conn.execute("SELECT r.workspace_id FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE w.kind='first_mate' AND w.desired_state='active' AND w.provisioning_state='bound'").fetchone()[0])
            old_workspace = str(secretary_binding["workspace_id"])

        changed = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "coordinationMode": "fleet"})
        self.assertEqual(changed["coordination_mode"], "fleet")
        with PiStore(self.root / "state") as store:
            fleet_binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (secretary_id,)).fetchone())
            fleet_workspace = dict(store.conn.execute("SELECT * FROM project_workspaces WHERE project_id=?", (project["project_id"],)).fetchone())
        self.assertEqual(fleet_binding["workspace_id"], first_mate_workspace)
        self.assertEqual(fleet_workspace["workspace_id"], first_mate_workspace)
        self.assertNotEqual(fleet_binding["workspace_id"], old_workspace)
        self.assertNotEqual(fleet_binding["workspace_surface_id"], secretary_binding["workspace_surface_id"])

        restored = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "coordinationMode": "project"})
        self.assertEqual(restored["coordination_mode"], "project")
        with PiStore(self.root / "state") as store:
            project_binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (secretary_id,)).fetchone())
            project_workspace = dict(store.conn.execute("SELECT * FROM project_workspaces WHERE project_id=?", (project["project_id"],)).fetchone())
        self.assertNotEqual(project_binding["workspace_id"], first_mate_workspace)
        self.assertEqual(project_binding["workspace_id"], project_workspace["workspace_id"])
        self.assertNotEqual(project_binding["workspace_surface_id"], fleet_binding["workspace_surface_id"])
        self.assertGreaterEqual(len([call for call in self.workspace.calls if call[0] == "move_surface_to_tab"]), 2)

    def test_project_mode_change_rejects_harness_mismatched_first_mate(self):
        project = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo)})
        self.dispatcher.dispatch("admin", "project.open", {"project": project["project_id"]})
        self.dispatcher.dispatch("admin", "first_mate.ensure", {"project": project["project_id"]})
        with PiStore(self.root / "state") as store:
            first_mate_id = store.conn.execute(
                "SELECT workstream_id FROM workstreams WHERE kind='first_mate' AND desired_state='active'"
            ).fetchone()[0]
            store.conn.execute(
                "UPDATE runtime_bindings SET harness_id=? WHERE workstream_id=?",
                ("mismatched-harness", first_mate_id),
            )
        with self.assertRaisesRegex(NeedsAttentionError, "usable bound First Mate"):
            self.dispatcher.dispatch(
                "admin",
                "project.register",
                {"path": str(self.repo), "coordinationMode": "fleet"},
            )

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
