"""C1b immutable migration mapping APIs."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.errors import MigrationUnresolvedError
from scripts.pi_control.models import new_id
from scripts.pi_control.store import ControllerStore
from scripts.pi_control.workstreams import create_migration_mapping


class MigrationMappingTests(unittest.TestCase):
    def test_mapping_requires_exact_disposition_and_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            store.register_build("build", source_tree_hash="tree", artifact_manifest_hash="manifest", pi_version="pi", package_lock_hash="lock", status="active")
            operation = store.create_operation(idempotency_key="migration-op", kind="migration", resource_type="migration", resource_id="migration-resource", actor_type="controller", request={})
            migration_id = new_id("mig")
            store.conn.execute("INSERT INTO migration_runs(migration_id,operation_id,idempotency_key,mode,controller_build_id,request_digest,source_manifest_digest,state,step,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (migration_id, operation.operation_id, "migration-op", "inventory", "build", "request", "source", "planned", "intent", 1, "t", "t"))
            result = create_migration_mapping(store, migration_id=migration_id, record_id="record-1", adapter_kind="git", source_kind="repository", source_digest="digest", resource_type="project", disposition="observe", reason_code="observation-only", detail={})
            self.assertEqual(result["record_id"], "record-1")
            with self.assertRaises(Exception):
                store.conn.execute("UPDATE migration_resource_mappings SET disposition='import' WHERE migration_id=? AND record_id=?", (migration_id, "record-1"))
            with self.assertRaises(Exception):
                store.conn.execute("DELETE FROM migration_resource_mappings WHERE migration_id=? AND record_id=?", (migration_id, "record-1"))

    def test_import_requires_target_and_unknown_disposition_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            with self.assertRaises(MigrationUnresolvedError):
                create_migration_mapping(store, migration_id=new_id("mig"), record_id="record", adapter_kind="git", source_kind="repository", source_digest="digest", resource_type="project", disposition="import", reason_code="missing", detail={})
            with self.assertRaises(MigrationUnresolvedError):
                create_migration_mapping(store, migration_id=new_id("mig"), record_id="record", adapter_kind="git", source_kind="repository", source_digest="digest", resource_type="project", disposition="invalid", reason_code="bad", detail={})


if __name__ == "__main__":
    unittest.main()
