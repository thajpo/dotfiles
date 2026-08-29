from pathlib import Path
import json
import subprocess
import tempfile
import unittest

from scripts.pisec.decisions import list_decisions, record_decision, resolve_decision
from scripts.pisec.models import ConflictError, InvalidRequestError
from scripts.pisec.first_mate import ensure_first_mate
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import deactivate_project, list_projects, observe_project, register_project, resolve_project
from scripts.pisec.secretary import ensure_secretary
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace


def make_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Pisec Test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "pisec@example.invalid"], check=True)
    (path / "README").write_text("fixture\n")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "fixture"], check=True)


class ProjectTests(unittest.TestCase):
    def test_deactivate_fleet_project_closes_tab_not_first_mate_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control_repo = root / "control"
            fleet_repo = root / "fleet"
            make_repo(control_repo)
            make_repo(fleet_repo)
            with PiStore(root / "state") as store:
                control = register_project(store, control_repo)
                fleet = register_project(store, fleet_repo)
                store.conn.execute("UPDATE projects SET active=1 WHERE project_id IN (?,?)", (control["project_id"], fleet["project_id"]))
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                first_mate = ensure_first_mate(store, control["project_id"], harness, workspace)
                store.conn.execute("UPDATE projects SET coordination_mode='fleet' WHERE project_id=?", (fleet["project_id"],))
                secretary = ensure_secretary(store, fleet["project_id"], harness, workspace)
                store.conn.execute("UPDATE runtime_bindings SET observed_state='idle' WHERE workstream_id=?", (secretary["workstream"]["workstream_id"],))

                deactivate_project(store, fleet["project_id"], workspace, harness)

                self.assertIn(("close_tab", secretary["binding"]["workspace_view_id"]), workspace.calls)
                self.assertNotIn(("close", first_mate["binding"]["workspace_id"]), workspace.calls)
                self.assertIsNotNone(store.conn.execute("SELECT 1 FROM runtime_bindings WHERE workstream_id=?", (first_mate["workstream"]["workstream_id"],)).fetchone())
                self.assertIsNone(store.conn.execute("SELECT 1 FROM project_workspaces WHERE project_id=?", (fleet["project_id"],)).fetchone())

    def test_registration_uses_common_git_directory_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            link = root / "alias"
            link.symlink_to(repo, target_is_directory=True)
            with PiStore(root / "state") as store:
                first = register_project(store, repo, display_name="Example", default_ref="main")
                second = register_project(store, link, display_name="Ignored", default_ref="main")
                self.assertEqual(first["project_id"], second["project_id"])
                self.assertEqual(first["git_common_dir"], observe_project(repo)["git_common_dir"])
                self.assertEqual(store.conn.execute("SELECT count(*) FROM projects").fetchone()[0], 1)

    def test_register_persists_data_dirs_within_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            data = repo / "data"
            data.mkdir()
            with PiStore(root / "state") as store:
                project = register_project(store, repo, default_ref="main", data_dirs=[str(data)])
                self.assertEqual(project["data_dirs"], [str(data.resolve())])
                self.assertEqual(store.conn.execute("SELECT data_dirs FROM projects WHERE project_id=?", (project["project_id"],)).fetchone()[0], json.dumps([str(data.resolve())], sort_keys=True))
                with self.assertRaises(InvalidRequestError):
                    register_project(store, repo, default_ref="main", data_dirs=[str(root / "outside")])

    def test_project_selectors_and_lists_return_typed_policy_arrays(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo, default_ref="main")
                selected = resolve_project(store, project["display_name"])
                self.assertEqual(selected["data_dirs"], [])
                self.assertEqual(selected["external_domains"], [])
                store.conn.execute("UPDATE projects SET active=1 WHERE project_id=?", (project["project_id"],))
                listed = list_projects(store)[0]
                self.assertEqual(listed["data_dirs"], [])
                self.assertEqual(listed["external_domains"], [])

    def test_linked_worktree_observes_same_common_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            linked = root / "linked"
            make_repo(repo)
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "other", str(linked), "main"], check=True)
            self.assertEqual(observe_project(repo)["git_common_dir"], observe_project(linked)["git_common_dir"])

    def test_deactivate_and_reactivate_project_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo, default_ref="main")
                store.conn.execute("UPDATE projects SET active=1 WHERE project_id=?", (project["project_id"],))

                class _Workspace:
                    manifest = type("M", (), {"adapter_id": "herdr", "session_name": "herdr"})()

                result = deactivate_project(store, project["project_id"], _Workspace(), None)
                self.assertFalse(result["reused"])
                self.assertIsNone(result["workstreamId"])
                row = store.conn.execute("SELECT active,deactivated_at,secretary_workstream_id FROM projects WHERE project_id=?", (project["project_id"],)).fetchone()
                self.assertEqual(row[0], 0)
                self.assertIsNotNone(row[1])
                self.assertIsNone(row[2])
                self.assertEqual([item["project_id"] for item in list_projects(store)], [])
                self.assertEqual([item["project_id"] for item in list_projects(store, include_inactive=True)], [project["project_id"]])
                reused = deactivate_project(store, project["display_name"], _Workspace(), None)
                self.assertTrue(reused["reused"])
                events = store.conn.execute("SELECT kind FROM events WHERE project_id=? ORDER BY sequence", (project["project_id"],)).fetchall()
                self.assertEqual([event[0] for event in events], ["project.registered", "project.deactivated"])
                state = store.conn.execute("SELECT active,deactivated_at,lifecycle_attention_reason FROM projects WHERE project_id=?", (project["project_id"],)).fetchone()
                self.assertEqual(state[0], 0)
                self.assertIsNotNone(state[1])
                self.assertIsNone(state[2])

    def test_deactivate_refuses_active_worker_workstreams(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo, default_ref="main")
                store.conn.execute("UPDATE projects SET active=1 WHERE project_id=?", (project["project_id"],))
                now = "2026-08-21T00:00:00Z"
                store.conn.execute(
                    "INSERT INTO workstreams(workstream_id,project_id,kind,title,purpose,brief,harness_id,workspace_adapter_id,execution_profile,target_ref,base_commit_oid,branch_name,worktree_path,desired_state,provisioning_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("ws_" + "0" * 32, project["project_id"], "worker", "Worker", "purpose", "brief", "omp", "herdr", "worker-default", "main", "0" * 40, "worker/ws_" + "0" * 12, str(root / "wt"), "active", "bound", now, now),
                )
                with self.assertRaises(ConflictError):
                    deactivate_project(store, project["project_id"], None, None)
                self.assertIsNone(
                    store.conn.execute(
                        "SELECT 1 FROM operations WHERE project_id=? AND kind='project.deactivate'",
                        (project["project_id"],),
                    ).fetchone()
                )
                state = store.conn.execute(
                    "SELECT active,lifecycle_attention_reason FROM projects WHERE project_id=?",
                    (project["project_id"],),
                ).fetchone()
                self.assertEqual(tuple(state), (1, None))

    def test_decisions_are_project_scoped_and_resolve_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo, default_ref="main")
                decision = record_decision(store, project_id=project["project_id"], summary="Use exact scopes", context={"reason": "fail closed"})
                self.assertEqual([decision["decision_id"]], [item["decision_id"] for item in list_decisions(store, project["project_id"], state="open")])
                resolved = resolve_decision(store, project_id=project["project_id"], decision_id=decision["decision_id"], resolution="Approved")
                self.assertEqual(resolved["state"], "resolved")
                self.assertEqual(resolve_decision(store, project_id=project["project_id"], decision_id=decision["decision_id"], resolution="Approved")["resolution"], "Approved")


if __name__ == "__main__":
    unittest.main()
