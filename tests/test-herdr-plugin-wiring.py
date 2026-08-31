from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "bin" / "herdr-plugin-wiring"
FRAGMENT = ROOT / "herdr" / "plugin-action-keys.toml"
VERSIONS = ROOT / "herdr" / "plugin-versions.toml"

EXPECTED_ACTIONS = {
    "annotate.capture": "prefix+a",
    "annotate.copy-context": "prefix+shift+a",
    "annotate.manage": "prefix+m",
    "annotate.open": "prefix+d",
    "annotate.last": "prefix+shift+l",
    "cloudmanic.herdr-plus.projects": "prefix+up",
    "cloudmanic.herdr-plus.quick-actions": "prefix+down",
}

HERDR_082_DEFAULT_KEYS = {
    "prefix+?", "prefix+s", "prefix+q", "prefix+shift+r", "prefix+o",
    "prefix+w", "prefix+g", "prefix+shift+n", "prefix+shift+g",
    "prefix+shift+w", "prefix+shift+d", "prefix+c", "prefix+shift+t",
    "prefix+p", "prefix+n", "prefix+1..9", "prefix+shift+x",
    "prefix+shift+p", "prefix+e", "prefix+h", "prefix+j", "prefix+k",
    "prefix+l", "prefix+tab", "prefix+shift+tab", "prefix+v",
    "prefix+minus", "prefix+x", "prefix+z", "prefix+r", "prefix+b",
}

BASE_CONFIG = '''onboarding = false
[theme]
name = "catppuccin"

[keys]
prefix = "ctrl+space"

# >>> dotfiles Herdr plugin keys >>>
[[keys.command]]
key = "prefix+shift+e"
type = "plugin_action"
command = "chmarax.herdr-nvim.toggle"

[[keys.command]]
key = "prefix+shift+v"
type = "plugin_action"
command = "persiyanov.reviewr.toggle"
# <<< dotfiles Herdr plugin keys <<<

[ui]
status_indicators = "dots"
'''


class HerdrPluginWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Path(self.temporary.name) / "config.toml"
        self.config.write_text(BASE_CONFIG, encoding="utf-8")

    def run_helper(self, operation: str, *, config: Path | None = None) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["HERDR_CONFIG_PATH"] = str(config or self.config)
        environment["HERDR_ENV"] = "1"
        return subprocess.run(
            [str(HELPER), operation],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )

    def test_reviewed_versions_and_action_ids_are_exact(self) -> None:
        versions = tomllib.loads(VERSIONS.read_text(encoding="utf-8"))
        self.assertEqual(versions["reviewed_herdr_version"], "0.8.2")
        self.assertEqual(
            versions["plugins"],
            [
                {
                    "source": "cloudmanic/herdr-plus",
                    "id": "cloudmanic.herdr-plus",
                    "version": "0.1.24",
                    "commit": "0ede9c763e0feb7800b6d2e3a7401f9198684caf",
                },
                {
                    "source": "plannotator/herdr-annotate",
                    "id": "annotate",
                    "version": "0.3.0",
                    "commit": "ba4903b28fbb77dd0a4bc55a4a7ba3c1ef0913ea",
                },
            ],
        )

        fragment = tomllib.loads(FRAGMENT.read_text(encoding="utf-8"))
        actions = {entry["command"]: entry["key"] for entry in fragment["keys"]["command"]}
        self.assertEqual(actions, EXPECTED_ACTIONS)
        self.assertTrue(all(entry["type"] == "plugin_action" for entry in fragment["keys"]["command"]))

    def test_bindings_do_not_collide_with_defaults_or_existing_dotfiles_keys(self) -> None:
        self.assertFalse(set(EXPECTED_ACTIONS.values()) & HERDR_082_DEFAULT_KEYS)
        self.assertNotIn("prefix+o", EXPECTED_ACTIONS.values())
        self.assertEqual(EXPECTED_ACTIONS["annotate.open"], "prefix+d")
        self.assertEqual(EXPECTED_ACTIONS["annotate.last"], "prefix+shift+l")
        existing = {"prefix+shift+e", "prefix+shift+f", "prefix+shift+v"}
        self.assertFalse(set(EXPECTED_ACTIONS.values()) & existing)

    def test_apply_is_idempotent_preserves_unrelated_config_and_rolls_back_exactly(self) -> None:
        first = self.run_helper("apply")
        self.assertEqual(first.returncode, 0, first.stderr)
        applied = self.config.read_text(encoding="utf-8")
        self.assertIn("# >>> dotfiles Herdr plugin keys >>>", applied)
        self.assertIn("# >>> dotfiles Annotate and Herdr Plus keys >>>", applied)
        parsed = tomllib.loads(applied)
        actions = {entry["command"]: entry["key"] for entry in parsed["keys"]["command"]}
        self.assertEqual(actions["chmarax.herdr-nvim.toggle"], "prefix+shift+e")
        self.assertEqual(actions["persiyanov.reviewr.toggle"], "prefix+shift+v")
        for action, key in EXPECTED_ACTIONS.items():
            self.assertEqual(actions[action], key)

        before_stat = self.config.stat()
        second = self.run_helper("apply")
        after_stat = self.config.stat()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already current", second.stdout)
        self.assertEqual(before_stat.st_ino, after_stat.st_ino)
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)
        self.assertEqual(self.config.read_text(encoding="utf-8"), applied)

        check = self.run_helper("check")
        self.assertEqual(check.returncode, 0, check.stderr)

        herdr = shutil.which("herdr")
        if herdr:
            environment = os.environ.copy()
            environment["HERDR_CONFIG_PATH"] = str(self.config)
            checked = subprocess.run(
                [herdr, "config", "check"], env=environment, text=True, capture_output=True
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)

        removed = self.run_helper("remove")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), BASE_CONFIG)
        removed_again = self.run_helper("remove")
        self.assertEqual(removed_again.returncode, 0, removed_again.stderr)
        self.assertIn("already absent", removed_again.stdout)

    def test_apply_refuses_external_key_collision_without_writing(self) -> None:
        collision = BASE_CONFIG + '''
[[keys.command]]
key = "prefix+d"
type = "plugin_action"
command = "example.existing"
'''
        self.config.write_text(collision, encoding="utf-8")
        result = self.run_helper("apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("managed keys collide with existing config: prefix+d", result.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), collision)

    def test_apply_refuses_explicit_builtin_collision_without_writing(self) -> None:
        collision = BASE_CONFIG.replace(
            'prefix = "ctrl+space"',
            'prefix = "ctrl+space"\nopen_notification_target = "prefix+d"',
        )
        self.config.write_text(collision, encoding="utf-8")
        result = self.run_helper("apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("managed keys collide with existing config: prefix+d", result.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), collision)

    def test_apply_refuses_unmatched_markers_without_writing(self) -> None:
        malformed = BASE_CONFIG + "\n# >>> dotfiles Annotate and Herdr Plus keys >>>\n"
        self.config.write_text(malformed, encoding="utf-8")
        result = self.run_helper("apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected one matched managed block", result.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), malformed)

    def test_missing_herdr_environment_fails_before_reading_config(self) -> None:
        environment = os.environ.copy()
        environment.pop("HERDR_ENV", None)
        environment["HERDR_CONFIG_PATH"] = str(self.config)
        result = subprocess.run(
            [str(HELPER), "check"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HERDR_ENV=1 is required", result.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), BASE_CONFIG)


if __name__ == "__main__":
    unittest.main()
