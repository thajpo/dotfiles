from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.greenfield_install import activate, ensure_fresh_state, rollback, stage, verify_stage


class GreenfieldInstallTests(unittest.TestCase):
    def test_stage_activate_and_rollback_preserve_new_root(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as raw:
            root = Path(raw)
            stage_root = root / "stage"
            data_root = root / "data"
            data_root.mkdir(mode=0o700)
            (data_root / "old-command-state").write_text("preserve\n", encoding="utf-8")
            staged = stage(Path(__file__).resolve().parents[1], stage_root)
            self.assertTrue(verify_stage(stage_root)["verified"])
            activated = activate(stage_root, data_root)
            self.assertTrue(activated["activated"])
            self.assertTrue(Path(activated["rollbackRoot"]).is_dir())
            self.assertTrue((data_root / "activation.json").is_file())
            state = ensure_fresh_state(root / "state")
            self.assertTrue(state["fresh"])
            result = rollback(data_root)
            self.assertTrue(result["rolledBack"])
            self.assertTrue((data_root / "old-command-state").is_file())
            self.assertTrue(Path(result["preservedNewRoot"]).is_dir())
            self.assertEqual(staged["buildId"], activated["buildId"])


if __name__ == "__main__":
    unittest.main()
