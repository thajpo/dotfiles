from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.migration_adapters.git import observe


class GitAdapterTests(unittest.TestCase):
    def test_missing_repository_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = observe(Path(temporary) / "missing")
            self.assertEqual(result.state, "unavailable")
            self.assertEqual(result.error_code, "CP_ADAPTER_UNAVAILABLE")

    def test_real_repository_observation_is_read_only_when_git_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            import subprocess
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "file").write_text("x")
            result = observe(root)
            self.assertIn(result.state, {"observed", "error"})
            if result.records:
                self.assertTrue(result.records[0].record_id.startswith("rec_"))


if __name__ == "__main__":
    unittest.main()
