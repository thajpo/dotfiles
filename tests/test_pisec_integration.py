from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.pisec.integration import apply_workstream_acceptance, prepare_workstream_acceptance, reconcile_integrations
from scripts.pisec.models import ConflictError
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import _git, register_project
from scripts.pisec.secretary import ensure_secretary
from scripts.pisec.workflow import checkpoint
from scripts.pisec.workstreams import authorize_apply_workstream, prepare_workstream
from scripts.pisec.git_runner import run_git
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace, make_repo


def git_worker(path: Path, *args: str) -> str:
    return run_git(path, args, role="worker").stdout.strip()


class IntegrationTests(unittest.TestCase):
    def fixture(self, root: Path):
        repo = root / "repo"
        make_repo(repo)
        repo.chmod(0o755)
        (repo / ".git" / "objects").chmod(0o700)
        (repo / ".git" / "objects" / "pack").chmod(0o700)
        store = PiStore(root / "state")
        project = register_project(store, repo, default_ref="main")
        harness = FixtureHarness(root)
        workspace = FixtureWorkspace(root, store)
        ensure_secretary(store, project["project_id"], harness, workspace)
        prepared = prepare_workstream(
            store,
            project_id=project["project_id"],
            title="Worker",
            purpose="Exercise secretary integration",
            brief="Make one bounded fixture change.",
            task_packet={"schemaVersion": 1, "outcome": "The fixture change is integrated.", "boundaries": ["Change the fixture only."], "acceptance": ["The fixture check passes."], "openQuestions": [], "evidence": ["Fixture output."]},
            idempotency_key="integration-worker",
            target_ref="main",
            harness=harness,
            workspace=workspace,
            work_root=root / "worktrees",
        )
        scope = prepared["approvalScope"]
        full_scope = dict(__import__("json").loads(store.conn.execute("SELECT result_json FROM operations WHERE workstream_id=?", (scope["workstreamId"],)).fetchone()[0]))
        authorize_apply_workstream(store, scope=full_scope, harness=harness, workspace=workspace)
        workstream = dict(store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (scope["workstreamId"],)).fetchone())
        worktree = Path(workstream["worktree_path"])
        (worktree / "feature.txt").write_text("implemented\n")
        git_worker(worktree, "add", "feature.txt")
        git_worker(worktree, "commit", "-qm", "implement feature")
        source = git_worker(worktree, "rev-parse", "HEAD").lower()
        binding = store.conn.execute("SELECT runtime_instance_id FROM runtime_bindings WHERE workstream_id=?", (scope["workstreamId"],)).fetchone()
        task = store.conn.execute("SELECT packet_sha256 FROM task_packets WHERE workstream_id=?", (scope["workstreamId"],)).fetchone()
        checkpoint(
            store,
            workstream_id=scope["workstreamId"],
            runtime_instance_id=binding["runtime_instance_id"],
            phase="ready_review",
            summary="Implementation is verified.",
            next_action="Review the candidate.",
            blocker_code=None,
            blocker=None,
            evidence=["fixture verification"],
            idempotency_key="integration-ready",
            completion_packet={
                "acceptance": [{"criterion": "The fixture check passes.", "status": "passed", "evidence": ["Fixture output."]}],
                "verification": [{"command": "fixture verification", "result": "passed"}],
                "sourceCommit": source,
                "taskPacketSha256": task["packet_sha256"],
                "changedSurfaces": ["fixture"],
                "residualRisk": "none",
            },
        )
        return store, project, harness, workspace, workstream, scope, repo, worktree, None, source

    def test_acceptance_is_the_only_user_gate_and_secretary_closes_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project, harness, workspace, workstream, scope, repo, worktree, private_objects, source = self.fixture(root)
            self.addCleanup(store.close)
            packet = store.conn.execute("SELECT packet_sha256,accepted_at FROM completion_packets WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()
            self.assertIsNone(packet["accepted_at"])
            prepared = prepare_workstream_acceptance(store, project["project_id"], workstream["workstream_id"])
            self.assertNotIn("targetCommitOid", prepared["approvalScope"])
            accepted = apply_workstream_acceptance(store, project["project_id"], prepared["approvalScope"])
            self.assertFalse(accepted["reused"])
            replay = apply_workstream_acceptance(store, project["project_id"], prepared["approvalScope"])
            self.assertTrue(replay["reused"])
            self.assertIsNotNone(store.conn.execute("SELECT accepted_at FROM completion_packets WHERE packet_sha256=?", (packet["packet_sha256"],)).fetchone()[0])
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE completion_packets SET accepted_at=? WHERE packet_sha256=?", ("2026-01-02T00:00:00Z", packet["packet_sha256"]))
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE workstream_acceptances SET scope_json=? WHERE acceptance_id=?", ("{}", accepted["acceptance"]["acceptance_id"]))
            binding = store.conn.execute("SELECT runtime_instance_id FROM runtime_bindings WHERE workstream_id=?", (scope["workstreamId"],)).fetchone()
            task = store.conn.execute("SELECT packet_sha256 FROM task_packets WHERE workstream_id=?", (scope["workstreamId"],)).fetchone()
            checkpoint(
                store,
                workstream_id=scope["workstreamId"],
                runtime_instance_id=binding["runtime_instance_id"],
                phase="ready_review",
                summary="Verification was rerun.",
                next_action="Continue existing integration.",
                blocker_code=None,
                blocker=None,
                evidence=["rerun verification"],
                idempotency_key="integration-ready-rerun",
                completion_packet={
                    "acceptance": [{"criterion": "The fixture check passes.", "status": "passed", "evidence": ["Fixture output."]}],
                    "verification": [{"command": "fixture verification rerun", "result": "passed"}],
                    "sourceCommit": source,
                    "taskPacketSha256": task["packet_sha256"],
                    "changedSurfaces": ["fixture"],
                    "residualRisk": "none",
                },
            )
            with self.assertRaises(ConflictError):
                prepare_workstream_acceptance(store, project["project_id"], workstream["workstream_id"])
            dirty = repo / "unrelated-untracked.txt"
            dirty.write_text("temporary\n")
            replay_dirty = apply_workstream_acceptance(store, project["project_id"], prepared["approvalScope"])
            self.assertTrue(replay_dirty["reused"])
            blocked = reconcile_integrations(store, workspace, harness)
            self.assertEqual(blocked["processed"][0]["state"], "needs_attention")
            dirty.unlink()
            result = reconcile_integrations(store, workspace, harness)
            self.assertEqual(result["errors"], [], result)
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["processed"][0].get("state"), "integrated", {"result": result, "job": dict(store.conn.execute("SELECT * FROM integration_jobs").fetchone())})
            self.assertEqual(result["processed"][0]["closeout"]["state"], "retired")
            job = store.conn.execute("SELECT integration_jobs.state,strategy,accepted_source_commit_oid,integration_jobs.attempt FROM integration_jobs JOIN merge_receipts USING(integration_id) WHERE integration_jobs.integration_id=?", (accepted["integration"]["integration_id"],)).fetchone()
            self.assertEqual(tuple(job), ("integrated", "rebase-then-ff", source, 2))
            self.assertEqual(_git(repo, "rev-parse", "HEAD"), source)
            self.assertFalse(worktree.exists())
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='project.git_integrated'").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE integration_reports SET residual_risk=? WHERE integration_id=?", ("changed", accepted["integration"]["integration_id"]))

    def test_target_drift_is_reconciled_by_the_worker_without_a_second_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project, harness, workspace, workstream, _scope, repo, _worktree, _private_objects, _source = self.fixture(root)
            self.addCleanup(store.close)
            prepared = prepare_workstream_acceptance(store, project["project_id"], workstream["workstream_id"])
            accepted = apply_workstream_acceptance(store, project["project_id"], prepared["approvalScope"])
            (repo / "target.txt").write_text("advanced\n")
            _git(repo, "add", "target.txt")
            _git(repo, "commit", "-qm", "advance target")
            result = reconcile_integrations(store, workspace, harness)
            self.assertEqual(result["processed"][0]["state"], "awaiting_worker")
            job = store.conn.execute("SELECT state,next_action FROM integration_jobs WHERE integration_id=?", (accepted["integration"]["integration_id"],)).fetchone()
            self.assertEqual(job["state"], "awaiting_worker")
            self.assertIn("new ready_review checkpoint", job["next_action"])
            self.assertIn("rebase", workspace.prompts[-1][1].lower())
            prompt_count = len(workspace.prompts)
            attempt = store.conn.execute("SELECT attempt FROM integration_jobs WHERE integration_id=?", (accepted["integration"]["integration_id"],)).fetchone()["attempt"]
            replay = reconcile_integrations(store, workspace, harness)
            self.assertEqual(replay["processed"][0]["state"], "awaiting_worker")
            self.assertEqual(len(workspace.prompts), prompt_count)
            self.assertEqual(store.conn.execute("SELECT attempt FROM integration_jobs WHERE integration_id=?", (accepted["integration"]["integration_id"],)).fetchone()["attempt"], attempt)

    def test_rebased_candidate_scopes_changes_against_refreshed_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project, harness, workspace, workstream, scope, repo, worktree, private_objects, _source = self.fixture(root)
            self.addCleanup(store.close)
            prepared = prepare_workstream_acceptance(store, project["project_id"], workstream["workstream_id"])
            accepted = apply_workstream_acceptance(store, project["project_id"], prepared["approvalScope"])
            (repo / "target.txt").write_text("advanced\n")
            _git(repo, "add", "target.txt")
            _git(repo, "commit", "-qm", "advance target")
            drift = reconcile_integrations(store, workspace, harness)
            self.assertEqual(drift["processed"][0]["state"], "awaiting_worker")
            target_ref = f"refs/pisec/target/{accepted['integration']['integration_id']}"
            git_worker(worktree, "rebase", target_ref)
            rebased_source = git_worker(worktree, "rev-parse", "HEAD").lower()
            binding = store.conn.execute("SELECT runtime_instance_id FROM runtime_bindings WHERE workstream_id=?", (scope["workstreamId"],)).fetchone()
            task = store.conn.execute("SELECT packet_sha256 FROM task_packets WHERE workstream_id=?", (scope["workstreamId"],)).fetchone()
            checkpoint(
                store,
                workstream_id=scope["workstreamId"],
                runtime_instance_id=binding["runtime_instance_id"],
                phase="ready_review",
                summary="Rebased implementation is verified.",
                next_action="Review the refreshed candidate.",
                blocker_code=None,
                blocker=None,
                evidence=["rebased fixture verification"],
                idempotency_key="integration-ready-rebased",
                completion_packet={
                    "acceptance": [{"criterion": "The fixture check passes.", "status": "passed", "evidence": ["Fixture output."]}],
                    "verification": [{"command": "fixture verification", "result": "passed"}],
                    "sourceCommit": rebased_source,
                    "taskPacketSha256": task["packet_sha256"],
                    "changedSurfaces": ["fixture"],
                    "residualRisk": "none",
                },
            )

            result = reconcile_integrations(store, workspace, harness)

            self.assertEqual(result["errors"], [], result)
            self.assertEqual(result["processed"][0]["state"], "integrated")
            report = store.conn.execute("SELECT changed_surfaces_json FROM integration_reports WHERE integration_id=?", (accepted["integration"]["integration_id"],)).fetchone()
            self.assertEqual(report["changed_surfaces_json"], '["feature.txt"]')


if __name__ == "__main__":
    unittest.main()
