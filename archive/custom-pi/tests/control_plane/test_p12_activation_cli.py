"""P12 activation CLI source tests: TTY gate, test-fixture approval, digest binding."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from scripts.pi_control.activation_cli import main


class ActivationCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp", prefix="p12-cli-")
        self.root = Path(self.temporary.name)
        self.stage = self.root / "stage"
        self.data = self.root / "data"
        self.state_root = self.root / "state"

    def tearDown(self) -> None:
        os.environ.pop("PI_ACTIVATE_TEST_FIXTURE", None)
        self.temporary.cleanup()

    def _stage(self) -> dict:
        from tests.system.staged_install import install
        return install(self.stage)

    def _marker(self) -> None:
        marker = self.data / ".pi-activate-test-fixture"
        self.data.mkdir(parents=True, exist_ok=True)
        marker.write_text("P12-NONPRODUCTION-TEST-ONLY\n", encoding="ascii")
        marker.chmod(0o600)
        os.environ["PI_ACTIVATE_TEST_FIXTURE"] = "1"

    def _unmark(self) -> None:
        os.environ.pop("PI_ACTIVATE_TEST_FIXTURE", None)
        marker = self.data / ".pi-activate-test-fixture"
        if marker.exists():
            marker.unlink()

    def test_test_fixture_rejects_without_marker(self) -> None:
        self._stage()
        self.assertEqual(main(["--staged-root", str(self.stage), "--data-root", str(self.data), "--state-root", str(self.state_root), "--allow-dirty", "--test-only-decision", "approve"]), 2)

    def test_test_fixture_rejects_wrong_marker_mode(self) -> None:
        self._stage()
        self._marker()
        (self.data / ".pi-activate-test-fixture").chmod(0o644)
        self.assertEqual(main(["--staged-root", str(self.stage), "--data-root", str(self.data), "--state-root", str(self.state_root), "--allow-dirty", "--test-only-decision", "approve"]), 2)
        self._unmark()

    def test_test_fixture_rejects_outside_tmp(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="p12-outside-"))
        try:
            self._stage()
            marker = outside / ".pi-activate-test-fixture"
            marker.write_text("P12-NONPRODUCTION-TEST-ONLY\n", encoding="ascii")
            marker.chmod(0o600)
            os.environ["PI_ACTIVATE_TEST_FIXTURE"] = "1"
            self.assertEqual(main(["--staged-root", str(self.stage), "--data-root", str(outside / "data"), "--state-root", str(outside / "state"), "--allow-dirty", "--test-only-decision", "approve"]), 2)
        finally:
            os.environ.pop("PI_ACTIVATE_TEST_FIXTURE", None)
            shutil.rmtree(outside, ignore_errors=True)

    def test_test_fixture_approve_activates_and_initializes_fresh_state(self) -> None:
        built = self._stage()
        self._marker()
        try:
            result = main(["--staged-root", str(self.stage), "--data-root", str(self.data), "--state-root", str(self.state_root), "--allow-dirty", "--test-only-decision", "approve"])
            self.assertEqual(result, 0)
            self.assertTrue(self.data.is_dir())
            marker_json = json.loads((self.data / "activation.json").read_text())
            self.assertEqual(marker_json["buildId"], built["buildId"])
            self.assertTrue((self.state_root / "control.db").is_file())
            self.assertFalse((self.data / "state" / "control.db").exists())
        finally:
            self._unmark()

    def test_test_fixture_reject_does_not_activate(self) -> None:
        self._stage()
        self._marker()
        try:
            result = main(["--staged-root", str(self.stage), "--data-root", str(self.data), "--state-root", str(self.state_root), "--allow-dirty", "--test-only-decision", "reject"])
            self.assertEqual(result, 0)
            self.assertFalse((self.data / "activation.json").exists())
            self.assertFalse((self.state_root / "control.db").exists())
        finally:
            self._unmark()

    def test_missing_staged_root_fails(self) -> None:
        self._marker()
        try:
            self.assertEqual(main(["--staged-root", str(self.root / "missing"), "--data-root", str(self.data), "--state-root", str(self.state_root), "--allow-dirty", "--test-only-decision", "approve"]), 2)
        finally:
            self._unmark()

    def test_activate_twice_preserves_rollback(self) -> None:
        self._stage()
        self._marker()
        try:
            self.assertEqual(main(["--staged-root", str(self.stage), "--data-root", str(self.data), "--state-root", str(self.state_root), "--allow-dirty", "--test-only-decision", "approve"]), 0)
            second = self.root / "stage2"
            from tests.system.staged_install import install
            install(second)
            self._marker()
            self.assertEqual(main(["--staged-root", str(second), "--data-root", str(self.data), "--state-root", str(self.state_root), "--allow-dirty", "--test-only-decision", "approve"]), 0)
            rollbacks = list(self.root.glob("data.rollback.*"))
            self.assertGreaterEqual(len(rollbacks), 1)
            self.assertTrue((self.data / "activation.json").is_file())
        finally:
            self._unmark()


if __name__ == "__main__":
    unittest.main()
