from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PiHelpTests(unittest.TestCase):
    def run_pi(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT / "bin/pi"), *args], text=True, capture_output=True,
            cwd=ROOT, check=False,
        )

    def test_pi_help_is_short_and_memorable(self):
        result = self.run_pi("help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Remember these three:", result.stdout)
        self.assertIn("pi-start all", result.stdout)
        self.assertIn("pi-restart", result.stdout)
        self.assertIn("pi help all", result.stdout)
        self.assertIn("pi --help", result.stdout)
        for command in [
            "pi-start all -mobile", "pi-start all -herdr",
            "pi-start all -mobile -herdr",
        ]:
            self.assertIn(command, result.stdout)
        self.assertIn("flags are per invocation, not persisted", result.stdout)
        self.assertIn("pi-start all` rebuilds it safely", result.stdout)
        self.assertNotIn("Recovery and maintenance:", result.stdout)

    def test_pi_help_all_contains_advanced_reference(self):
        result = self.run_pi("help", "all")
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in [
            "Full workspace reference", "Backend and layout", "Switch and navigate",
            "Secretary projects", "Recovery and maintenance",
            "pi-root-session migrate --dry-run", "pi-sandbox-gc",
        ]:
            self.assertIn(text, result.stdout)

    def test_legacy_help_and_invalid_topics(self):
        legacy = self.run_pi("--help-custom")
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertIn("Remember these three:", legacy.stdout)
        invalid = self.run_pi("help", "unknown")
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("usage: pi help [all]", invalid.stderr)


if __name__ == "__main__":
    unittest.main()
