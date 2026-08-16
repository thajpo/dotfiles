"""Headful workstream approval loop: propose -> approve -> apply."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.pi_client import PiControllerClient
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.launch import prepare_run
from scripts.pi_control.pi_workstreams import (
    WorkstreamProposalError,
    apply_workstream_proposal, approve_workstream_proposal, propose_workstream,
)
from tests.pi_test_build import allow_test_only_registered_build_rows
from tests.test_pi_core import _BUILD_ID, _host, _register_build, _repo


class WorkstreamAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = allow_test_only_registered_build_rows()
        patcher.start()
        self.addCleanup(patcher.stop)

    def _proposal(self, root: Path, name: str = "approve"):
        client = PiControllerClient(root / "state")
        project = client.register_project(str(_repo(root, name)), name)
        with PiStore(root / "state") as store:
            if store.conn.execute("SELECT 1 FROM installed_builds WHERE build_id=?", (_BUILD_ID,)).fetchone() is None:
                _register_build(store, root)
            secretary = store.conn.execute("SELECT * FROM conversations WHERE project_id=? AND role='secretary'", (project["project_id"],)).fetchone()
            secretary_run = prepare_run(store, conversation_id=secretary["conversation_id"], build_id=_BUILD_ID, host_process=_host("secretary"))
            proposal = propose_workstream(
                store, project_id=project["project_id"],
                secretary_conversation_id=secretary["conversation_id"],
                secretary_run_id=secretary_run.run["run_id"],
                title="feature work", purpose="implement the feature", idempotency_key=f"prop-{name}",
            )
            secretary_run.close()
            return client, project, proposal

    def test_propose_records_intent_without_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client, project, proposal = self._proposal(root)
            with PiStore(root / "state") as store:
                message = store.conn.execute("SELECT * FROM project_messages WHERE message_id=?", (proposal["message"]["message_id"],)).fetchone()
                self.assertEqual(message["kind"], "needs-user")
                self.assertIn("workstream", message["payload_json"])
                operation = store.conn.execute("SELECT state FROM operations WHERE operation_id=?", (proposal["operation"]["operation_id"],)).fetchone()
                self.assertEqual(operation["state"], "planned")
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0], 0)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM worktrees_placeholder").fetchone()[0] if False else store.conn.execute("SELECT COUNT(*) FROM working_copies WHERE kind='worktree'").fetchone()[0], 0)

    def test_approve_binds_exact_scope_and_replays(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client, project, proposal = self._proposal(root)
            with PiStore(root / "state") as store:
                first = approve_workstream_proposal(store, message_id=proposal["message"]["message_id"], actor_id="test-user")
                second = approve_workstream_proposal(store, message_id=proposal["message"]["message_id"], actor_id="test-user")
                self.assertEqual(first["authorizationId"], second["authorizationId"])
                authorization = store.conn.execute("SELECT * FROM authorizations WHERE authorization_id=?", (first["authorizationId"],)).fetchone()
                self.assertEqual(authorization["kind"], "create-workstream")
                self.assertEqual(authorization["state"], "active")
                self.assertEqual(authorization["request_context_id"], proposal["message"]["message_id"])
                self.assertEqual(authorization["resource_id"], proposal["scope"]["workstreamId"])
                self.assertEqual(authorization["project_id"], project["project_id"])

    def test_apply_creates_exactly_the_approved_workstream(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client, project, proposal = self._proposal(root)
            with PiStore(root / "state") as store:
                authorized = approve_workstream_proposal(store, message_id=proposal["message"]["message_id"], actor_id="test-user")
                applied = apply_workstream_proposal(store, message_id=proposal["message"]["message_id"], authorization_id=authorized["authorizationId"], actor_id="test-user")
                workstream = applied["workstream"]
                self.assertEqual(workstream["desired_state"], "active")
                self.assertEqual(workstream["project_id"], project["project_id"])
                self.assertTrue(Path(workstream["worktree_path"]).is_dir())
                message = store.conn.execute("SELECT state FROM project_messages WHERE message_id=?", (proposal["message"]["message_id"],)).fetchone()
                self.assertEqual(message["state"], "resolved")
                authorization = store.conn.execute("SELECT state FROM authorizations WHERE authorization_id=?", (authorized["authorizationId"],)).fetchone()
                self.assertEqual(authorization["state"], "consumed")
                assignment = store.conn.execute("SELECT desired_state FROM presentation_assignments WHERE conversation_id=?", (workstream["conversation_id"],)).fetchone()
                self.assertEqual(assignment["desired_state"], "present")
                replay = apply_workstream_proposal(store, message_id=proposal["message"]["message_id"], authorization_id=authorized["authorizationId"], actor_id="test-user")
                self.assertEqual(replay["workstream"]["workstream_id"], workstream["workstream_id"])

    def test_apply_without_approval_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client, project, proposal = self._proposal(root)
            with PiStore(root / "state") as store:
                with self.assertRaises(WorkstreamProposalError):
                    apply_workstream_proposal(store, message_id=proposal["message"]["message_id"], authorization_id="auth_" + "1" * 32, actor_id="test-user")
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0], 0)

    def test_apply_refuses_moved_head(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client, project, proposal = self._proposal(root)
            repository = Path(project["primary_checkout"])
            env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@i", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@i"}
            import subprocess
            subprocess.run(["git", "-C", str(repository), "commit", "-qm", "move", "--allow-empty"], env=env, check=True)
            with PiStore(root / "state") as store:
                authorized = approve_workstream_proposal(store, message_id=proposal["message"]["message_id"], actor_id="test-user")
                with self.assertRaises(WorkstreamProposalError):
                    apply_workstream_proposal(store, message_id=proposal["message"]["message_id"], authorization_id=authorized["authorizationId"], actor_id="test-user")
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0], 0)

    def test_authorization_scope_mismatch_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client, project, proposal = self._proposal(root)
            other, _other_project, other_proposal = self._proposal(root, name="other")
            with PiStore(root / "state") as store:
                other_authorized = approve_workstream_proposal(store, message_id=other_proposal["message"]["message_id"], actor_id="test-user")
                with self.assertRaises(WorkstreamProposalError):
                    apply_workstream_proposal(store, message_id=proposal["message"]["message_id"], authorization_id=other_authorized["authorizationId"], actor_id="test-user")
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
