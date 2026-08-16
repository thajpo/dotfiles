from __future__ import annotations
from unittest.mock import patch
import subprocess
import unittest

try:
    from .driver import CommandExecutionError, _run
    from .fixture import SystemFixture
except ImportError:
    from driver import CommandExecutionError, _run
    from fixture import SystemFixture

class DriverTests(unittest.TestCase):
    def test_timeout_retains_command_record(self):
        with SystemFixture.create() as fixture:
            with patch("tests.system.driver.subprocess.run", side_effect=subprocess.TimeoutExpired(["hung"], 20, output=b"out", stderr=b"err")):
                with self.assertRaises(CommandExecutionError) as raised:
                    _run(fixture, ["hung"], expected="zero")
            record = raised.exception.record
            self.assertEqual(record.argv, ("hung",))
            self.assertEqual(record.returncode, 124)
            self.assertTrue(record.stdout_digest.startswith("sha256:"))
            self.assertTrue(record.stderr_digest.startswith("sha256:"))

if __name__ == "__main__": unittest.main()
