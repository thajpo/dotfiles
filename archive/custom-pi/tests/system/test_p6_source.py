"""P6 release reachability and authority source gates."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class P6SourceTests(unittest.TestCase):
    def test_model_resources_have_no_approval_execution_or_host_shell_path(self) -> None:
        catalog = json.loads((ROOT / "pi/pi-resources.v1.json").read_text(encoding="utf-8"))
        self.assertIn("bin/pi-authorize", catalog["launchers"])
        for role in catalog["roles"]:
            self.assertNotIn("bin/pi-authorize", role["resources"])
        for profile in catalog["hostLaunchProfiles"]:
            self.assertNotIn("host_command", profile["tools"])
            self.assertNotIn("shell", profile["tools"])
        cli = (ROOT / "scripts/pi_control/pi_cli.py").read_text(encoding="utf-8")
        self.assertNotIn('(\"request\", \"authorize\"', cli)
        self.assertNotIn('"command.authorize"', (ROOT / "scripts/pi_control/pi_protocol.py").read_text(encoding="utf-8"))

    def test_sensitive_extensions_are_channel_only_and_identity_free(self) -> None:
        for relative in ("project-messages", "project-commands", "dependency-review"):
            source = (ROOT / f"pi/extensions/{relative}/index.ts").read_text(encoding="utf-8")
            for forbidden in ("pi.exec", "child_process", "PI_SYSTEM_PROJECT_ID", "PI_SYSTEM_CONVERSATION_ID", "PI_SYSTEM_RUN_ID", "PI_SYSTEM_WRITER_GENERATION", "pi-control"):
                self.assertNotIn(forbidden, source)
            self.assertIn("pi.controllerChannel.v1", source)
        commands = (ROOT / "pi/extensions/project-commands/index.ts").read_text(encoding="utf-8")
        for forbidden_tool in ("approve_project", "authorize_project", "execute_project", "integrate_project"):
            self.assertNotIn(forbidden_tool, commands)

    def test_docker_lifecycle_and_package_argv_remain_controller_owned(self) -> None:
        command_source = (ROOT / "scripts/pi_control/command_requests.py").read_text(encoding="utf-8")
        self.assertNotIn('shutil.which("docker"', command_source)
        self.assertIn("run_one_shot_network", command_source)
        package_extension = (ROOT / "pi/extensions/dependency-review/index.ts").read_text(encoding="utf-8")
        self.assertNotIn("argv", package_extension)
        self.assertIn("scripts disabled", package_extension)
        self.assertFalse((ROOT / "scripts/pi_control/network_runner.py").exists())


if __name__ == "__main__":
    unittest.main()
