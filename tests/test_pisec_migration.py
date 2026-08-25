"""Temporary Phase 4 evidence that unsupported state is refusal-only."""

from pathlib import Path
import os
import sqlite3
import tempfile
import unittest

from scripts.pisec.pi_schema import SCHEMA_NAME, SCHEMA_VERSION, schema_digest
from scripts.pisec.pi_store import PiStore, archive_and_reset_state
from scripts.pisec.models import SchemaError


class UnsupportedStateTests(unittest.TestCase):
    def _tamper(self, root: Path, *, name: str, version: int, digest: str) -> bytes:
        with PiStore(root) as store:
            store.conn.execute("UPDATE control_meta SET schema_name=?,schema_version=?,schema_sha256=?", (name, version, digest))
        return (root / "control.db").read_bytes()

    def test_legacy_v15_unsupported_is_rejected_without_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            before = self._tamper(root, name="pisec-core", version=15, digest="0" * 64)
            with self.assertRaises(SchemaError):
                PiStore(root)
            self.assertEqual(before, (root / "control.db").read_bytes())

    def test_legacy_v16_unsupported_is_rejected_without_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            before = self._tamper(root, name="pisec-core", version=16, digest="1" * 64)
            with self.assertRaises(SchemaError):
                PiStore(root)
            self.assertEqual(before, (root / "control.db").read_bytes())

    def test_wrong_identity_is_rejected_without_changes(self):
        for name, version, digest in (("wrong", 1, schema_digest()), (SCHEMA_NAME, 2, schema_digest()), (SCHEMA_NAME, SCHEMA_VERSION, "f" * 64)):
            with self.subTest(name=name, version=version):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp) / "state"
                    before = self._tamper(root, name=name, version=version, digest=digest)
                    with self.assertRaises(SchemaError):
                        PiStore(root)
                    self.assertEqual(before, (root / "control.db").read_bytes())

    def test_archive_reset_preserves_complete_owner_only_root_and_creates_fresh_v1(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            with PiStore(root) as store:
                store.conn.execute("CREATE TABLE retained_test(value TEXT)")
                store.conn.execute("INSERT INTO retained_test VALUES('opaque')")
            archive = archive_and_reset_state(root)
            self.assertIsNotNone(archive)
            assert archive is not None
            self.assertEqual((archive / "control.db").read_bytes()[:16], b"SQLite format 3\x00")
            self.assertEqual(archive.stat().st_mode & 0o777, 0o700)
            with PiStore(root) as store:
                self.assertEqual(tuple(store.conn.execute("SELECT schema_name,schema_version,schema_sha256 FROM control_meta").fetchone()), (SCHEMA_NAME, SCHEMA_VERSION, schema_digest()))
                self.assertIsNone(store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='retained_test'").fetchone())


if __name__ == "__main__":
    unittest.main()
