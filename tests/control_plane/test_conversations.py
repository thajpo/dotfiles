"""Phase 4 conversation binding and session observation tests."""

from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from scripts.pi_control.session_adapter import SessionObservationError, observe_session, register_conversation, session_cwd_disagrees
from scripts.pi_control.store import ControllerStore
from tests.control_plane.test_operations import add_project


class ConversationTests(unittest.TestCase):
    def _session(self, root: Path, name: str, *, cwd: str = "/wrong/cwd") -> Path:
        path = root / name
        path.write_text(json.dumps({"type": "session", "id": "pi-" + name, "cwd": cwd}) + "\n{" + "\"type\":\"message\"}\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_session_binding_preserves_identity_when_header_cwd_disagrees(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = self._session(root, "personal.jsonl")
            with ControllerStore(root / "state") as store:
                project_id, working_copy_id, _ = add_project(store)
                conversation = register_conversation(store, session, role="personal", display_name="Personal", project_id=project_id, working_copy_id=working_copy_id)
                self.assertEqual(conversation["session_file"], str(session.resolve()))
                observation = observe_session(session)
                self.assertTrue(session_cwd_disagrees(observation, "/selected/controller/path"))
                self.assertEqual(store.conn.execute("SELECT working_copy_id FROM conversations WHERE conversation_id=?", (conversation["conversation_id"],)).fetchone()[0], working_copy_id)

    def test_secretary_and_workstream_roles_are_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secretary_session = self._session(root, "secretary.jsonl")
            workstream_session = self._session(root, "workstream.jsonl")
            with ControllerStore(root / "state") as store:
                project_id, working_copy_id, _ = add_project(store)
                secretary = register_conversation(store, secretary_session, role="secretary", display_name="Secretary", project_id=project_id)
                workstream = register_conversation(store, workstream_session, role="workstream", display_name="Workstream", project_id=project_id, working_copy_id=working_copy_id)
                self.assertIsNone(secretary["working_copy_id"])
                self.assertEqual(workstream["working_copy_id"], working_copy_id)

    def test_invalid_role_bindings_are_rejected_before_insert(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with ControllerStore(root / "state") as store:
                project_id, working_copy_id, _ = add_project(store)
                cases = [
                    ("secretary", None, working_copy_id),
                    ("secretary", None, None),
                    ("workstream", project_id, None),
                    ("integration", project_id, None),
                    ("review", project_id, working_copy_id),
                    ("host", project_id, None),
                    ("personal", "missing", working_copy_id),
                ]
                for index, (role, project, working) in enumerate(cases):
                    session = self._session(root, f"invalid-{index}.jsonl")
                    with self.assertRaises(SessionObservationError):
                        register_conversation(store, session, role=role, display_name=role, project_id=project, working_copy_id=working)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM conversations").fetchone()[0], 1)

    def test_duplicate_session_and_malformed_header_fail_without_rebinding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = self._session(root, "duplicate.jsonl")
            bad = root / "bad.jsonl"
            bad.write_text("not-json\n", encoding="utf-8")
            with ControllerStore(root / "state") as store:
                project_id, working_copy_id, _ = add_project(store)
                register_conversation(store, session, role="personal", display_name="One", project_id=project_id, working_copy_id=working_copy_id)
                with self.assertRaises(Exception):
                    register_conversation(store, session, role="personal", display_name="Two", project_id=project_id, working_copy_id=working_copy_id)
                with self.assertRaises(SessionObservationError):
                    observe_session(bad)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM conversations").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
