from pathlib import Path
import json
import os
import socket
import subprocess
import tempfile
import threading
import unittest

from scripts.pisec.adapters import AdapterRegistry
from scripts.pisec.broker import BrokerDispatcher, BrokerService
from scripts.pisec.models import InvalidRequestError, PisecError, new_id
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import register_project
from scripts.pisec.protocol import decode_request, request, success_response
from scripts.pisec.secretary import ensure_secretary
from tests.pisec_fixture import FixtureGitObjects, FixtureHarness, FixtureWorkspace, make_repo


class ProtocolUnitTests(unittest.TestCase):
    def test_decoder_rejects_trailing_duplicate_oversized_and_unknown(self):
        base = {"protocolVersion": 1, "requestId": new_id("req"), "operation": "project.list", "payload": {}}
        with self.assertRaises(InvalidRequestError):
            decode_request((json.dumps(base) + "\n{}\n").encode())
        with self.assertRaises(InvalidRequestError):
            decode_request(b'{"protocolVersion":1,"protocolVersion":1,"requestId":"req_00000000000000000000000000000000","operation":"x","payload":{}}\n')
        unknown = dict(base, extra=True)
        with self.assertRaises(InvalidRequestError):
            decode_request((json.dumps(unknown) + "\n").encode())
        with self.assertRaises(InvalidRequestError):
            decode_request(b" " * (64 * 1024) + b"\n")


class BrokerSocketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        make_repo(self.repo)
        self.state = self.root / "state"
        self.harness = FixtureHarness(self.root)
        with PiStore(self.state) as store:
            project = register_project(store, self.repo, default_ref="main")
            self.project_id = project["project_id"]
            ensured = ensure_secretary(store, self.project_id, self.harness, FixtureWorkspace(self.root, store))
            self.token = Path(ensured["binding"]["launch_secret_path"]).read_text().strip()
            self.binding = ensured["binding"]
            store.conn.execute("UPDATE runtime_bindings SET runtime_instance_id=NULL,report_seq=0 WHERE workstream_id=?", (ensured["workstream"]["workstream_id"],))
        self.workspace = FixtureWorkspace(self.root, None)
        self.registry = AdapterRegistry()
        self.registry.register_harness(self.harness)
        self.registry.register_workspace(self.workspace)
        dispatcher = BrokerDispatcher(lambda: PiStore(self.state), registry=self.registry, harness=self.harness, workspace=self.workspace, git_objects=FixtureGitObjects())
        self.service = BrokerService(dispatcher, runtime_root=self.root / "runtime")
        self.service.start()

    def tearDown(self):
        self.service.stop()
        self.temp.cleanup()

    def test_socket_directories_and_files_are_owner_only(self):
        for path in self.service.paths.values():
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_admin_and_secretary_positive_paths(self):
        projects = request(self.service.paths["admin"], "project.list", {})
        self.assertEqual(projects["projects"][0]["project_id"], self.project_id)
        status = request(self.service.paths["secretary"], "project.status", {"authToken": self.token})
        self.assertEqual(status["project"]["project_id"], self.project_id)

    def test_cross_socket_operations_and_bad_token_fail(self):
        with self.assertRaises(PisecError) as secretary_error:
            request(self.service.paths["secretary"], "project.list", {"authToken": self.token})
        self.assertEqual(secretary_error.exception.code, "authorization_denied")
        with self.assertRaises(PisecError) as admin_error:
            request(self.service.paths["admin"], "project.status", {})
        self.assertEqual(admin_error.exception.code, "authorization_denied")
        with self.assertRaises(PisecError) as runtime_error:
            request(self.service.paths["runtime"], "project.status", {})
        self.assertEqual(runtime_error.exception.code, "authorization_denied")
        with self.assertRaises(PisecError) as bad_token_error:
            request(self.service.paths["secretary"], "project.status", {"authToken": "x" * 48})
        self.assertEqual(bad_token_error.exception.code, "authorization_denied")

    def test_runtime_handler_is_only_on_runtime_socket(self):
        session = Path(self.binding["harness_home"]) / "sessions" / "one.jsonl"
        session.write_text("session\n")
        payload = {"workstreamId": self.binding["workstream_id"], "runtimeInstanceId": "protocol-runtime", "seq": 1, "event": "session_start", "state": "starting", "nativeSessionKind": "path", "nativeSessionValue": str(session), "startSource": "startup", "surfaceId": self.binding["workspace_surface_id"], "token": self.token}
        result = request(self.service.paths["runtime"], "runtime.report", payload)
        self.assertTrue(result["accepted"])
        with self.assertRaises(PisecError):
            request(self.service.paths["admin"], "runtime.report", payload)

    def test_malformed_request_returns_bounded_public_error(self):
        path = self.service.paths["admin"]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(path))
            client.sendall(b'{"bad":true}\n')
            client.shutdown(socket.SHUT_WR)
            response = client.recv(65536)
        body = json.loads(response)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertNotIn("Traceback", response.decode())

    def test_response_does_not_require_client_eof(self):
        path = self.service.paths["runtime"]
        request_id = new_id("req")
        wire = json.dumps({"protocolVersion": 1, "requestId": request_id, "operation": "runtime.report", "payload": {"state": "idle"}}).encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(path))
            client.sendall(wire)
            response = client.recv(65536)
        body = json.loads(response)
        self.assertFalse(body["ok"])
        self.assertEqual(body["requestId"], request_id)

    def test_workspace_callback_reconciliation_is_deferred(self):
        called = threading.Event()
        release = threading.Event()

        def blocking_reconcile(store, payload):
            called.set()
            release.wait(2)
            return {"reconciled": True}

        self.workspace.reconcile = blocking_reconcile
        service = BrokerService(
            BrokerDispatcher(lambda: PiStore(self.state), registry=self.registry, harness=self.harness, workspace=self.workspace, git_objects=FixtureGitObjects()),
            runtime_root=self.root / "deferred-runtime",
        )
        service.start()
        try:
            result = request(service.paths["admin"], "workspace.event", {"adapterId": self.workspace.manifest.adapter_id, "event": "pane.agent_status_changed", "payload": {}}, timeout=0.5)
            self.assertEqual(result, {"accepted": True, "reconcileQueued": True})
            self.assertTrue(called.wait(0.5))
        finally:
            release.set()
            service.stop()

    def test_secretary_projection_omits_binding_material(self):
        status = request(self.service.paths["secretary"], "project.status", {"authToken": self.token})
        encoded = json.dumps(status)
        for field in ("repository_path", "git_common_dir", "launch_secret_path", "fence_policy_path", "private_git_object_dir", "runtime_token_sha256", "harness_home"):
            self.assertNotIn(field, encoded)
        prepared = request(self.service.paths["secretary"], "workstream.prepare", {"authToken": self.token, "title": "Bounded worker", "purpose": "Verify projection", "brief": "Use only the approved scope.", "taskPacket": {"schemaVersion": 1, "outcome": "Projection is bounded.", "boundaries": ["Keep host paths private."], "acceptance": ["Public projection omits private paths."], "openQuestions": [], "evidence": ["Protocol test."]}, "idempotencyKey": "projection-check"})
        prepared_encoded = json.dumps(prepared)
        for field in ("privateGitObjectDir", "gitCommonObjectDir", "private_git_object_dir", "git_common_dir"):
            self.assertNotIn(field, prepared_encoded)

    def test_success_response_is_bounded(self):
        response = json.loads(success_response(new_id("req"), {"large": "x" * (64 * 1024)}))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
