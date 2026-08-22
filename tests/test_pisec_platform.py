import os
import pathlib
import unittest
from unittest.mock import patch

from scripts.pisec.platform import is_linux, is_macos, runtime_root


class PlatformTests(unittest.TestCase):
    def test_platform_detection(self):
        with patch("scripts.pisec.platform.platform.system", return_value="Linux"), patch("scripts.pisec.platform.sys.platform", "linux"):
            self.assertTrue(is_linux())
            self.assertFalse(is_macos())
        with patch("scripts.pisec.platform.platform.system", return_value="Darwin"), patch("scripts.pisec.platform.sys.platform", "darwin"):
            self.assertTrue(is_macos())
            self.assertFalse(is_linux())

    def test_runtime_root_prefers_explicit_override(self):
        with patch.dict(os.environ, {"PISEC_RUNTIME_ROOT": "/opt/pisec-runtime"}):
            self.assertEqual(runtime_root(), pathlib.Path("/opt/pisec-runtime"))

    def test_runtime_root_linux_default_uses_run_user(self):
        env = {"XDG_RUNTIME_DIR": "/run/user/4242"}
        with patch("scripts.pisec.platform.is_macos", return_value=False), patch.dict(os.environ, env, clear=True):
            self.assertEqual(str(runtime_root()), "/run/user/4242/pisec")

    def test_runtime_root_linux_fallback_uses_uid(self):
        with patch("scripts.pisec.platform.is_macos", return_value=False), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(str(runtime_root()), f"/run/user/{os.getuid()}/pisec")

    def test_runtime_root_macos_defaults_under_state_root(self):
        home = os.path.expanduser("~")
        with patch("scripts.pisec.platform.is_macos", return_value=True), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(str(runtime_root()), f"{home}/.local/state/pisec/runtime")

    def test_runtime_root_macos_honors_xdg_runtime_dir(self):
        with patch("scripts.pisec.platform.is_macos", return_value=True), patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/Users/j/run"}, clear=True):
            self.assertEqual(str(runtime_root()), "/Users/j/run/pisec")


if __name__ == "__main__":
    unittest.main()
