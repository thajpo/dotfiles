from pathlib import Path
import json
import tempfile
import unittest

from scripts.pisec.attention import backfill_attention, list_open_attention
from scripts.pisec.adapters import AdapterRegistry
from scripts.pisec.broker import BrokerDispatcher
from scripts.pisec.git_runner import run_git
from scripts.pisec.integration import apply_workstream_acceptance, prepare_workstream_acceptance, reconcile_integrations
from scripts.pisec.models import ConflictError, json_digest
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import register_project
from scripts.pisec.first_mate import ensure_first_mate
from scripts.pisec.secretary import ensure_secretary
from scripts.pisec.workflow import _append_issue_update, acknowledge_issue, checkpoint, escalate_issue, inspect_issue, link_issue_remediation, report_issue, request_issue_remediation, request_issue_verification, submit_completion, verify_issue
from scripts.pisec.workstreams import authorize_apply_workstream, prepare_workstream, retire_workstream
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace, make_repo


def task_packet(outcome: str, *, issue_anchors: dict[str, str] | None = None) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "outcome": outcome,
        "boundaries": ["Change only the approved Pisec recovery surfaces."],
        "acceptance": ["The bounded issue remediation is verified."],
        "openQuestions": [],
        "evidence": ["The immutable issue contract is included in the task."],
        **({"issueAnchors": issue_anchors} if issue_anchors is not None else {}),
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

        registry = AdapterRegistry()
        registry.register_harness(harness)
        registry.register_workspace(workspace)
        dispatcher = BrokerDispatcher(lambda: store, registry=registry, harness=harness, workspace=workspace, prepare_surfaces=False)
        secretary_id = platform_secretary["workstream"]["workstream_id"]
        rejected_payload = {
            "title": "Unrequested remediation",
            "purpose": "This must not create a worker before First Mate authority.",
            "brief": "The unresolved escalation alone is not sufficient authority.",
            "taskPacket": task_packet("Attempt remediation.", issue_anchors={"platformIssueId": escalated["issue_id"], "sourceIssueId": issue["issue_id"]}),
            "idempotencyKey": "platform-remediation-worker-before-request",
            "remediationIssueId": escalated["issue_id"],
        }
        operation_count = store.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
        workstream_count = store.conn.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0]
        with self.assertRaises(ConflictError):
            dispatcher._secretary(store, "workstream.prepare", platform["project_id"], secretary_id, rejected_payload)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0], operation_count)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0], workstream_count)

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
        self.assertEqual(
            store.conn.execute(
                "SELECT COUNT(*) FROM issue_updates WHERE issue_id=? AND actor_kind='first_mate' AND update_kind='remediation_requested'",
                (escalated["issue_id"],),
            ).fetchone()[0],
            1,
        )

        operation_count = store.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
        workstream_count = store.conn.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0]
        with self.assertRaises(ConflictError):
            dispatcher._secretary(store, "workstream.prepare", platform["project_id"], secretary_id, {
                "title": "Unanchored remediation",
                "purpose": "This packet must be rejected.",
                "brief": "The remediation packet omits the typed issue anchors.",
                "taskPacket": task_packet("Remediate the issue by matching its prose only."),
                "idempotencyKey": "platform-remediation-worker-unanchored",
                "remediationIssueId": escalated["issue_id"],
            })
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0], operation_count)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0], workstream_count)
        with self.assertRaises(ConflictError):
            dispatcher._secretary(store, "workstream.prepare", platform["project_id"], secretary_id, {
                "title": "Wrongly anchored remediation",
                "purpose": "This packet must be rejected.",
                "brief": "The remediation packet points at another issue.",
                "taskPacket": task_packet("Remediate the requested platform issue.", issue_anchors={"platformIssueId": escalated["issue_id"], "sourceIssueId": "iss_" + "f" * 32}),
                "idempotencyKey": "platform-remediation-worker-wrong-anchor",
                "remediationIssueId": escalated["issue_id"],
            })
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0], operation_count)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0], workstream_count)

        generic_scope = dispatcher._secretary(store, "workstream.prepare", platform["project_id"], secretary_id, {
            "title": "Unbound remediation identity",
            "purpose": "This worker has anchors but no immutable remediation issue binding.",
            "brief": "Linking must reject a generic worker even when its task packet names both issues.",
            "taskPacket": task_packet("Attempt remediation with anchors only.", issue_anchors={"platformIssueId": escalated["issue_id"], "sourceIssueId": issue["issue_id"]}),
            "idempotencyKey": "platform-remediation-worker-no-execution-anchor",
        })
        generic_worker = authorize_apply_workstream(store, scope=generic_scope["approvalScope"], harness=harness, workspace=workspace)["workstream"]
        with self.assertRaises(ConflictError):
            link_issue_remediation(store, project_id=platform["project_id"], issue_id=escalated["issue_id"], actor_id=platform_secretary["workstream"]["workstream_id"], target_id=generic_worker["workstream_id"], idempotency_key="platform-remediation-link-without-execution-anchor")
        self.assertIsNone(store.conn.execute("SELECT 1 FROM issue_remediations WHERE workstream_id=?", (generic_worker["workstream_id"],)).fetchone())

        stale_scope = dispatcher._secretary(store, "workstream.prepare", platform["project_id"], secretary_id, {
            "title": "Stale authority worker",
            "purpose": "This worker carries the remediation identity before authority becomes stale.",
            "brief": "Linking must reject it after the current projected lifecycle leaves remediation_planned.",
            "taskPacket": task_packet("Attempt remediation after authority expires.", issue_anchors={"platformIssueId": escalated["issue_id"], "sourceIssueId": issue["issue_id"]}),
            "idempotencyKey": "platform-remediation-worker-stale-link",
            "remediationIssueId": escalated["issue_id"],
        })
        stale_worker = authorize_apply_workstream(store, scope=stale_scope["approvalScope"], harness=harness, workspace=workspace)["workstream"]

        # Make the prior authority stale.  The immutable update is the same
        # durable lifecycle record used by remediation-failure retirement.
        stale_payload = {"workstreamId": generic_worker["workstream_id"], "failureReason": "stale authority test"}
        with store.transaction():
            _append_issue_update(store, issue=escalated, actor_kind="secretary", actor_id=secretary_id, update_kind="remediation_failed", payload=stale_payload, idempotency_key="platform-remediation-stale-authority")
            store.conn.execute("UPDATE issues SET state='acknowledged',updated_at=? WHERE issue_id=?", ("2026-08-29T00:00:00Z", escalated["issue_id"]))
        self.assertEqual(inspect_issue(store, issue_id=escalated["issue_id"], project_id=platform["project_id"])["state"], "triaged")
        with self.assertRaises(ConflictError):
            link_issue_remediation(store, project_id=platform["project_id"], issue_id=escalated["issue_id"], actor_id=platform_secretary["workstream"]["workstream_id"], target_id=stale_worker["workstream_id"], idempotency_key="platform-remediation-link-stale-authority")
        self.assertIsNone(store.conn.execute("SELECT 1 FROM issue_remediations WHERE workstream_id=?", (stale_worker["workstream_id"],)).fetchone())
        with self.assertRaises(ConflictError):
            dispatcher._secretary(store, "workstream.prepare", platform["project_id"], secretary_id, {
                "title": "Stale remediation authority",
                "purpose": "A historical request must not authorize new work.",
                "brief": "The latest lifecycle update records a failed remediation request.",
                "taskPacket": task_packet("Reject stale authority.", issue_anchors={"platformIssueId": escalated["issue_id"], "sourceIssueId": issue["issue_id"]}),
                "idempotencyKey": "platform-remediation-worker-stale-authority",
                "remediationIssueId": escalated["issue_id"],
            })

        request_issue_remediation(
            store,
            project_id=platform["project_id"],
            issue_id=escalated["issue_id"],
            actor_id=first_mate["workstream"]["workstream_id"],
            outcome="Re-authorize the bounded completion contract repair.",
            allowed_paths=["scripts/pisec", "omp/extensions", "tests"],
            verification=["focused lifecycle tests"],
            non_effects=["No deployment"],
            idempotency_key="platform-remediation-plan-retry",
        )
        self.assertEqual(inspect_issue(store, issue_id=escalated["issue_id"], project_id=platform["project_id"])["state"], "remediation_planned")

        worker_scope = dispatcher._secretary(store, "workstream.prepare", platform["project_id"], secretary_id, {
            "title": "Pisec remediation",
            "purpose": "Repair the approved Pisec completion contract.",
            "brief": "Implement the platform issue remediation and report the original goal, verification, risks, and next action.",
            "taskPacket": task_packet(f"Remediate platform issue {escalated['issue_id']} and verify the original report.", issue_anchors={"platformIssueId": escalated["issue_id"], "sourceIssueId": issue["issue_id"]}),
            "idempotencyKey": "platform-remediation-worker",
            "remediationIssueId": escalated["issue_id"],
        })
        remediation_worker = authorize_apply_workstream(store, scope=worker_scope["approvalScope"], harness=harness, workspace=workspace)["workstream"]
        packet_row = store.conn.execute("SELECT packet_json FROM task_packets WHERE workstream_id=?", (remediation_worker["workstream_id"],)).fetchone()
        self.assertIsNotNone(packet_row)
        issued_packet = json.loads(str(packet_row["packet_json"]))
        self.assertEqual(issued_packet["taskPacket"]["issueAnchors"], {"platformIssueId": escalated["issue_id"], "sourceIssueId": issue["issue_id"]})
        self.assertEqual(issued_packet["execution"]["remediationIssueId"], escalated["issue_id"])
        self.assertEqual(issued_packet["execution"].get("approvalScopeSha256"), json_digest(worker_scope["approvalScope"]))
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
        checkpoint(store, workstream_id=remediation_worker["workstream_id"], runtime_instance_id=runtime_instance_id, phase="implementing", summary="Implemented the bounded contract repair.", next_action="Submit the verified completion packet.", evidence=["recovery.txt"], idempotency_key="remediation-checkpoint")
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
        delete_trigger = store.conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='issue_updates_no_delete'").fetchone()[0]
        with store.transaction():
            store.conn.execute("DROP TRIGGER issue_updates_no_delete")
            store.conn.execute("DELETE FROM issue_updates WHERE update_kind='remediation_completed' AND actor_id=?", (remediation_worker["workstream_id"],))
            store.conn.execute(str(delete_trigger))
            store.conn.execute("UPDATE issues SET state='remediating' WHERE issue_id IN (?,?)", (issue["issue_id"], escalated["issue_id"]))
        request_issue_verification(store, project_id=platform["project_id"], issue_id=escalated["issue_id"], actor_id=first_mate["workstream"]["workstream_id"], evidence=["integration complete"], idempotency_key="verification-request")
        self.assertEqual(
            store.conn.execute(
                "SELECT COUNT(*) FROM issue_updates WHERE update_kind='remediation_completed' AND actor_id=?",
                (remediation_worker["workstream_id"],),
            ).fetchone()[0],
            2,
        )
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
