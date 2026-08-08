from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.migration_adapters.policy import observe


class PolicyAdapterTests(unittest.TestCase):
    def test_policy_is_normalized_and_hashed(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "policy.json"
            source.write_text('{"version":1,"defaultMode":"isolated","trustedRoots":[],"isolatedRoots":[],"controlPlaneRepositories":[],"protectedBranches":[],"worktreeRoot":null}')
            result = observe(source)
            self.assertEqual(result.state, "observed")
            self.assertTrue(result.records[0].source_digest.startswith("sha256:"))

    def test_malformed_policy_is_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "policy.json"
            source.write_text('{"defaultMode":"unsafe"}')
            self.assertEqual(observe(source).state, "error")


if __name__ == "__main__":
    unittest.main()
