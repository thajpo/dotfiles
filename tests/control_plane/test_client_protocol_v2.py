"""C1c protocol-v2 envelope and semantic operation shapes."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.client import ClientProtocolError, ControllerClient, protocol_request
from scripts.pi_control.errors import UnsafeDatabaseError
from scripts.pi_control.store import ControllerStore


class ClientProtocolV2Tests(unittest.TestCase):
    def test_negotiation_and_exact_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            with ControllerStore(root):
                pass
            client = ControllerClient(root, read_only=True)
            negotiated = client.negotiate()
            self.assertEqual(negotiated["protocolVersion"], 2)
            self.assertEqual(negotiated["supportedProtocolVersions"], [2])
            response = protocol_request(client, {"protocolVersion": 2, "operation": "negotiate", "request": {}})
            self.assertEqual(response["protocolVersion"], 2)
            self.assertEqual(response["operation"], "negotiate")
            with self.assertRaises(ClientProtocolError):
                protocol_request(client, {"protocolVersion": 2, "operation": "negotiate", "request": {}, "extra": True})
            with self.assertRaises(ClientProtocolError):
                protocol_request(client, {"protocolVersion": 1, "operation": "negotiate", "request": {}})
            with self.assertRaises(ClientProtocolError):
                protocol_request(client, {"protocolVersion": 2, "operation": "negotiate", "request": "not-an-object"})

    def test_planned_operations_are_fake_effects_and_host_only_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            with ControllerStore(root):
                pass
            client = ControllerClient(root, read_only=True)
            result = protocol_request(client, {"protocolVersion": 2, "operation": "conversation.ensure", "request": {"projectId": "prj_" + "1" * 32, "conversationId": "conv_" + "2" * 32}})
            self.assertTrue(result["value"]["planned"])
            self.assertEqual(result["value"]["effects"], [])
            with self.assertRaises(ClientProtocolError):
                protocol_request(client, {"protocolVersion": 2, "operation": "activation.apply", "request": {"projectId": "prj_" + "1" * 32}})
            with self.assertRaises(ClientProtocolError):
                protocol_request(client, {"protocolVersion": 2, "operation": "conversation.ensure", "request": {"sql": "DROP TABLE projects"}})

    def test_oversized_and_read_only_mutations_fail_before_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            with ControllerStore(root):
                pass
            client = ControllerClient(root, read_only=True)
            with self.assertRaises(ClientProtocolError):
                protocol_request(client, b"{" + b"a" * (64 * 1024) + b"}")
            request = {
                "projectId": "prj_" + "1" * 32,
                "workingCopyId": "wc_" + "2" * 32,
                "targetRef": "refs/heads/main",
                "title": "x",
                "summary": "x",
                "captureMode": "all",
                "selectedPaths": [],
                "excludedPaths": [],
                "expectedStatusHash": "hash",
                "idempotencyKey": "key",
                "conversationId": "conv_" + "3" * 32,
                "actorType": "controller",
                "actorId": None,
                "authorizationId": None,
            }
            with self.assertRaises(UnsafeDatabaseError):
                protocol_request(client, {"protocolVersion": 2, "operation": "submit", "request": request})


if __name__ == "__main__":
    unittest.main()
