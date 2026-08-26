from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from scripts.pisec.pi_store import PiStore
from scripts.pisec.models import NeedsAttentionError, PisecError, canonical_json
from scripts.pisec.first_mate import ensure_first_mate
from scripts.pisec.projects import deactivate_project, register_project
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
    def test_initial_secretary_binding_reserves_launch_generation_for_session_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                captured = []

                def launch(*args, **kwargs):
                    del args, kwargs
                    workstream_id = store.conn.execute("SELECT workstream_id FROM workstreams WHERE project_id=? AND kind='secretary'", (project["project_id"],)).fetchone()[0]
                    binding = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()
                    captured.append((binding["applied_generation_sha256"], binding["launch_generation_sha256"]))
                    workspace.start_agent(binding["workspace_surface_id"], binding["agent_name"], harness.manifest.agent_kind)

                with patch("scripts.pisec.secretary.start_bound_agent", side_effect=launch):
                    ensure_secretary(store, project["project_id"], harness, workspace)

                self.assertEqual(len(captured), 1)
                self.assertIsNone(captured[0][0])
                self.assertIsNotNone(captured[0][1], captured)

    def test_fleet_secretary_is_a_tab_in_first_mate_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                store.conn.execute("UPDATE projects SET active=1 WHERE project_id=?", (project["project_id"],))
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                first_mate = ensure_first_mate(store, project["project_id"], harness, workspace)
                store.conn.execute("UPDATE projects SET coordination_mode='fleet' WHERE project_id=?", (project["project_id"],))
                secretary = ensure_secretary(store, project["project_id"], harness, workspace)
                self.assertEqual(secretary["binding"]["workspace_id"], first_mate["binding"]["workspace_id"])
                self.assertNotEqual(secretary["binding"]["workspace_view_id"], first_mate["binding"]["workspace_view_id"])
                self.assertEqual(len([call for call in workspace.calls if call[0] == "create_workspace"]), 1)
                self.assertIn((secretary["binding"]["workspace_view_id"], f"Project: {project['display_name']}"), workspace.renamed)

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
                legacy.pop("targetRef")
                store.conn.execute("UPDATE operations SET result_json=? WHERE kind='secretary.ensure' AND workstream_id=?", (json.dumps(legacy), workstream_id))
                second = ensure_secretary(store, project["project_id"], harness, workspace)
                self.assertTrue(second["reused"])
                self.assertEqual(second["workstream"]["provisioning_state"], "bound")
                repaired = json.loads(store.conn.execute("SELECT result_json FROM operations WHERE workstream_id=?", (workstream_id,)).fetchone()[0])
                self.assertIn("targetRef", repaired)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='secretary.scope.repaired'").fetchone()[0], 1)

    def test_ensure_repairs_missing_or_malformed_scope_before_restart(self):
        for stored_scope in (None, "{"):
            with self.subTest(stored_scope=stored_scope), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / "repo"
                make_repo(repo)
                with PiStore(root / "state") as store:
                    project = register_project(store, repo)
                    harness = FixtureHarness(root)
                    workspace = FixtureWorkspace(root, store)
                    first = ensure_secretary(store, project["project_id"], harness, workspace)
                    workstream_id = first["workstream"]["workstream_id"]
                    workspace.agents.clear()
                    store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason='workspace runtime is missing' WHERE workstream_id=?", (workstream_id,))
                    store.conn.execute("UPDATE operations SET result_json=? WHERE workstream_id=?", (stored_scope, workstream_id))
                    second = ensure_secretary(store, project["project_id"], harness, workspace)
                    self.assertTrue(second["reused"])
                    self.assertEqual(second["workstream"]["provisioning_state"], "bound")
                    repaired = json.loads(store.conn.execute("SELECT result_json FROM operations WHERE workstream_id=?", (workstream_id,)).fetchone()[0])
                    self.assertEqual(repaired["workstreamId"], workstream_id)
                    self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='secretary.scope.repaired'").fetchone()[0], 1)

    def test_inactive_attention_retries_through_normal_open_path(self):
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
                workspace.agents.clear()
                store.conn.execute("UPDATE projects SET active=0,lifecycle_attention_reason='project open requires repair' WHERE project_id=?", (project["project_id"],))
                store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason='workspace runtime is missing' WHERE workstream_id=?", (workstream_id,))
                store.conn.execute("UPDATE operations SET state='applying',step='map_committed' WHERE workstream_id=?", (workstream_id,))
                store.conn.execute("UPDATE runtime_bindings SET desired_generation_sha256=? WHERE workstream_id=?", ("0" * 64, workstream_id))
                retried = ensure_secretary(store, project["project_id"], harness, workspace)
                self.assertEqual(retried["project"]["active"], 1)
                self.assertEqual(retried["workstream"]["provisioning_state"], "bound")
                self.assertIsNone(retried["workstream"]["attention_reason"])
                self.assertEqual(harness.launch_replacements, [False, True])
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='secretary.binding.repaired'").fetchone()[0], 1)
                binding = store.conn.execute("SELECT desired_generation_sha256,applied_generation_sha256,launch_generation_sha256 FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()
                self.assertEqual(binding["desired_generation_sha256"], binding["applied_generation_sha256"])
                self.assertIsNone(binding["launch_generation_sha256"])

    def test_inactive_retry_guard_failures_remain_needs_attention(self):
        for runtime_state in ("live", "unknown"):
            with self.subTest(runtime_state=runtime_state), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / "repo"
                make_repo(repo)
                with PiStore(root / "state") as store:
                    project = register_project(store, repo)
                    harness = FixtureHarness(root)
                    workspace = FixtureWorkspace(root, store)
                    first = ensure_secretary(store, project["project_id"], harness, workspace)
                    workstream_id = first["workstream"]["workstream_id"]
                    surface_id = first["binding"]["workspace_surface_id"]
                    workspace.agents.clear()
                    workspace.runtime_states[surface_id] = runtime_state
                    store.conn.execute("UPDATE projects SET active=0 WHERE project_id=?", (project["project_id"],))
                    store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason='stale launch' WHERE workstream_id=?", (workstream_id,))
                    store.conn.execute("UPDATE operations SET state='applying',step='map_committed' WHERE workstream_id=?", (workstream_id,))
                    store.conn.execute("UPDATE runtime_bindings SET desired_generation_sha256=? WHERE workstream_id=?", ("0" * 64, workstream_id))
                    with self.assertRaisesRegex(NeedsAttentionError, "requires attention"):
                        ensure_secretary(store, project["project_id"], harness, workspace)
                    operation = store.conn.execute("SELECT state FROM operations WHERE workstream_id=?", (workstream_id,)).fetchone()
                    workstream = store.conn.execute("SELECT provisioning_state,attention_reason FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()
                    self.assertEqual(operation["state"], "needs_attention")
                    self.assertEqual(workstream["provisioning_state"], "needs_attention")
                    expected_reason = "stopped runtime" if runtime_state == "live" else "ambiguous"
                    self.assertIn(expected_reason, workstream["attention_reason"])

    def test_active_reopen_repairs_stale_generation_before_start(self):
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
                workspace.runtime_states[first["binding"]["workspace_surface_id"]] = "stopped"
                store.conn.execute("UPDATE runtime_bindings SET desired_generation_sha256=?,applied_generation_sha256=? WHERE workstream_id=?", ("0" * 64, "0" * 64, workstream_id))
                reopened = ensure_secretary(store, project["project_id"], harness, workspace)
                self.assertTrue(reopened["reused"])
                self.assertEqual(harness.launch_replacements, [False, True])
                binding = store.conn.execute("SELECT desired_generation_sha256,applied_generation_sha256,launch_generation_sha256 FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()
                self.assertEqual(binding["desired_generation_sha256"], binding["applied_generation_sha256"])
                self.assertIsNone(binding["launch_generation_sha256"])

    def test_deactivated_project_open_reuses_secretary_without_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                first = ensure_secretary(store, project["project_id"], harness, workspace)
                secretary_id = first["workstream"]["workstream_id"]
                deactivate_project(store, project["project_id"], workspace, harness)
                workspace.agents.clear()

                reopened = ensure_secretary(store, project["project_id"], harness, workspace)

                self.assertEqual(reopened["workstream"]["workstream_id"], secretary_id)
                self.assertEqual(reopened["project"]["active"], 1)
                self.assertEqual(reopened["workstream"]["desired_state"], "active")
                self.assertEqual(
                    store.conn.execute("SELECT COUNT(*) FROM workstreams WHERE project_id=? AND kind='secretary'", (project["project_id"],)).fetchone()[0],
                    1,
                )

    def test_inactive_missing_binding_rewinds_partial_open_saga(self):
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
                deactivate_project(store, project["project_id"], workspace, harness)
                workspace.agents.clear()
                with store.transaction():
                    store.conn.execute("UPDATE workstreams SET desired_state='active',provisioning_state='needs_attention',attention_reason='missing binding' WHERE workstream_id=?", (workstream_id,))
                    store.conn.execute("UPDATE projects SET active=0,lifecycle_attention_reason='project open requires repair' WHERE project_id=?", (project["project_id"],))
                    store.conn.execute("UPDATE operations SET state='needs_attention',step='map_committed' WHERE workstream_id=?", (workstream_id,))

                reopened = ensure_secretary(store, project["project_id"], harness, workspace)

                self.assertEqual(reopened["project"]["active"], 1)
                self.assertEqual(reopened["workstream"]["provisioning_state"], "bound")
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='secretary.ensure.rewound'").fetchone()[0], 1)

    def test_inactive_open_recreates_missing_stale_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)

                class MissingWorkspaceFixture(FixtureWorkspace):
                    fail_next_tab = True

                    def create_tab(self, *, workspace_id, cwd, label, focus=False):
                        if self.fail_next_tab:
                            self.fail_next_tab = False
                            raise PisecError(f"workspace {workspace_id} not found")
                        return super().create_tab(workspace_id=workspace_id, cwd=cwd, label=label, focus=focus)

                workspace = MissingWorkspaceFixture(root, store)
                first = ensure_secretary(store, project["project_id"], harness, workspace)
                workstream_id = first["workstream"]["workstream_id"]
                old_surface_id = first["binding"]["workspace_surface_id"]
                workspace.worktrees.pop(str(repo.resolve()), None)
                workspace.agents.clear()
                workspace.runtime_states.pop(old_surface_id, None)
                with store.transaction():
                    store.conn.execute(
                        "UPDATE projects SET active=0,lifecycle_attention_reason='project open requires repair' WHERE project_id=?",
                        (project["project_id"],),
                    )
                    store.conn.execute(
                        "UPDATE workstreams SET provisioning_state='needs_attention',attention_reason='stale binding' WHERE workstream_id=?",
                        (workstream_id,),
                    )
                    store.conn.execute(
                        "UPDATE operations SET state='needs_attention',step='map_committed' WHERE workstream_id=?",
                        (workstream_id,),
                    )

                reopened = ensure_secretary(store, project["project_id"], harness, workspace)

                self.assertEqual(reopened["project"]["active"], 1)
                self.assertEqual(reopened["workstream"]["provisioning_state"], "bound")
                self.assertNotEqual(reopened["binding"]["workspace_surface_id"], old_surface_id)
                self.assertIn(workstream_id, harness.cleaned)
                self.assertEqual(
                    store.conn.execute("SELECT COUNT(*) FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()[0],
                    1,
                )
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='secretary.binding.recreated'").fetchone()[0], 1)

    def test_secretary_replay_reuses_persisted_surface_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                with self.assertRaises(RuntimeError):
                    ensure_secretary(store, project["project_id"], harness, workspace, CrashOnce("after_secretary_profile_materialization"))
                self.assertEqual(harness.surface_calls, 1)
                (root / "runtime-surface" / "changed.txt").write_text("changed\n")
                with self.assertRaises(NeedsAttentionError):
                    ensure_secretary(store, project["project_id"], harness, workspace)
                self.assertEqual(harness.surface_calls, 1)

    def test_secretary_persists_production_sized_surface_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                harness.surface_extra = "x" * 5000
                workspace = FixtureWorkspace(root, store)
                result = ensure_secretary(store, project["project_id"], harness, workspace)
                self.assertEqual(result["workstream"]["provisioning_state"], "bound")
                operation = store.conn.execute("SELECT result_json FROM operations WHERE kind='secretary.ensure'").fetchone()
                snapshot = json.loads(operation["result_json"])
                self.assertGreater(len(snapshot["runtimeSurfaceManifest"]), 4096)
                self.assertEqual(json.loads(snapshot["runtimeSurfaceManifest"])["surfaceExtra"], "x" * 5000)

    def test_recover_start_relaunches_stopped_runtime_with_stale_agent_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                first = ensure_secretary(store, project["project_id"], harness, workspace)
                surface_id = first["binding"]["workspace_surface_id"]
                workspace.runtime_states[surface_id] = "stopped"
                second = ensure_secretary(store, project["project_id"], harness, workspace)
                self.assertTrue(second["reused"])
                self.assertEqual(len([call for call in workspace.calls if call[0] == "start"]), 2)

    def test_ensure_refuses_scope_identity_mismatch(self):
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
                row = store.conn.execute("SELECT result_json FROM operations WHERE workstream_id=?", (workstream_id,)).fetchone()
                scope = json.loads(row[0])
                scope["projectId"] = "prj_" + "0" * 32
                store.conn.execute("UPDATE operations SET result_json=? WHERE workstream_id=?", (canonical_json(scope), workstream_id))
                with self.assertRaisesRegex(NeedsAttentionError, "scope identity mismatch"):
                    ensure_secretary(store, project["project_id"], harness, workspace)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='secretary.scope.repaired'").fetchone()[0], 0)

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
