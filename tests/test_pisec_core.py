from pathlib import Path
import os
import sqlite3
import tempfile
import unittest

from scripts.pisec.events import append_event, list_events
from scripts.pisec.models import IdempotencyConflictError, InvalidRequestError, canonical_json, json_digest, new_id, parse_json_strict, validate_id
from scripts.pisec.operations import create_operation, update_operation
from scripts.pisec.pi_store import PiStore
from scripts.pisec.models import SchemaError, UnsafeStateError
from scripts.pisec.pi_schema import PREVIOUS_MIGRATION_NAME, PREVIOUS_SCHEMA_DIGEST


class ModelTests(unittest.TestCase):
    def test_ids_are_typed_128_bit_lowercase_hex(self):
        value = new_id("ws")
        self.assertEqual(validate_id(value, prefix="ws"), value)
        self.assertEqual(len(value), 35)
        with self.assertRaises(InvalidRequestError):
            validate_id(value, prefix="prj")

    def test_canonical_json_and_digest_are_stable(self):
        left = {"z": [3, 2, 1], "a": {"yes": True}}
        right = {"a": {"yes": True}, "z": [3, 2, 1]}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(json_digest(left), json_digest(right))

    def test_strict_json_rejects_duplicates_depth_and_nonfinite(self):
        with self.assertRaises(InvalidRequestError):
            parse_json_strict('{"x":1,"x":2}')
        with self.assertRaises(InvalidRequestError):
            parse_json_strict('{"x":NaN}')
        deep = "[" * 34 + "0" + "]" * 34
        with self.assertRaises(InvalidRequestError):
            parse_json_strict(deep)


class StoreTests(unittest.TestCase):
    def test_store_has_owner_only_modes_and_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            with PiStore(root) as store:
                self.assertEqual(store.conn.execute("SELECT schema_name FROM control_meta").fetchone()[0], "pisec-core")
                self.assertEqual(store.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((root / "control.db").stat().st_mode & 0o777, 0o600)

    def test_store_rejects_symlink_and_open_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            actual = base / "actual"
            actual.mkdir(mode=0o700)
            link = base / "link"
            link.symlink_to(actual, target_is_directory=True)
            with self.assertRaises(UnsafeStateError):
                PiStore(link)
            os.chmod(actual, 0o755)
            with self.assertRaises(UnsafeStateError):
                PiStore(actual)

    def test_store_rejects_database_mode_and_schema_digest_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            store = PiStore(root)
            store.close()
            database = root / "control.db"
            os.chmod(database, 0o644)
            with self.assertRaises(UnsafeStateError):
                PiStore(root)
            os.chmod(database, 0o600)
            with PiStore(root) as store:
                store.conn.execute("UPDATE control_meta SET schema_sha256='sha256:bad'")
            with self.assertRaises(SchemaError):
                PiStore(root)


    def test_epoch_sixteen_fresh_store_uses_current_surface_and_permission_schema(self):
        with tempfile.TemporaryDirectory() as tmp, PiStore(Path(tmp) / "state") as store:
            metadata = store.conn.execute("SELECT schema_name,schema_version,schema_sha256 FROM control_meta").fetchone()
            self.assertEqual(metadata["schema_name"], "pisec-core")
            self.assertEqual(metadata["schema_version"], 16)
            self.assertTrue(metadata["schema_sha256"].startswith("sha256:"))
            binding_columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(runtime_bindings)")}
            self.assertIn("desired_generation_sha256", binding_columns)
            self.assertIn("applied_generation_sha256", binding_columns)
            self.assertNotIn("desired_release_id", binding_columns)
            self.assertNotIn("applied_release_id", binding_columns)
            for table in ("runtime_releases", "runtime_release_channels", "access_grants", "deployment_actions"):
                self.assertIsNone(store.conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone())
            remediation_columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(issue_remediations)")}
            self.assertEqual(remediation_columns, {"remediation_id", "issue_id", "kind", "workstream_id", "created_at"})
            operation_sql = store.conn.execute("SELECT sql FROM sqlite_master WHERE name='operations'").fetchone()[0]
            self.assertNotIn("'authorized'", operation_sql)

    def test_epoch_fifteen_state_migration_requires_explicitly_safe_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            with PiStore(root) as store:
                store.conn.execute("ALTER TABLE workstreams ADD COLUMN worker_creation_policy_json TEXT NOT NULL DEFAULT '{}'")
                store.conn.execute("ALTER TABLE control_meta ADD COLUMN migration_name TEXT")
                store.conn.execute(
                    "UPDATE control_meta SET schema_version=15,schema_sha256=?,migration_name=?",
                    (PREVIOUS_SCHEMA_DIGEST, PREVIOUS_MIGRATION_NAME),
                )
            with PiStore(root) as migrated:
                metadata = migrated.conn.execute("SELECT schema_version,schema_sha256 FROM control_meta").fetchone()
                self.assertEqual(metadata["schema_version"], 16)
                self.assertTrue(metadata["schema_sha256"].startswith("sha256:"))

    def test_legacy_epoch_six_state_is_rejected_without_resetting_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            store = PiStore(root)
            project_id = "prj_" + "a" * 32
            store.conn.execute("INSERT INTO projects(project_id,display_name,repository_path,git_common_dir,default_ref,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (project_id, "Project", "/repo", "/repo/.git", "main", "now", "now"))
            store.conn.execute("ALTER TABLE control_meta ADD COLUMN migration_name TEXT")
            store.conn.execute("UPDATE control_meta SET schema_version=6,schema_sha256=?,migration_name='pisec-core-epoch-6'", ("sha256:c00cd142b2cd4dd775c3d7878820c4fd69f945e9e4254cfd18414bc82877ca59",))
            store.close()
            with self.assertRaises(SchemaError):
                PiStore(root)
            connection = sqlite3.connect(root / "control.db")
            try:
                self.assertIsNotNone(connection.execute("SELECT project_id FROM projects WHERE project_id=?", (project_id,)).fetchone())
            finally:
                connection.close()
    def test_epoch_fourteen_state_is_rejected_without_implicit_downgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            with PiStore(root) as store:
                store.conn.execute("ALTER TABLE control_meta ADD COLUMN migration_name TEXT")
                store.conn.execute("UPDATE control_meta SET schema_version=14,schema_sha256=?,migration_name='pisec-core-epoch-14'", (PREVIOUS_SCHEMA_DIGEST,))
            with self.assertRaises(SchemaError):
                PiStore(root)


class OperationEventTests(unittest.TestCase):
    def test_idempotency_binds_key_to_request_forever(self):
        with tempfile.TemporaryDirectory() as tmp, PiStore(Path(tmp) / "state") as store:
            first, created = create_operation(store, kind="project.register", idempotency_key="same", request={"path": "/repo"})
            replay, replay_created = create_operation(store, kind="project.register", idempotency_key="same", request={"path": "/repo"})
            self.assertTrue(created)
            self.assertFalse(replay_created)
            self.assertEqual(first.operation_id, replay.operation_id)
            with self.assertRaises(IdempotencyConflictError):
                create_operation(store, kind="project.register", idempotency_key="same", request={"path": "/other"})

    def test_operation_cas_and_event_append(self):
        with tempfile.TemporaryDirectory() as tmp, PiStore(Path(tmp) / "state") as store:
            operation, _ = create_operation(store, kind="project.register", idempotency_key="register", request={"path": "/repo"})
            operation = update_operation(store, operation.operation_id, state="succeeded", step="committed", expected_states=("planned",), result={"ok": True})
            self.assertEqual(operation.state, "succeeded")
            event = append_event(store, kind="project.registered", operation_id=operation.operation_id, payload={"ok": True})
            self.assertEqual(event["sequence"], 1)
            self.assertEqual(len(list_events(store)), 1)


if __name__ == "__main__":
    unittest.main()
