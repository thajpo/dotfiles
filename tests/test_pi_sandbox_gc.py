import hashlib
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pi_sandbox_gc", ROOT / "scripts/pi-sandbox-gc.py")
gc = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(gc)


class SandboxGcOwnershipTests(unittest.TestCase):
    def labels(self, pid="1234", identity="linux:42"):
        return {
            "pi.container-sandbox.owner": pid,
            "pi.container-sandbox.owner-identity": hashlib.sha256(identity.encode()).hexdigest()[:16],
        }

    def test_malformed_owner_metadata_is_unknown_and_not_alive(self):
        self.assertIsNone(gc.owner_status({}))
        self.assertIsNone(gc.owner_status(self.labels(pid="not-a-pid")))
        self.assertFalse(gc.owner_alive({}))

    def test_pid_reuse_is_dead_but_unreadable_identity_is_unknown(self):
        with mock.patch.object(gc.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(gc.owner_status(self.labels()))
        with mock.patch.object(gc.os, "kill", return_value=None), \
             mock.patch.object(gc, "owner_identity", return_value=None):
            self.assertIsNone(gc.owner_status(self.labels()))
        with mock.patch.object(gc.os, "kill", return_value=None), \
             mock.patch.object(gc, "owner_identity", return_value="linux:other"):
            self.assertFalse(gc.owner_status(self.labels()))


if __name__ == "__main__":
    unittest.main()
