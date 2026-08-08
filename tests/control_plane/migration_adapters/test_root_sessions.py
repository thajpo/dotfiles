from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.migration_adapters.root_sessions import observe


class RootSessionAdapterTests(unittest.TestCase):
    def test_jsonl_headers_are_observed_without_message_bodies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "root-session.jsonl"
            source.write_text('{"sessionId":"s1","cwd":"/p","token":"secret"}\n{"role":"user","message":"private"}\n')
            result = observe(root)
            self.assertEqual(result.state, "observed")
            record = result.records[0]
            self.assertNotIn("token", record.normalized)
            self.assertNotIn("message", str(record.normalized))
            self.assertTrue(record.record_id.startswith("rec_"))

    def test_missing_and_empty_are_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(observe(root / "missing").state, "unavailable")
            self.assertEqual(observe(root).state, "empty")

    def test_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside.json"
            outside.write_text('{"sessionId":"outside"}')
            (root / "link.json").symlink_to(outside)
            self.assertIn(observe(root).state, {"error", "empty"})


if __name__ == "__main__":
    unittest.main()
