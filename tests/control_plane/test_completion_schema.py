"""Schema-v7 completion resource and migration tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.pi_control.migrations import (
    v001_initial,
    v002_child_source,
    v003_artifacts,
    v004_revision_immutability,
    v005_review_authority,
    v006_receipt_operation_immutability,
)
from scripts.pi_control.models import new_id
from scripts.pi_control.schema import COMPLETION_SCHEMA_SQL, SCHEMA_SQL, iter_statements
from scripts.pi_control.store import ControllerStore


_V7_AUTH_KINDS = """    ('create-workstream','relaunch-workstream','retire-workstream',
     'adopt-working-copy','archive-conversation','submit-change','close-change',
     'integrate-change','publish','cleanup','host-command','migration-resolve',
     'migration-cutover','activation-change')"""
_V6_AUTH_KINDS = """    ('create-workstream','submit-change','integrate-change','close-change',
     'cleanup','publish','host-command','migration-cutover')"""


def _schema_before_v7() -> str:
    return SCHEMA_SQL.replace(COMPLETION_SCHEMA_SQL, "").replace(_V7_AUTH_KINDS, _V6_AUTH_KINDS)


def _make_v6_root(parent: Path) -> Path:
    root = parent / "state"
    root.mkdir(mode=0o700)
    database = root / "control.db"
    with sqlite3.connect(database) as connection:
        for statement in iter_statements(_schema_before_v7()):
            connection.execute(statement)
        modules = (v001_initial, v002_child_source, v003_artifacts,
                   v004_revision_immutability, v005_review_authority,
                   v006_receipt_operation_immutability)
        for module in modules:
            connection.execute(
                "INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES(?,?,?,?)",
                (module.VERSION, module.NAME, module.SOURCE_SHA256, "t"),
            )
        connection.execute(
            "INSERT INTO control_meta(singleton,schema_version,controller_build_id,created_at,updated_at) VALUES(1,?,?,?,?)",
            (6, "build", "t", "t"),
        )
        connection.execute("PRAGMA user_version = 6")
    database.chmod(0o600)
    return root


def _project(store: ControllerStore, project_id: str, suffix: str = "p") -> None:
    store.conn.execute(
        "INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (project_id, suffix, f"/{suffix}/git", 1, 1, f"/{suffix}/checkout", "sha1", "trusted", "policy", "active", "unknown", 1, "t", "t"),
    )


def _workstream_resources(store: ControllerStore, project_id: str) -> tuple[str, str, str]:
    working_copy_id = new_id("wc")
    conversation_id = new_id("conv")
    workstream_id = new_id("ws")
    store.conn.execute(
        "INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,branch_ref,effective_mode,desired_state,observed_state,resource_version,controller_owned,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (working_copy_id, project_id, "ws", "worktree", "workstream", "/ws", "refs/heads/ws", "trusted-live", "present", "unknown", 1, 1, "t", "t"),
    )
    store.conn.execute(
        "INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (conversation_id, project_id, working_copy_id, "workstream", "ws", "pi-ws", "/ws/session.jsonl", "active", "unknown", 1, "t", "t"),
    )
    return workstream_id, working_copy_id, conversation_id


class CompletionSchemaTests(unittest.TestCase):
    def test_fresh_v7_resources_and_authorization_vocabulary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            tables = {row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            self.assertTrue({"workstreams", "presentation_assignments", "project_activations", "migration_resource_mappings"} <= tables)
            kinds = [
                "create-workstream", "relaunch-workstream", "retire-workstream", "adopt-working-copy",
                "archive-conversation", "submit-change", "close-change", "integrate-change", "publish",
                "cleanup", "host-command", "migration-resolve", "migration-cutover", "activation-change",
            ]
            for index, kind in enumerate(kinds):
                store.conn.execute(
                    "INSERT INTO authorizations(authorization_id,kind,actor_type,resource_type,resource_id,request_context_id,scope_json,scope_digest,issued_at,state) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (new_id("auth"), kind, "controller", "project", f"r-{index}", f"ctx-{index}", "{}", f"d-{index}", "t", "active"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute(
                    "INSERT INTO authorizations(authorization_id,kind,actor_type,resource_type,resource_id,request_context_id,scope_json,scope_digest,issued_at,state) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (new_id("auth"), "not-a-kind", "controller", "project", "r-bad", "ctx-bad", "{}", "d-bad", "t", "active"),
                )

    def test_v6_upgrade_preserves_authorizations_and_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _make_v6_root(Path(temporary))
            with sqlite3.connect(root / "control.db") as connection:
                connection.execute(
                    "INSERT INTO authorizations(authorization_id,kind,actor_type,resource_type,resource_id,request_context_id,scope_json,scope_digest,issued_at,state) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ("auth_legacy", "create-workstream", "controller", "project", "p", "legacy", "{}", "legacy-digest", "t", "active"),
                )
            with mock.patch("scripts.pi_control.store.v007_completion_resources.apply", side_effect=RuntimeError("interrupt")):
                with self.assertRaises(RuntimeError):
                    ControllerStore(root).open()
            with sqlite3.connect(root / "control.db") as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_master WHERE name='workstreams'").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT kind FROM authorizations WHERE authorization_id='auth_legacy'").fetchone()[0], "create-workstream")
            with ControllerStore(root) as store:
                self.assertEqual(store.schema_status().migration_versions, (1, 2, 3, 4, 5, 6, 7))
                self.assertEqual(store.conn.execute("SELECT kind FROM authorizations WHERE authorization_id='auth_legacy'").fetchone()[0], "create-workstream")

    def test_workstream_project_links_and_immutable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            _project(store, "prj-one", "one")
            workstream_id, working_copy_id, conversation_id = _workstream_resources(store, "prj-one")
            store.conn.execute(
                "INSERT INTO workstreams(workstream_id,project_id,working_copy_id,conversation_id,title,brief_json,target_ref,starting_oid,desired_state,observed_state,controller_owned,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (workstream_id, "prj-one", working_copy_id, conversation_id, "ws", "{}", "refs/heads/main", "a" * 40, "active", "planned", 1, 1, "t", "t"),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE workstreams SET conversation_id=? WHERE workstream_id=?", (new_id("conv"), workstream_id))
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE workstreams SET workstream_id=? WHERE workstream_id=?", (new_id("ws"), workstream_id))

    def test_presentation_and_activation_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            _project(store, "prj-one", "one")
            _, _, conversation_id = _workstream_resources(store, "prj-one")
            assignment_id = new_id("pa")
            store.conn.execute(
                "INSERT INTO presentation_assignments(presentation_assignment_id,conversation_id,backend,desired_state,observed_state,locator_json,resource_version,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (assignment_id, conversation_id, "tmux", "present", "unknown", "{}", 1, "t"),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE presentation_assignments SET conversation_id=? WHERE presentation_assignment_id=?", (new_id("conv"), assignment_id))
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("INSERT INTO project_activations(project_id,mode,controller_build_id,migration_id,expected_project_version,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", ("prj-one", "controller", None, None, 1, 1, "t", "t"))

    def test_migration_mapping_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            store.register_build("build", source_tree_hash="tree", artifact_manifest_hash="manifest", pi_version="pi", package_lock_hash="lock", status="active")
            operation = store.create_operation(idempotency_key="migration-op", kind="migration", resource_type="migration", resource_id="mig-resource", actor_type="controller", request={})
            migration_id = new_id("mig")
            store.conn.execute(
                "INSERT INTO migration_runs(migration_id,operation_id,idempotency_key,mode,controller_build_id,request_digest,source_manifest_digest,state,step,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (migration_id, operation.operation_id, "migration-op", "inventory", "build", "request", "source", "planned", "intent", 1, "t", "t"),
            )
            store.conn.execute(
                "INSERT INTO migration_resource_mappings(migration_id,record_id,adapter_kind,source_kind,source_digest,resource_type,disposition,reason_code,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (migration_id, "record-1", "git", "repository", "digest", "project", "observe", "historical", "{}", "t"),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE migration_resource_mappings SET reason_code='changed' WHERE migration_id=? AND record_id=?", (migration_id, "record-1"))
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("DELETE FROM migration_resource_mappings WHERE migration_id=? AND record_id=?", (migration_id, "record-1"))


if __name__ == "__main__":
    unittest.main()
