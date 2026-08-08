"""Phase 4 process start-identity observations."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from scripts.pi_control.process_adapter import observe_process, process_start_identity


class ProcessIdentityTests(unittest.TestCase):
    def test_current_process_has_stable_start_identity(self):
        identity = process_start_identity(os.getpid())
        self.assertTrue(identity)
        first = observe_process(os.getpid(), expected_start_identity=identity)
        second = observe_process(os.getpid(), expected_start_identity=identity)
        self.assertTrue(first.exists)
        self.assertEqual(first.start_identity, second.start_identity)
        self.assertNotEqual(first.state, "reused")

    def test_missing_and_reused_identity_are_not_alive(self):
        missing = observe_process(99999999, expected_start_identity="linux:missing:0")
        self.assertIn(missing.state, {"gone", "unknown"})
        current = process_start_identity(os.getpid())
        reused = observe_process(os.getpid(), expected_start_identity=current + ":different")
        self.assertEqual(reused.state, "reused")

    def test_child_identity_can_be_observed_before_exit(self):
        child = subprocess.Popen(["sleep", "2"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            identity = process_start_identity(child.pid)
            observation = observe_process(child.pid, expected_start_identity=identity)
            self.assertTrue(observation.exists)
            self.assertIn(observation.state, {"R", "S", "sleeping"})
        finally:
            child.terminate()
            child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
