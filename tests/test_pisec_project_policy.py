from pathlib import Path
import tempfile
import unittest

from scripts.pisec.models import ConflictError, InvalidRequestError
from scripts.pisec.pi_store import PiStore
from scripts.pisec.project_workspaces import project_workspace
from scripts.pisec.projects import register_project, update_project_policy
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
