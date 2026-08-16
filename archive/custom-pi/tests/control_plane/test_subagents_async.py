"""Async headless subagent assignments: detached launch, status, wait, workers."""

from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest
from unittest import mock

from scripts.pi_control.controller_channel import ControllerChannelError, PROTOCOL_VERSION
from scripts.pi_control.pi_client import PiControllerClient
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.host_supervisor import _ROLE_OPERATIONS, _rpc
from scripts.pi_control.launch import prepare_run
from scripts.pi_control.models import new_id
from scripts.pi_control.subagents import SEMANTIC_ROLES, SubagentError, _spawn_detached, child_status, interrupt_child, resume_child, start_child_assignment, start_worker_assignment, stop_child, wait_child_terminal
from tests.pi_test_build import allow_test_only_registered_build_rows
from tests.test_pi_core import _BUILD_ID, _host, _register_build, _repo


def _generation_fixture(root: Path) -> Path:
    """Create a fake registered generation with launcher stubs."""
    generation = root / "generation"
    (generation / "bin").mkdir(parents=True)
    for name in ("pi-system-run", "pi-system-container-run", "pi-system-workstream-run"):
        launcher = generation / "bin" / name
        launcher.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        launcher.chmod(0o755)
    return generation


def _register_generation_build(store: PiStore, generation: Path) -> None:
    from scripts.pi_control.models import utc_now
    digest = "sha256:" + "a" * 64
    store.conn.execute(
        "INSERT INTO installed_builds(build_id,source_commit,source_tree_hash,build_manifest_path,build_manifest_digest,resource_manifest_path,resource_manifest_digest,pi_version,package_lock_hash,status,installed_at,activated_at,rollback_path,verification_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_BUILD_ID, None, digest, str(generation / "build-manifest.json"), digest, str(generation / "release-resources.json"), digest, "0.83.0", digest, "staged", utc_now(), None, None, json.dumps({"verified": True})),
    )


class AsyncSubagentTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = allow_test_only_registered_build_rows()
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fixture(self, root: Path, role: str = "secretary") -> tuple[PiStore, dict, dict]:
        generation = _generation_fixture(root)
        (generation / "build-manifest.json").write_text("fixture", encoding="utf-8")
        (generation / "release-resources.json").write_text("fixture", encoding="utf-8")
        client = PiControllerClient(root / "state")
        project = client.register_project(str(_repo(root, "async")), "async")
        store = PiStore(root / "state").open()
        _register_generation_build(store, generation)
        if role == "secretary":
            conversation = store.conn.execute("SELECT * FROM conversations WHERE project_id=? AND role='secretary'", (project["project_id"],)).fetchone()
        else:
            working = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary'", (project["project_id"],)).fetchone()
            conversation = client.create_conversation(project_id=project["project_id"], role="personal", display_name="writer", working_copy_id=working["working_copy_id"])
            store.conn.execute("UPDATE conversations SET observed_state='ready' WHERE conversation_id=?", (conversation["conversation_id"],))
        return store, dict(project), dict(conversation)

    def _attested_run(self, store: PiStore, client: PiControllerClient, conversation: dict) -> dict:
        role = conversation["role"]
        if role == "secretary":
            prepared = prepare_run(store, conversation_id=conversation["conversation_id"], build_id=_BUILD_ID, host_process=_host("secretary"))
        else:
            working = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (conversation["working_copy_id"],)).fetchone()
            run_id = new_id("run")
            from tests.control_plane.test_p2_contract import tool_runtime
            prepared = prepare_run(store, conversation_id=conversation["conversation_id"], build_id=_BUILD_ID, host_process=_host("personal"), tool_runtime=tool_runtime(run_id, {"project_id": conversation["project_id"]}, working), run_id=run_id)
        client.attest_run(run_id=prepared.run["run_id"], manifest_digest=prepared.manifest["manifestDigest"])
        return prepared

    def test_semantic_role_catalog_expanded(self) -> None:
        self.assertTrue({"scout", "investigator", "researcher", "planner", "oracle", "delegate", "reviewer"} <= SEMANTIC_ROLES)
        self.assertIn("worker", SEMANTIC_ROLES)
        self.assertIn("subagent.start", _ROLE_OPERATIONS["secretary"])
        self.assertIn("worker.start", _ROLE_OPERATIONS["personal"])
        self.assertNotIn("worker.start", _ROLE_OPERATIONS["secretary"])

    def test_start_child_launches_detached_and_returns_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, project, conversation = self._fixture(root)
            try:
                prepared = self._attested_run(store, PiControllerClient(root / "state"), conversation)
                launched = []
                with mock.patch("scripts.pi_control.subagents._spawn_detached", side_effect=lambda argv, log: launched.append(argv) or 4242) as spawn:
                    result = start_child_assignment(store, parent_run_id=prepared.run["run_id"], semantic_role="planner", task="plan the migration", idempotency_key="plan-1", build_id=_BUILD_ID, model="scripted/scripted-1")
                self.assertTrue(result["launched"])
                argv = launched[0]
                self.assertEqual(argv[0].endswith("bin/pi-system-run"), True)
                self.assertIn("--child-request-id", argv)
                self.assertIn("--expected-role", argv)
                self.assertIn("investigator", argv)
                self.assertTrue(any("plan the migration" in item for item in argv))
                request_id = result["childRequest"]["child_request_id"]
                status = child_status(store, parent_run_id=prepared.run["run_id"], child_request_id=request_id)
                self.assertIsNone(status["terminal"])
                self.assertEqual(status["childRequest"]["state"], "ready")
                listing = [row["child_request_id"] for row in __import__("scripts.pi_control.subagents", fromlist=["list_child_requests"]).list_child_requests(store, parent_run_id=prepared.run["run_id"])]
                self.assertIn(request_id, listing)
                prepared.close()
            finally:
                store.close()

    def test_wait_returns_terminal_when_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, project, conversation = self._fixture(root)
            try:
                prepared = self._attested_run(store, PiControllerClient(root / "state"), conversation)
                with mock.patch("scripts.pi_control.subagents._spawn_detached", return_value=4243):
                    result = start_child_assignment(store, parent_run_id=prepared.run["run_id"], semantic_role="scout", task="map the surface", idempotency_key="scout-1", build_id=_BUILD_ID, model="scripted/scripted-1")
                request_id = result["childRequest"]["child_request_id"]
                with mock.patch("scripts.pi_control.subagents.time.sleep"):
                    # No run bound yet -> wait would poll; bind the parent run
                    # id (a valid FK) and write a terminal record to prove the
                    # fast path.
                    store.conn.execute("UPDATE child_requests SET child_run_id=? WHERE child_request_id=?", (prepared.run["run_id"], request_id))
                    from scripts.pi_control.subagents import record_child_terminal
                    record_child_terminal(store, child_request_id=request_id, terminal_class="success", result={"returnCode": 0})
                    waited = wait_child_terminal(store, parent_run_id=prepared.run["run_id"], child_request_id=request_id, timeout=5)
                self.assertTrue(waited["waited"])
                self.assertEqual(waited["terminal"]["terminal_class"], "success")
                with self.assertRaises(SubagentError):
                    child_status(store, parent_run_id="run_" + "f" * 32, child_request_id=request_id)
                prepared.close()
            finally:
                store.close()

    def test_worker_start_creates_isolated_working_copy_and_container_launch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, project, conversation = self._fixture(root, role="personal")
            try:
                prepared = self._attested_run(store, PiControllerClient(root / "state"), conversation)
                launched = []
                with mock.patch("scripts.pi_control.subagents._spawn_detached", side_effect=lambda argv, log: launched.append(argv) or 4244):
                    result = start_worker_assignment(store, parent_run_id=prepared.run["run_id"], task="implement the fix", title="fix-flag", idempotency_key="worker-1", build_id=_BUILD_ID, model="scripted/scripted-1", tool_image="python:3.11-slim@sha256:" + "a" * 64)
                self.assertTrue(result["launched"])
                argv = launched[0]
                self.assertEqual(argv[0].endswith("bin/pi-system-workstream-run"), True)
                self.assertIn("--tool-image", argv)
                worker = result["childRequest"]
                self.assertEqual(worker["runtime_role"], "workstream")
                workstream = result["workstream"]
                self.assertEqual(workstream["conversation_id"], worker["child_conversation_id"])
                # The worker copy is distinct from the parent's primary copy.
                parent_copy = store.conn.execute("SELECT working_copy_id FROM runs WHERE run_id=?", (prepared.run["run_id"],)).fetchone()["working_copy_id"]
                self.assertNotEqual(parent_copy, worker["child_working_copy_id"])
                # One writer per working copy: a second worker on the same copy
                # is impossible because each worker owns its own copy; verify
                # the writer epoch claim starts fresh on the worker copy.
                claim = store.conn.execute("SELECT writer_epoch,active_writer_run_id FROM working_copies WHERE working_copy_id=?", (worker["child_working_copy_id"],)).fetchone()
                self.assertEqual(claim["active_writer_run_id"], None)
                prepared.close()
            finally:
                store.close()

    def test_interrupt_and_stop_signal_the_detached_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, project, conversation = self._fixture(root)
            try:
                prepared = self._attested_run(store, PiControllerClient(root / "state"), conversation)
                with mock.patch("scripts.pi_control.subagents._spawn_detached", return_value=4242):
                    result = start_child_assignment(store, parent_run_id=prepared.run["run_id"], semantic_role="scout", task="map", idempotency_key="ctrl-1", build_id=_BUILD_ID, model="scripted/scripted-1")
                request_id = result["childRequest"]["child_request_id"]
                store.conn.execute("UPDATE child_requests SET child_run_id=? WHERE child_request_id=?", (prepared.run["run_id"], request_id))
                with mock.patch("scripts.pi_control.subagents.os.kill") as kill:
                    interrupted = interrupt_child(store, parent_run_id=prepared.run["run_id"], child_request_id=request_id)
                    self.assertTrue(interrupted["signaled"])
                    self.assertEqual(kill.call_args.args[0], 4242)
                    import signal as signal_module
                    self.assertEqual(kill.call_args.args[1], signal_module.SIGINT)
                    stopped = stop_child(store, parent_run_id=prepared.run["run_id"], child_request_id=request_id)
                    self.assertTrue(stopped["signaled"])
                    self.assertEqual(kill.call_args.args[1], signal_module.SIGTERM)
                prepared.close()
            finally:
                store.close()

    def test_resume_relaunches_only_interrupted_children(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, project, conversation = self._fixture(root)
            try:
                prepared = self._attested_run(store, PiControllerClient(root / "state"), conversation)
                with mock.patch("scripts.pi_control.subagents._spawn_detached", return_value=4242):
                    result = start_child_assignment(store, parent_run_id=prepared.run["run_id"], semantic_role="scout", task="map again", idempotency_key="ctrl-2", build_id=_BUILD_ID, model="scripted/scripted-1")
                request_id = result["childRequest"]["child_request_id"]
                with self.assertRaisesRegex(SubagentError, "only an interrupted child"):
                    resume_child(store, parent_run_id=prepared.run["run_id"], child_request_id=request_id, build_id=_BUILD_ID, model="scripted/scripted-1")
                # mark interrupted via a bound run + terminal record
                store.conn.execute("UPDATE child_requests SET child_run_id=? WHERE child_request_id=?", (prepared.run["run_id"], request_id))
                from scripts.pi_control.subagents import record_child_terminal
                record_child_terminal(store, child_request_id=request_id, terminal_class="interrupted", result={"returnCode": 130})
                launched = []
                with mock.patch("scripts.pi_control.subagents._spawn_detached", side_effect=lambda argv, log: launched.append(argv) or 4243):
                    resumed = resume_child(store, parent_run_id=prepared.run["run_id"], child_request_id=request_id, build_id=_BUILD_ID, model="scripted/scripted-1")
                self.assertTrue(resumed["launched"])
                argv = launched[0]
                self.assertIn("--child-request-id", argv)
                self.assertEqual(argv[argv.index("--conversation-id") + 1], result["childRequest"]["child_conversation_id"])
                prepared.close()
            finally:
                store.close()

    def test_channel_control_ops_validate_and_steer_posts_message(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, project, conversation = self._fixture(root)
            try:
                client = PiControllerClient(root / "state")
                prepared = self._attested_run(store, client, conversation)
                base = {"protocolVersion": PROTOCOL_VERSION, "type": "request", "requestId": "request-1"}
                with mock.patch("scripts.pi_control.subagents._spawn_detached", return_value=4244):
                    started = _rpc(store, prepared.run["run_id"], prepared.manifest["manifestDigest"], {**base, "operation": "subagent.start", "payload": {"role": "delegate", "task": "verify the flag", "idempotencyKey": "ctrl-3"}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                request_id = started["childRequest"]["child_request_id"]
                store.conn.execute("UPDATE child_requests SET child_run_id=? WHERE child_request_id=?", (prepared.run["run_id"], request_id))
                with mock.patch("scripts.pi_control.subagents.os.kill") as kill:
                    _rpc(store, prepared.run["run_id"], prepared.manifest["manifestDigest"], {**base, "operation": "subagent.interrupt", "payload": {"childRequestId": request_id}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                    self.assertEqual(kill.call_args.args[0], 4244)
                store.conn.execute("UPDATE child_requests SET child_run_id=? WHERE child_request_id=?", (prepared.run["run_id"], request_id))
                steered = _rpc(store, prepared.run["run_id"], prepared.manifest["manifestDigest"], {**base, "operation": "subagent.steer", "payload": {"childRequestId": request_id, "message": "focus on the flag"}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                self.assertEqual(steered["kind"], "progress")
                self.assertEqual(json.loads(steered["payload_json"])["steer"], "focus on the flag")
                with self.assertRaisesRegex(ControllerChannelError, "fields are invalid"):
                    _rpc(store, prepared.run["run_id"], prepared.manifest["manifestDigest"], {**base, "operation": "subagent.stop", "payload": {"childRequestId": 123}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                prepared.close()
            finally:
                store.close()

    def test_spawn_detached_creates_private_log_and_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            log_path = root / "child.log"
            pid = _spawn_detached(["/usr/bin/true"], log_path)
            self.assertGreater(pid, 0)
            self.assertTrue(log_path.is_file())
            self.assertEqual(log_path.stat().st_mode & 0o777, 0o600)

    def test_channel_async_ops_are_scoped_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, project, conversation = self._fixture(root)
            try:
                prepared = self._attested_run(store, PiControllerClient(root / "state"), conversation)
                base = {"protocolVersion": PROTOCOL_VERSION, "type": "request", "requestId": "request-1"}
                with mock.patch("scripts.pi_control.subagents._spawn_detached", return_value=4245):
                    value = _rpc(store, prepared.run["run_id"], prepared.manifest["manifestDigest"], {**base, "operation": "subagent.start", "payload": {"role": "oracle", "task": "review the direction", "idempotencyKey": "oracle-1"}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                self.assertTrue(value["launched"])
                request_id = value["childRequest"]["child_request_id"]
                status = _rpc(store, prepared.run["run_id"], prepared.manifest["manifestDigest"], {**base, "operation": "subagent.status", "payload": {"childRequestId": request_id}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                self.assertIsNone(status["terminal"])
                listing = _rpc(store, prepared.run["run_id"], prepared.manifest["manifestDigest"], {**base, "operation": "subagent.list", "payload": {}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                self.assertTrue(any(row["child_request_id"] == request_id for row in listing))
                with self.assertRaisesRegex(ControllerChannelError, "fields are invalid"):
                    _rpc(store, prepared.run["run_id"], prepared.manifest["manifestDigest"], {**base, "operation": "subagent.start", "payload": {"role": "worker", "task": "x", "idempotencyKey": "y"}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                with self.assertRaisesRegex(ControllerChannelError, "authenticated role"):
                    _rpc(store, prepared.run["run_id"], prepared.manifest["manifestDigest"], {**base, "operation": "worker.start", "payload": {"task": "x", "title": "t", "idempotencyKey": "y"}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1", "toolImage": "python:3.11-slim@sha256:" + "a" * 64})
                prepared.close()
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
