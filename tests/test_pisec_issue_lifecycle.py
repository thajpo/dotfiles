from pathlib import Path
import tempfile
import unittest

from scripts.pisec.attention import backfill_attention, list_open_attention
from scripts.pisec.git_runner import run_git
from scripts.pisec.integration import apply_workstream_acceptance, prepare_workstream_acceptance, reconcile_integrations
from scripts.pisec.models import ConflictError
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import register_project
from scripts.pisec.first_mate import ensure_first_mate
from scripts.pisec.secretary import ensure_secretary
from scripts.pisec.workflow import acknowledge_issue, checkpoint, escalate_issue, inspect_issue, link_issue_remediation, report_issue, request_issue_remediation, request_issue_verification, submit_completion, verify_issue
from scripts.pisec.workstreams import authorize_apply_workstream, prepare_workstream, retire_workstream
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace, make_repo


def task_packet(outcome: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "outcome": outcome,
        "boundaries": ["Change only the approved Pisec recovery surfaces."],
        "acceptance": ["The bounded issue remediation is verified."],
        "openQuestions": [],
        "evidence": ["The immutable issue contract is included in the task."],
    }


class IssueLifecycleTests(unittest.TestCase):
    def fixture(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        source_repo = root / "vla-lens"
        platform_repo = root / "dotfiles"
        make_repo(source_repo)
        make_repo(platform_repo)
        store = PiStore(root / "state")
        source = register_project(store, source_repo, display_name="VLA Lens")
        platform = register_project(store, platform_repo, display_name="dotfiles")
        harness = FixtureHarness(root)
        workspace = FixtureWorkspace(root, store)
        source_secretary = ensure_secretary(store, source["project_id"], harness, workspace)
        platform_secretary = ensure_secretary(store, platform["project_id"], harness, workspace)
        first_mate = ensure_first_mate(store, platform["project_id"], harness, workspace)
        prepared = prepare_workstream(
            store,
            project_id=source["project_id"],
            title="Reporter",
            purpose="Report a bounded platform defect.",
            brief="The worker reports a tooling issue for lifecycle testing.",
            task_packet=task_packet("Report the tooling defect with evidence."),
            idempotency_key="issue-reporter",
            harness=harness,
            workspace=workspace,
            work_root=root / "worktrees",
        )
        worker = authorize_apply_workstream(store, scope=prepared["approvalScope"], harness=harness, workspace=workspace)["workstream"]
        return temp, root, store, source, platform, source_secretary, platform_secretary, first_mate, worker, harness, workspace

    def test_cross_project_escalation_and_lifecycle_ownership(self):
        temp, root, store, source, platform, source_secretary, platform_secretary, first_mate, worker, harness, workspace = self.fixture()
        self.addCleanup(store.close)
        self.addCleanup(temp.cleanup)
        issue = report_issue(
            store,
            project_id=source["project_id"],
            reporter_workstream_id=worker["workstream_id"],
            category="tooling",
            severity="blocking",
            summary="Completion contract is unavailable",
            details="The worker cannot submit the required completion evidence.",
            requested_action="Repair the Pisec completion contract.",
            evidence=["fixture"],
            idempotency_key="source-issue",
        )
        self.assertEqual(issue["state"], "open")
        self.assertEqual(issue["issueClass"], "project")
        self.assertEqual(issue["reportingProjectId"], source["project_id"])
        self.assertEqual(issue["remediationProjectId"], source["project_id"])
        self.assertEqual(len(list_open_attention(store, recipient_workstream_id=source_secretary["workstream"]["workstream_id"])), 1)

        triaged = acknowledge_issue(store, project_id=source["project_id"], issue_id=issue["issue_id"], actor_id=source_secretary["workstream"]["workstream_id"])
        self.assertEqual(triaged["state"], "triaged")
        self.assertEqual(list_open_attention(store, recipient_workstream_id=source_secretary["workstream"]["workstream_id"]), [])

        escalated = escalate_issue(
            store,
            project_id=source["project_id"],
            reporter_workstream_id=source_secretary["workstream"]["workstream_id"],
            source_issue_id=issue["issue_id"],
            category="tooling",
            severity="blocking",
            summary="Pisec completion contract defect",
            details="The project worker cannot satisfy the current completion contract.",
            requested_action="Repair the completion and reporting path in dotfiles.",
            evidence=["source issue", "worker report"],
            idempotency_key="platform-escalation",
        )
        self.assertEqual(escalated["state"], "open")
        self.assertEqual(escalated["issueClass"], "pisec_platform")
        self.assertEqual(escalated["reportingProjectId"], source["project_id"])
        self.assertEqual(escalated["reportingWorkstreamId"], worker["workstream_id"])
        self.assertEqual(escalated["remediationProjectId"], platform["project_id"])
        self.assertEqual(escalated["currentOwner"]["kind"], "first_mate")
        self.assertTrue(list_open_attention(store, recipient_workstream_id=first_mate["workstream"]["workstream_id"]))

        planned = request_issue_remediation(
            store,
            project_id=platform["project_id"],
            issue_id=escalated["issue_id"],
            actor_id=first_mate["workstream"]["workstream_id"],
            outcome="Repair the completion contract and preserve the original reporter verification path.",
            allowed_paths=["scripts/pisec", "omp/extensions", "tests"],
            verification=["python3 -m unittest tests.test_pisec_issue_lifecycle"],
            non_effects=["No deployment", "No change to the source VLA checkout"],
            idempotency_key="platform-remediation-plan",
        )
        self.assertEqual(planned["state"], "remediation_planned")

        worker_scope = prepare_workstream(
            store,
            project_id=platform["project_id"],
            title="Pisec remediation",
            purpose="Repair the approved Pisec completion contract.",
            brief="Implement the platform issue remediation and report the original goal, verification, risks, and next action.",
            task_packet=task_packet(f"Remediate platform issue {escalated['issue_id']} and verify the original report."),
            idempotency_key="platform-remediation-worker",
            harness=harness,
            workspace=workspace,
            work_root=root / "worktrees",
        )
        remediation_worker = authorize_apply_workstream(store, scope=worker_scope["approvalScope"], harness=harness, workspace=workspace)["workstream"]
        linked = link_issue_remediation(store, project_id=platform["project_id"], issue_id=escalated["issue_id"], actor_id=platform_secretary["workstream"]["workstream_id"], target_id=remediation_worker["workstream_id"], idempotency_key="platform-remediation-link")
        self.assertEqual(linked["state"], "remediating")
        self.assertEqual(linked["currentOwner"]["workstreamId"], remediation_worker["workstream_id"])
        source_after_link = __import__("scripts.pisec.workflow", fromlist=["inspect_issue"]).inspect_issue(store, issue_id=issue["issue_id"], project_id=source["project_id"])
        self.assertEqual(source_after_link["state"], "remediating")
        self.assertEqual(source_after_link["remediationProjectId"], platform["project_id"])

        worktree = Path(remediation_worker["worktree_path"])
        (worktree / "recovery.txt").write_text("repaired\n")
        run_git(worktree, ("add", "recovery.txt"), role="worker")
        run_git(worktree, ("commit", "-qm", "repair completion contract"), role="worker")
        source_commit = run_git(worktree, ("rev-parse", "HEAD"), role="worker").stdout.strip().lower()
        runtime_instance_id = store.conn.execute("SELECT runtime_instance_id FROM runtime_bindings WHERE workstream_id=?", (remediation_worker["workstream_id"],)).fetchone()[0]
        task_sha = store.conn.execute("SELECT packet_sha256 FROM task_packets WHERE workstream_id=?", (remediation_worker["workstream_id"],)).fetchone()[0]
        checkpoint(store, workstream_id=remediation_worker["workstream_id"], runtime_instance_id=runtime_instance_id, phase="implementing", summary="Implemented the bounded contract repair.", next_action="Submit the verified completion packet.", evidence=["recovery.txt"], idempotency_key="remediation-checkpoint", remediation_issue_id=escalated["issue_id"])
        submit_completion(store, workstream_id=remediation_worker["workstream_id"], runtime_instance_id=runtime_instance_id, packet={
            "acceptance": [{"criterion": "The bounded issue remediation is verified.", "status": "passed", "evidence": ["recovery.txt"]}],
            "verification": [{"command": "test fixture", "result": "passed"}],
            "sourceCommit": source_commit,
            "taskPacketSha256": task_sha,
            "changedSurfaces": ["recovery.txt"],
            "residualRisk": "none",
        })
        candidate = inspect_issue(store, issue_id=escalated["issue_id"], project_id=platform["project_id"])
        self.assertEqual(candidate["state"], "candidate_ready")
        with self.assertRaises(ConflictError):
            request_issue_verification(store, project_id=platform["project_id"], issue_id=escalated["issue_id"], actor_id=first_mate["workstream"]["workstream_id"], evidence=["not integrated"], idempotency_key="too-early")
        accepted = apply_workstream_acceptance(store, platform["project_id"], prepare_workstream_acceptance(store, platform["project_id"], remediation_worker["workstream_id"])["approvalScope"])
        integrated = reconcile_integrations(store, workspace, harness)
        self.assertEqual(integrated["processed"][0]["state"], "integrated")
        self.assertEqual(inspect_issue(store, issue_id=escalated["issue_id"], project_id=platform["project_id"])["state"], "integrated")
        request_issue_verification(store, project_id=platform["project_id"], issue_id=escalated["issue_id"], actor_id=first_mate["workstream"]["workstream_id"], evidence=["integration complete"], idempotency_key="verification-request")
        verified = verify_issue(store, project_id=source["project_id"], issue_id=issue["issue_id"], actor_id=worker["workstream_id"], status="fixed", evidence=["original reporter confirmed the fix"], idempotency_key="reporter-verification")
        self.assertEqual(verified["state"], "resolved")
        self.assertEqual(inspect_issue(store, issue_id=escalated["issue_id"], project_id=platform["project_id"])["state"], "resolved")

    def test_reporter_retirement_is_blocked_while_verification_is_unresolved(self):
        temp, root, store, source, _platform, source_secretary, _platform_secretary, _first_mate, worker, harness, workspace = self.fixture()
        self.addCleanup(store.close)
        self.addCleanup(temp.cleanup)
        issue = report_issue(store, project_id=source["project_id"], reporter_workstream_id=worker["workstream_id"], category="tooling", severity="degraded", summary="Needs verification", details="The report remains unresolved.", requested_action="Verify the remediation.", evidence=["fixture"], idempotency_key="retirement-issue")
        acknowledge_issue(store, project_id=source["project_id"], issue_id=issue["issue_id"], actor_id=source_secretary["workstream"]["workstream_id"])
        store.conn.execute("UPDATE workstreams SET desired_state='completed',completed_at='2026-08-27T00:00:00Z' WHERE workstream_id=?", (worker["workstream_id"],))
        with self.assertRaises(ConflictError):
            retire_workstream(store, source["project_id"], worker["workstream_id"], workspace)
        store.conn.execute("UPDATE issues SET state='verifying' WHERE issue_id=?", (issue["issue_id"],))
        self.assertEqual(backfill_attention(store, recipient_workstream_id=worker["workstream_id"]), 1)
        self.assertEqual(len(list_open_attention(store, recipient_workstream_id=worker["workstream_id"])), 1)

        verified = verify_issue(
            store,
            project_id=source["project_id"],
            issue_id=issue["issue_id"],
            actor_id=worker["workstream_id"],
            status="fixed",
            evidence=["completed reporter verified the repair"],
            idempotency_key="completed-reporter-verification",
        )

        self.assertEqual(verified["state"], "resolved")
        retired = retire_workstream(store, source["project_id"], worker["workstream_id"], workspace)
        self.assertEqual(retired["desired_state"], "retired")


if __name__ == "__main__":
    unittest.main()
