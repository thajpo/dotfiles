from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import scripts.pi_control.client as client_module
from scripts.pi_control.client import ClientProtocolError, ControllerClient
from scripts.pi_control.errors import ConstraintError
from scripts.pi_control.store import ControllerStore


class SecretaryClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "Secretary Test")
        self._git("config", "user.email", "secretary@example.invalid")
        (self.repo / "file.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "file.txt")
        self._git("commit", "-qm", "base")
        self.project_id = "prj_" + "1" * 32
        self.primary_wc = "wc_" + "2" * 32
        self.workstream_wc = "wc_" + "3" * 32
        self.personal_conv = "conv_" + "4" * 32
        with ControllerStore(self.root / "state") as store:
            store.register_build("build", source_tree_hash="tree", artifact_manifest_hash="manifest", pi_version="pi", package_lock_hash="lock", status="active")
            store.conn.execute("INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (self.project_id, "p", str(self.repo / ".git"), 1, 1, str(self.repo), "sha1", "trusted", "policy", "active", "ready", 1, "t", "t"))
            store.conn.execute("INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,effective_mode,desired_state,observed_state,writer_epoch,resource_version,controller_owned,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (self.primary_wc, self.project_id, "primary", "primary", "personal", str(self.repo), "trusted-live", "present", "ready", 0, 1, 0, "t", "t"))
            store.conn.execute("INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,effective_mode,desired_state,observed_state,writer_epoch,resource_version,controller_owned,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (self.workstream_wc, self.project_id, "workstream", "worktree", "workstream", str(self.root / "workstream"), "isolated", "present", "ready", 0, 1, 1, "t", "t"))
            store.conn.execute("INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (self.personal_conv, self.project_id, self.primary_wc, "personal", "personal", "personal-session", str(self.root / "personal.jsonl"), "active", "ready", 1, "t", "t"))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str) -> None:
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_AUTHOR_NAME": "Secretary Test", "GIT_AUTHOR_EMAIL": "secretary@example.invalid", "GIT_COMMITTER_NAME": "Secretary Test", "GIT_COMMITTER_EMAIL": "secretary@example.invalid"}
        subprocess.run(["git", *args], cwd=self.repo, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_workstream_creation_requires_exact_semantic_approval(self) -> None:
        client = ControllerClient(self.root / "state")
        request = {
            "projectId": self.project_id, "workingCopyId": self.workstream_wc, "displayName": "analysis",
            "piSessionId": "secretary-analysis", "sessionFile": str(self.root / "analysis.jsonl"),
            "approval": {"action": "create-workstream", "projectId": self.project_id, "workingCopyId": self.workstream_wc, "approved": True},
        }
        conversation = client.create_workstream(request)
        self.assertEqual(conversation["role"], "workstream")
        self.assertEqual(conversation["working_copy_id"], self.workstream_wc)
        with self.assertRaises(ClientProtocolError):
            client.create_workstream({**request, "approval": {"yes": True}})

    def test_workstream_event_failure_rolls_back_conversation(self) -> None:
        client = ControllerClient(self.root / "state")
        request = {
            "projectId": self.project_id, "workingCopyId": self.workstream_wc, "displayName": "atomic",
            "piSessionId": "atomic-session", "sessionFile": str(self.root / "atomic.jsonl"),
            "approval": {"action": "create-workstream", "projectId": self.project_id, "workingCopyId": self.workstream_wc, "approved": True},
        }
        with ControllerStore(self.root / "state") as store:
            before = store.conn.execute("SELECT count(*) FROM conversations").fetchone()[0]
            events_before = store.conn.execute("SELECT count(*) FROM control_events").fetchone()[0]
        with mock.patch.object(client_module, "append_event_in_transaction", side_effect=RuntimeError("event injection")):
            with self.assertRaisesRegex(RuntimeError, "event injection"):
                client.create_workstream(request)
        with ControllerStore(self.root / "state") as store:
            self.assertEqual(store.conn.execute("SELECT count(*) FROM conversations").fetchone()[0], before)
            self.assertEqual(store.conn.execute("SELECT count(*) FROM control_events").fetchone()[0], events_before)

    def test_secretary_status_is_controller_bound_and_no_env_fallback(self) -> None:
        os.environ["PI_CONTROL_ACTIVATION"] = "legacy"
        try:
            client = ControllerClient(self.root / "state", read_only=True)
            status = client.status(self.project_id, refresh=False)
            self.assertEqual(status["project"]["project_id"], self.project_id)
            self.assertEqual(status["facts"]["source"], "controller+git-observation")
        finally:
            os.environ.pop("PI_CONTROL_ACTIVATION", None)

    def test_unmanaged_or_primary_working_copy_cannot_be_secretary_workstream(self) -> None:
        client = ControllerClient(self.root / "state")
        request = {
            "projectId": self.project_id, "workingCopyId": self.primary_wc, "displayName": "bad",
            "piSessionId": "secretary-bad", "sessionFile": str(self.root / "bad.jsonl"),
            "approval": {"action": "create-workstream", "projectId": self.project_id, "workingCopyId": self.primary_wc, "approved": True},
        }
        with self.assertRaises(ConstraintError):
            client.create_workstream(request)


if __name__ == "__main__":
    unittest.main()
