import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.pisec.broker import BrokerDispatcher
from scripts.pisec.codex_mcp import PAYLOAD_ADAPTERS, TOOLS, TOOL_DESCRIPTIONS, _adapter_idempotency_key, _request
from scripts.pisec.config import DEFAULT_WORKER_MODEL, DEFAULT_WORKER_ROUTE, _validate_worker_routing
from scripts.pisec.harnesses.codex import _prompt as codex_worker_prompt
from scripts.pisec.models import InvalidRequestError
from scripts.pisec.protocol import decode_request, success_response
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace
from scripts.pisec.adapters import AdapterRegistry


class PisecRoutingCompletionTests(unittest.TestCase):
    def test_default_route_is_codex_luna_high(self):
        self.assertEqual(DEFAULT_WORKER_MODEL, "openai-codex/gpt-5.6-luna:high")
        self.assertEqual(DEFAULT_WORKER_ROUTE, {"harness": "codex", "model": "gpt-5.6-luna", "reasoningEffort": "high"})
        valid = {
            "defaultModel": DEFAULT_WORKER_MODEL,
            "fallbackHarness": "omp",
            "routes": {DEFAULT_WORKER_MODEL: DEFAULT_WORKER_ROUTE},
        }
        self.assertEqual(_validate_worker_routing(valid)["defaultModel"], DEFAULT_WORKER_MODEL)
        invalid_default = {**valid, "defaultModel": "worker-default"}
        with self.assertRaises(InvalidRequestError):
            _validate_worker_routing(invalid_default)
        invalid_route = {**valid, "routes": {DEFAULT_WORKER_MODEL: {**DEFAULT_WORKER_ROUTE, "harness": "omp"}}}
        with self.assertRaises(InvalidRequestError):
            _validate_worker_routing(invalid_route)

    def test_explicit_unknown_model_does_not_fallback(self):
        root = Path("/tmp/pisec-routing-test")
        harness = FixtureHarness(root)
        workspace = FixtureWorkspace(root)
        registry = AdapterRegistry()
        registry.register_harness(harness)
        registry.register_workspace(workspace)
        dispatcher = BrokerDispatcher(
            lambda: None,
            registry=registry,
            harness=harness,
            workspace=workspace,
            config={},
            prepare_surfaces=False,
        )
        with self.assertRaisesRegex(InvalidRequestError, "not a configured worker route"):
            dispatcher._worker_route("worker-default")
        selected, model, effort, adapter_id = dispatcher._worker_route(None)
        self.assertIs(selected, harness)
        self.assertEqual((model, effort, adapter_id), (None, None, "fixture-harness"))
        dispatcher.stop_background()

    def test_codex_exposes_the_same_completion_contract(self):
        self.assertEqual(TOOLS["pisec_submit_completion"][0], "workstream.completion.submit")
        schema = TOOLS["pisec_submit_completion"][1]
        self.assertEqual(schema["required"], ["completion"])
        completion = schema["properties"]["completion"]
        self.assertEqual(
            completion["required"],
            ["acceptance", "verification", "source_commit", "task_packet_sha256", "changed_surfaces", "residual_risk"],
        )
        self.assertEqual(completion["properties"]["source_commit"]["pattern"], "^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
        self.assertIn("creates the matching ready_review checkpoint atomically", TOOL_DESCRIPTIONS["pisec_submit_completion"])
        self.assertIn("accepted target drift", TOOL_DESCRIPTIONS["pisec_submit_completion"])
        self.assertIn("replacement completion packet", TOOL_DESCRIPTIONS["pisec_submit_completion"])
        self.assertIn("criterion text, order, and passed status unchanged", TOOL_DESCRIPTIONS["pisec_submit_completion"])
        self.assertNotIn("Use once", TOOL_DESCRIPTIONS["pisec_submit_completion"])

    def test_codex_worker_prompt_explains_the_post_acceptance_drift_exception(self):
        prompt = codex_worker_prompt({"taskPacket": {}, "harnessModel": "gpt-5.6-luna", "reasoningEffort": "high"})
        self.assertIn("sole final handoff", prompt)
        self.assertIn("accepted target drift", prompt)
        self.assertIn("replacement completion packet", prompt)
        self.assertIn("existing human acceptance", prompt)

    def test_codex_exposes_typed_progress_and_issue_contracts(self):
        self.assertIn("authorized attention record and its typed source", TOOL_DESCRIPTIONS["pisec_inspect_attention"])
        self.assertIn("exact accepted completion contract", TOOL_DESCRIPTIONS["pisec_inspect_attention"])
        self.assertIn("integration source ID", TOOL_DESCRIPTIONS["pisec_inspect_attention"])
        checkpoint = TOOLS["pisec_checkpoint_workstream"][1]
        self.assertFalse(checkpoint["additionalProperties"])
        self.assertEqual(checkpoint["required"], ["phase", "summary", "next_action", "evidence"])
        self.assertEqual(checkpoint["properties"]["phase"]["enum"], ["investigating", "implementing", "verifying"])
        issue = TOOLS["pisec_report_issue"][1]
        self.assertEqual(issue["required"], ["category", "severity", "summary", "details", "requested_action", "evidence"])
        self.assertEqual(issue["properties"]["severity"]["enum"], ["blocking", "degraded", "improvement"])

    def test_codex_completion_adapter_matches_the_broker_contract(self):
        completion = {
            "acceptance": [{"criterion": "README updated", "status": "passed", "evidence": ["README.md"]}],
            "verification": [{"command": "git diff --check", "result": "passed"}],
            "source_commit": "a" * 40,
            "task_packet_sha256": "b" * 64,
            "changed_surfaces": ["README.md"],
            "residual_risk": "none",
        }
        self.assertEqual(
            PAYLOAD_ADAPTERS["workstream.completion.submit"]({"completion": completion}),
            {
                "completionPacket": {
                    "acceptance": completion["acceptance"],
                    "verification": completion["verification"],
                    "sourceCommit": "a" * 40,
                    "taskPacketSha256": "b" * 64,
                    "changedSurfaces": ["README.md"],
                    "residualRisk": "none",
                }
            },
        )

    def test_codex_progress_and_issue_adapters_match_the_broker_contract(self):
        checkpoint = PAYLOAD_ADAPTERS["workstream.checkpoint"](
            {"phase": "verifying", "summary": "Checks pass", "next_action": "Submit completion", "evidence": ["git diff --check"]}
        )
        self.assertEqual(checkpoint["nextAction"], "Submit completion")
        self.assertNotIn("next_action", checkpoint)
        issue = PAYLOAD_ADAPTERS["issue.report"](
            {"category": "tooling", "severity": "degraded", "summary": "Socket failed", "details": "Connection refused", "requested_action": "Repair update ordering", "evidence": ["errno 111"]}
        )
        self.assertEqual(issue["requestedAction"], "Repair update ordering")
        self.assertNotIn("requested_action", issue)

    def test_codex_adapter_owns_stable_retry_keys(self):
        first = _adapter_idempotency_key("issue.report", {"summary": "blocked", "evidence": ["fixture"]}, "call-1")
        reordered = _adapter_idempotency_key("issue.report", {"evidence": ["fixture"], "summary": "blocked"}, "call-1")
        different_call = _adapter_idempotency_key("issue.report", {"summary": "blocked", "evidence": ["fixture"]}, "call-2")
        self.assertEqual(first, reordered)
        self.assertNotEqual(first, different_call)
        self.assertRegex(first, r"^adapter:codex:[0-9a-f]{64}$")

    def test_codex_mcp_sends_the_complete_authenticated_protocol_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "runtime.sock"
            ready = threading.Event()
            captured = {}
            failures = []

            def serve():
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                        server.bind(str(socket_path))
                        server.listen()
                        ready.set()
                        connection, _address = server.accept()
                        with connection:
                            request_bytes = b""
                            while not request_bytes.endswith(b"\n"):
                                request_bytes += connection.recv(65536)
                            request = decode_request(request_bytes)
                            captured.update(request)
                            connection.sendall(success_response(request["requestId"], {"task": "available"}))
                except Exception as error:  # pragma: no cover - surfaced in the main test thread
                    failures.append(error)
                    ready.set()

            server_thread = threading.Thread(target=serve, daemon=True)
            server_thread.start()
            self.assertTrue(ready.wait(5))
            environment = {
                "PISEC_RUNTIME_SOCKET": str(socket_path),
                "PISEC_RUNTIME_TOKEN": "t" * 64,
                "PISEC_RUNTIME_GENERATION": "a" * 64,
                "PISEC_WORKSTREAM_ID": "ws_" + "b" * 32,
                "PISEC_RUNTIME_INSTANCE_ID": "c" * 32,
                "PISEC_SURFACE_ID": "workspace:pane",
            }
            with patch.dict(os.environ, environment, clear=False):
                result = _request("task.get", {}, "call-1")
            server_thread.join(timeout=5)

            self.assertFalse(server_thread.is_alive())
            self.assertFalse(failures, failures)
            self.assertEqual(result, {"task": "available"})
            self.assertRegex(captured["requestId"], r"^req_[0-9a-f]{32}$")
            self.assertEqual(captured["payload"]["generation"], "a" * 64)


if __name__ == "__main__":
    unittest.main()
