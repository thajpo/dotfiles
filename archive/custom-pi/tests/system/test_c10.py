from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.pi_control.pi_install import stage
from tests.test_pi_install import pack_test_pi_core

try:
    from .staged_install import StagedInstallUnavailable, _raise_offline_install_error, build_generation, install
    from .staged_proof import prove_loaded_root
except ImportError:
    from staged_install import StagedInstallUnavailable, _raise_offline_install_error, build_generation, install
    from staged_proof import prove_loaded_root


ROOT = Path(__file__).resolve().parents[2]


class C10Tests(unittest.TestCase):
    def test_compatibility_wrapper_returns_the_production_artifact(self):
        self.assertIs(build_generation, stage)
        with tempfile.TemporaryDirectory(prefix="pi-c10-production-") as raw:
            root = Path(raw)
            core = pack_test_pi_core(root)
            built = install(root / "stage", pi_core_tarball=core)
            loaded = prove_loaded_root(root / "stage", built["buildId"])
            self.assertEqual(loaded["buildId"], built["buildId"])
            with self.assertRaisesRegex(ValueError, "does not match"):
                prove_loaded_root(root / "stage", "build_" + "0" * 32)

    def test_npm_integrity_failure_is_not_unavailable(self):
        integrity = subprocess.CalledProcessError(1, ["npm", "install"], stderr=b"npm ERR! code EINTEGRITY")
        with self.assertRaisesRegex(RuntimeError, "integrity or resolution") as raised:
            _raise_offline_install_error(integrity)
        self.assertNotIsInstance(raised.exception, StagedInstallUnavailable)
        unavailable = subprocess.CalledProcessError(1, ["npm", "install"], stderr=b"npm ERR! code ENOTCACHED")
        with self.assertRaises(StagedInstallUnavailable):
            _raise_offline_install_error(unavailable)
        mixed = subprocess.CalledProcessError(1, ["npm", "install"], stderr=b"npm ERR! code EINTEGRITY while using only-if-cached")
        with self.assertRaisesRegex(RuntimeError, "integrity or resolution") as raised:
            _raise_offline_install_error(mixed)
        self.assertNotIsInstance(raised.exception, StagedInstallUnavailable)

    def test_staged_runner_passes_or_truthfully_reports_missing_prerequisite(self):
        with tempfile.TemporaryDirectory(prefix="pi-c10-evidence-") as evidence:
            result = subprocess.run(["bash", "tests/system/run-staged-installed.sh"], cwd=ROOT, capture_output=True, text=True, env={**os.environ, "PI_SYSTEM_EVIDENCE_DIR": evidence})
        if result.returncode == 77:
            self.assertIn("STOP/77", result.stderr)
        else:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PASS: two identical production builds", result.stdout)

if __name__ == "__main__":
    unittest.main()
