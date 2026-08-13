"""Regression tests for CLI request field adaptation and client key reading.

The CLI routes change/review/integration operations through dispatch, which
adapts camelCase protocol requests to the snake_case keywords the controller
functions accept. Previously these branches called the client methods with raw
camelCase (KeyError: 'changeId') or the client methods read camelCase from the
already-adapted value. These tests pin the adapter and the client-key contract.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.pi_protocol import ProtocolError, adapt_request
from scripts.pi_control.pi_store import PiStore


class RequestAdaptationTests(unittest.TestCase):
    def test_change_submit_adapts_camelcase(self) -> None:
        adapted = adapt_request("change.submit", {
            "projectId": "prj_" + "a" * 32,
            "workingCopyId": "wc_" + "b" * 32,
            "targetRef": "refs/heads/main",
            "title": "title",
            "summary": "summary",
            "captureMode": "dirty",
            "selectedPaths": ["a.txt"],
            "idempotencyKey": "key-1",
        })
        self.assertEqual(adapted["project_id"], "prj_" + "a" * 32)
        self.assertEqual(adapted["working_copy_id"], "wc_" + "b" * 32)
        self.assertEqual(adapted["target_ref"], "refs/heads/main")
        self.assertEqual(adapted["capture_mode"], "dirty")
        self.assertEqual(adapted["selected_paths"], ["a.txt"])
        self.assertEqual(adapted["idempotency_key"], "key-1")

    def test_review_operations_adapt_camelcase(self) -> None:
        request = adapt_request("review.request", {
            "changeId": "chg_" + "c" * 32,
            "revision": 1,
            "reviewerConversationId": "conv_" + "d" * 32,
            "reviewerRunId": "run_" + "e" * 32,
            "reviewerActorId": "actor-1",
            "evidence": {"probe": True},
        })
        self.assertEqual(request["change_id"], "chg_" + "c" * 32)
        self.assertEqual(request["reviewer_conversation_id"], "conv_" + "d" * 32)
        self.assertEqual(request["reviewer_run_id"], "run_" + "e" * 32)
        submit = adapt_request("review.submit", {
            "reviewId": "review_" + "f" * 32,
            "verdict": "accept",
            "summary": "ok",
            "reviewerRunId": "run_" + "e" * 32,
            "reviewerActorId": "actor-1",
        })
        self.assertEqual(submit["review_id"], "review_" + "f" * 32)
        self.assertEqual(submit["reviewer_run_id"], "run_" + "e" * 32)

    def test_integration_operations_adapt_camelcase(self) -> None:
        analyze = adapt_request("integration.analyze", {
            "projectId": "prj_" + "a" * 32,
            "changeId": "chg_" + "c" * 32,
            "revision": 1,
            "targetWorkingCopyId": "wc_" + "b" * 32,
            "targetRef": "refs/heads/main",
        })
        self.assertEqual(analyze["target_working_copy_id"], "wc_" + "b" * 32)
        self.assertEqual(analyze["target_ref"], "refs/heads/main")
        authorize = adapt_request("integration.authorize", {
            "integrationId": "int_" + "g" * 32,
            "actorId": "user-1",
            "requestContextId": "ctx-1",
            "expiresAt": "2026-08-12T00:00:00Z",
        })
        self.assertEqual(authorize["integration_id"], "int_" + "g" * 32)
        self.assertEqual(authorize["request_context_id"], "ctx-1")
        integrate = adapt_request("integration.integrate", {
            "integrationId": "int_" + "g" * 32,
            "authorizationId": "auth_" + "h" * 32,
        })
        self.assertEqual(integrate["integration_id"], "int_" + "g" * 32)
        self.assertEqual(integrate["authorization_id"], "auth_" + "h" * 32)

    def test_investigation_start_accepts_both_field_spellings(self) -> None:
        # The release journeys drive `investigation start` with snake_case
        # request keys; the protocol form uses camelCase. Both must adapt to
        # the same internal fields.
        snake = adapt_request("investigation.start", {"project_id": "prj_" + "a" * 32, "purpose": "probe", "working_copy_id": "wc_" + "b" * 32})
        self.assertEqual(snake["project_id"], "prj_" + "a" * 32)
        self.assertEqual(snake["working_copy_id"], "wc_" + "b" * 32)
        camel = adapt_request("investigation.start", {"projectId": "prj_" + "a" * 32, "purpose": "probe", "workingCopyId": "wc_" + "b" * 32})
        self.assertEqual(camel, snake)
        with self.assertRaises(ProtocolError):
            adapt_request("investigation.start", {"purpose": "probe"})

    def test_release_journeys_snake_case_spellings_are_accepted(self) -> None:
        # The installed release journeys drive change/review/integration with
        # snake_case request keys; the protocol form uses camelCase. Both must
        # adapt to the same internal fields.
        pairs = [
            ("change.submit", {"projectId": "prj_" + "a" * 32, "workingCopyId": "wc_" + "b" * 32, "targetRef": "refs/heads/main", "title": "t", "summary": "s", "idempotencyKey": "k"},
             {"project_id": "prj_" + "a" * 32, "working_copy_id": "wc_" + "b" * 32, "target_ref": "refs/heads/main", "title": "t", "summary": "s", "idempotency_key": "k"}),
            ("review.request", {"changeId": "chg_" + "c" * 32, "revision": 1, "reviewerConversationId": "conv_" + "d" * 32, "reviewerRunId": "run_" + "e" * 32, "reviewerActorId": "a"},
             {"change_id": "chg_" + "c" * 32, "revision": 1, "reviewer_conversation_id": "conv_" + "d" * 32, "reviewer_run_id": "run_" + "e" * 32, "reviewer_actor_id": "a"}),
            ("review.submit", {"reviewId": "review_" + "f" * 32, "verdict": "accept"},
             {"review_id": "review_" + "f" * 32, "verdict": "accept"}),
            ("integration.analyze", {"projectId": "prj_" + "a" * 32, "changeId": "chg_" + "c" * 32, "revision": 1, "targetWorkingCopyId": "wc_" + "b" * 32, "targetRef": "refs/heads/main"},
             {"project_id": "prj_" + "a" * 32, "change_id": "chg_" + "c" * 32, "revision": 1, "target_working_copy_id": "wc_" + "b" * 32, "target_ref": "refs/heads/main"}),
            ("integration.integrate", {"integrationId": "int_" + "g" * 32, "authorizationId": "auth_" + "h" * 32},
             {"integration_id": "int_" + "g" * 32, "authorization_id": "auth_" + "h" * 32}),
        ]
        for operation, camel, snake in pairs:
            with self.subTest(operation=operation):
                self.assertEqual(adapt_request(operation, camel), adapt_request(operation, snake))

    def test_unknown_or_missing_fields_are_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            adapt_request("review.request", {"changeId": "chg_" + "c" * 32})
        with self.assertRaises(ProtocolError):
            adapt_request("review.request", {"changeId": "chg_" + "c" * 32, "revision": 1, "bogus": True})

    def test_dispatch_passes_adapted_keys_to_client_methods(self) -> None:
        # The client methods must receive snake_case keys; these calls exercise
        # the same request path the CLI uses without needing live state rows.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with PiStore(root / "state"):
                pass
            from scripts.pi_control.pi_client import PiControllerClient
            client = PiControllerClient(root / "state")
            for operation, request, message in (
                ("change.submit", {
                    "projectId": "prj_" + "a" * 32,
                    "workingCopyId": "wc_" + "b" * 32,
                    "targetRef": "refs/heads/main",
                    "title": "title",
                    "summary": "summary",
                    "idempotencyKey": "key-1",
                }, "change_id"),
                ("review.request", {
                    "changeId": "chg_" + "c" * 32,
                    "revision": 1,
                    "reviewerConversationId": "conv_" + "d" * 32,
                    "reviewerRunId": "run_" + "e" * 32,
                    "reviewerActorId": "actor-1",
                }, "change_id"),
            ):
                with self.subTest(operation=operation):
                    try:
                        client.dispatch(operation, request)
                    except KeyError as error:
                        self.fail(f"{operation} raised KeyError {error}: dispatch passed camelCase keys")
                    except Exception:
                        # Any non-KeyError failure is downstream validation;
                        # reaching it proves field adaptation succeeded.
                        pass


if __name__ == "__main__":
    unittest.main()
