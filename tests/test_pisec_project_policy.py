from pathlib import Path
import tempfile
import unittest

from scripts.pisec.models import AuthorizationError
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import fleet_activity, list_fleet_projects, register_project, require_fleet_project
from tests.pisec_fixture import make_repo


class ProjectModeTests(unittest.TestCase):
    def test_fresh_projects_use_invariant_project_mode_without_policy_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                self.assertEqual(project["coordination_mode"], "project")
                self.assertFalse(project["active"])
                self.assertEqual(project["data_dirs"], [])
                self.assertEqual(project["external_domains"], [])
                self.assertNotIn("worker_creation_policy", project)
                self.assertNotIn("merge_policy", project)

    def test_fleet_scope_is_explicit_database_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fleet_repo = root / "fleet"
            project_repo = root / "project"
            make_repo(fleet_repo)
            make_repo(project_repo)
            with PiStore(root / "state") as store:
                fleet = register_project(store, fleet_repo)
                project = register_project(store, project_repo)
                store.conn.execute("UPDATE projects SET coordination_mode='fleet',active=1 WHERE project_id=?", (fleet["project_id"],))
                store.conn.execute("UPDATE projects SET active=1 WHERE project_id=?", (project["project_id"],))
                self.assertEqual([row["project_id"] for row in list_fleet_projects(store)], [fleet["project_id"]])
                self.assertEqual(fleet_activity(store)["projects"][0]["projectId"], fleet["project_id"])
                self.assertEqual(require_fleet_project(store, fleet["project_id"])["project_id"], fleet["project_id"])
                with self.assertRaises(AuthorizationError):
                    require_fleet_project(store, project["project_id"])


if __name__ == "__main__":
    unittest.main()
