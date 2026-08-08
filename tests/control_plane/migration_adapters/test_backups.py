from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.migration_adapters.backups import observe


class BackupAdapterTests(unittest.TestCase):
    def test_backup_manifest_is_observed_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "backup.json"
            source.write_text('{"generation":"g1","path":"/backup"}')
            before = source.read_bytes()
            result = observe(root)
            self.assertEqual(result.state, "observed")
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
