from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.pi_control.staged_build import create_build_manifest, write_build_manifest
try:
    from .staged_proof import copy_manifest_entries, prove_loaded_root
    from .scenarios.rollback import ROLLBACK_MATRIX
    from .rollback_matrix import run_matrix
except ImportError:
    from staged_proof import copy_manifest_entries, prove_loaded_root
    from scenarios.rollback import ROLLBACK_MATRIX
    from rollback_matrix import run_matrix

ROOT = Path(__file__).resolve().parents[2]

class C10Tests(unittest.TestCase):
    def test_source_stage_loaded_manifest_is_exact(self):
        with tempfile.TemporaryDirectory(prefix="pi-c10-test-") as raw:
            source = Path(raw) / "source"; stage = Path(raw) / "stage"
            source.mkdir(); stage.mkdir(mode=0o700)
            (source / "launcher").write_text("#!/bin/sh\n", encoding="utf-8"); (source / "launcher").chmod(0o750)
            (source / "config.json").write_text("{}\n", encoding="utf-8")
            manifest = create_build_manifest(source, metadata={"test": True}, test_outcomes={"source": "PASS"})
            copy_manifest_entries(source, stage, manifest); write_build_manifest(manifest, stage / "build-manifest.json")
            (stage / "loaded-resources.json").write_text(json.dumps({"buildId": manifest.build_id, "legacyCoLoad": []}), encoding="utf-8")
            result = prove_loaded_root(stage, manifest.build_id)
            self.assertEqual(result["buildId"], manifest.build_id)

    def test_tamper_and_loaded_mismatch_fail(self):
        with tempfile.TemporaryDirectory(prefix="pi-c10-test-") as raw:
            source = Path(raw) / "source"; stage = Path(raw) / "stage"
            source.mkdir(); stage.mkdir(mode=0o700)
            (source / "entry").write_text("good\n", encoding="utf-8")
            manifest = create_build_manifest(source, metadata={}, test_outcomes={})
            copy_manifest_entries(source, stage, manifest); write_build_manifest(manifest, stage / "build-manifest.json")
            (stage / "entry").write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(RuntimeError): prove_loaded_root(stage, manifest.build_id)

    def test_staged_runner_without_installed_attestation_is_stop_77(self):
        result = subprocess.run(["bash", "tests/system/run-staged-installed.sh", "--group", "packages"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 77)

    def test_docker_runner_preserves_unavailable_as_stop_77(self):
        result = subprocess.run(["bash", "tests/system/run-docker.sh"], cwd=ROOT, capture_output=True, text=True)
        if result.returncode == 77:
            self.assertIn("STOP/77", result.stderr)
        else:
            self.assertEqual(result.returncode, 0)

    def test_rollback_gate_preserves_docker_stop(self):
        result = subprocess.run(["bash", "tests/system/run-rollback.sh"], cwd=ROOT, capture_output=True, text=True, timeout=180)
        if result.returncode == 77:
            self.assertTrue("Docker" in result.stdout or "Docker" in result.stderr)
        else:
            self.assertEqual(result.returncode, 0)

    def test_rollback_matrix_preserves_recovery_resources(self):
        self.assertEqual(len(ROLLBACK_MATRIX), 5)
        for item in ROLLBACK_MATRIX:
            self.assertEqual(set(item["preserve"]), {"db", "refs", "worktrees", "evidence"})
        results = run_matrix()
        self.assertEqual(len(results), 5)
        self.assertTrue(all(item["restored"] and item["recoveryPreserved"] for item in results))

if __name__ == "__main__": unittest.main()
