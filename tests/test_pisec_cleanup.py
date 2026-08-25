from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.pisec.cleanup import cleanup_workstream
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import _git, register_project
from scripts.pisec.secretary import ensure_secretary
from scripts.pisec.workflow import submit_completion
from scripts.pisec.integration import apply_workstream_acceptance, prepare_workstream_acceptance, reconcile_integrations
from scripts.pisec.workstreams import authorize_apply_workstream, complete_workstream, prepare_workstream, retire_workstream
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace, make_repo


class FailingWorkspace(FixtureWorkspace):
    def close_tab(self, view_id):
        raise RuntimeError(f"cannot close {view_id}")


class CleanupTests(unittest.TestCase):
    def setup_bound_worker(self, root: Path):
        repo = root / "repo"
        make_repo(repo)
        store = PiStore(root / "state")
        project = register_project(store, repo)
        harness = FixtureHarness(root)
        workspace = FixtureWorkspace(root, store)
        ensure_secretary(store, project["project_id"], harness, workspace)
        packet = {"schemaVersion": 1, "outcome": "Cleanup behavior is verified.", "boundaries": ["Cleanup only."], "acceptance": ["Cleanup completes."], "openQuestions": [], "evidence": ["Fixture."]}
        prepared = prepare_workstream(store, project_id=project["project_id"], title="Worker", purpose="Test cleanup", brief="Test cleanup", task_packet=packet, idempotency_key="cleanup-test", harness=harness, workspace=workspace, work_root=root / "worktrees")
        result = authorize_apply_workstream(store, scope=prepared["approvalScope"], harness=harness, workspace=workspace)
        return store, project, harness, workspace, result["workstream"]

    def submit_completion(self, store, workstream):
        binding = store.conn.execute("SELECT runtime_instance_id FROM runtime_bindings WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()
        task = store.conn.execute("SELECT packet_sha256 FROM task_packets WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()
        source = _git(Path(workstream["worktree_path"]), "rev-parse", "HEAD").lower()
        packet = {
            "acceptance": [{"criterion": "Cleanup completes.", "status": "passed", "evidence": ["Fixture."]}],
            "verification": [{"command": "fixture cleanup", "result": "passed"}],
            "sourceCommit": source,
            "taskPacketSha256": task["packet_sha256"],
            "changedSurfaces": ["fixture"],
            "residualRisk": "none",
        }
        return submit_completion(store, workstream_id=workstream["workstream_id"], runtime_instance_id=binding["runtime_instance_id"], packet=packet)

    def test_cleanup_removes_checkout_and_retains_branch_and_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project, harness, workspace, workstream = self.setup_bound_worker(root)
            self.addCleanup(store.close)
            completion = self.submit_completion(store, workstream)
            acceptance = prepare_workstream_acceptance(store, project["project_id"], workstream["workstream_id"])
            apply_workstream_acceptance(store, project["project_id"], acceptance["approvalScope"])
            result = reconcile_integrations(store, workspace=workspace, harness=harness)
            worktree = Path(workstream["worktree_path"])
            self.assertFalse(worktree.exists())
            branch = subprocess.run(["git", "-C", str(project["repository_path"]), "symbolic-ref", "--short", "HEAD"], check=True, text=True, capture_output=True).stdout.strip()
            self.assertEqual(branch, "main")
            self.assertIsNone(store.conn.execute("SELECT 1 FROM runtime_bindings WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone())
            self.assertEqual(result["processed"][0]["state"], "integrated")

    def test_unexpected_cleanup_failure_records_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project, harness, workspace, workstream = self.setup_bound_worker(root)
            self.addCleanup(store.close)
            completion = self.submit_completion(store, workstream)
            acceptance = prepare_workstream_acceptance(store, project["project_id"], workstream["workstream_id"])
            apply_workstream_acceptance(store, project["project_id"], acceptance["approvalScope"])
            complete_workstream(store, project["project_id"], workstream["workstream_id"], completion["packet_sha256"], workspace)
            store.conn.execute("UPDATE runtime_bindings SET observed_state='idle' WHERE workstream_id=?", (workstream["workstream_id"],))
            retire_workstream(store, project["project_id"], workstream["workstream_id"], workspace)
            failing = FailingWorkspace(root, store)
            with self.assertRaisesRegex(Exception, "cleanup"):
                cleanup_workstream(store, {"workstreamId": workstream["workstream_id"], "confirm": workstream["workstream_id"]}, failing, harness)
            operation = store.conn.execute("SELECT state,error_code,error_message FROM operations WHERE kind='workstream.cleanup'").fetchone()
            row = store.conn.execute("SELECT provisioning_state,attention_reason FROM workstreams WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()
            self.assertEqual(tuple(operation), ("needs_attention", "cleanup_failed", "cannot close fixture-view-2"))
            self.assertEqual(tuple(row), ("needs_attention", "cannot close fixture-view-2"))


if __name__ == "__main__":
    unittest.main()
