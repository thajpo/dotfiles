import pathlib
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from scripts import pisec_launchd


class LaunchdRenderTests(unittest.TestCase):
    def make_home(self, root: pathlib.Path, *, zen_key: str | None) -> pathlib.Path:
        home = root / "home"
        (home / ".config/pisec").mkdir(parents=True)
        key_line = f"OPENCODE_API_KEY={zen_key}\n" if zen_key else "OPENCODE_API_KEY=\n"
        (home / ".config/pisec/ports.env").write_text(
            "PISEC_AUTH_BROKER_PORT=9001\nPISEC_AUTH_GATEWAY_PORT=9002\n" + key_line
        )
        (home / ".local/lib/pisec/bin").mkdir(parents=True)
        return home

    def test_renders_four_valid_plists_without_provider_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            home = self.make_home(root, zen_key="k" * 40)
            out = root / "LaunchAgents"
            written = pisec_launchd.render(out, home, root / "dotfiles", None)
            self.assertEqual(len(written), 4)
            by_label = {plistlib.loads(p.read_bytes())["Label"]: plistlib.loads(p.read_bytes()) for p in out.glob("*.plist")}
            self.assertEqual(set(by_label), set(pisec_launchd.LABELS))
            broker = by_label["com.dotfiles.pisec-broker"]
            self.assertNotIn("OPENCODE_API_KEY", broker["EnvironmentVariables"])
            self.assertEqual(broker["EnvironmentVariables"]["PISEC_AUTH_BROKER_PORT"], "9001")
            gateway = by_label["com.dotfiles.pisec-auth-gateway"]
            self.assertNotIn("OPENCODE_API_KEY", gateway["EnvironmentVariables"])
            for definition in by_label.values():
                self.assertTrue(definition["RunAtLoad"])
                self.assertTrue(definition["KeepAlive"])
                self.assertEqual(definition["Umask"], 0o077)

    def test_empty_zen_key_is_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            home = self.make_home(root, zen_key=None)
            out = root / "LaunchAgents"
            pisec_launchd.render(out, home, root / "dotfiles", None)
            broker = plistlib.loads((out / "com.dotfiles.pisec-broker.plist").read_bytes())
            self.assertNotIn("OPENCODE_API_KEY", broker["EnvironmentVariables"])

    def test_herdr_binary_falls_back_to_shim_without_brew_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            home = self.make_home(root, zen_key=None)

            def no_brew(self_path):
                if str(self_path) in {"/opt/homebrew/bin/herdr", "/usr/local/bin/herdr"}:
                    return False
                return original_exists(self_path)

            original_exists = pathlib.Path.exists
            with patch.object(pathlib.Path, "exists", no_brew):
                out = root / "LaunchAgents"
                pisec_launchd.render(out, home, root / "dotfiles", None)
            herdr = plistlib.loads((out / "com.dotfiles.herdr.plist").read_bytes())
            self.assertEqual(herdr["ProgramArguments"], [str(home / ".local/lib/pisec/bin/herdr")])


if __name__ == "__main__":
    unittest.main()
