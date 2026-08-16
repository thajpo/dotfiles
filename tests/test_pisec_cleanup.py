from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.pisec.cleanup import cleanup_workstream
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import register_project
from scripts.pisec.workstreams import authorize_apply_workstream, complete_workstream, prepare_workstream, retire_workstream
from tests.pisec_fixture import FixtureGitObjects, FixtureHarness, FixtureWorkspace, make_repo


class FailingWorkspace(FixtureWorkspace):
    def close_workspace(self, workspace_id):
        raise RuntimeError(f"cannot close {workspace_id}")


class CleanupTests(unittest.TestCase):
    def setup_bound_worker(self, root: Path):
        repo = root / "repo"
        make_repo(repo)
        store = PiStore(root / "state")
        project = register_project(store, repo)
        harness = FixtureHarness(root)
        workspace = FixtureWorkspace(root, store)
        packet = {"schemaVersion": 1, "outcome": "Cleanup behavior is verified.", "boundaries": ["Cleanup only."], "acceptance": ["Cleanup completes."], "openQuestions": [], "evidence": ["Fixture."]}
        prepared = prepare_workstream(store, project_id=project["project_id"], title="Worker", purpose="Test cleanup", brief="Test cleanup", task_packet=packet, idempotency_key="cleanup-test", harness=harness, workspace=workspace, work_root=root / "worktrees", object_root=root / "objects")
        result = authorize_apply_workstream(store, scope=prepared["approvalScope"], harness=harness, workspace=workspace, git_objects=FixtureGitObjects())
        return store, project, harness, workspace, result["workstream"]

    def test_cleanup_removes_checkout_and_retains_branch_and_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project, harness, workspace, workstream = self.setup_bound_worker(root)
            self.addCleanup(store.close)
            complete_workstream(store, project["project_id"], workstream["workstream_id"])
            store.conn.execute("UPDATE runtime_bindings SET observed_state='idle' WHERE workstream_id=?", (workstream["workstream_id"],))
            retire_workstream(store, project["project_id"], workstream["workstream_id"], workspace)
            worktree = Path(workstream["worktree_path"])
            binding = store.conn.execute("SELECT private_git_object_dir FROM runtime_bindings WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()
            private_objects = Path(binding["private_git_object_dir"])
            result = cleanup_workstream(store, {"workstreamId": workstream["workstream_id"], "confirm": workstream["workstream_id"]}, workspace, harness)
            self.assertFalse(worktree.exists())
            branch = subprocess.run(["git", "-C", str(project["repository_path"]), "for-each-ref", "--format=%(refname:short)", f"refs/heads/{workstream['branch_name']}"], check=True, text=True, capture_output=True).stdout.strip()
            self.assertEqual(branch, workstream["branch_name"])
            self.assertTrue(private_objects.exists())
            self.assertEqual(result["operation"]["state"], "succeeded")
            self.assertIsNone(result["operation"]["error_code"])
            self.assertIsNone(result["operation"]["error_message"])

    def test_unexpected_cleanup_failure_records_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project, harness, workspace, workstream = self.setup_bound_worker(root)
            self.addCleanup(store.close)
            complete_workstream(store, project["project_id"], workstream["workstream_id"])
            store.conn.execute("UPDATE runtime_bindings SET observed_state='idle' WHERE workstream_id=?", (workstream["workstream_id"],))
            retire_workstream(store, project["project_id"], workstream["workstream_id"], workspace)
            failing = FailingWorkspace(root, store)
            with self.assertRaisesRegex(Exception, "cleanup"):
                cleanup_workstream(store, {"workstreamId": workstream["workstream_id"], "confirm": workstream["workstream_id"]}, failing, harness)
            operation = store.conn.execute("SELECT state,error_code,error_message FROM operations WHERE kind='workstream.cleanup'").fetchone()
            row = store.conn.execute("SELECT provisioning_state,attention_reason FROM workstreams WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()
            self.assertEqual(tuple(operation), ("needs_attention", "cleanup_failed", "cannot close fixture-workspace-1"))
            self.assertEqual(tuple(row), ("needs_attention", "cannot close fixture-workspace-1"))


if __name__ == "__main__":
    unittest.main()
