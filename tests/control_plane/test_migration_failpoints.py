from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.migration import InventoryV2Report
from scripts.pi_control.migration_importer import shadow_import_v2
from scripts.pi_control.migration_planner import create_resolution_manifest


class MigrationFailpointTests(unittest.TestCase):
    def test_manifest_boundary_failure_preserves_disposable_intent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = {"schemaVersion": 2, "createdAt": None, "host": {}, "sources": [], "records": [], "relationships": [], "contradictions": [], "adapterStates": []}
            import hashlib, json
            digest = "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            report = InventoryV2Report(payload, digest, "inv_" + "1" * 32)
            resolution = create_resolution_manifest(report, decisions=[])
            class Failpoint:
                def hit(self, name, detail):
                    if name == "shadow.manifest.before": raise RuntimeError("injected")
            with self.assertRaises(RuntimeError):
                shadow_import_v2(report, resolution, root / "shadow", idempotency_key="failure", failpoint=Failpoint())
            self.assertTrue((root / "shadow" / "shadow-import-v2.marker").exists())


if __name__ == "__main__": unittest.main()
