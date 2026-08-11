from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.pi_control.client import ClientProtocolError, ControllerClient, protocol_request
from scripts.pi_control.errors import NotFoundError
from scripts.pi_control.store import ControllerStore


class PersonalClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "Client Test")
        self._git("config", "user.email", "client@example.invalid")
        (self.repo / "file.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "file.txt")
        self._git("commit", "-qm", "base")
        self.project_id = "prj_" + "1" * 32
        self.wc_id = "wc_" + "2" * 32
        self.conv_id = "conv_" + "3" * 32
        with ControllerStore(self.root / "state") as store:
            store.register_build("build", source_tree_hash="tree", artifact_manifest_hash="manifest", pi_version="pi", package_lock_hash="lock", status="active")
            store.conn.execute("INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (self.project_id, "p", str(self.repo / ".git"), 1, 1, str(self.repo), "sha1", "trusted", "policy", "active", "ready", 1, "t", "t"))
            store.conn.execute("INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,effective_mode,desired_state,observed_state,writer_epoch,resource_version,controller_owned,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (self.wc_id, self.project_id, "primary", "primary", "personal", str(self.repo), "trusted-live", "present", "ready", 0, 1, 0, "t", "t"))
            store.conn.execute("INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (self.conv_id, self.project_id, self.wc_id, "personal", "personal", "personal-session", str(self.root / "session.jsonl"), "active", "ready", 1, "t", "t"))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str) -> None:
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_AUTHOR_NAME": "Client Test", "GIT_AUTHOR_EMAIL": "client@example.invalid", "GIT_COMMITTER_NAME": "Client Test", "GIT_COMMITTER_EMAIL": "client@example.invalid"}
        subprocess.run(["git", *args], cwd=self.repo, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def _submit_request(self, key: str = "client") -> dict[str, object]:
        return {
            "projectId": self.project_id, "workingCopyId": self.wc_id, "targetRef": "refs/heads/main",
            "title": "Client change", "summary": "submitted by personal client", "captureMode": "clean",
            "selectedPaths": [], "excludedPaths": [], "expectedStatusHash": None, "idempotencyKey": key,
            "conversationId": self.conv_id, "actorType": "personal", "actorId": "personal", "authorizationId": None,
        }

    def test_personal_explicit_primary_selection_and_exact_focus(self) -> None:
        client = ControllerClient(self.root / "state", read_only=True)
        selected = client.select_personal(self.project_id, self.conv_id, self.wc_id, "primary")
        self.assertEqual(selected["workingCopyId"], self.wc_id)
        focused = client.focus(self.project_id, self.conv_id, expected_resource_version=1)
        self.assertTrue(focused["presentationOnly"])
        with self.assertRaises(ClientProtocolError):
            client.focus(self.project_id, "primary")

    def test_status_refresh_and_submit_share_controller_queue(self) -> None:
        client = ControllerClient(self.root / "state")
        status = client.status(self.project_id, refresh=True)
        self.assertEqual(status["project"]["project_id"], self.project_id)
        result = client.submit(self._submit_request())
        self.assertEqual(result["revision"], 1)
        read_status = ControllerClient(self.root / "state", read_only=True).status(self.project_id, refresh=False)
        self.assertEqual(read_status["changes"][0]["change_id"], result["changeId"])

    def test_recovery_and_technical_details_are_read_only_and_bounded(self) -> None:
        client = ControllerClient(self.root / "state", read_only=True)
        recovery = client.recovery_status(self.project_id)
        self.assertEqual(recovery["schemaVersion"], 1)
        details = client.technical_details(self.project_id, "project", self.project_id)
        self.assertTrue(details["readOnly"])
        self.assertNotIn("capability_hash", str(details))

    def test_technical_details_rejects_cross_project_resources(self) -> None:
        other_project = "prj_" + "9" * 32
        with ControllerStore(self.root / "state") as store:
            store.conn.execute("INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (other_project, "other", str(self.root / "other.git"), 2, 2, str(self.root / "other"), "sha1", "isolated", "policy", "active", "ready", 1, "t", "t"))
        client = ControllerClient(self.root / "state", read_only=True)
        with self.assertRaises(NotFoundError):
            client.technical_details(self.project_id, "project", other_project)
        own = client.technical_details(other_project, "project", other_project)
        self.assertEqual(own["projectId"], other_project)
        with ControllerStore(self.root / "state") as store:
            operation = store.create_operation(idempotency_key="details-operation", kind="observe", resource_type="project", resource_id=self.project_id, actor_type="controller", request={})
        operation_details = client.technical_details(self.project_id, "operation", operation.operation_id)
        self.assertEqual(operation_details["projectId"], self.project_id)
        with self.assertRaises(NotFoundError):
            client.technical_details(other_project, "operation", operation.operation_id)

    def test_cli_process_reaches_status_and_change_semantics(self) -> None:
        state = self.root / "greenfield-state"
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        cli = Path(__file__).resolve().parents[2] / "bin" / "pi-control"
        request_path = self.root / "greenfield-request.json"
        request_path.write_text(json.dumps({"protocolVersion": 2, "operation": "negotiate", "request": {}}), encoding="utf-8")
        from scripts.pi_control.greenfield_store import GreenfieldStore
        with GreenfieldStore(state):
            pass
        status = subprocess.run([str(cli), "--state-root", str(state), "--json", "protocol", "--request-json", str(request_path)], env=environment, check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(status.stdout)["result"]["protocolVersion"], 2)

    def test_protocol_negotiation_and_unknown_fields_fail_closed(self) -> None:
        client = ControllerClient(self.root / "state", read_only=True)
        negotiated = client.negotiate()
        self.assertEqual(negotiated["protocolVersion"], 2)
        response = protocol_request(client, {"protocolVersion": 2, "operation": "status", "request": {"projectId": self.project_id, "refresh": False}})
        self.assertEqual(response["operation"], "status")
        with self.assertRaises(ClientProtocolError):
            protocol_request(client, {"protocolVersion": 2, "operation": "status", "request": {"projectId": self.project_id, "refresh": False, "fuzzy": "primary"}})
        with self.assertRaises(ClientProtocolError):
            protocol_request(client, {"protocolVersion": 99, "operation": "negotiate", "request": {}})


if __name__ == "__main__":
    unittest.main()
