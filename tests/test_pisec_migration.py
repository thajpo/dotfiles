from pathlib import Path
import tempfile
import unittest

from scripts.pisec.first_mate import ensure_first_mate
from scripts.pisec.migration import migrate_legacy_bindings
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import register_project, update_project_policy
from scripts.pisec.secretary import ensure_secretary
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace, make_repo


class FleetTopologyMigrationTests(unittest.TestCase):
    def test_idle_fleet_secretary_moves_into_first_mate_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                secretary = ensure_secretary(store, project["project_id"], harness, workspace)
                first_mate = ensure_first_mate(store, project["project_id"], harness, workspace)
                self.assertNotEqual(secretary["binding"]["workspace_id"], first_mate["binding"]["workspace_id"])
                update_project_policy(store, project["project_id"], coordination_mode="fleet")

                result = migrate_legacy_bindings(store, harness, workspace)

                self.assertEqual(result["errors"], [])
                self.assertEqual(result["deferred"], [])
                self.assertEqual([item["workstreamId"] for item in result["migrated"]], [secretary["workstream"]["workstream_id"]])
                moved = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (secretary["workstream"]["workstream_id"],)).fetchone()
                self.assertEqual(moved["workspace_id"], first_mate["binding"]["workspace_id"])
                self.assertEqual(moved["observed_state"], "idle")
                self.assertEqual(moved["refresh_pending"], 0)
                project_workspace = store.conn.execute("SELECT workspace_id FROM project_workspaces WHERE project_id=?", (project["project_id"],)).fetchone()
                self.assertEqual(project_workspace["workspace_id"], first_mate["binding"]["workspace_id"])
                self.assertEqual(len([call for call in workspace.calls if call[0] == "move_surface_to_tab"]), 1)

    def test_working_fleet_runtime_is_deferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                secretary = ensure_secretary(store, project["project_id"], harness, workspace)
                first_mate = ensure_first_mate(store, project["project_id"], harness, workspace)
                update_project_policy(store, project["project_id"], coordination_mode="fleet")
                store.conn.execute("UPDATE runtime_bindings SET observed_state='working' WHERE workstream_id=?", (secretary["workstream"]["workstream_id"],))

                result = migrate_legacy_bindings(store, harness, workspace)

                self.assertEqual(result["migrated"], [])
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["deferred"], [{"workstreamId": secretary["workstream"]["workstream_id"], "state": "working", "deferred": True}])
                current = store.conn.execute("SELECT workspace_id FROM runtime_bindings WHERE workstream_id=?", (secretary["workstream"]["workstream_id"],)).fetchone()
                self.assertNotEqual(current["workspace_id"], first_mate["binding"]["workspace_id"])
                self.assertFalse(any(call[0] == "move_surface_to_tab" for call in workspace.calls))

    def test_attested_move_clears_crash_left_refresh_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                secretary = ensure_secretary(store, project["project_id"], harness, workspace)
                ensure_first_mate(store, project["project_id"], harness, workspace)
                update_project_policy(store, project["project_id"], coordination_mode="fleet")
                first = migrate_legacy_bindings(store, harness, workspace)
                self.assertEqual(first["errors"], [])
                store.conn.execute("UPDATE runtime_bindings SET refresh_pending=1 WHERE workstream_id=?", (secretary["workstream"]["workstream_id"],))

                replay = migrate_legacy_bindings(store, harness, workspace)

                self.assertEqual(replay["errors"], [])
                self.assertTrue(replay["migrated"][0]["recovered"])
                binding = store.conn.execute("SELECT refresh_pending,observed_state FROM runtime_bindings WHERE workstream_id=?", (secretary["workstream"]["workstream_id"],)).fetchone()
                self.assertEqual(tuple(binding), (0, "idle"))

    def test_reserved_live_pane_with_mismatched_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                secretary = ensure_secretary(store, project["project_id"], harness, workspace)
                ensure_first_mate(store, project["project_id"], harness, workspace)
                update_project_policy(store, project["project_id"], coordination_mode="fleet")
                first = migrate_legacy_bindings(store, harness, workspace)
                self.assertEqual(first["errors"], [])
                workstream_id = secretary["workstream"]["workstream_id"]
                store.conn.execute(
                    "UPDATE runtime_bindings SET workspace_surface_id='stale-surface',refresh_pending=1 WHERE workstream_id=?",
                    (workstream_id,),
                )
                workspace.calls.clear()

                replay = migrate_legacy_bindings(store, harness, workspace)

                self.assertEqual(replay["migrated"], [])
                self.assertEqual(replay["errors"][0]["workstreamId"], workstream_id)
                self.assertIn("pane identity is ambiguous", replay["errors"][0]["error"])
                self.assertFalse(any(call[0] == "stop" for call in workspace.calls))


if __name__ == "__main__":
    unittest.main()
