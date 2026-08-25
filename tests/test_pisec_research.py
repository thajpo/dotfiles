import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from scripts.pisec.adapters import AdapterRegistry
from scripts.pisec.broker import BrokerDispatcher
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


if __name__ == "__main__":
    unittest.main()
