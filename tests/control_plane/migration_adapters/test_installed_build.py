from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.migration_adapters.installed_build import observe


class InstalledBuildAdapterTests(unittest.TestCase):
    def test_exact_manifest_is_observed(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "build.json"
            source.write_text('{"buildId":"b1","files":[],"secret":"hidden"}')
            result = observe(source)
            self.assertEqual(result.state, "observed")
            self.assertNotIn("secret", str(result.as_dict()))

    def test_missing_manifest_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(observe(Path(temporary) / "missing.json").state, "unavailable")


if __name__ == "__main__":
    unittest.main()
