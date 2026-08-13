"""Secretary semantic operations over the authenticated controller channel."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.controller_channel import ControllerChannelError, PROTOCOL_VERSION
from scripts.pi_control.pi_client import PiControllerClient
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.host_supervisor import _ROLE_OPERATIONS, _rpc
from scripts.pi_control.launch import prepare_run
from scripts.pi_control.models import new_id
from tests.pi_test_build import allow_test_only_registered_build_rows
from tests.test_pi_core import _BUILD_ID, _host, _register_build, _repo


class SecretaryWorkTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = allow_test_only_registered_build_rows()
        patcher.start()
        self.addCleanup(patcher.stop)

    def _secretary_run(self, client: PiControllerClient, store: PiStore, project_id: str) -> dict:
        conversation = store.conn.execute("SELECT * FROM conversations WHERE project_id=? AND role='secretary'", (project_id,)).fetchone()
        return prepare_run(store, conversation_id=conversation["conversation_id"], build_id=_BUILD_ID, host_process=_host("secretary"))

    def test_secretary_role_grants_work_index(self) -> None:
        self.assertIn("project.work-index", _ROLE_OPERATIONS["secretary"])
        self.assertIn("project.work-index", _ROLE_OPERATIONS["personal"])
        self.assertNotIn("change.submit", _ROLE_OPERATIONS["secretary"])
        self.assertNotIn("review.request", _ROLE_OPERATIONS["personal"])

    def test_work_index_over_authenticated_channel(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = client.register_project(str(_repo(root, "work")), "work")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                prepared = self._secretary_run(client, store, project["project_id"])
                client.attest_run(run_id=prepared.run["run_id"], manifest_digest=prepared.manifest["manifestDigest"])
                request = {"protocolVersion": PROTOCOL_VERSION, "type": "request", "requestId": "request-1", "operation": "project.work-index", "payload": {}}
                value = _rpc(store, prepared.run["run_id"], prepared.manifest["manifestDigest"], request)
                self.assertIn("Working now", value)
                self.assertIn("Needs attention", value)
                prepared.close()

    def test_work_index_rejects_payload_and_forged_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw, PiStore(Path(raw) / "state") as store:
            request = {"protocolVersion": PROTOCOL_VERSION, "type": "request", "requestId": "request-1", "operation": "project.work-index", "payload": {"projectId": "forged"}}
            with self.assertRaisesRegex(ControllerChannelError, "override"):
                _rpc(store, "run_" + "1" * 32, "sha256:" + "2" * 64, request)

    def test_secretary_operations_include_no_authority_surface(self) -> None:
        for forbidden in ("command.authorize", "command.execute", "integration.authorize", "integration.integrate", "workstream.create", "host_command", "shell"):
            self.assertNotIn(forbidden, _ROLE_OPERATIONS["secretary"])

    def test_investigation_start_maps_to_snapshot_investigator(self) -> None:
        # The secretary's investigation tool rides the existing snapshot
        # subagent path; verify the role grant and that a forged direct
        # workstream creation is not reachable from the secretary channel.
        self.assertIn("subagent.spawn", _ROLE_OPERATIONS["secretary"])
        self.assertNotIn("workstream.create", _ROLE_OPERATIONS["secretary"])


if __name__ == "__main__":
    unittest.main()
