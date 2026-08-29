from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import scripts.pisec.integration as integration_module
from scripts.pisec.attention import inspect_attention
from scripts.pisec.integration import apply_workstream_acceptance, prepare_workstream_acceptance, reconcile_integrations
from scripts.pisec.models import ConflictError
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import _git, register_project
from scripts.pisec.secretary import ensure_secretary
from scripts.pisec.workflow import report_issue, submit_completion
from scripts.pisec.workstreams import authorize_apply_workstream, prepare_workstream
from scripts.pisec.git_runner import run_git
from scripts.pisec.worker_repo import validate_worker_resume_git
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
        submit_completion(store, workstream_id=scope["workstreamId"], runtime_instance_id=binding["runtime_instance_id"], packet={
                "acceptance": [{"criterion": "The fixture check passes.", "status": "passed", "evidence": ["Fixture output."]}],
                "verification": [{"command": "fixture verification", "result": "passed"}],
                "sourceCommit": source,
                "taskPacketSha256": task["packet_sha256"],
                "changedSurfaces": ["fixture"],
                "residualRisk": "none",
            })
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
            submit_completion(store, workstream_id=scope["workstreamId"], runtime_instance_id=binding["runtime_instance_id"], packet={
                    "acceptance": [{"criterion": "The fixture check passes.", "status": "passed", "evidence": ["Fixture output."]}],
                    "verification": [{"command": "fixture verification rerun", "result": "passed"}],
                    "sourceCommit": source,
                    "taskPacketSha256": task["packet_sha256"],
                    "changedSurfaces": ["fixture"],
                    "residualRisk": "none",
                })
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

    def test_closeout_keeps_an_unresolved_issue_reporter_live_until_verification_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project, harness, workspace, workstream, _scope, _repo, worktree, _private_objects, _source = self.fixture(root)
            self.addCleanup(store.close)
            issue = report_issue(
                store,
                project_id=project["project_id"],
                reporter_workstream_id=workstream["workstream_id"],
                category="tooling",
                severity="degraded",
                summary="The reporter must verify the integrated repair.",
                details="Keep its runtime binding usable after task completion.",
                requested_action="Return verification through the original reporter.",
                evidence=["fixture"],
                idempotency_key="retain-reporter",
            )
            prepared = prepare_workstream_acceptance(store, project["project_id"], workstream["workstream_id"])
            apply_workstream_acceptance(store, project["project_id"], prepared["approvalScope"])

            integrated = reconcile_integrations(store, workspace, harness)

            job = dict(store.conn.execute("SELECT * FROM integration_jobs").fetchone())
            self.assertIn("closeout", integrated["processed"][0], {"result": integrated, "job": job})
            closeout = integrated["processed"][0]["closeout"]
            self.assertEqual(closeout["state"], "completed")
            self.assertTrue(closeout["retainedForVerification"])
            self.assertEqual(closeout["issueId"], issue["issue_id"])
            self.assertIn(closeout["runtime"]["action"], {"already_live", "started"})
            retained = store.conn.execute(
                "SELECT w.desired_state,w.provisioning_state,r.observed_state FROM workstreams w JOIN runtime_bindings r USING(workstream_id) WHERE w.workstream_id=?",
                (workstream["workstream_id"],),
            ).fetchone()
            self.assertEqual(tuple(retained), ("completed", "bound", "idle"))
            self.assertTrue(worktree.exists())

            store.conn.execute(
                "UPDATE integration_jobs SET state='needs_attention',last_error='worker is the only authorized verifier for an unresolved issue; resolve it or retain the binding' WHERE integration_id=?",
                (job["integration_id"],),
            )
            replayed_closeout = reconcile_integrations(store, workspace, harness)
            self.assertEqual(replayed_closeout["processed"][0]["state"], "integrated")
            self.assertTrue(replayed_closeout["processed"][0]["closeout"]["retainedForVerification"])

            store.conn.execute(
                "UPDATE issues SET state='resolved',disposition='fixed',resolution='verified',resolved_at='2026-08-29T00:00:00Z' WHERE issue_id=?",
                (issue["issue_id"],),
            )
            closed = reconcile_integrations(store, workspace, harness)

            self.assertEqual(closed["processed"][0]["closeout"]["state"], "retired")
            self.assertFalse(worktree.exists())

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
            self.assertFalse(any("rebase" in prompt.lower() for _surface, prompt in workspace.prompts))
            attention = store.conn.execute("SELECT source_kind FROM attention_items WHERE source_kind='integration' AND source_id=?", (accepted["integration"]["integration_id"],)).fetchone()
            self.assertIsNotNone(attention)
            attention_revision = store.conn.execute(
                "SELECT source_event_sequence FROM attention_items WHERE source_kind='integration' AND source_id=?",
                (accepted["integration"]["integration_id"],),
            ).fetchone()[0]
            prompt_count = len(workspace.prompts)
            attempt = store.conn.execute("SELECT attempt FROM integration_jobs WHERE integration_id=?", (accepted["integration"]["integration_id"],)).fetchone()["attempt"]
            replay = reconcile_integrations(store, workspace, harness)
            self.assertEqual(replay["processed"][0]["state"], "awaiting_worker")
            self.assertTrue(replay["processed"][0]["reused"])
            self.assertEqual(len(workspace.prompts), prompt_count)
            self.assertEqual(store.conn.execute("SELECT attempt FROM integration_jobs WHERE integration_id=?", (accepted["integration"]["integration_id"],)).fetchone()["attempt"], attempt)
            self.assertEqual(
                store.conn.execute(
                    "SELECT source_event_sequence FROM attention_items WHERE source_kind='integration' AND source_id=?",
                    (accepted["integration"]["integration_id"],),
                ).fetchone()[0],
                attention_revision,
            )

    def test_repaired_git_failure_retries_under_the_existing_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project, harness, workspace, workstream, _scope, _repo, _worktree, _private_objects, source = self.fixture(root)
            self.addCleanup(store.close)
            prepared = prepare_workstream_acceptance(store, project["project_id"], workstream["workstream_id"])
            accepted = apply_workstream_acceptance(store, project["project_id"], prepared["approvalScope"])
            real_run_git = integration_module._run_git
            failed = False

            def fail_candidate_import(path, *args, **kwargs):
                nonlocal failed
                if not failed and args[0] == "fetch" and path == _repo and "refs/pisec/candidates/" in args[-1]:
                    failed = True
                    raise ConflictError("Git operation refused")
                return real_run_git(path, *args, **kwargs)

            with patch.object(integration_module, "_run_git", side_effect=fail_candidate_import):
                first = reconcile_integrations(store, workspace, harness)
            self.assertEqual(first["processed"][0]["state"], "needs_attention")

            retried = reconcile_integrations(store, workspace, harness)

            self.assertEqual(retried["errors"], [], retried)
            self.assertEqual(retried["processed"][0]["state"], "integrated")
            self.assertEqual(_git(_repo, "rev-parse", "HEAD"), source)
            self.assertEqual(
                store.conn.execute("SELECT acceptance_id FROM integration_jobs WHERE integration_id=?", (accepted["integration"]["integration_id"],)).fetchone()[0],
                accepted["acceptance"]["acceptance_id"],
            )

    def test_partial_target_refresh_recovers_from_its_private_review_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project, harness, workspace, workstream, _scope, repo, worktree, _private_objects, _source = self.fixture(root)
            self.addCleanup(store.close)
            prepared = prepare_workstream_acceptance(store, project["project_id"], workstream["workstream_id"])
            accepted = apply_workstream_acceptance(store, project["project_id"], prepared["approvalScope"])
            (repo / "first-target.txt").write_text("first\n")
            _git(repo, "add", "first-target.txt")
            _git(repo, "commit", "-qm", "first target")
            first_target = _git(repo, "rev-parse", "HEAD")
            private_ref = f"refs/pisec/target/{accepted['integration']['integration_id']}"
            integration_module._run_git(
                worktree,
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                str(repo),
                f"{first_target}:{private_ref}",
            )
            integration_module._run_git(worktree, "update-ref", "refs/remotes/origin/main", first_target)
            (repo / "second-target.txt").write_text("second\n")
            _git(repo, "add", "second-target.txt")
            _git(repo, "commit", "-qm", "second target")
            store.conn.execute(
                "UPDATE integration_jobs SET state='needs_attention',target_oid=NULL,last_error='worker Reviewr base ref does not match the approved base' WHERE integration_id=?",
                (accepted["integration"]["integration_id"],),
            )
            refs_before = git_worker(worktree, "for-each-ref", "--format=%(refname)")

            recovered = reconcile_integrations(store, workspace, harness)

            job = store.conn.execute(
                "SELECT state,target_oid,acceptance_id,last_error FROM integration_jobs WHERE integration_id=?",
                (accepted["integration"]["integration_id"],),
            ).fetchone()
            self.assertEqual(recovered["processed"][0]["state"], "awaiting_worker", {"result": recovered, "job": dict(job), "refs": refs_before})
            self.assertEqual(job["state"], "awaiting_worker")
            self.assertEqual(job["target_oid"], _git(repo, "rev-parse", "HEAD"))
            self.assertEqual(job["acceptance_id"], accepted["acceptance"]["acceptance_id"])

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
            submit_completion(store, workstream_id=scope["workstreamId"], runtime_instance_id=binding["runtime_instance_id"], packet={
                    "acceptance": [{"criterion": "A widened criterion.", "status": "passed", "evidence": ["Rebased fixture output."]}],
                    "verification": [{"command": "fixture verification", "result": "passed"}],
                    "sourceCommit": rebased_source,
                    "taskPacketSha256": task["packet_sha256"],
                    "changedSurfaces": ["fixture"],
                    "residualRisk": "none",
                })
            prior_attention = store.conn.execute(
                "SELECT attention_id,source_event_sequence FROM attention_items WHERE recipient_workstream_id=? AND source_kind='integration' AND source_id=?",
                (scope["workstreamId"], accepted["integration"]["integration_id"]),
            ).fetchone()
            rejected = reconcile_integrations(store, workspace, harness)
            self.assertEqual(rejected["processed"][0]["state"], "awaiting_worker")
            job = store.conn.execute(
                "SELECT last_error,next_action FROM integration_jobs WHERE integration_id=?",
                (accepted["integration"]["integration_id"],),
            ).fetchone()
            self.assertEqual(job["last_error"], "replacement completion packet changed the accepted criteria")
            self.assertIn("source.accepted_completion_contract.criteria", job["next_action"])
            current_attention = store.conn.execute(
                "SELECT attention_id,source_event_sequence FROM attention_items WHERE recipient_workstream_id=? AND source_kind='integration' AND source_id=?",
                (scope["workstreamId"], accepted["integration"]["integration_id"]),
            ).fetchone()
            self.assertGreater(current_attention["source_event_sequence"], prior_attention["source_event_sequence"])
            inspected = inspect_attention(store, recipient_workstream_id=scope["workstreamId"], attention_id=current_attention["attention_id"])
            self.assertEqual(
                inspected["source"]["accepted_completion_contract"],
                {
                    "criteria": [{"criterion": "The fixture check passes.", "status": "passed"}],
                    "changed_paths": ["feature.txt"],
                    "conflict_policy": "bounded-worker-reconciliation",
                },
            )
            replay_revision = current_attention["source_event_sequence"]
            replayed_rejection = reconcile_integrations(store, workspace, harness)
            self.assertEqual(replayed_rejection["processed"][0]["state"], "awaiting_worker")
            self.assertEqual(
                store.conn.execute(
                    "SELECT source_event_sequence FROM attention_items WHERE recipient_workstream_id=? AND source_kind='integration' AND source_id=?",
                    (scope["workstreamId"], accepted["integration"]["integration_id"]),
                ).fetchone()[0],
                replay_revision,
            )
            replacement_packet = {
                "acceptance": [{"criterion": "The fixture check passes.", "status": "passed", "evidence": ["Rebased fixture output."]}],
                "verification": [{"command": "fixture verification", "result": "passed"}],
                "sourceCommit": rebased_source,
                "taskPacketSha256": task["packet_sha256"],
                "changedSurfaces": ["fixture"],
                "residualRisk": "none",
            }
            submit_completion(store, workstream_id=scope["workstreamId"], runtime_instance_id=binding["runtime_instance_id"], packet=replacement_packet)

            result = reconcile_integrations(store, workspace, harness)

            self.assertEqual(result["errors"], [], result)
            self.assertEqual(result["processed"][0]["state"], "integrated")
            report = store.conn.execute("SELECT changed_surfaces_json FROM integration_reports WHERE integration_id=?", (accepted["integration"]["integration_id"],)).fetchone()
            self.assertEqual(report["changed_surfaces_json"], '["feature.txt"]')

    def test_rebased_candidate_waits_for_a_second_target_advance_without_false_scope_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project, harness, workspace, workstream, scope, repo, worktree, _private_objects, _source = self.fixture(root)
            self.addCleanup(store.close)
            prepared = prepare_workstream_acceptance(store, project["project_id"], workstream["workstream_id"])
            accepted = apply_workstream_acceptance(store, project["project_id"], prepared["approvalScope"])
            (repo / "first-target.txt").write_text("first\n")
            _git(repo, "add", "first-target.txt")
            _git(repo, "commit", "-qm", "first target advance")
            first_drift = reconcile_integrations(store, workspace, harness)
            self.assertEqual(first_drift["processed"][0]["state"], "awaiting_worker")
            target_ref = f"refs/pisec/target/{accepted['integration']['integration_id']}"
            git_worker(worktree, "rebase", target_ref)
            rebased_source = git_worker(worktree, "rev-parse", "HEAD").lower()
            binding = store.conn.execute("SELECT runtime_instance_id FROM runtime_bindings WHERE workstream_id=?", (scope["workstreamId"],)).fetchone()
            task = store.conn.execute("SELECT packet_sha256 FROM task_packets WHERE workstream_id=?", (scope["workstreamId"],)).fetchone()
            replacement_packet = {
                "acceptance": [{"criterion": "The fixture check passes.", "status": "passed", "evidence": ["Rebased fixture output."]}],
                "verification": [{"command": "fixture verification", "result": "passed"}],
                "sourceCommit": rebased_source,
                "taskPacketSha256": task["packet_sha256"],
                "changedSurfaces": ["fixture"],
                "residualRisk": "none",
            }
            submitted = submit_completion(store, workstream_id=scope["workstreamId"], runtime_instance_id=binding["runtime_instance_id"], packet=replacement_packet)
            (repo / "second-target.txt").write_text("second\n")
            _git(repo, "add", "second-target.txt")
            _git(repo, "commit", "-qm", "second target advance")
            second_drift = reconcile_integrations(store, workspace, harness)
            self.assertEqual(second_drift["processed"][0]["state"], "awaiting_worker")
            store.conn.execute(
                "UPDATE integration_jobs SET state='needs_attention',last_error='replacement completion packet changed paths outside the accepted scope' WHERE integration_id=?",
                (accepted["integration"]["integration_id"],),
            )
            self.assertEqual(validate_worker_resume_git(store, {"workstream_id": scope["workstreamId"]}), rebased_source)
            replayed_packet = submit_completion(store, workstream_id=scope["workstreamId"], runtime_instance_id=binding["runtime_instance_id"], packet=replacement_packet)
            self.assertEqual(replayed_packet["completion_packet_id"], submitted["completion_packet_id"])

            recovered = reconcile_integrations(store, workspace, harness)

            self.assertEqual(recovered["processed"][0]["state"], "awaiting_worker", recovered)

            replay = reconcile_integrations(store, workspace, harness)

            self.assertEqual(replay["processed"][0]["state"], "awaiting_worker", replay)
            self.assertTrue(replay["processed"][0]["reused"])
            job = store.conn.execute("SELECT state,last_error,next_action FROM integration_jobs WHERE integration_id=?", (accepted["integration"]["integration_id"],)).fetchone()
            self.assertEqual(job["state"], "awaiting_worker")
            self.assertEqual(job["last_error"], "target advanced beyond the accepted candidate")
            self.assertIn("rebase onto the current target", job["next_action"])


if __name__ == "__main__":
    unittest.main()
