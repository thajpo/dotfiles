"""C1b workstream and presentation state APIs."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.pi_control.errors import ResourceStaleError, WorkstreamConflictError
from scripts.pi_control.models import new_id
from scripts.pi_control.store import ControllerStore
from scripts.pi_control.workstreams import (
    create_presentation_assignment,
    create_workstream,
    update_presentation_assignment,
    update_workstream,
)


def _project(store: ControllerStore, project_id: str) -> None:
    store.conn.execute(
        "INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (project_id, project_id, f"/{project_id}/git", 1, 1, f"/{project_id}", "sha1", "trusted", "policy", "active", "unknown", 1, "t", "t"),
    )


def _resources(store: ControllerStore, project_id: str) -> tuple[str, str, str]:
    wc, conv, ws = new_id("wc"), new_id("conv"), new_id("ws")
    store.conn.execute("INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,branch_ref,effective_mode,desired_state,observed_state,resource_version,controller_owned,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (wc, project_id, "ws", "worktree", "workstream", f"/{project_id}/ws", "refs/heads/ws", "trusted-live", "present", "unknown", 1, 1, "t", "t"))
    store.conn.execute("INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (conv, project_id, wc, "workstream", "ws", f"pi-{ws}", f"/{project_id}/ws.jsonl", "active", "unknown", 1, "t", "t"))
    return ws, wc, conv


class WorkstreamTests(unittest.TestCase):
    def test_create_exact_replay_and_cas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            _project(store, "prj_" + "1" * 32)
            ws, wc, conv = _resources(store, "prj_" + "1" * 32)
            request = dict(project_id="prj_" + "1" * 32, working_copy_id=wc, conversation_id=conv, title="work", brief={"goal": "x"}, target_ref="refs/heads/main", starting_oid="a" * 40, workstream_id=ws, idempotency_key="ws-create")
            first = create_workstream(store, **request)
            second = create_workstream(store, **request)
            self.assertEqual(first, second)
            self.assertEqual(store.conn.execute("SELECT count(*) FROM control_events WHERE resource_id=?", (ws,)).fetchone()[0], 1)
            updated = update_workstream(store, ws, expected_resource_version=1, updates={"observedState": "ready"}, idempotency_key="ws-update")
            self.assertEqual(updated["resource_version"], 2)
            with self.assertRaises(ResourceStaleError):
                update_workstream(store, ws, expected_resource_version=1, updates={"observedState": "stopped"})

    def test_cross_project_link_and_event_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            p1, p2 = "prj_" + "1" * 32, "prj_" + "2" * 32
            _project(store, p1)
            _project(store, p2)
            ws, wc, conv = _resources(store, p1)
            with self.assertRaises(WorkstreamConflictError):
                create_workstream(store, project_id=p2, working_copy_id=wc, conversation_id=conv, title="bad", brief={}, target_ref="refs/heads/main", starting_oid="a" * 40, workstream_id=ws)
            class Failpoint:
                def hit(self, name, detail):
                    if name == "completion.event.after":
                        raise RuntimeError("event failure")
            with self.assertRaises(RuntimeError):
                create_workstream(store, project_id=p1, working_copy_id=wc, conversation_id=conv, title="rollback", brief={}, target_ref="refs/heads/main", starting_oid="a" * 40, workstream_id=ws, failpoint=Failpoint())
            self.assertIsNone(store.conn.execute("SELECT 1 FROM workstreams WHERE workstream_id=?", (ws,)).fetchone())
            self.assertEqual(store.conn.execute("SELECT count(*) FROM control_events WHERE resource_id=?", (ws,)).fetchone()[0], 0)

    def test_presentation_assignment_uses_cas_and_bounded_locator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            _project(store, "prj_" + "1" * 32)
            _, _, conv = _resources(store, "prj_" + "1" * 32)
            assignment = create_presentation_assignment(store, conversation_id=conv, backend="tmux", locator={"session": "managed"})
            changed = update_presentation_assignment(store, assignment["presentation_assignment_id"], expected_resource_version=1, updates={"observedState": "present", "locator": {"session": "managed", "window": "0"}})
            self.assertEqual(changed["resource_version"], 2)
            with self.assertRaises(ResourceStaleError):
                update_presentation_assignment(store, assignment["presentation_assignment_id"], expected_resource_version=1, updates={"observedState": "missing"})
            with self.assertRaises(sqlite3.IntegrityError):
                create_presentation_assignment(store, conversation_id=conv, backend="herdr")


if __name__ == "__main__":
    unittest.main()
