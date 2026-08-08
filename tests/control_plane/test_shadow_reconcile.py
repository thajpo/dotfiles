from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.migration import collect_inventory, shadow_import, shadow_reconcile
from scripts.pi_control.store import ControllerStore


class ShadowReconcileTests(unittest.TestCase):
    def test_comparison_is_read_only_and_reports_missing_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "legacy.json").write_text('{"sessionId":"legacy-session"}\n', encoding="utf-8")
            report = collect_inventory([root])
            state = root / "shadow"
            shadow_import(report, state)
            with ControllerStore(state) as store:
                before = store.conn.execute("SELECT COUNT(*) FROM control_events").fetchone()[0]
                result = shadow_reconcile(store, report)
                after = store.conn.execute("SELECT COUNT(*) FROM control_events").fetchone()[0]
            self.assertEqual(result["state"], "matched")
            self.assertEqual(before, after)
            self.assertEqual(result["sourceManifestDigest"], report.digest)

    def test_changed_source_is_not_silently_compared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "legacy.json"
            source.write_text('{"sessionId":"one"}\n', encoding="utf-8")
            report = collect_inventory([root])
            source.write_text('{"sessionId":"two"}\n', encoding="utf-8")
            state = root / "shadow"
            with ControllerStore(state) as store:
                with self.assertRaises(RuntimeError):
                    shadow_reconcile(store, report)


if __name__ == "__main__":
    unittest.main()
