from pathlib import Path
import json
import os
import socketserver
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "herdr" / "plugins" / "pisec" / "pisec-plugin"


class FakeAdminState:
    def __init__(self):
        self.requests = []

    def result(self, request):
        self.requests.append(request)
        operation = request["operation"]
        payload = request["payload"]
        if operation == "system.status" and not payload:
            return {"projects": [{"project_id": "prj_" + "a" * 32, "display_name": "Demo"}]}
        if operation == "system.status":
            return {
                "workstreams": [
                    {
                        "workstream_id": "ws_" + "b" * 32,
                        "kind": "worker",
                        "title": "title\nspoof",
                        "desired_state": "active",
                        "provisioning_state": "bound",
                        "observed_state": "working",
                        "execution_profile": "worker-default",
                        "last_observed_at": "now",
                        "attention_reason": "none",
                    }
                ]
            }
        return {"ok": True}


class FakeAdminServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.socket_path = self.root / "admin" / "control.sock"
        self.socket_path.parent.mkdir(mode=0o700)
        self.state = FakeAdminState()
        state = self.state

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                request = json.loads(self.rfile.readline())
                response = {
                    "protocolVersion": 1,
                    "requestId": request["requestId"],
                    "ok": True,
                    "result": state.result(request),
                }
                self.wfile.write((json.dumps(response) + "\n").encode())

        self.server = FakeAdminServer(str(self.socket_path), Handler)
        os.chmod(self.socket_path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def run_plugin(self, command, *, extra=None):
        environment = os.environ.copy()
        environment["PISEC_ADMIN_SOCKET"] = str(self.socket_path)
        if extra:
            environment.update(extra)
        return subprocess.run([str(PLUGIN), command], cwd=ROOT, env=environment, text=True, capture_output=True)

    def test_startup_forwards_only_dedicated_herdr_session(self):
        socket_path = Path.home() / ".config" / "herdr" / "sessions" / "pisec" / "herdr.sock"
        result = self.run_plugin("startup", extra={"HERDR_SOCKET_PATH": str(socket_path)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.state.requests), 1)
        self.assertEqual(self.state.requests[0]["operation"], "workspace.startup")
        self.assertEqual(self.state.requests[0]["payload"], {"adapterId": "herdr", "socketPath": str(socket_path)})

    def test_noninteractive_board_renders_bounded_sanitized_rows(self):
        result = self.run_plugin("board")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Pisec board", result.stdout)
        self.assertIn("Demo", result.stdout)
        self.assertIn("title spoof", result.stdout)
        self.assertNotIn("title\nspoof", result.stdout)
        self.assertEqual([request["operation"] for request in self.state.requests], ["system.status", "system.status"])

    def test_focus_secretary_uses_canonical_context_repository(self):
        repository = self.root / "repo"
        repository.mkdir()
        context = {"workspace": {"worktree": {"checkout_path": str(repository)}}}
        result = self.run_plugin("focus-secretary", extra={"HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state.requests[0]["operation"], "secretary.focus")
        self.assertEqual(self.state.requests[0]["payload"], {"project": str(repository.resolve())})

    def test_focus_secretary_accepts_primary_workspace_pane_cwd(self):
        repository = self.root / "repo"
        repository.mkdir()
        context = {"workspace": {"workspace_id": "w1"}, "pane": {"cwd": str(repository)}}
        result = self.run_plugin("focus-secretary", extra={"HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state.requests[0]["operation"], "secretary.focus")
        self.assertEqual(self.state.requests[0]["payload"], {"project": str(repository.resolve())})

    def test_event_rejects_control_name_before_socket_request(self):
        result = self.run_plugin("event", extra={"HERDR_PLUGIN_EVENT": "bad\nname", "HERDR_PLUGIN_EVENT_JSON": "{}"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("event name is invalid", result.stderr)
        self.assertEqual(self.state.requests, [])


if __name__ == "__main__":
    unittest.main()
