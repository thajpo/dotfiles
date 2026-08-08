"""Phase 2 secure SQLite store tests."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock

from scripts.pi_control.errors import (
    DatabaseCorruptError,
    SchemaNewerError,
    SQLiteUnsupportedError,
    UnsafeDatabaseError,
)
from scripts.pi_control.events import append_event_in_transaction
from scripts.pi_control.models import canonical_json, new_id
from scripts.pi_control.store import ControllerStore
from scripts.pi_control.schema import SCHEMA_VERSION


class StoreIsolationTests(unittest.TestCase):
    def test_fresh_store_has_secure_root_pragmas_and_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            with ControllerStore(root) as store:
                status = store.schema_status()
                self.assertEqual(status.schema_version, SCHEMA_VERSION)
                self.assertEqual(status.user_version, SCHEMA_VERSION)
                self.assertTrue(status.foreign_keys)
                self.assertEqual(status.journal_mode, "wal")
                self.assertEqual(status.synchronous, 2)
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(store.db_path.stat().st_mode), 0o600)
                self.assertEqual(store.list_projects(), [])
                self.assertEqual(store.list_operations(), [])
                self.assertEqual(store.list_events(), [])
            for sidecar in (Path(str(root / "control.db") + "-wal"), Path(str(root / "control.db") + "-shm")):
                if sidecar.exists():
                    self.assertFalse(sidecar.is_symlink())
                    self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode) & 0o077, 0)

    def test_database_outside_state_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "state"
            external = base / "external.db"
            with self.assertRaises(UnsafeDatabaseError):
                ControllerStore(root, db_path=external).open()

    def test_root_symlink_and_database_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            real = base / "real"
            real.mkdir(mode=0o700)
            link = base / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(UnsafeDatabaseError):
                ControllerStore(link).open()
            root = base / "state"
            root.mkdir(mode=0o700)
            target = base / "target.db"
            target.touch(mode=0o600)
            (root / "control.db").symlink_to(target)
            with self.assertRaises(UnsafeDatabaseError):
                ControllerStore(root).open()

    def test_network_filesystem_fails_before_state_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            with mock.patch("scripts.pi_control.store.filesystem_type", return_value="nfs4"):
                with self.assertRaises(UnsafeDatabaseError):
                    ControllerStore(root).open()
            self.assertFalse(root.exists())

    def test_unsupported_capability_leaves_root_uncreated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            with mock.patch("scripts.pi_control.store.probe_capabilities", side_effect=SQLiteUnsupportedError("unsupported")):
                with self.assertRaises(SQLiteUnsupportedError):
                    ControllerStore(root).open()
            self.assertFalse(root.exists())

    def test_transaction_rolls_back_state_and_event_together(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            with self.assertRaises(RuntimeError):
                with store.transaction():
                    store.conn.execute(
                        "INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (new_id("prj"), "p", "/g", 1, 1, "/p", "sha1", "trusted", "h", "active", "unknown", 1, "t", "t"),
                    )
                    append_event_in_transaction(store.conn, event_kind="x", resource_type="project", resource_id="p", payload={"x": 1})
                    raise RuntimeError("rollback")
            self.assertEqual(store.conn.execute("SELECT count(*) FROM projects").fetchone()[0], 0)
            self.assertEqual(store.conn.execute("SELECT count(*) FROM control_events").fetchone()[0], 0)

    def test_newer_schema_and_corruption_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            with ControllerStore(root):
                pass
            with sqlite3.connect(root / "control.db") as connection:
                connection.execute("PRAGMA user_version = 99")
            with self.assertRaises(SchemaNewerError):
                ControllerStore(root).open()
            corrupt_root = Path(temporary) / "corrupt"
            corrupt_root.mkdir(mode=0o700)
            (corrupt_root / "control.db").write_bytes(b"not sqlite")
            with self.assertRaises((DatabaseCorruptError, UnsafeDatabaseError)):
                ControllerStore(corrupt_root).open()

    def test_canonical_json_is_bounded_and_does_not_pickle(self):
        self.assertEqual(canonical_json({"b": 2, "a": 1}), '{"a":1,"b":2}')
        with self.assertRaises(Exception):
            canonical_json({"value": float("nan")})


if __name__ == "__main__":
    unittest.main()
