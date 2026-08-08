"""Phase 3 legacy inventory tests."""

from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest

from scripts.pi_control.legacy_inventory import inventory_file, inventory_legacy


class LegacyInventoryTests(unittest.TestCase):
    def test_inventory_hashes_and_parses_json_without_following_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "legacy"
            root.mkdir()
            first = root / "one.json"
            second = root / "two.json"
            first.write_text('{"workingCopy":"/same","oid":"one"}\n', encoding="utf-8")
            second.write_text('{"workingCopy":"/same","oid":"two"}\n', encoding="utf-8")
            link = root / "link.json"
            link.symlink_to(first)
            result = inventory_legacy([root])
            self.assertEqual(result["recordCount"], 3)
            self.assertTrue(result["contradictions"])
            self.assertTrue(all(record["observed_at"] for record in result["records"]))
            link_record = inventory_file(link)
            self.assertEqual(link_record.file_type, "symlink")
            self.assertIsNone(link_record.sha256)

    def test_inventory_is_bounded_and_default_is_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "legacy"
            root.mkdir()
            (root / "too-large.bin").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
            result = inventory_legacy([root])
            self.assertEqual(result["recordCount"], 1)
            self.assertEqual(result["records"][0]["parser"], "oversize")
            self.assertEqual(inventory_legacy()["recordCount"], 0)

    def test_invalid_json_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            result = inventory_file(path)
            self.assertEqual(result.parser, "invalid")
            self.assertIsNotNone(result.error)


if __name__ == "__main__":
    unittest.main()
