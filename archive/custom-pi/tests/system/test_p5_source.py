"""P5 release-reachability and Docker-ownership source gates."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class P5SourceTests(unittest.TestCase):
    def test_writer_package_is_channel_broker_only(self) -> None:
        source = (ROOT / "pi/packages/pi-sandbox-control/src/index.ts").read_text(encoding="utf-8")
        for forbidden in (
            "child_process", "spawn(", "spawnSync", "docker", "checkpoint", "PI_TASK_", "route", "worktree",
            "git ", "git\"", "network", "dockerport", "package-lock", "npm ", "container create", "writeFileSync",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source.lower())
        for required in ("writer-tool", 'name: "read"', 'name: "write"', 'name: "edit"', 'name: "bash"', "PI_RUNTIME_MANIFEST", "pi.controllerChannel.v1"):
            self.assertIn(required, source)
        self.assertFalse((ROOT / "pi/packages/pi-sandbox-control/src/legacy-route-binding.ts").exists())

    def test_acceptance_runners_do_not_create_containers_directly(self) -> None:
        for relative in ("tests/system/run-docker.sh", "tests/pi-docker-control-plane-e2e.sh", "tests/system/writer_docker_journey.py"):
            body = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("docker create", body.lower(), relative)
        self.assertIn("create_start_container", (ROOT / "scripts/pi_control/host_supervisor.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
