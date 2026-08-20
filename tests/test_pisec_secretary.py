from pathlib import Path
import json
import tempfile
import unittest

from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import register_project
from scripts.pisec.secretary import ensure_secretary
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace, make_repo


class CrashOnce:
    def __init__(self, target):
        self.target = target
        self.hit_target = False

    def hit(self, name, context):
        if name == self.target and not self.hit_target:
            self.hit_target = True
            raise RuntimeError(f"crash at {name}")


class SecretaryTests(unittest.TestCase):
    def test_ensure_is_one_per_project_and_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                first = ensure_secretary(store, project["project_id"], harness, workspace)
                second = ensure_secretary(store, project["project_id"], harness, workspace)
                self.assertFalse(first["reused"])
                self.assertTrue(second["reused"])
                self.assertEqual(first["project"]["secretary_workstream_id"], first["workstream"]["workstream_id"])
                self.assertEqual(len([call for call in workspace.calls if call[0] == "start"]), 1)
                self.assertEqual(len(workspace.prompts), 1)
                self.assertEqual(workspace.prompts[0][0], "fixture-surface-1")
                self.assertEqual(workspace.calls[-1][0], "focus")
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM workstreams WHERE kind='secretary'").fetchone()[0], 1)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0], 0)
                workstream_id = first["workstream"]["workstream_id"]
                store.conn.execute("UPDATE runtime_bindings SET workspace_report_seq=9 WHERE workstream_id=?", (workstream_id,))
                store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason='workspace runtime is missing' WHERE workstream_id=?", (workstream_id,))
                workspace.agents.clear()
                third = ensure_secretary(store, project["project_id"], harness, workspace)
                self.assertTrue(third["reused"])
                self.assertEqual(len([call for call in workspace.calls if call[0] == "start"]), 2)
                self.assertEqual(store.conn.execute("SELECT workspace_report_seq FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()[0], 9)
                self.assertEqual(third["workstream"]["provisioning_state"], "bound")
                self.assertIsNone(third["workstream"]["attention_reason"])

    def test_ensure_replays_legacy_scope_without_project_store_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                first = ensure_secretary(store, project["project_id"], harness, workspace)
                workstream_id = first["workstream"]["workstream_id"]
                row = store.conn.execute("SELECT result_json FROM operations WHERE kind='secretary.ensure' AND workstream_id=?", (workstream_id,)).fetchone()
                legacy = json.loads(row["result_json"])
                legacy.pop("projectWorktreesDir")
                legacy.pop("projectGitObjectsDir")
                store.conn.execute("UPDATE operations SET result_json=? WHERE kind='secretary.ensure' AND workstream_id=?", (json.dumps(legacy), workstream_id))
                second = ensure_secretary(store, project["project_id"], harness, workspace)
                self.assertTrue(second["reused"])
                self.assertEqual(second["workstream"]["provisioning_state"], "bound")

    def test_replay_converges_after_each_secretary_checkpoint(self):
        failpoints = [
            "after_secretary_proposal_commit",
            "after_secretary_workspace_creation",
            "after_secretary_profile_materialization",
            "after_secretary_binding_commit",
            "after_secretary_policy_map_materialization",
            "after_secretary_agent_start",
            "after_secretary_brief_delivery",
            "before_secretary_final_event_commit",
            "after_secretary_final_event_commit",
        ]
        for point in failpoints:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / "repo"
                make_repo(repo)
                with PiStore(root / "state") as store:
                    project = register_project(store, repo)
                    harness = FixtureHarness(root)
                    workspace = FixtureWorkspace(root, store)
                    with self.assertRaises(RuntimeError):
                        ensure_secretary(store, project["project_id"], harness, workspace, CrashOnce(point))
                    result = ensure_secretary(store, project["project_id"], harness, workspace)
                    self.assertIn(result["reused"], (False, True))
                    self.assertEqual(result["workstream"]["provisioning_state"], "bound")
                    self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM workstreams WHERE kind='secretary'").fetchone()[0], 1)
                    self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='secretary.bound'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
