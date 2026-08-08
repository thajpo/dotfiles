from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
try:
    from .aggregate import aggregate_evidence
    from .evidence import Evidence, aggregate, validate_evidence, write_evidence
    from .fixture import SystemFixture
except ImportError:
    from aggregate import aggregate_evidence
    from evidence import Evidence, aggregate, validate_evidence, write_evidence
    from fixture import SystemFixture

class EvidenceTests(unittest.TestCase):
    def test_evidence_schema_and_exit_propagation(self):
        value = Evidence("SYS-1", ("HA-001",), "PASS", "T0", {"unchanged": True}, commands=({"argv": ["fixture"], "returncode": 0, "stdoutDigest": "sha256:" + "0" * 64, "stderrDigest": "sha256:" + "0" * 64},), before={"state": "before"}, after={"state": "after"}, capability={"fixture": True}).as_dict()
        self.assertEqual(validate_evidence(value)["status"], "PASS")
        self.assertEqual(aggregate(["PASS", "STOP"]), 77)
        self.assertEqual(aggregate(["PASS", "FAIL"]), 1)
        self.assertEqual(aggregate([]), 77)
        self.assertEqual(aggregate(["SKIP"]), 77)
        self.assertEqual(aggregate_evidence([value, {**value, "status": "STOP"}])["exitCode"], 77)
    def test_fixture_does_not_escape(self):
        with SystemFixture.create() as fixture:
            (fixture.home / "test").write_text("fixture")
            fixture.assert_host_unchanged()
    def test_immutable_evidence_write(self):
        with tempfile.TemporaryDirectory() as directory:
            value = Evidence("SYS-1", ("HA-001",), "STOP", "T5", {}, reason="missing").as_dict()
            path = write_evidence(value, Path(directory) / "evidence.json")
            with self.assertRaises(FileExistsError): write_evidence(value, path)

if __name__ == "__main__": unittest.main()
