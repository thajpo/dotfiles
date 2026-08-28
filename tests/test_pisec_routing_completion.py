import unittest
from pathlib import Path

from scripts.pisec.broker import BrokerDispatcher
from scripts.pisec.codex_mcp import TOOLS, TOOL_DESCRIPTIONS, _adapter_idempotency_key
from scripts.pisec.config import DEFAULT_WORKER_MODEL, DEFAULT_WORKER_ROUTE, _validate_worker_routing
from scripts.pisec.models import InvalidRequestError
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

    def test_codex_adapter_owns_stable_retry_keys(self):
        first = _adapter_idempotency_key("issue.report", {"summary": "blocked", "evidence": ["fixture"]}, "call-1")
        reordered = _adapter_idempotency_key("issue.report", {"evidence": ["fixture"], "summary": "blocked"}, "call-1")
        different_call = _adapter_idempotency_key("issue.report", {"summary": "blocked", "evidence": ["fixture"]}, "call-2")
        self.assertEqual(first, reordered)
        self.assertNotEqual(first, different_call)
        self.assertRegex(first, r"^adapter:codex:[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
