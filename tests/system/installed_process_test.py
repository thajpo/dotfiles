#!/usr/bin/env python3
"""Exercise the installed controller executable through a disposable project."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class InstalledProcessTests(unittest.TestCase):
    def test_installed_scoped_read_transcript(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as raw:
            root = Path(raw)
            repository = root / "repo"
            repository.mkdir()
            env = dict(os.environ, GIT_AUTHOR_NAME="pi-test", GIT_AUTHOR_EMAIL="pi-test@example.invalid", GIT_COMMITTER_NAME="pi-test", GIT_COMMITTER_EMAIL="pi-test@example.invalid")
            subprocess.run(["git", "init", "-q", str(repository)], check=True, env=env)
            (repository / "README").write_text("installed process\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "README"], check=True, env=env)
            subprocess.run(["git", "-C", str(repository), "commit", "-qm", "fixture"], check=True, env=env)
            state = root / "state"
            registered = subprocess.run([str(ROOT / "bin/pi-control"), "--state-root", str(state), "project", "register", "--repository", str(repository)], check=True, stdout=subprocess.PIPE, text=True)
            project = json.loads(registered.stdout)
            with subprocess.Popen([str(ROOT / "bin/pi-control"), "--state-root", str(state), "project", "status", project["project_id"]], stdout=subprocess.PIPE, text=True) as status_process:
                status = json.loads(status_process.communicate(timeout=30)[0])
            working_copy = next(item for item in status["workingCopies"] if item["kind"] == "primary")
            evidence = root / "evidence.json"
            driver = subprocess.run(["python3", str(ROOT / "tests/system/fixtures/installed-pi.py"), "--executable", str(ROOT / "bin/pi-control"), "--state-root", str(state), "--project-id", project["project_id"], "--working-copy-id", working_copy["working_copy_id"], "--evidence", str(evidence)], check=True, stdout=subprocess.PIPE, text=True, env=dict(os.environ, PI_SYSTEM_BUILD_ID="test-installed"))
            value = json.loads(driver.stdout)
            self.assertEqual(value["status"], "PASS")
            self.assertTrue(value["evidenceOutsideRepository"])
            self.assertFalse(value["forbiddenTool"]["accepted"])
            self.assertEqual(json.loads(evidence.read_text(encoding="utf-8"))["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
