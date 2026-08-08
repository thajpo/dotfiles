"""Phase 4 kernel writer leases, epochs, and fences."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.pi_control.errors import ConstraintError, LockBusyError, WriterStaleError, WriterUnknownError
from scripts.pi_control.leases import LockOrderContext, WriterLease, check_run_authority, create_writer_run
from scripts.pi_control.locks import secure_directory_fd
from scripts.pi_control.models import new_id
from scripts.pi_control.store import ControllerStore
from tests.control_plane.test_operations import add_project


RUNTIME_HASH = "sha256:" + "2" * 64


class WriterFencingTests(unittest.TestCase):
    def test_lifetime_lease_epoch_fence_and_terminal_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            with ControllerStore(Path(temporary) / "state") as store:
                project_id, working_copy_id, conversation_id = add_project(store)
                store.register_build("active", source_tree_hash="tree", artifact_manifest_hash="artifact", pi_version="pi", package_lock_hash="lock", status="active")
                handle = create_writer_run(store, conversation_id=conversation_id, working_copy_id=working_copy_id, build_id="active", runtime_spec_hash=RUNTIME_HASH, project_id=project_id)
                self.assertEqual(handle.writer_epoch, 1)
                current_version = store.conn.execute("SELECT resource_version FROM working_copies WHERE working_copy_id=?", (working_copy_id,)).fetchone()[0]
                self.assertEqual(handle.fence(expected_resource_version=current_version, operation_kind="git-observe")["writer_epoch"], 1)
                with self.assertRaises(LockBusyError):
                    WriterLease(store.state_root, working_copy_id).acquire()
                with self.assertRaises(WriterUnknownError):
                    create_writer_run(store, conversation_id=conversation_id, working_copy_id=working_copy_id, build_id="active", runtime_spec_hash=RUNTIME_HASH, project_id=project_id)
                handle.close()
                self.assertIsNone(store.conn.execute("SELECT active_writer_run_id FROM working_copies WHERE working_copy_id=?", (working_copy_id,)).fetchone()[0])
                second = create_writer_run(store, conversation_id=conversation_id, working_copy_id=working_copy_id, build_id="active", runtime_spec_hash=RUNTIME_HASH, project_id=project_id)
                self.assertEqual(second.writer_epoch, 2)
                second.close()

    def test_idempotent_writer_replay_preserves_one_run_and_lifecycle_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            with ControllerStore(Path(temporary) / "state") as store:
                project_id, working_copy_id, conversation_id = add_project(store)
                store.register_build("active", source_tree_hash="tree", artifact_manifest_hash="artifact", pi_version="pi", package_lock_hash="lock", status="active")
                kwargs = {
                    "conversation_id": conversation_id,
                    "working_copy_id": working_copy_id,
                    "build_id": "active",
                    "runtime_spec_hash": RUNTIME_HASH,
                    "project_id": project_id,
                    "idempotency_key": "writer-replay",
                    "expected_working_copy_version": 1,
                    "expected_writer_epoch": 1,
                }
                first = create_writer_run(store, **kwargs)
                first_id = first.run_id
                with self.assertRaises(WriterUnknownError):
                    create_writer_run(store, **kwargs)
                first.close()
                replay = create_writer_run(store, **kwargs)
                self.assertEqual(replay.run_id, first_id)
                self.assertTrue(replay.replayed)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM runs WHERE working_copy_id=?", (working_copy_id,)).fetchone()[0], 1)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM operations WHERE idempotency_key=?", ("writer-replay",)).fetchone()[0], 1)
                self.assertEqual(
                    [row[0] for row in store.conn.execute("SELECT event_kind FROM control_events WHERE resource_type IN ('run','working_copy') ORDER BY sequence")],
                    ["run.created", "writer.claimed", "run.terminalized", "writer.claim.cleared"],
                )
                replay.close()
                with self.assertRaises(Exception):
                    create_writer_run(store, **{**kwargs, "runtime_spec_hash": "sha256:" + "3" * 64})

    def test_stale_epoch_and_terminal_run_are_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            with ControllerStore(Path(temporary) / "state") as store:
                project_id, working_copy_id, conversation_id = add_project(store)
                store.register_build("active", source_tree_hash="tree", artifact_manifest_hash="artifact", pi_version="pi", package_lock_hash="lock", status="active")
                handle = create_writer_run(store, conversation_id=conversation_id, working_copy_id=working_copy_id, build_id="active", runtime_spec_hash=RUNTIME_HASH, project_id=project_id)
                version = store.conn.execute("SELECT resource_version FROM working_copies WHERE working_copy_id=?", (working_copy_id,)).fetchone()[0]
                with self.assertRaises(WriterStaleError):
                    check_run_authority(store, handle.run_id, working_copy_id=working_copy_id, writer_epoch=0, expected_resource_version=version, operation_kind="git-write", capability_secret=handle.capability_secret)
                handle.close()
                with self.assertRaises(WriterStaleError):
                    check_run_authority(store, handle.run_id, working_copy_id=working_copy_id, writer_epoch=1, expected_resource_version=version, operation_kind="git-write", capability_secret=handle.capability_secret)

    def test_secretary_role_cannot_be_promoted_to_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            with ControllerStore(Path(temporary) / "state") as store:
                project_id, working_copy_id, _ = add_project(store)
                store.conn.execute(
                    "INSERT INTO conversations(conversation_id,project_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("conv_" + "c" * 32, project_id, "secretary", "Secretary", "secretary-session", "/tmp/secretary-session", "active", "ready", 1, "t", "t"),
                )
                store.register_build("active", source_tree_hash="tree", artifact_manifest_hash="artifact", pi_version="pi", package_lock_hash="lock", status="active")
                with self.assertRaises(WriterStaleError):
                    create_writer_run(store, conversation_id="conv_" + "c" * 32, working_copy_id=working_copy_id, build_id="active", runtime_spec_hash=RUNTIME_HASH, project_id=project_id)
                with self.assertRaises(ConstraintError):
                    store.create_run(run_id=new_id("run"), conversation_id="conv_" + "c" * 32, authority="writer", runtime_spec_hash="r", build_id="active", project_id=project_id, working_copy_id=working_copy_id, expected_working_copy_version=1, writer_epoch=1, capability_hash="h")
                store.conn.execute(
                    "INSERT INTO conversations(conversation_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("conv_" + "d" * 32, "host", "Host", "host-session", "/tmp/host-session", "active", "ready", 1, "t", "t"),
                )
                with self.assertRaises(ConstraintError):
                    store.create_run(run_id=new_id("run"), conversation_id="conv_" + "d" * 32, authority="read-only", runtime_spec_hash="r", build_id="active", project_id=project_id)

    def test_secure_directory_creation_is_not_redirected_by_parent_swap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "parent"
            moved = root / "parent-moved"
            redirected = root / "redirected"
            parent.mkdir(mode=0o700)
            redirected.mkdir(mode=0o700)
            real_mkdir = os.mkdir

            def swap_then_mkdir(name, mode=0o777, *, dir_fd=None):
                if name == "child" and parent.exists() and not parent.is_symlink():
                    parent.rename(moved)
                    parent.symlink_to(redirected, target_is_directory=True)
                return real_mkdir(name, mode, dir_fd=dir_fd)

            with mock.patch("scripts.pi_control.locks.os.mkdir", side_effect=swap_then_mkdir):
                descriptor = secure_directory_fd(parent / "child", create=True)
            self.assertIsNotNone(descriptor)
            os.close(descriptor)
            self.assertTrue((moved / "child").is_dir())
            self.assertFalse((redirected / "child").exists())

    def test_lease_is_not_reentrant_and_paths_are_controller_id_based(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                WriterLease(Path(temporary), "wc_../escape")
            lifecycle = WriterLease(Path(temporary), "wc_" + "a" * 32, kind="lifecycle")
            writer = WriterLease(Path(temporary), "wc_" + "a" * 32, kind="writer")
            order = LockOrderContext()
            order.acquire(lifecycle)
            order.acquire(writer)
            with self.assertRaises(Exception):
                order.acquire(lifecycle)
            order.release_all()
            lease = WriterLease(Path(temporary), "wc_" + "a" * 32)
            lease.acquire()
            try:
                with self.assertRaises(Exception):
                    lease.acquire()
                self.assertTrue(lease.path.name.startswith("wc_"))
            finally:
                lease.release()


if __name__ == "__main__":
    unittest.main()
