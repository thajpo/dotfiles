from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.migration import collect_inventory_v2


class MigrationContradictionTests(unittest.TestCase):
    def test_unavailable_is_not_empty_and_duplicate_sources_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.json").write_text('{"projectId":"same"}')
            (root / "b.json").write_text('{"projectId":"same","extra":true}')
            report = collect_inventory_v2({"secretary": root})
            state = {item["adapterKind"]: item["state"] for item in report.payload["adapterStates"]}
            self.assertEqual(state["docker"], "unavailable")
            self.assertEqual(len(report.payload["records"]), 2)
            self.assertIsInstance(report.payload["contradictions"], list)


if __name__ == "__main__": unittest.main()
