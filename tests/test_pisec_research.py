import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scripts.pisec.adapters import AdapterRegistry, AgentObservation
from scripts.pisec.attention import ATTENTION_WAKE_PROMPT, backfill_attention
from scripts.pisec.broker import BrokerDispatcher
from scripts.pisec.events import append_event_in_transaction
from scripts.pisec.models import AuthorizationError, IdempotencyConflictError, InvalidRequestError, NotFoundError
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import register_project
from scripts.pisec.research import inspect_research, list_research_requests, list_unacknowledged_research, research_counts, validate_research_request
from scripts.pisec.secretary import ensure_secretary
from scripts.pisec.workstreams import authorize_apply_workstream, prepare_workstream
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace, make_repo


def task_packet() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "outcome": "Produce bounded verified findings.",
        "boundaries": ["Do not modify the repository."],
        "acceptance": ["Return evidence and uncertainties."],
        "openQuestions": [],
        "evidence": ["The worker task packet is authoritative."],
    }


class WakeWorkspace(FixtureWorkspace):
    def __init__(self, root: Path, store, *, fail: bool = False):
        super().__init__(root, store)
        self.fail = fail
        self.called = threading.Event()

    def prompt_agent(self, surface_id: str, text: str, wait_until: tuple[str, ...], timeout_ms: int):
        if self.fail:
            raise RuntimeError("workspace unavailable")
        return super().prompt_agent(surface_id, text, wait_until, timeout_ms)

    def prompt_agent_nowait(self, surface_id: str, text: str):
        if text.startswith("Pending worker research"):
            if self.fail:
                raise RuntimeError("workspace unavailable")
            self.called.set()
        return super().prompt_agent_nowait(surface_id, text)

    def trigger_agent_nowait(self, surface_id: str, trigger: str, process_identity: str):
        if self.fail:
            raise RuntimeError("workspace unavailable")
        self.called.set()
        return super().trigger_agent_nowait(surface_id, trigger, process_identity)


class SelectiveWakeWorkspace(FixtureWorkspace):
    def __init__(self, root: Path, store):
        super().__init__(root, store)
        self.failed_surface: str | None = None
        self.trigger_attempts: list[str] = []

    def trigger_agent_nowait(self, surface_id: str, trigger: str, process_identity: str):
        self.trigger_attempts.append(surface_id)
        if surface_id == self.failed_surface:
            raise RuntimeError("first recipient unavailable")
        return super().trigger_agent_nowait(surface_id, trigger, process_identity)


class ResearchTests(unittest.TestCase):
    def fixture(self, *, workspace_type=WakeWorkspace):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        repo_a = root / "repo-a"
        repo_b = root / "repo-b"
        make_repo(repo_a)
        make_repo(repo_b)
        store = PiStore(root / "state")
        project_a = register_project(store, repo_a)
        project_b = register_project(store, repo_b)
        harness = FixtureHarness(root)
        workspace = workspace_type(root, store)
        secretary = ensure_secretary(store, project_a["project_id"], harness, workspace)
        ensure_secretary(store, project_b["project_id"], harness, workspace)
        worker_results = []
        for project, key in ((project_a, "worker-a"), (project_b, "worker-b")):
            prepared = prepare_workstream(store, project_id=project["project_id"], title="Worker", purpose="Research", brief="Bounded fixture", task_packet=task_packet(), idempotency_key=key, harness=harness, workspace=workspace, work_root=root / "worktrees")
            worker_results.append(authorize_apply_workstream(store, scope=prepared["approvalScope"], harness=harness, workspace=workspace))
        secretary_binding = dict(secretary["binding"])
        workers = [dict(result["workstream"]) for result in worker_results]
        return temp, root, store, harness, workspace, dict(project_a), dict(project_b), secretary_binding, workers[0], workers[1]

    @staticmethod
    def runtime_auth(store: PiStore, workstream: dict[str, object]) -> dict[str, str]:
        binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone())
        token = Path(binding["launch_secret_path"]).read_text().strip()
        return {"workstreamId": str(workstream["workstream_id"]), "runtimeInstanceId": str(binding["runtime_instance_id"]), "surfaceId": str(binding["workspace_surface_id"]), "token": token, "generation": str(binding["launch_generation_sha256"] or binding["applied_generation_sha256"])}

    @staticmethod
    def dispatcher(root: Path, harness, workspace) -> BrokerDispatcher:
        registry = AdapterRegistry()
        registry.register_harness(harness)
        registry.register_workspace(workspace)
        return BrokerDispatcher(lambda: PiStore(root / "state"), registry=registry, harness=harness, workspace=workspace)

    def test_task_get_exposes_approved_python_env(self):
        temp, root, store, harness, workspace, project_a, project_b, secretary_binding, worker_a, worker_b = self.fixture()
        self.addCleanup(store.close)
        self.addCleanup(temp.cleanup)
        env_dir = root / "venv"
        env_dir.mkdir()
        prepared = prepare_workstream(store, project_id=project_a["project_id"], title="Worker", purpose="Research", brief="Bounded fixture", task_packet=task_packet(), idempotency_key="worker-env", harness=harness, workspace=workspace, work_root=root / "worktrees", python_env=str(env_dir))
        worker = dict(authorize_apply_workstream(store, scope=prepared["approvalScope"], harness=harness, workspace=workspace)["workstream"])
        dispatcher = self.dispatcher(root, harness, workspace)
        packet = dispatcher.dispatch("runtime", "task.get", self.runtime_auth(store, worker))
        self.assertEqual(packet["pythonEnv"], str(env_dir.resolve()))

    def test_authenticated_runtime_research_lifecycle_is_durable_and_isolated(self):
        temp, root, store, harness, workspace, project_a, project_b, secretary_binding, worker_a, worker_b = self.fixture()
        self.addCleanup(store.close)
        self.addCleanup(temp.cleanup)
        dispatcher = self.dispatcher(root, harness, workspace)
        auth = self.runtime_auth(store, worker_a)
        packet = dispatcher.dispatch("runtime", "task.get", auth)
        self.assertEqual(packet["taskPacketId"], store.conn.execute("SELECT task_packet_id FROM task_packets WHERE workstream_id=?", (worker_a["workstream_id"],)).fetchone()[0])
        self.assertIn("pythonEnv", packet)
        self.assertIsNone(packet["pythonEnv"])
        request_payload = {"kind": "research", "summary": "Need public documentation", "question": "What is the documented behavior?", "context": "The worker checked its local sources.", "attempted": ["Local source search"], "candidateSources": ["https://example.com/docs"], "blocking": True}
        first = dispatcher.dispatch("runtime", "research.request", {**auth, "idempotencyKey": "research-1", "request": request_payload})
        replay = dispatcher.dispatch("runtime", "research.request", {**auth, "idempotencyKey": "research-1", "request": request_payload})
        self.assertEqual(first["request_id"], replay["request_id"])
        with self.assertRaises(IdempotencyConflictError):
            dispatcher.dispatch("runtime", "research.request", {**auth, "idempotencyKey": "research-1", "request": {**request_payload, "question": "Different"}})
        with self.assertRaises(AuthorizationError):
            dispatcher.dispatch("runtime", "task.get", {**self.runtime_auth(store, worker_b), "workstreamId": worker_a["workstream_id"]})
        secretary_auth = {"authToken": Path(secretary_binding["launch_secret_path"]).read_text().strip()}
        claimed = dispatcher.dispatch("secretary", "research.claim", {**secretary_auth, "requestId": first["request_id"]})
        self.assertEqual(claimed["state"], "researching")
        context_request = {"schemaVersion": 1, "message": "Provide the exact public source URL.", "missing": ["A primary source URL"]}
        context = dispatcher.dispatch("secretary", "research.request_context", {**secretary_auth, "requestId": first["request_id"], "idempotencyKey": "context-1", "contextRequest": context_request})
        self.assertEqual(context["state"], "needs_context")
        context_replay = dispatcher.dispatch("secretary", "research.request_context", {**secretary_auth, "requestId": first["request_id"], "idempotencyKey": "context-other", "contextRequest": context_request})
        self.assertEqual(context_replay["request_id"], first["request_id"])
        added = dispatcher.dispatch("runtime", "research.add_context", {**auth, "requestId": first["request_id"], "idempotencyKey": "worker-context-1", "context": {"context": "The worker can use the requested source.", "attempted": ["Local source search"], "candidateSources": ["https://example.com/docs"]}})
        self.assertEqual(added["state"], "pending")
        dispatcher.dispatch("secretary", "research.claim", {**secretary_auth, "requestId": first["request_id"]})
        result_payload = {"schemaVersion": 1, "findings": ["The documented behavior is bounded."], "sources": [{"url": "https://example.com/docs", "title": "Documentation", "excerpt": "Relevant excerpt."}], "uncertainties": ["The fixture is not the real service."]}
        answered = dispatcher.dispatch("secretary", "research.answer", {**secretary_auth, "requestId": first["request_id"], "idempotencyKey": "answer-1", "result": result_payload})
        self.assertEqual(answered["state"], "answered")
        answered_replay = dispatcher.dispatch("secretary", "research.answer", {**secretary_auth, "requestId": first["request_id"], "idempotencyKey": "answer-other", "result": result_payload})
        self.assertEqual(answered_replay["state"], "answered")
        packet_count = store.conn.execute("SELECT count(*) FROM research_packets WHERE request_id=?", (first["request_id"],)).fetchone()[0]
        self.assertEqual(packet_count, 4)
        listed = dispatcher.dispatch("runtime", "research.list", auth)
        listed_requests = [r for r in listed["requests"] if r["request_id"] == first["request_id"]]
        self.assertEqual(len(listed_requests), 1)
        self.assertEqual(listed_requests[0]["state"], "answered")
        self.assertEqual(listed_requests[0]["packetCount"], 4)
        self.assertNotIn("payload", listed_requests[0]["packets"][0])
        inspected = dispatcher.dispatch("runtime", "research.inspect", {**auth, "requestId": first["request_id"]})
        self.assertEqual(inspected["state"], "answered")
        self.assertEqual(len(inspected["packets"]), 4)
        result_packet = next(p for p in inspected["packets"] if p["kind"] == "result")
        self.assertEqual(result_packet["payload"], result_payload)
        with self.assertRaises(NotFoundError):
            dispatcher.dispatch("runtime", "research.inspect", {**auth, "requestId": "wrq_" + "a" * 32})
        secretary_inspect = dispatcher.dispatch("secretary", "research.inspect", {**secretary_auth, "requestId": first["request_id"]})
        self.assertEqual(secretary_inspect["state"], "answered")
        self.assertEqual(len(secretary_inspect["packets"]), 4)
        unack = list_unacknowledged_research(store, project_id=str(project_a["project_id"]), workstream_id=str(worker_a["workstream_id"]))
        self.assertEqual(unack[0]["state"], "answered")
        dispatcher.dispatch("runtime", "research.acknowledge", {**auth, "requestId": first["request_id"]})
        acknowledged_replay = dispatcher.dispatch("runtime", "research.acknowledge", {**auth, "requestId": first["request_id"]})
        self.assertEqual(acknowledged_replay["state"], "acknowledged")

    def test_malformed_research_is_rejected_before_mutation(self):
        temp, _root, store, _harness, _workspace, project_a, _project_b, _secretary, worker_a, _worker_b = self.fixture(workspace_type=FixtureWorkspace)
        self.addCleanup(store.close)
        self.addCleanup(temp.cleanup)
        malformed = {"kind": "research", "summary": "x", "question": "x", "context": "", "attempted": [], "candidateSources": [], "blocking": True, "extra": "reject"}
        with self.assertRaises(InvalidRequestError):
            validate_research_request(malformed)
        with self.assertRaises(InvalidRequestError):
            validate_research_request({"kind": "research", "summary": "x", "question": "x", "context": "", "attempted": [], "candidateSources": ["file:///etc/passwd"], "blocking": True})
        self.assertEqual(research_counts(store, str(project_a["project_id"]))["pending"], 0)

    def test_research_wake_replays_after_workspace_failure(self):
        temp, root, store, harness, workspace, project_a, _project_b, _secretary, worker_a, _worker_b = self.fixture()
        store.conn.execute("UPDATE projects SET coordination_mode='fleet' WHERE project_id=?", (project_a["project_id"],))
        self.addCleanup(temp.cleanup)
        dispatcher = self.dispatcher(root, harness, workspace)
        auth = self.runtime_auth(store, worker_a)
        dispatcher.dispatch("runtime", "research.request", {**auth, "idempotencyKey": "wake-1", "request": {"kind": "research", "summary": "Wake", "question": "Wake the secretary", "context": "", "attempted": [], "candidateSources": [], "blocking": True}})
        rows = store.conn.execute("SELECT source_kind,source_id,recipient_workstream_id FROM attention_items WHERE project_id=?", (project_a["project_id"],)).fetchall()
        self.assertEqual([(row["source_kind"], row["source_id"], row["recipient_workstream_id"]) for row in rows], [("research", store.conn.execute("SELECT request_id FROM research_requests WHERE workstream_id=?", (worker_a["workstream_id"],)).fetchone()[0], store.conn.execute("SELECT secretary_workstream_id FROM projects WHERE project_id=?", (project_a["project_id"],)).fetchone()[0])])

    def test_attention_index_failure_rolls_back_source_and_event(self):
        temp, root, store, harness, workspace, project_a, _project_b, _secretary, worker_a, _worker_b = self.fixture(workspace_type=FixtureWorkspace)
        self.addCleanup(store.close)
        self.addCleanup(temp.cleanup)
        dispatcher = self.dispatcher(root, harness, workspace)
        auth = self.runtime_auth(store, worker_a)
        request_payload = {"kind": "research", "summary": "Atomic attention", "question": "Does the index roll back?", "context": "", "attempted": [], "candidateSources": [], "blocking": True}
        with patch("scripts.pisec.attention._upsert", side_effect=RuntimeError("attention fail")):
            with self.assertRaises(RuntimeError):
                dispatcher.dispatch("runtime", "research.request", {**auth, "idempotencyKey": "atomic-attention", "request": request_payload})
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM research_requests WHERE workstream_id=?", (worker_a["workstream_id"],)).fetchone()[0], 0)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='research.requested' AND workstream_id=?", (worker_a["workstream_id"],)).fetchone()[0], 0)

    def test_attention_revision_replay_and_fixed_watcher_prompt(self):
        temp, root, store, harness, workspace, project_a, _project_b, secretary_binding, worker_a, _worker_b = self.fixture(workspace_type=FixtureWorkspace)
        self.addCleanup(store.close)
        self.addCleanup(temp.cleanup)
        dispatcher = self.dispatcher(root, harness, workspace)
        auth = self.runtime_auth(store, worker_a)
        help_result = dispatcher.dispatch("runtime", "help.request", {**auth, "kind": "access", "summary": "Need approved source", "details": "The source is unavailable.", "requestedAction": "Review the approved read path.", "blocking": True, "evidence": ["fixture"], "idempotencyKey": "attention-issue"})
        issue_id = help_result["request"]["issue_id"]
        secretary_id = str(secretary_binding["workstream_id"])
        first_revision = store.conn.execute("SELECT source_event_sequence FROM attention_items WHERE recipient_workstream_id=? AND source_kind='issue' AND source_id=?", (secretary_id, issue_id)).fetchone()[0]
        secretary_auth = {"authToken": Path(secretary_binding["launch_secret_path"]).read_text().strip()}
        dispatcher.dispatch("secretary", "issue.add_context", {**secretary_auth, "issueId": issue_id, "context": {"note": "first"}, "idempotencyKey": "attention-context-1"})
        second_revision = store.conn.execute("SELECT source_event_sequence FROM attention_items WHERE recipient_workstream_id=? AND source_kind='issue' AND source_id=?", (secretary_id, issue_id)).fetchone()[0]
        self.assertGreater(second_revision, first_revision)
        dispatcher.dispatch("secretary", "issue.add_context", {**secretary_auth, "issueId": issue_id, "context": {"note": "first"}, "idempotencyKey": "attention-context-1"})
        self.assertEqual(store.conn.execute("SELECT source_event_sequence FROM attention_items WHERE recipient_workstream_id=? AND source_kind='issue' AND source_id=?", (secretary_id, issue_id)).fetchone()[0], second_revision)
        listed = dispatcher.dispatch("secretary", "attention.list", {**secretary_auth})["items"]
        self.assertTrue(listed)
        self.assertEqual(set(listed[0]), {"attentionId", "sourceKind", "sourceId", "priority", "revision", "revisionAt"})
        with self.assertRaises(InvalidRequestError):
            dispatcher.dispatch("secretary", "attention.list", {**secretary_auth, "limit": 33})
        binding = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (secretary_id,)).fetchone()
        watcher_generation = str(binding["desired_generation_sha256"])
        session_event = append_event_in_transaction(
            store.conn,
            kind="runtime.session_started",
            project_id=project_a["project_id"],
            workstream_id=secretary_id,
            payload={"generationSha256": watcher_generation, "reportSeq": 1, "runtimeInstanceId": "watcher-runtime"},
        )
        store.conn.execute("UPDATE runtime_bindings SET observed_state='idle',refresh_pending=0,launch_generation_sha256=NULL,applied_generation_sha256=desired_generation_sha256,runtime_instance_id='watcher-runtime',report_seq=1,session_start_event_sequence=?,session_start_report_seq=1,session_started_at='2026-08-25T00:00:00Z' WHERE workstream_id=?", (session_event["sequence"], secretary_id))
        agent_name = str(binding["agent_name"])
        workspace.agents[agent_name] = AgentObservation(agent_name, str(binding["workspace_surface_id"]), True, "idle")
        workspace.runtime_states[str(binding["workspace_surface_id"])] = "live"
        dispatcher._scan_attention()
        self.assertEqual(workspace.prompts[-1][1], ATTENTION_WAKE_PROMPT)

    def test_attention_watcher_isolates_recipient_failure_and_backs_off_both(self):
        temp, root, store, harness, workspace, project_a, project_b, secretary_a, worker_a, worker_b = self.fixture(workspace_type=SelectiveWakeWorkspace)
        self.addCleanup(store.close)
        self.addCleanup(temp.cleanup)
        dispatcher = self.dispatcher(root, harness, workspace)
        dispatcher.dispatch("runtime", "help.request", {**self.runtime_auth(store, worker_a), "kind": "access", "summary": "Need A", "details": "A is unavailable.", "requestedAction": "Review A.", "blocking": True, "evidence": ["a"], "idempotencyKey": "wake-a"})
        dispatcher.dispatch("runtime", "help.request", {**self.runtime_auth(store, worker_b), "kind": "access", "summary": "Need B", "details": "B is unavailable.", "requestedAction": "Review B.", "blocking": True, "evidence": ["b"], "idempotencyKey": "wake-b"})
        secretary_rows = store.conn.execute(
            "SELECT r.*,w.project_id FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE w.kind='secretary' ORDER BY w.project_id"
        ).fetchall()
        self.assertEqual(len(secretary_rows), 2)
        for index, binding in enumerate(secretary_rows, start=1):
            event = append_event_in_transaction(
                store.conn,
                kind="runtime.session_started",
                project_id=str(binding["project_id"]),
                workstream_id=str(binding["workstream_id"]),
                payload={"generationSha256": str(binding["desired_generation_sha256"]), "reportSeq": 1, "runtimeInstanceId": f"watcher-{index}"},
            )
            store.conn.execute(
                "UPDATE runtime_bindings SET observed_state='idle',refresh_pending=0,launch_generation_sha256=NULL,applied_generation_sha256=desired_generation_sha256,runtime_instance_id=?,report_seq=1,session_start_event_sequence=?,session_start_report_seq=1,session_started_at='2026-08-25T00:00:00Z' WHERE workstream_id=?",
                (f"watcher-{index}", event["sequence"], binding["workstream_id"]),
            )
            workspace.agents[str(binding["agent_name"])] = AgentObservation(str(binding["agent_name"]), str(binding["workspace_surface_id"]), True, "idle")
            workspace.runtime_states[str(binding["workspace_surface_id"])] = "live"
        workspace.failed_surface = str(secretary_rows[0]["workspace_surface_id"])
        dispatcher._scan_attention()
        self.assertEqual(set(workspace.trigger_attempts), {str(row["workspace_surface_id"]) for row in secretary_rows})
        self.assertEqual(set(dispatcher._attention_wake_deadlines), {str(row["workstream_id"]) for row in secretary_rows})
        self.assertIn((str(secretary_rows[1]["workspace_surface_id"]), ATTENTION_WAKE_PROMPT), workspace.prompts)

    def test_supervisor_backfill_is_bounded_and_emits_typed_events(self):
        temp, _root, store, _harness, _workspace, project_a, _project_b, secretary_binding, worker_a, _worker_b = self.fixture(workspace_type=FixtureWorkspace)
        self.addCleanup(store.close)
        self.addCleanup(temp.cleanup)
        issue_ids = []
        for index in range(3):
            store.conn.execute("INSERT INTO issues(issue_id,project_id,reporter_workstream_id,reporter_kind,category,severity,summary,details,requested_action,evidence_json,idempotency_key,report_sha256,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"iss_backfill_{index:02d}", project_a["project_id"], worker_a["workstream_id"], "worker", "access", "degraded", f"Backfill {index}", "Details", "Review", "[]", f"backfill-{index}", "a" * 64, "open", "2026-08-25T00:00:00Z", "2026-08-25T00:00:00Z"))
            issue_ids.append(f"iss_backfill_{index:02d}")
        store.conn.execute("DELETE FROM attention_items WHERE recipient_workstream_id=?", (secretary_binding["workstream_id"],))
        inserted = backfill_attention(store, recipient_workstream_id=secretary_binding["workstream_id"], limit=2)
        self.assertEqual(inserted, 2)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='attention.backfilled' AND workstream_id=?", (secretary_binding["workstream_id"],)).fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
