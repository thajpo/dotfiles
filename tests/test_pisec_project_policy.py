from pathlib import Path
import tempfile
import unittest

from scripts.pisec.models import AuthorizationError, ConflictError, InvalidRequestError
from scripts.pisec.first_mate import ensure_first_mate
from scripts.pisec.pi_store import PiStore
from scripts.pisec.project_workspaces import project_workspace
from scripts.pisec.projects import fleet_activity, list_fleet_projects, register_project, require_fleet_project, update_project_policy
from scripts.pisec.secretary import ensure_secretary
from scripts.pisec.workstreams import authorize_apply_workstream, prepare_workstream
from tests.pisec_fixture import FixtureGitObjects, FixtureHarness, FixtureWorkspace, make_repo


PACKET = {
    "schemaVersion": 1,
    "outcome": "Implement a bounded change",
    "boundaries": ["stay in the project checkout"],
    "acceptance": ["focused tests pass"],
    "openQuestions": [],
    "evidence": [],
}


class ProjectPolicyTests(unittest.TestCase):
    def test_fleet_worker_is_a_project_labeled_first_mate_tab(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            harness = FixtureHarness(root)
            with PiStore(root / "state") as store:
                workspace = FixtureWorkspace(root, store)
                project = register_project(store, repo)
                first_mate = ensure_first_mate(store, project["project_id"], harness, workspace)
                update_project_policy(store, project["project_id"], coordination_mode="fleet")
                prepared = prepare_workstream(
                    store,
                    project_id=project["project_id"],
                    title="Fleet task",
                    purpose="Verify fleet tab placement",
                    brief="Stay inside the approved worktree.",
                    task_packet=PACKET,
                    idempotency_key="fleet-tab-worker",
                    harness=harness,
                    workspace=workspace,
                )
                applied = authorize_apply_workstream(
                    store,
                    scope=prepared["approvalScope"],
                    harness=harness,
                    workspace=workspace,
                    git_objects=FixtureGitObjects(),
                    actor="first_mate",
                )
                binding = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (applied["workstream"]["workstream_id"],)).fetchone()
                self.assertEqual(binding["workspace_id"], first_mate["binding"]["workspace_id"])
                create = next(call for call in workspace.calls if call[0] == "create_tab")
                self.assertEqual(create[1][0], first_mate["binding"]["workspace_id"])
                self.assertEqual(create[1][2], f"{project['display_name']}: Fleet task")

    def test_defaults_are_safe_and_policy_update_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                self.assertEqual(project["coordination_mode"], "direct")
                self.assertEqual(project["worker_creation_policy"], "review")
                self.assertEqual(project["merge_policy"], "review")
                updated = update_project_policy(
                    store,
                    project["project_id"],
                    coordination_mode="fleet",
                    worker_creation_policy="bounded_auto",
                    worker_creation_policy_json={"workerLimit": 2, "approvedProfiles": ["worker-default"]},
                )
                self.assertFalse(updated["reused"])
                self.assertEqual(updated["coordination_mode"], "fleet")
                self.assertEqual(updated["worker_creation_policy"], "bounded_auto")
                self.assertEqual(updated["worker_creation_policy_json"]["workerLimit"], 2)

    def test_provisioned_fleet_project_must_deactivate_before_leaving_fleet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control_repo = root / "control"
            fleet_repo = root / "fleet"
            make_repo(control_repo)
            make_repo(fleet_repo)
            harness = FixtureHarness(root)
            with PiStore(root / "state") as store:
                workspace = FixtureWorkspace(root, store)
                control = register_project(store, control_repo)
                fleet = register_project(store, fleet_repo)
                ensure_first_mate(store, control["project_id"], harness, workspace)
                update_project_policy(store, fleet["project_id"], coordination_mode="fleet")
                ensure_secretary(store, fleet["project_id"], harness, workspace)
                with self.assertRaises(ConflictError):
                    update_project_policy(store, fleet["project_id"], coordination_mode="project")

    def test_first_mate_scope_only_includes_fleet_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fleet_repo = root / "fleet-repo"
            secretary_repo = root / "secretary-repo"
            make_repo(fleet_repo)
            make_repo(secretary_repo)
            with PiStore(root / "state") as store:
                fleet_project = register_project(store, fleet_repo)
                secretary_project = register_project(store, secretary_repo)
                update_project_policy(store, fleet_project["project_id"], coordination_mode="fleet")
                update_project_policy(store, secretary_project["project_id"], coordination_mode="project")

                self.assertEqual([row["project_id"] for row in list_fleet_projects(store)], [fleet_project["project_id"]])
                self.assertEqual(fleet_activity(store)["projects"], [{"projectId": fleet_project["project_id"], "displayName": fleet_project["display_name"], "cards": []}])
                self.assertEqual(require_fleet_project(store, fleet_project["project_id"])["project_id"], fleet_project["project_id"])
                with self.assertRaises(AuthorizationError):
                    require_fleet_project(store, secretary_project["project_id"])

    def test_bounded_worker_policy_rechecks_profile_and_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            harness = FixtureHarness(root)
            workspace = FixtureWorkspace(root)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                update_project_policy(
                    store,
                    project["project_id"],
                    worker_creation_policy="bounded_auto",
                    worker_creation_policy_json={"workerLimit": 1, "approvedProfiles": ["worker-default"]},
                )
                with self.assertRaises(ConflictError):
                    prepare_workstream(
                        store,
                        project_id=project["project_id"],
                        title="Networked worker",
                        purpose="Should be rejected",
                        brief="The profile is outside policy.",
                        task_packet=PACKET,
                        idempotency_key="bounded-profile-rejected",
                        execution_profile="worker-networked",
                        harness=harness,
                        workspace=workspace,
                    )
                prepared = prepare_workstream(
                    store,
                    project_id=project["project_id"],
                    title="First worker",
                    purpose="Consume the one-worker bound",
                    brief="The profile is approved.",
                    task_packet=PACKET,
                    idempotency_key="bounded-first-worker",
                    harness=harness,
                    workspace=workspace,
                )
                authorize_apply_workstream(
                    store,
                    scope=prepared["approvalScope"],
                    harness=harness,
                    workspace=workspace,
                    git_objects=FixtureGitObjects(),
                    actor="first_mate",
                )
                with self.assertRaises(ConflictError):
                    prepare_workstream(
                        store,
                        project_id=project["project_id"],
                        title="Second worker",
                        purpose="Should exceed the bound",
                        brief="The policy permits only one active worker.",
                        task_packet=PACKET,
                        idempotency_key="bounded-second-worker",
                        harness=harness,
                        workspace=workspace,
                    )

    def test_automatic_policy_modes_require_bounded_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                with self.assertRaises(InvalidRequestError):
                    update_project_policy(
                        store,
                        project["project_id"],
                        worker_creation_policy="bounded_auto",
                        worker_creation_policy_json={"approvedProfiles": ["worker-default"]},
                    )
                with self.assertRaises(InvalidRequestError):
                    update_project_policy(
                        store,
                        project["project_id"],
                        merge_policy="checked_auto",
                        merge_policy_json={"requiredChecks": ["tests"]},
                    )

    def test_worker_apply_owns_project_workspace_without_secretary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            harness = FixtureHarness(root)
            workspace = FixtureWorkspace(root)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                prepared = prepare_workstream(
                    store,
                    project_id=project["project_id"],
                    title="Independent worker",
                    purpose="Test project-owned workspace ownership",
                    brief="Work only within the approved project scope.",
                    task_packet=PACKET,
                    idempotency_key="worker-without-secretary",
                    harness=harness,
                    workspace=workspace,
                )
                applied = authorize_apply_workstream(
                    store,
                    scope=prepared["approvalScope"],
                    harness=harness,
                    workspace=workspace,
                    git_objects=FixtureGitObjects(),
                    actor="first_mate",
                )
                workstream_id = applied["workstream"]["workstream_id"]
                self.assertIsNone(store.conn.execute("SELECT 1 FROM workstreams WHERE project_id=? AND kind='secretary'", (project["project_id"],)).fetchone())
                owned = project_workspace(store, project["project_id"])
                self.assertIsNotNone(owned)
                binding = store.conn.execute("SELECT workspace_id FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()
                self.assertEqual(binding["workspace_id"], owned["workspace_id"])


if __name__ == "__main__":
    unittest.main()
