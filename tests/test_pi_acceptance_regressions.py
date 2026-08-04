import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PiAcceptanceRegressionTests(unittest.TestCase):
    def test_task_route_creation_time_fails_closed(self):
        patch = (ROOT / "pi/patches/pi-sandbox-0.2.0-task-routing.patch").read_text()
        self.assertIn('typeof createdAt !== "number"', patch)
        self.assertIn("!Number.isFinite(createdAt)", patch)
        self.assertIn("createdAt < 0", patch)
        self.assertIn("createdAt > now", patch)
        self.assertNotIn("Number(route.createdAt ?? 0)", patch)

    def test_installer_removes_group_and_other_write_bits_before_activation(self):
        installer = (ROOT / "install.sh").read_text()
        hardening = 'chmod -R go-w "$STAGING_DIR"'
        activation = 'activate_path "$STAGING_DIR/npm" "$PI_CONFIG_DIR/npm"'
        self.assertIn(hardening, installer)
        self.assertIn(activation, installer)
        self.assertLess(installer.index(hardening), installer.index(activation))


if __name__ == "__main__":
    unittest.main()
