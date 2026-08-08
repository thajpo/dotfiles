from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.migration import InventoryV2Report
from scripts.pi_control.migration_importer import shadow_import_v2
from scripts.pi_control.migration_planner import create_resolution_manifest
from scripts.pi_control.migration_reconcile import reconcile_shadow_v2
from scripts.pi_control.store import ControllerStore


class ShadowReconcileV2Tests(unittest.TestCase):
    def test_exact_import_reconciles_and_tamper_becomes_attention(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = {"schemaVersion": 2, "createdAt": None, "host": {}, "sources": [], "records": [{"record_id": "rec_" + "1" * 32, "adapter_kind": "git", "source_kind": "git-repository", "source_locator": "/repo", "source_digest": "sha256:" + "2" * 64, "resource_kind": "project-observation", "identity": {}, "normalized": {"common_dir": "/repo/.git", "top_level": "/repo", "object_format": "sha1"}, "observation_state": "observed"}], "relationships": [], "contradictions": [], "adapterStates": []}
            import hashlib, json
            digest = "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            report = InventoryV2Report(payload, digest, "inv_" + "3" * 32)
            resolution = create_resolution_manifest(report, decisions=[{"recordId": "rec_" + "1" * 32, "disposition": "import", "resourceType": "project", "resourceId": None, "reason": "exact", "expectedDigest": "sha256:" + "2" * 64}])
            result = shadow_import_v2(report, resolution, root / "shadow", idempotency_key="import")
            with ControllerStore(root / "shadow") as store:
                matched = reconcile_shadow_v2(store, report, resolution, migration_id=result["migrationId"])
                self.assertEqual(matched["state"], "matched")
                store.conn.execute("UPDATE projects SET primary_checkout='/tampered'")
                attention = reconcile_shadow_v2(store, report, resolution, migration_id=result["migrationId"])
                self.assertEqual(attention["state"], "needs_attention")
                self.assertEqual(store.conn.execute("SELECT state FROM attention WHERE attention_id=?", ("attention_" + result["migrationId"],)).fetchone()[0], "open")


if __name__ == "__main__":
    unittest.main()
