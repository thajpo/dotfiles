from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.migration_adapters.secretary import observe


class SecretaryAdapterTests(unittest.TestCase):
    def test_records_are_deterministic_and_redacted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "secretary-registry.json"
            source.write_text('{"projectId":"legacy","workstreamId":"ws-old","environment":"private","command":"rm -rf"}')
            first = observe(root)
            second = observe(root)
            self.assertEqual(first.records[0].record_id, second.records[0].record_id)
            self.assertNotIn("environment", first.records[0].normalized)
            self.assertNotIn("command", first.records[0].normalized)

    def test_oversized_file_is_typed_observation_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "registry.json"
            source.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
            result = observe(Path(temporary))
            self.assertEqual(result.state, "error")
            self.assertIsNotNone(result.error_code)


if __name__ == "__main__":
    unittest.main()
