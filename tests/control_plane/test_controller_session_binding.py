from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from scripts.pi_control.models import new_id
from scripts.pi_control.session_adapter import SessionObservationError, bind_controller_session
from scripts.pi_control.store import ControllerStore
from scripts.pi_control.workstreams import ensure_project_activation, transition_activation


class ControllerSessionBindingTests(unittest.TestCase):
    def test_exact_session_binding_ignores_header_cwd_as_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            session = root / "session.jsonl"
            session.write_text(json.dumps({"id": "pi-session", "cwd": "/wrong"}) + "\n")
            with ControllerStore(state) as store:
                project = "prj_" + "1" * 32
                store.conn.execute("INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (project, "p", "/p/.git", 1, 1, "/p", "sha1", "trusted", "p", "active", "unknown", 1, "t", "t"))
                conversation = new_id("conv")
                store.conn.execute("INSERT INTO conversations(conversation_id,project_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (conversation, project, "personal", "p", "pi-session", str(session), "active", "unknown", 1, "t", "t"))
                store.register_build("build", source_tree_hash="t", artifact_manifest_hash="a", pi_version="p", package_lock_hash="l", status="staged")
                op = store.create_operation(idempotency_key="mig", kind="migration", resource_type="migration", resource_id="migration-resource", actor_type="controller", request={})
                migration = new_id("mig")
                store.conn.execute("INSERT INTO migration_runs(migration_id,operation_id,idempotency_key,mode,controller_build_id,request_digest,source_manifest_digest,state,step,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (migration, op.operation_id, "mig", "shadow-import", "build", "r", "s", "succeeded", "done", 1, "t", "t"))
                activation = ensure_project_activation(store, project_id=project)
                shadow = transition_activation(store, project_id=project, mode="shadow", expected_resource_version=activation["resource_version"], controller_build_id="build", migration_id=migration)
                store.conn.execute("UPDATE installed_builds SET status='active' WHERE build_id='build'")
                transition_activation(store, project_id=project, mode="controller", expected_resource_version=shadow["resource_version"], controller_build_id="build", migration_id=migration)
                projection = bind_controller_session(store, conversation_id=conversation, session_file=session, project_id=project)
                self.assertEqual(projection["headerCwd"], "/wrong")
                self.assertEqual(projection["conversationId"], conversation)

    def test_duplicate_history_is_not_selected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / "session.jsonl"
            session.write_text('{"id":"pi-session"}\n')
            with ControllerStore(root / "state") as store:
                project = "prj_" + "1" * 32
                store.conn.execute("INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (project, "p", "/p/.git", 1, 1, "/p", "sha1", "trusted", "p", "active", "unknown", 1, "t", "t"))
                store.conn.execute("INSERT INTO conversations(conversation_id,project_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (new_id("conv"), project, "personal", "p", "pi-session", str(root / "session-1.jsonl"), "active", "unknown", 1, "t", "t"))
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute("INSERT INTO conversations(conversation_id,project_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (new_id("conv"), project, "personal", "p", "pi-session", str(root / "session-2.jsonl"), "active", "unknown", 1, "t", "t"))


if __name__ == "__main__": unittest.main()
