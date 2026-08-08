from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.migration import collect_inventory_v2
from scripts.pi_control.migration_planner import allocate_migration_mappings, create_resolution_manifest
from scripts.pi_control.models import new_id, validate_id
from scripts.pi_control.store import ControllerStore


class MigrationPlanTests(unittest.TestCase):
    def _report(self, root: Path):
        (root / "registry.json").write_text('{"projectId":"legacy"}')
        return collect_inventory_v2({"secretary": root})

    def _migration(self, store: ControllerStore):
        store.register_build("build", source_tree_hash="tree", artifact_manifest_hash="manifest", pi_version="pi", package_lock_hash="lock", status="active")
        operation = store.create_operation(idempotency_key="migration-op", kind="migration", resource_type="migration", resource_id="migration-resource", actor_type="controller", request={})
        migration_id = new_id("mig")
        store.conn.execute("INSERT INTO migration_runs(migration_id,operation_id,idempotency_key,mode,controller_build_id,request_digest,source_manifest_digest,state,step,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (migration_id, operation.operation_id, "migration-op", "inventory", "build", "request", "source", "planned", "intent", 1, "t", "t"))
        return migration_id

    def test_resolution_is_bound_to_inventory_and_allocates_random_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = self._report(root)
            record = report.payload["records"][0]
            decision = {"recordId": record["record_id"], "disposition": "import", "resourceType": "project", "resourceId": None, "reason": "exact Git identity", "expectedDigest": record["source_digest"]}
            resolution = create_resolution_manifest(report, decisions=[decision])
            with ControllerStore(root / "state") as store:
                migration_id = self._migration(store)
                first = allocate_migration_mappings(store, migration_id=migration_id, inventory=report, resolution=resolution)
                second = allocate_migration_mappings(store, migration_id=migration_id, inventory=report, resolution=resolution)
                self.assertEqual(first, second)
                validate_id(first[record["record_id"]], prefix="prj")

    def test_missing_or_stale_decisions_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = self._report(root)
            record = report.payload["records"][0]
            base = {"recordId": record["record_id"], "disposition": "observe", "resourceType": "project", "resourceId": None, "reason": "history", "expectedDigest": record["source_digest"]}
            with self.assertRaises(Exception):
                create_resolution_manifest(report, decisions=[])
            with self.assertRaises(Exception):
                create_resolution_manifest(report, decisions=[{**base, "expectedDigest": "sha256:" + "0" * 64}])


if __name__ == "__main__":
    unittest.main()
