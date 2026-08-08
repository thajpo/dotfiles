from __future__ import annotations
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]

class ProcessFixtureTests(unittest.TestCase):
    def test_strict_fake_rejects_unknown_argument(self):
        for args in (("unknown",), ("--version", "--bogus")):
            result = subprocess.run([sys.executable, "-m", "tests.system.fake_process", *args], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, args)
    def test_incomplete_process_runner_is_stop_77(self):
        env = dict(os.environ); env.pop("PI_SYSTEM_PROCESS_FIXTURE", None)
        result = subprocess.run(["bash", "tests/system/run-process-fixture.sh", "--group", "launch-session-presentation"], cwd=ROOT, env=env)
        self.assertEqual(result.returncode, 77)
    def test_source_runner_is_not_staging_evidence(self):
        result = subprocess.run(["bash", "tests/system/run-source-gate.sh"], cwd=ROOT)
        self.assertEqual(result.returncode, 0)

    def test_planned_actions_remain_stop_77(self):
        env = dict(os.environ); env["PI_SYSTEM_PROCESS_FIXTURE"] = "1"
        result = subprocess.run(["bash", "tests/system/run-process-fixture.sh", "--group", "migration-admin"], cwd=ROOT, env=env)
        self.assertEqual(result.returncode, 77)

if __name__ == "__main__": unittest.main()
