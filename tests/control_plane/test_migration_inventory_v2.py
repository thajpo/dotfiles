from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.migration import collect_inventory_v2, load_inventory_v2, write_inventory_v2


class MigrationInventoryV2Tests(unittest.TestCase):
    def test_every_adapter_state_is_present_and_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sessions").mkdir()
            (root / "sessions" / "root.jsonl").write_text('{"sessionId":"s"}\n')
            sources = {"root_sessions": root / "sessions", "secretary": root / "sessions", "routes_leases": root / "sessions", "artifacts": root / "sessions", "backups": root / "sessions"}
            first = collect_inventory_v2(sources)
            second = collect_inventory_v2(sources)
            self.assertEqual(first.digest, second.digest)
            self.assertEqual({item["adapterKind"] for item in first.payload["adapterStates"]}, {"git", "root_sessions", "secretary", "routes_leases", "artifacts", "processes", "docker", "tmux", "herdr", "installed_build", "policy", "backups"})
            self.assertEqual(first.payload["schemaVersion"], 2)
            self.assertTrue(first.inventory_id.startswith("inv_"))

    def test_manifest_write_and_load_are_immutable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = collect_inventory_v2({})
            path = root / "inventory.json"
            saved = write_inventory_v2(report, path)
            self.assertEqual(load_inventory_v2(path).digest, report.digest)
            with self.assertRaises(FileExistsError):
                write_inventory_v2(saved, path)


if __name__ == "__main__":
    unittest.main()
