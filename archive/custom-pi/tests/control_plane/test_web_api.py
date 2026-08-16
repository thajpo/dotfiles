from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.controller_channel import ChannelReader, ControllerChannelError, send_frame
from scripts.pi_control.web_api import DEFAULT_WEB_ROOT, PiWebServer, PiWebRequestHandler


class ConnectedSocket:
    def __init__(self, inner):
        self.inner = inner

    def settimeout(self, _timeout):
        return None

    def connect(self, _path):
        return None

    def sendall(self, value):
        return self.inner.sendall(value)

    def recv(self, size):
        return self.inner.recv(size)

    def fileno(self):
        return self.inner.fileno()

    def close(self):
        return self.inner.close()


class StreamWriter:
    def __init__(self):
        self.value = bytearray()

    def write(self, value):
        self.value.extend(value)
        return len(value)

    def flush(self):
        return None


class WebApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_root = Path(self.tempdir.name) / "state"
        with PiStore(self.state_root):
            pass
        self.server = PiWebServer(("127.0.0.1", 0), state_root=self.state_root, web_root=DEFAULT_WEB_ROOT)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def get(self, path: str):
        with urlopen(self.base + path, timeout=2) as response:
            return response.status, response.headers, response.read()

    def post(self, path: str, value: dict):
        request = Request(
            self.base + path,
            data=json.dumps(value).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": self.base},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return response.status, response.headers, response.read()

    def test_health_and_empty_bootstrap(self):
        status, _, body = self.get("/api/v1/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

        status, _, body = self.get("/api/v1/bootstrap")
        self.assertEqual(status, 200)
        data = json.loads(body)["data"]
        self.assertEqual(data["projects"], [])
        self.assertEqual(data["conversations"], [])

    def test_static_shell_is_served_without_api_cors(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Pi Web", body)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_path_traversal_is_not_served(self):
        with self.assertRaises(HTTPError) as raised:
            self.get("/%2e%2e/WEB_CONTROL_PLANE_PLAN.md")
        self.assertEqual(raised.exception.code, 404)

    def test_non_loopback_bind_is_rejected(self):
        with self.assertRaises(ValueError):
            PiWebServer(("0.0.0.0", 0), state_root=self.state_root, web_root=DEFAULT_WEB_ROOT)

    def test_bridge_requires_client_idempotency_key(self):
        with self.assertRaises(HTTPError) as raised:
            self.post(
                "/api/v1/projects/prj_11111111111111111111111111111111/conversations/conv_22222222222222222222222222222222/bridge",
                {"operation": "prompt", "text": "hello"},
            )
        self.assertEqual(raised.exception.code, 400)

    def test_bridge_rejects_invalid_runtime_setting_before_live_lookup(self):
        with self.assertRaises(HTTPError) as raised:
            self.post(
                "/api/v1/projects/prj_11111111111111111111111111111111/conversations/conv_22222222222222222222222222222222/bridge",
                {"operation": "setModel", "model": "not-a-model", "idempotencyKey": "setting-1"},
            )
        self.assertEqual(raised.exception.code, 400)

    def test_bridge_rejects_invalid_queue_id_before_live_lookup(self):
        with self.assertRaises(HTTPError) as raised:
            self.post(
                "/api/v1/projects/prj_11111111111111111111111111111111/conversations/conv_22222222222222222222222222222222/bridge",
                {"operation": "removeQueued", "inputId": "not valid", "idempotencyKey": "remove-1"},
            )
        self.assertEqual(raised.exception.code, 400)

    def test_bridge_forwards_remove_queued_payload(self):
        handler = object.__new__(PiWebRequestHandler)
        handler._request_body = mock.Mock(return_value={
            "operation": "removeQueued",
            "inputId": "web-input-1",
            "idempotencyKey": "remove-1",
        })
        handler._send = mock.Mock()
        store_context = mock.MagicMock()
        handler._store = mock.Mock(return_value=store_context)
        row = {"run_id": "run_11111111111111111111111111111111"}
        descriptor = {"runId": row["run_id"]}
        channel = mock.Mock()
        reader = mock.Mock()
        reader.receive.return_value = {"requestId": "web-fixed", "type": "accepted", "operation": "removeQueued", "deliveryState": "removed"}
        path = "/api/v1/projects/prj_11111111111111111111111111111111/conversations/conv_22222222222222222222222222222222/bridge"
        with mock.patch.object(handler, "_live_run", return_value=(row, descriptor)), mock.patch.object(PiWebRequestHandler, "_connect_bridge", return_value=(channel, reader)), mock.patch("scripts.pi_control.web_api.new_id", return_value="web-fixed"):
            handler._bridge_command(path)
        request = json.loads(channel.sendall.call_args.args[0].decode("utf-8"))
        self.assertEqual(request["operation"], "removeQueued")
        self.assertEqual(request["inputId"], "web-input-1")
        self.assertEqual(request["idempotencyKey"], "remove-1")
        handler._send.assert_called_once()

    def test_bridge_stream_forwards_event_ids_keepalive_and_end(self):
        handler = object.__new__(PiWebRequestHandler)
        handler.command = "GET"
        handler.headers = {"Last-Event-ID": "event-previous"}
        handler.wfile = StreamWriter()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        channel = mock.Mock()
        reader = mock.Mock()
        reader.receive.side_effect = [
            {"type": "subscribed"},
            {"type": "event", "eventId": "event-next", "event": {"type": "message_start", "text": "hello"}},
            ControllerChannelError("channel receive timed out"),
            ControllerChannelError("channel receive timed out"),
        ]
        row = {"run_id": "run_11111111111111111111111111111111"}
        descriptor = {"runId": row["run_id"]}
        with mock.patch.object(handler, "_live_run", return_value=(row, descriptor)), mock.patch.object(PiWebRequestHandler, "_connect_bridge", return_value=(channel, reader)):
            with mock.patch("scripts.pi_control.web_api.time.monotonic", side_effect=[0.0, 0.0, 1.0, 26.0]):
                handler._bridge_stream(
                    mock.Mock(),
                    "prj_11111111111111111111111111111111",
                    "conv_22222222222222222222222222222222",
                )
        output = handler.wfile.value.decode("utf-8")
        self.assertTrue(handler.close_connection)
        self.assertIn("id: event-next\ndata: {\"text\":\"hello\",\"type\":\"message_start\"}\n\n", output)
        self.assertIn(": keep-alive\n\n", output)
        self.assertTrue(output.endswith('data: {"type":"stream_end"}\n\n'))
        sent_subscribe = json.loads(channel.sendall.call_args.args[0].decode("utf-8"))
        self.assertEqual(sent_subscribe["afterEventId"], "event-previous")

    def test_options_advertises_mutation_method(self):
        request = Request(self.base + "/api/v1/bootstrap", method="OPTIONS")
        with urlopen(request, timeout=2) as response:
            self.assertIn("POST", response.headers["Allow"])

    def test_bridge_handshake_checks_all_process_identity_fields(self):
        left, right = socket.socketpair()
        row = {
            "run_id": "run_11111111111111111111111111111111",
            "conversation_id": "conv_22222222222222222222222222222222",
            "pi_session_id": "pi-conv_22222222222222222222222222222222",
            "build_id": "build-run",
            "child_pid": 4242,
            "child_start_identity": "linux:boot:123",
        }
        descriptor = {
            "socketPath": "unused",
            "projectId": "prj_44444444444444444444444444444444",
            "runId": row["run_id"],
            "conversationId": row["conversation_id"],
            "sessionId": row["pi_session_id"],
            "controllerBuildId": "build-controller",
            "runBuildId": row["build_id"],
            "manifestDigest": "sha256:" + "1" * 64,
            "childPid": row["child_pid"],
            "childStartIdentity": row["child_start_identity"],
            "restartEpoch": "ctl_33333333333333333333333333333333",
            "capability": "capability",
        }

        def server():
            request = ChannelReader(right).receive(timeout=2)
            self.assertEqual(request["protocolVersion"], 2)
            send_frame(right, {"protocolVersion": 2, "type": "connected", "runId": row["run_id"], "conversationId": row["conversation_id"], "projectId": descriptor["projectId"], "sessionId": row["pi_session_id"], "controllerBuildId": "build-controller", "runBuildId": row["build_id"], "manifestDigest": descriptor["manifestDigest"], "childPid": row["child_pid"], "childStartIdentity": row["child_start_identity"], "restartEpoch": descriptor["restartEpoch"]})

        worker = threading.Thread(target=server)
        worker.start()
        original = descriptor["socketPath"]
        descriptor["socketPath"] = "unused"
        # The static connector only needs a socket object at this point; use a
        # temporary method wrapper so the identity checks stay in one test.
        with mock.patch("socket.socket") as socket_factory:
            socket_factory.return_value = ConnectedSocket(left)
            channel, _reader = PiWebRequestHandler._connect_bridge(row, descriptor)
        channel.close()
        worker.join(timeout=2)
        right.close()
        descriptor["socketPath"] = original

    def test_bridge_handshake_rejects_child_identity_mismatch(self):
        left, right = socket.socketpair()
        row = {
            "run_id": "run_11111111111111111111111111111111",
            "conversation_id": "conv_22222222222222222222222222222222",
            "pi_session_id": "pi-conv_22222222222222222222222222222222",
            "build_id": "build-run",
            "child_pid": 4242,
            "child_start_identity": "linux:boot:123",
        }
        descriptor = {
            "socketPath": "unused", "projectId": "prj_44444444444444444444444444444444", "runId": row["run_id"], "conversationId": row["conversation_id"],
            "sessionId": row["pi_session_id"], "controllerBuildId": "build-controller", "runBuildId": row["build_id"],
            "manifestDigest": "sha256:" + "1" * 64, "childPid": row["child_pid"],
            "childStartIdentity": "linux:boot:wrong", "restartEpoch": "ctl_33333333333333333333333333333333", "capability": "capability",
        }
        def server():
            ChannelReader(right).receive(timeout=2)
            send_frame(right, {"protocolVersion": 2, "type": "connected", "runId": row["run_id"], "conversationId": row["conversation_id"], "projectId": descriptor["projectId"], "sessionId": row["pi_session_id"], "controllerBuildId": "build-controller", "runBuildId": row["build_id"], "manifestDigest": descriptor["manifestDigest"], "childPid": row["child_pid"], "childStartIdentity": "linux:boot:wrong", "restartEpoch": descriptor["restartEpoch"]})
        worker = threading.Thread(target=server)
        worker.start()
        with self.assertRaises(ControllerChannelError):
            with mock.patch("socket.socket") as socket_factory:
                socket_factory.return_value = ConnectedSocket(left)
                PiWebRequestHandler._connect_bridge(row, descriptor)
        worker.join(timeout=2)
        left.close()
        right.close()


if __name__ == "__main__":
    unittest.main()
