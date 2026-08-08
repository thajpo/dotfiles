from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.activation import plan_latch, read_latch, verify_latch, write_latch
from scripts.pi_control.activation import ActivationMismatchError, ActivationUnavailableError


class ActivationLatchTests(unittest.TestCase):
    def test_canonical_digest_atomic_permissions_and_verify(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            latch = plan_latch(project_git_identity={"commonDir": "/repo/.git", "objectFormat": "sha1"}, project_id="prj_" + "1" * 32, mode="legacy", activation_resource_version=1, expected_db_path=root / "control.db", controller_schema_version=7, build_id=None, migration_id=None, expected_project_version=1)
            saved = write_latch(latch, root / "activation.v1.json")
            loaded = read_latch(saved.path)
            self.assertEqual(loaded.manifest_digest, latch.manifest_digest)
            self.assertEqual((root.stat().st_mode & 0o777), 0o700)
            self.assertEqual((Path(saved.path).stat().st_mode & 0o777), 0o600)
            verify_latch(loaded, expected_db_path=root / "control.db", project={"project_id": "prj_" + "1" * 32, "resource_version": 1})

    def test_corrupt_missing_and_mismatched_latch_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ActivationUnavailableError): read_latch(root / "missing.json")
            latch = plan_latch(project_git_identity={"commonDir": "/repo/.git"}, project_id="prj_" + "1" * 32, mode="legacy", activation_resource_version=1, expected_db_path=root / "db", controller_schema_version=7, build_id=None, migration_id=None, expected_project_version=1)
            path = write_latch(latch, root / "latch.json").path
            Path(path).write_text('{"mode":"controller"}')
            with self.assertRaises((ActivationUnavailableError, ActivationMismatchError)): read_latch(path)


if __name__ == "__main__": unittest.main()
