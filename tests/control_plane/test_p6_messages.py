"""P6 message transitions and controller-channel identity fencing."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.controller_channel import ControllerChannelError, PROTOCOL_VERSION
from scripts.pi_control.pi_client import PiControllerClient
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.host_supervisor import _ROLE_OPERATIONS, _rpc
from scripts.pi_control.launch import prepare_run
from scripts.pi_control.messages import ProjectMessageError, acknowledge_message, list_messages, post_message, reply_message
from scripts.pi_control.models import new_id
from tests.control_plane.test_p2_contract import tool_runtime
from tests.pi_test_build import allow_test_only_registered_build_rows
from tests.test_pi_core import _BUILD_ID, _host, _register_build, _repo


class P6MessageTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = allow_test_only_registered_build_rows()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_writer_post_list_ack_reply_and_epoch_fence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = client.register_project(str(_repo(root, "messages")), "messages")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                working = dict(store.conn.execute("SELECT * FROM working_copies WHERE project_id=?", (project["project_id"],)).fetchone())
                conversation = client.create_conversation(project_id=project["project_id"], role="personal", display_name="writer", working_copy_id=working["working_copy_id"])
                run_id = new_id("run")
                prepared = prepare_run(store, conversation_id=conversation["conversation_id"], build_id=_BUILD_ID, host_process=_host("personal"), tool_runtime=tool_runtime(run_id, project, working), run_id=run_id)
                epoch = prepared.run["writer_epoch"]
                first = post_message(store, project_id=project["project_id"], conversation_id=conversation["conversation_id"], run_id=run_id, writer_generation=epoch, kind="progress", payload={"step": 1}, idempotency_key="p6-message")
                self.assertEqual([item["message_id"] for item in list_messages(store, project_id=project["project_id"])], [first["message_id"]])
                acknowledged = acknowledge_message(store, project_id=project["project_id"], message_id=first["message_id"], conversation_id=conversation["conversation_id"], run_id=run_id, writer_generation=epoch)
                self.assertEqual(acknowledged["state"], "acknowledged")
                reply = reply_message(store, project_id=project["project_id"], target_message_id=first["message_id"], conversation_id=conversation["conversation_id"], run_id=run_id, writer_generation=epoch, payload={"answer": True}, idempotency_key="p6-reply")
                self.assertEqual(reply["reply_to_message_id"], first["message_id"])
                resolved = acknowledge_message(store, project_id=project["project_id"], message_id=first["message_id"], resolve=True, conversation_id=conversation["conversation_id"], run_id=run_id, writer_generation=epoch)
                self.assertEqual(resolved["state"], "resolved")
                with self.assertRaisesRegex(ProjectMessageError, "backward"):
                    acknowledge_message(store, project_id=project["project_id"], message_id=first["message_id"], conversation_id=conversation["conversation_id"], run_id=run_id, writer_generation=epoch)
                with store.transaction():
                    store.conn.execute("UPDATE working_copies SET writer_epoch=writer_epoch+1 WHERE working_copy_id=?", (working["working_copy_id"],))
                with self.assertRaisesRegex(ProjectMessageError, "epoch|claim"):
                    post_message(store, project_id=project["project_id"], conversation_id=conversation["conversation_id"], run_id=run_id, writer_generation=epoch, kind="progress", payload={}, idempotency_key="stale")
                prepared.close()

    def test_channel_rejects_identity_override_and_has_no_authority_operations(self) -> None:
        request = {"protocolVersion": PROTOCOL_VERSION, "type": "request", "requestId": "request-1", "operation": "message.post", "payload": {"projectId": "forged"}}
        with tempfile.TemporaryDirectory() as raw, PiStore(Path(raw) / "state") as store, self.assertRaisesRegex(ControllerChannelError, "override"):
            _rpc(store, "run_" + "1" * 32, "sha256:" + "2" * 64, request)
        all_operations = set().union(*_ROLE_OPERATIONS.values())
        for forbidden in ("command.authorize", "command.execute", "command.consume", "integration.authorize", "integration.integrate", "host_command", "shell"):
            self.assertNotIn(forbidden, all_operations)


if __name__ == "__main__":
    unittest.main()
