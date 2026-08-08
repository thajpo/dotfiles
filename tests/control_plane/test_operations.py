"""Phase 2 transactional operation and CAS tests."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.pi_control.errors import (
    ConstraintError,
    IdempotencyConflictError,
    LockBusyError,
    ResourceStaleError,
    WriterStaleError,
)
from scripts.pi_control.models import new_id
from scripts.pi_control.operations import complete_operation, mutate_with_event, update_operation
from scripts.pi_control.store import ControllerStore


def add_project(store: ControllerStore, *, project_id: str | None = None, working_copy_id: str | None = None, conversation_id: str | None = None) -> tuple[str, str, str]:
    project_id = project_id or new_id("prj")
    working_copy_id = working_copy_id or new_id("wc")
    conversation_id = conversation_id or new_id("conv")
    now = "2024-01-01T00:00:00Z"
    store.conn.execute(
        "INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (project_id, "p", "/g/" + project_id, 1, 1, "/p/" + project_id, "sha1", "trusted", "policy", "active", "unknown", 1, now, now),
    )
    store.conn.execute(
        "INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,effective_mode,desired_state,observed_state,writer_epoch,resource_version,controller_owned,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (working_copy_id, project_id, "primary", "primary", "personal", "/p/" + project_id, "trusted-live", "present", "unknown", 0, 1, 1, now, now),
    )
    store.conn.execute(
        "INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (conversation_id, project_id, working_copy_id, "personal", "personal", "pi-" + conversation_id, "/s/" + conversation_id, "active", "unknown", 1, now, now),
    )
    return project_id, working_copy_id, conversation_id


class OperationTests(unittest.TestCase):
    def test_idempotent_operation_replay_and_changed_request_conflict(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            first = store.create_operation(idempotency_key="same", kind="observe", resource_type="project", resource_id="p", actor_type="controller", request={"a": 1})
            replay = store.create_operation(idempotency_key="same", kind="observe", resource_type="project", resource_id="p", actor_type="controller", request={"a": 1})
            self.assertEqual(first.operation_id, replay.operation_id)
            with self.assertRaises(IdempotencyConflictError):
                store.create_operation(idempotency_key="same", kind="observe", resource_type="project", resource_id="p", actor_type="controller", request={"a": 2})
            with self.assertRaises(IdempotencyConflictError):
                store.create_operation(idempotency_key="same", kind="mutate", resource_type="project", resource_id="other", actor_type="controller", request={"a": 1})
            self.assertEqual(store.conn.execute("SELECT count(*) FROM operations").fetchone()[0], 1)

    def test_terminal_operation_outcome_is_immutable_and_identical_replay_is_allowed(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            operation = store.create_operation(idempotency_key="terminal", kind="observe", resource_type="project", resource_id="p", actor_type="controller", request={"a": 1})
            completed = complete_operation(store, operation.operation_id, result={"ok": True})
            replay = complete_operation(store, operation.operation_id, result={"ok": True})
            self.assertEqual(replay.result_json, completed.result_json)
            with self.assertRaises(ConstraintError):
                update_operation(store, operation.operation_id, state="applying", step="regressed", result={"ok": False})
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE operations SET state='applying',step='direct-regression' WHERE operation_id=?", (operation.operation_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("DELETE FROM operations WHERE operation_id=?", (operation.operation_id,))
            row = store.conn.execute("SELECT state,step,result_json FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone()
            self.assertEqual((row["state"], row["step"], row["result_json"]), ("succeeded", "completed", '{"ok":true}'))

    def test_resource_version_cas_is_atomic_and_rejects_stale(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            project_id, _, _ = add_project(store)
            result = store.cas_update("projects", "project_id", project_id, 1, {"display_name": "changed", "observed_state": "ready"})
            self.assertEqual((result.previous_version, result.resource_version), (1, 2))
            with self.assertRaises(ResourceStaleError):
                store.cas_update("projects", "project_id", project_id, 1, {"display_name": "stale"})
            self.assertEqual(store.conn.execute("SELECT resource_version FROM projects WHERE project_id=?", (project_id,)).fetchone()[0], 2)

    def test_state_and_event_share_transaction(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            project_id, _, _ = add_project(store)
            result = mutate_with_event(
                store,
                lambda connection: connection.execute("UPDATE projects SET observed_state='ready',resource_version=resource_version+1 WHERE project_id=?", (project_id,)).rowcount,
                event_kind="project.observed",
                resource_type="project",
                resource_id=project_id,
                resource_version=2,
                payload={"observed_state": "ready"},
            )
            self.assertEqual(result, 1)
            self.assertEqual(store.conn.execute("SELECT count(*) FROM control_events").fetchone()[0], 1)
            with self.assertRaises(Exception):
                mutate_with_event(
                    store,
                    lambda connection: (_ for _ in ()).throw(RuntimeError("fail before outbox")),
                    event_kind="never",
                    resource_type="project",
                    resource_id=project_id,
                    payload={},
                )
            self.assertEqual(store.conn.execute("SELECT count(*) FROM control_events").fetchone()[0], 1)

    def test_begin_immediate_busy_is_translated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            first = ControllerStore(root).open()
            second = ControllerStore(root).open()
            try:
                first.conn.execute("BEGIN IMMEDIATE")
                second.conn.execute("PRAGMA busy_timeout=1")
                with self.assertRaises(LockBusyError):
                    with second.transaction():
                        pass
            finally:
                first.conn.rollback()
                first.close()
                second.close()

    def test_metadata_listing_scales_to_one_hundred_projects(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            now = "2024-01-01T00:00:00Z"
            with store.transaction():
                for index in range(100):
                    project_id = new_id("prj")
                    store.conn.execute(
                        "INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (project_id, f"p{index}", f"/g/{index}", 1, index + 1, f"/p/{index}", "sha1", "trusted", "h", "active", "ready", 1, now, now),
                    )
            self.assertEqual(len(store.list_projects()), 100)

    def test_run_requires_active_build_and_claims_writer_epoch(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            project_id, working_copy_id, conversation_id = add_project(store)
            store.register_build("staged", source_tree_hash="t", artifact_manifest_hash="a", pi_version="p", package_lock_hash="l", status="staged")
            with self.assertRaises(ConstraintError):
                store.create_run(run_id=new_id("run"), conversation_id=conversation_id, authority="writer", runtime_spec_hash="r", build_id="staged", project_id=project_id, working_copy_id=working_copy_id, expected_working_copy_version=1, writer_epoch=1, capability_hash="h")
            store.register_build("active", source_tree_hash="t", artifact_manifest_hash="a", pi_version="p", package_lock_hash="l", status="active")
            run = store.create_run(run_id=new_id("run"), conversation_id=conversation_id, authority="writer", runtime_spec_hash="r", build_id="active", project_id=project_id, working_copy_id=working_copy_id, expected_working_copy_version=1, writer_epoch=1, capability_hash="h")
            self.assertEqual(store.conn.execute("SELECT active_writer_run_id,writer_epoch FROM working_copies WHERE working_copy_id=?", (working_copy_id,)).fetchone()[0], run["run_id"])
            with self.assertRaises(WriterStaleError):
                store.create_run(run_id=new_id("run"), conversation_id=conversation_id, authority="writer", runtime_spec_hash="r", build_id="active", project_id=project_id, working_copy_id=working_copy_id, expected_working_copy_version=2, writer_epoch=2, capability_hash="h")
            store.terminalize_run(run["run_id"])
            claim = store.conn.execute("SELECT active_writer_run_id FROM working_copies WHERE working_copy_id=?", (working_copy_id,)).fetchone()[0]
            self.assertIsNone(claim)
            self.assertEqual(store.conn.execute("SELECT observed_state FROM runs WHERE run_id=?", (run["run_id"],)).fetchone()[0], "stopped")


if __name__ == "__main__":
    unittest.main()
