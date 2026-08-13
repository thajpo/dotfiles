#!/usr/bin/env python3
"""Exercise the installed controller executable through a disposable project."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from .evidence import validate_evidence


ROOT = Path(__file__).resolve().parents[2]


class InstalledProcessTests(unittest.TestCase):
    def test_scoped_read_is_inert_without_controller_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = root / "repo"
            repository.mkdir()
            node = shutil.which("node")
            pi_core = Path(os.environ.get("PI_CORE_DIR", Path.home() / ".local/share/pi/core"))
            pi_cli = pi_core / "node_modules/@earendil-works/pi-coding-agent/dist/cli.js"
            if node is None or not pi_cli.is_file():
                self.skipTest("pinned local Pi process is unavailable")
            home = root / "home"
            agent = home / ".pi/agent"
            agent.mkdir(parents=True)
            command = [
                node, str(pi_cli), "--mode", "json", "--offline", "--no-approve", "--no-extensions",
                "-e", str(ROOT / "pi/extensions/scoped-project-read/index.ts"),
                "-e", str(ROOT / "tests/system/fixtures/scripted-provider.ts"),
                "--no-builtin-tools", "--no-session", "--no-skills", "--no-prompt-templates",
                "--no-context-files", "--no-themes", "--model", "scripted/scripted-1",
                "verify no controller scope",
            ]
            env = {"PATH": os.defpath, "HOME": str(home), "PI_CODING_AGENT_DIR": str(agent), "LANG": "C", "LC_ALL": "C"}
            result = subprocess.run(command, cwd=repository, env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            records = [json.loads(line) for line in result.stdout.splitlines() if line]
            final = next(record["message"] for record in reversed(records) if record.get("type") == "message_end" and record.get("message", {}).get("role") == "assistant")
            self.assertEqual(final["content"], [{"type": "text", "text": "NO_SCOPE_FINAL"}])

    def test_scoped_read_rejects_partial_controller_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            node = shutil.which("node")
            pi_core = Path(os.environ.get("PI_CORE_DIR", Path.home() / ".local/share/pi/core"))
            pi_cli = pi_core / "node_modules/@earendil-works/pi-coding-agent/dist/cli.js"
            if node is None or not pi_cli.is_file():
                self.skipTest("pinned local Pi process is unavailable")
            home = root / "home"
            agent = home / ".pi/agent"
            agent.mkdir(parents=True)
            command = [
                node, str(pi_cli), "--mode", "json", "--offline", "--no-extensions",
                "-e", str(ROOT / "pi/extensions/scoped-project-read/index.ts"),
                "-e", str(ROOT / "tests/system/fixtures/scripted-provider.ts"),
                "--no-builtin-tools", "--no-session", "--no-skills", "--no-prompt-templates",
                "--no-context-files", "--no-themes", "--model", "scripted/scripted-1",
                "verify no controller scope",
            ]
            env = {"PATH": os.defpath, "HOME": str(home), "PI_CODING_AGENT_DIR": str(agent), "LANG": "C", "LC_ALL": "C", "PI_SYSTEM_PROJECT_ID": "prj_" + "1" * 32}
            result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            records = [json.loads(line) for line in result.stdout.splitlines() if line]
            final = next(record["message"] for record in reversed(records) if record.get("type") == "message_end" and record.get("message", {}).get("role") == "assistant")
            self.assertEqual(final["content"], [{"type": "text", "text": "NO_SCOPE_FINAL"}])

    def test_installed_scoped_read_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            staged_value = os.environ.get("PI_SYSTEM_STAGED_ROOT")
            if not staged_value:
                self.skipTest("exact registered staged generation is required")
            staged_root = Path(staged_value).resolve(strict=True)
            controller = staged_root / "bin/pi-control"
            launcher = staged_root / "bin/pi-system-secretary"
            if not controller.is_file() or not launcher.is_file():
                self.skipTest("staged P3 launch surface is unavailable")
            repository = root / "repo"
            repository.mkdir()
            env = dict(os.environ, GIT_AUTHOR_NAME="pi-test", GIT_AUTHOR_EMAIL="pi-test@example.invalid", GIT_COMMITTER_NAME="pi-test", GIT_COMMITTER_EMAIL="pi-test@example.invalid")
            subprocess.run(["git", "init", "-q", str(repository)], check=True, env=env)
            (repository / "README").write_text("installed process\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "README"], check=True, env=env)
            subprocess.run(["git", "-C", str(repository), "commit", "-qm", "fixture"], check=True, env=env)
            state = root / "state"
            build = json.loads(subprocess.run([str(controller), "--state-root", str(state), "build", "register", "--staged-root", str(staged_root)], check=True, stdout=subprocess.PIPE, text=True).stdout)
            registered = subprocess.run([str(controller), "--state-root", str(state), "project", "register", "--repository", str(repository)], check=True, stdout=subprocess.PIPE, text=True)
            project = json.loads(registered.stdout)
            with subprocess.Popen([str(controller), "--state-root", str(state), "project", "status", project["project_id"]], stdout=subprocess.PIPE, text=True) as status_process:
                status = json.loads(status_process.communicate(timeout=30)[0])
            working_copy = next(item for item in status["workingCopies"] if item["kind"] == "primary")
            conversation = next(item for item in status["conversations"] if item["role"] == "secretary")
            evidence = root / "evidence.json"
            driver = subprocess.run([
                "python3", str(ROOT / "tests/system/fixtures/installed-pi.py"),
                "--launcher", str(launcher),
                "--state-root", str(state),
                "--project-id", project["project_id"],
                "--working-copy-id", working_copy["working_copy_id"],
                "--conversation-id", conversation["conversation_id"],
                "--pi-session-id", conversation["pi_session_id"],
                "--session-file", conversation["session_file"],
                "--provider", str(ROOT / "tests/system/fixtures/scripted-provider.ts"),
                "--probe", str(ROOT / "tests/system/loaded_resource_probe.ts"),
                "--repository", str(repository),
                "--build-id", build["build_id"],
                "--staged-root", str(staged_root),
                "--evidence", str(evidence),
            ], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(driver.returncode, 0, driver.stderr)
            value = json.loads(driver.stdout)
            self.assertEqual(value["status"], "PASS")
            self.assertTrue(value["assertions"]["evidenceOutsideRepository"])
            self.assertTrue(value["assertions"]["rejectedUnavailableWrite"])
            self.assertEqual(value["before"], value["after"])
            self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)
            self.assertEqual(validate_evidence(json.loads(evidence.read_text(encoding="utf-8")))["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
