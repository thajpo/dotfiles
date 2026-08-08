from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.pi_control.migration import collect_inventory, inventory


class MigrationInventoryTests(unittest.TestCase):
    def test_inventory_is_explicit_hashed_and_deterministic_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "legacy"
            root.mkdir()
            (root / "one.json").write_text('{"projectId":"p1","path":"one"}\n', encoding="utf-8")
            (root / "two.json").write_text('{"projectId":"p1","path":"two"}\n', encoding="utf-8")
            before = hashlib.sha256((root / "one.json").read_bytes()).hexdigest()
            first = collect_inventory([root])
            second = collect_inventory([root])
            self.assertEqual(first.digest, second.digest)
            self.assertTrue(first.contradictions)
            entry = next(item for item in first.payload["entries"] if item.get("path", "").endswith("one.json"))
            self.assertEqual(entry["sha256"], "sha256:" + before)
            self.assertIn("mode", entry)
            self.assertEqual((root / "one.json").read_bytes(), b'{"projectId":"p1","path":"one"}\n')
            self.assertIn("docker", {item["sourceType"] for item in first.payload["adapterStates"]})
            self.assertTrue(first.payload["unmigratedResources"])
            destination = Path(temporary) / "staging" / "source-inventory.json"
            saved = inventory([root], destination=destination)
            self.assertEqual(saved.digest, first.digest)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o400)
            self.assertFalse((root / ".git").exists())

    def test_jsonl_identity_contradictions_are_flattened_and_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "legacy"
            root.mkdir()
            (root / "first.jsonl").write_text('{"sessionId":"same","cwd":"one"}\n', encoding="utf-8")
            (root / "second.jsonl").write_text('{"sessionId":"same","cwd":"two"}\n', encoding="utf-8")
            report = collect_inventory([root])
            self.assertTrue(any(item["identity"]["kind"] == "sessionId" for item in report.contradictions))
            entries = [item for item in report.payload["entries"] if item.get("kind") == "file"]
            self.assertTrue(all(isinstance(identity, dict) for entry in entries for identity in entry.get("identities", [])))

    def test_symlink_is_recorded_and_source_change_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "legacy"
            root.mkdir()
            (root / "record.json").write_text('{"sessionId":"s1"}\n', encoding="utf-8")
            (root / "link").symlink_to("record.json")
            report = collect_inventory([root])
            self.assertTrue(any(item["kind"] == "symlink" for item in report.payload["entries"]))
            (root / "record.json").write_text('{"sessionId":"s2"}\n', encoding="utf-8")
            with self.assertRaises(RuntimeError):
                report.verify_sources()

    def test_new_symlink_parent_is_rejected_before_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "legacy"
            nested = root / "nested"
            nested.mkdir(parents=True)
            source = nested / "record.json"
            source.write_text('{"sessionId":"s1"}\n', encoding="utf-8")
            report = collect_inventory([root])
            replacement = Path(temporary) / "replacement"
            replacement.mkdir()
            (replacement / "record.json").write_text('{"sessionId":"redirected"}\n', encoding="utf-8")
            nested.rename(root / "nested-real")
            (root / "nested").symlink_to(replacement, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                report.verify_sources()


if __name__ == "__main__":
    unittest.main()
