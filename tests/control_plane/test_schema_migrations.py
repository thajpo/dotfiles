"""Phase 2 schema, migration, and trigger tests."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from scripts.pi_control.errors import DatabaseCorruptError, ConstraintError, SQLiteUnsupportedError, SchemaNewerError
from scripts.pi_control.models import new_id
from scripts.pi_control.schema import ARTIFACT_SCHEMA_SQL, COMPLETION_SCHEMA_SQL, RECEIPT_OPERATION_IMMUTABILITY_SQL, REVISION_SCHEMA_SQL, SCHEMA_VERSION, SCHEMA_SQL, iter_statements, schema_digest
from scripts.pi_control.store import ControllerStore
from scripts.pi_control.migrations import v001_initial, v002_child_source, v003_artifacts, v004_revision_immutability, v005_review_authority, v006_receipt_operation_immutability, v007_completion_resources


_V7_AUTH_KINDS = """    ('create-workstream','relaunch-workstream','retire-workstream',
     'adopt-working-copy','archive-conversation','submit-change','close-change',
     'integrate-change','publish','cleanup','host-command','migration-resolve',
     'migration-cutover','activation-change')"""
_V6_AUTH_KINDS = """    ('create-workstream','submit-change','integrate-change','close-change',
     'cleanup','publish','host-command','migration-cutover')"""


def _schema_before_v7() -> str:
    return SCHEMA_SQL.replace(COMPLETION_SCHEMA_SQL, '').replace(_V7_AUTH_KINDS, _V6_AUTH_KINDS)


EXPECTED_TABLES = {
    "control_meta", "schema_migrations", "installed_builds", "projects",
    "working_copies", "conversations", "runs", "changes", "change_revisions",
    "change_revision_inputs", "reviews", "integration_attempts", "authorizations",
    "operations", "control_events", "event_consumers", "attention",
    "migration_runs", "migration_manifests", "migration_resource_mappings",
    "artifact_manifests", "child_terminal_records", "workstreams",
    "presentation_assignments", "project_activations", "installed_builds",
}


class SchemaMigrationTests(unittest.TestCase):
    def test_exact_phase_two_tables_indexes_and_triggers(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            tables = {row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            self.assertEqual(tables, EXPECTED_TABLES)
            indexes = {row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertIn("installed_builds_one_active_uq", indexes)
            self.assertIn("changes_one_draft_per_working_copy_uq", indexes)
            triggers = {row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            for name in (
                "working_copy_active_writer_valid",
                "active_writer_run_not_deleted",
                "conversation_role_project_required_insert",
                "writer_run_shape_insert",
                "changes_current_revision_exists_on_update",
                "migration_runs_request_immutable",
                "migration_manifests_immutable_delete",
                "artifact_manifests_immutable_delete",
                "child_terminal_records_immutable_delete",
                "change_revisions_immutable_delete",
                "change_revision_inputs_immutable_delete",
                "submitted_review_receipt_immutable",
                "terminal_operation_immutable",
                "authorization_scope_immutable",
                "authorization_terminal_immutable",
                "workstream_link_valid_insert",
                "workstream_link_valid_update",
                "workstream_identity_immutable",
                "presentation_identity_immutable",
                "migration_resource_mapping_immutable_update",
                "migration_resource_mapping_immutable_delete",
            ):
                self.assertIn(name, triggers)
            migrations = [tuple(row) for row in store.conn.execute("SELECT version,name,source_sha256 FROM schema_migrations ORDER BY version")]
            self.assertEqual(migrations, [(1, "initial", v001_initial.SOURCE_SHA256), (2, "child-source", v002_child_source.SOURCE_SHA256), (3, "artifacts-terminals", v003_artifacts.SOURCE_SHA256), (4, "revision-immutability", v004_revision_immutability.SOURCE_SHA256), (5, "review-authority", v005_review_authority.SOURCE_SHA256), (6, "receipt-operation-immutability", v006_receipt_operation_immutability.SOURCE_SHA256), (7, "completion-resources", v007_completion_resources.SOURCE_SHA256)])
            self.assertEqual(store.conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_newer_migration_row_is_rejected_even_when_user_version_is_current(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            with ControllerStore(root) as store:
                store.conn.execute("INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES (?,?,?,?)", (8, "future", "future", "t"))
            with self.assertRaises(SchemaNewerError):
                ControllerStore(root).open()

    def test_capability_failure_precedes_state_creation_for_old_sqlite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            with mock.patch("scripts.pi_control.schema.sqlite3.sqlite_version", "3.39.0"):
                with self.assertRaises(SQLiteUnsupportedError):
                    ControllerStore(root).open()
            self.assertFalse(root.exists())

    def test_interrupted_initial_migration_rolls_back_and_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            from scripts.pi_control import store as store_module
            real_apply = store_module.apply_schema
            def interrupted(connection):
                real_apply(connection)
                raise RuntimeError("injected migration interruption")
            with mock.patch.object(store_module, "apply_schema", side_effect=interrupted):
                with self.assertRaises(RuntimeError):
                    ControllerStore(root).open()
            with sqlite3.connect(root / "control.db") as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='control_meta'").fetchone()[0], 0)
            with ControllerStore(root) as store:
                self.assertEqual(store.schema_status().schema_version, SCHEMA_VERSION)

    def test_v1_database_upgrades_atomically_to_child_source_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            root.mkdir(mode=0o700)
            database = root / "control.db"
            legacy_sql = _schema_before_v7().replace("  child_source_json TEXT CHECK (child_source_json IS NULL OR json_valid(child_source_json)),\n", "").replace(ARTIFACT_SCHEMA_SQL, "").replace(REVISION_SCHEMA_SQL, "").replace(RECEIPT_OPERATION_IMMUTABILITY_SQL, "")
            with sqlite3.connect(database) as connection:
                for statement in iter_statements(legacy_sql):
                    connection.execute(statement)
                connection.execute("INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES (?,?,?,?)", (1, "initial", v001_initial.SOURCE_SHA256, "t"))
                connection.execute("INSERT INTO control_meta(singleton,schema_version,controller_build_id,created_at,updated_at) VALUES (1,?,?,?,?)", (1, "build", "t", "t"))
                connection.execute("PRAGMA user_version = 1")
            database.chmod(0o600)
            with ControllerStore(root) as store:
                self.assertEqual(store.schema_status().migration_versions, (1, 2, 3, 4, 5, 6, 7))
                columns = {row[1] for row in store.conn.execute("PRAGMA table_info(runs)")}
                self.assertIn("child_source_json", columns)
                self.assertIsNotNone(store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_manifests'").fetchone())

    def test_v2_database_upgrades_atomically_to_artifact_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            root.mkdir(mode=0o700)
            database = root / "control.db"
            v2_sql = _schema_before_v7().replace(ARTIFACT_SCHEMA_SQL, "").replace(REVISION_SCHEMA_SQL, "").replace(RECEIPT_OPERATION_IMMUTABILITY_SQL, "")
            with sqlite3.connect(database) as connection:
                for statement in iter_statements(v2_sql):
                    connection.execute(statement)
                connection.execute("INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES (?,?,?,?)", (1, "initial", v001_initial.SOURCE_SHA256, "t"))
                connection.execute("INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES (?,?,?,?)", (2, "child-source", v002_child_source.SOURCE_SHA256, "t"))
                connection.execute("INSERT INTO control_meta(singleton,schema_version,controller_build_id,created_at,updated_at) VALUES (1,?,?,?,?)", (2, "build", "t", "t"))
                connection.execute("PRAGMA user_version = 2")
            database.chmod(0o600)
            with ControllerStore(root) as store:
                self.assertEqual(store.schema_status().migration_versions, (1, 2, 3, 4, 5, 6, 7))
                self.assertIsNotNone(store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='child_terminal_records'").fetchone())

    def test_v3_database_upgrades_atomically_to_revision_immutability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            root.mkdir(mode=0o700)
            database = root / "control.db"
            v3_sql = _schema_before_v7().replace(REVISION_SCHEMA_SQL, "").replace(RECEIPT_OPERATION_IMMUTABILITY_SQL, "")
            with sqlite3.connect(database) as connection:
                for statement in iter_statements(v3_sql):
                    connection.execute(statement)
                connection.execute("INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES (?,?,?,?)", (1, "initial", v001_initial.SOURCE_SHA256, "t"))
                connection.execute("INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES (?,?,?,?)", (2, "child-source", v002_child_source.SOURCE_SHA256, "t"))
                connection.execute("INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES (?,?,?,?)", (3, "artifacts-terminals", v003_artifacts.SOURCE_SHA256, "t"))
                connection.execute("INSERT INTO control_meta(singleton,schema_version,controller_build_id,created_at,updated_at) VALUES (1,?,?,?,?)", (3, "build", "t", "t"))
                connection.execute("PRAGMA user_version = 3")
            database.chmod(0o600)
            with ControllerStore(root) as store:
                self.assertEqual(store.schema_status().migration_versions, (1, 2, 3, 4, 5, 6, 7))
                self.assertIsNotNone(store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='change_revisions_immutable_delete'").fetchone())

    def test_v5_database_upgrades_to_receipt_operation_immutability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            root.mkdir(mode=0o700)
            database = root / "control.db"
            v5_sql = _schema_before_v7().replace(RECEIPT_OPERATION_IMMUTABILITY_SQL, "")
            with sqlite3.connect(database) as connection:
                for statement in iter_statements(v5_sql):
                    connection.execute(statement)
                for module in (v001_initial, v002_child_source, v003_artifacts, v004_revision_immutability, v005_review_authority):
                    connection.execute("INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES (?,?,?,?)", (module.VERSION, module.NAME, module.SOURCE_SHA256, "t"))
                connection.execute("INSERT INTO control_meta(singleton,schema_version,controller_build_id,created_at,updated_at) VALUES (1,?,?,?,?)", (5, "build", "t", "t"))
                connection.execute("PRAGMA user_version = 5")
            database.chmod(0o600)
            with ControllerStore(root) as store:
                self.assertEqual(store.schema_status().migration_versions, (1, 2, 3, 4, 5, 6, 7))
                self.assertIsNotNone(store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='submitted_review_receipt_immutable'").fetchone())
                self.assertIsNotNone(store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='terminal_operation_immutable'").fetchone())

    def test_reopen_is_idempotent_and_checksum_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            with ControllerStore(root):
                pass
            with ControllerStore(root) as store:
                self.assertEqual(store.schema_status().migration_versions, (1, 2, 3, 4, 5, 6, 7))
            with sqlite3.connect(root / "control.db") as connection:
                connection.execute("UPDATE schema_migrations SET source_sha256='bad' WHERE version=1")
            with self.assertRaises(DatabaseCorruptError):
                ControllerStore(root).open()

    def test_foreign_keys_checks_and_role_triggers_are_mechanical(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute(
                    "INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,effective_mode,desired_state,observed_state,resource_version,controller_owned,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (new_id("wc"), "missing", "w", "primary", "personal", "/p", "trusted-live", "present", "unknown", 1, 0, "t", "t"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute(
                    "INSERT INTO conversations(conversation_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (new_id("conv"), "secretary", "s", "pi", "/s", "active", "unknown", 1, "t", "t"),
                )
            project_id = new_id("prj")
            review_wc = new_id("wc")
            store.conn.execute(
                "INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, "p", "/review/g", 1, 1, "/review/p", "sha1", "trusted", "h", "active", "unknown", 1, "t", "t"),
            )
            store.conn.execute(
                "INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,branch_ref,effective_mode,desired_state,observed_state,resource_version,controller_owned,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (review_wc, project_id, "review", "review", "review", "/review/wc", "refs/heads/x", "trusted-live", "present", "ready", 1, 1, "t", "t"),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute(
                    "INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (new_id("conv"), project_id, review_wc, "review", "r", "pi-review-bad", "/s/review-bad", "active", "unknown", 1, "t", "t"),
                )
            store.conn.execute("UPDATE working_copies SET branch_ref=NULL,effective_mode='read-only' WHERE working_copy_id=?", (review_wc,))
            store.conn.execute(
                "INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("conv"), project_id, review_wc, "review", "r", "pi-review-good", "/s/review-good", "active", "unknown", 1, "t", "t"),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE working_copies SET kind='worktree',effective_mode='trusted-live',branch_ref='refs/heads/main' WHERE working_copy_id=?", (review_wc,))

    def test_change_state_checks_reject_invalid_revision_transitions(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            project_id = new_id("prj")
            store.conn.execute(
                "INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, "p", "/g", 1, 2, "/p", "sha1", "trusted", "h", "active", "unknown", 1, "t", "t"),
            )
            change_id = new_id("chg")
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute(
                    "INSERT INTO changes(change_id,project_id,title,summary,target_ref,baseline_oid,baseline_tree_oid,baseline_state_json,state,current_revision,resource_version,created_at,updated_at,submitted_at,merged_at,closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (change_id, project_id, "c", "s", "refs/heads/x", "a", "b", "{}", "open", 0, 1, "t", "t", None, None, None),
                )
            store.conn.execute(
                "INSERT INTO changes(change_id,project_id,title,summary,target_ref,baseline_oid,baseline_tree_oid,baseline_state_json,state,current_revision,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (change_id, project_id, "c", "s", "refs/heads/x", "a", "b", "{}", "draft", 0, 1, "t", "t"),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE changes SET state='closed' WHERE change_id=?", (change_id,))

    def test_migration_request_and_manifest_are_immutable(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            store.register_build("b", source_tree_hash="tree", artifact_manifest_hash="manifest", pi_version="pi", package_lock_hash="lock", status="active")
            op = store.create_operation(idempotency_key="migration-1", kind="migration", resource_type="migration", resource_id="m", actor_type="controller", request={"x": 1})
            store.conn.execute(
                "INSERT INTO migration_runs(migration_id,operation_id,idempotency_key,mode,controller_build_id,request_digest,source_manifest_digest,state,step,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("migration"), op.operation_id, "migration-1", "inventory", "b", "r", "mhash", "planned", "intent", 1, "t", "t"),
            )
            migration_id = store.conn.execute("SELECT migration_id FROM migration_runs").fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE migration_runs SET mode='rollback' WHERE migration_id=?", (migration_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("DELETE FROM migration_runs WHERE migration_id=?", (migration_id,))


if __name__ == "__main__":
    unittest.main()
