from __future__ import annotations
import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "pi/control-plane/PHASE_11D_CANARY_RUNBOOK.md"

class CanaryRunbookTests(unittest.TestCase):
    def setUp(self):
        self.text = RUNBOOK.read_text(encoding="utf-8")

    def test_blocked_status_and_required_safety_sections(self):
        self.assertIn("Status:** `BLOCKED_AWAITING_CANARY_SELECTION`", self.text)
        self.assertIn("Execution authorization:** `NOT_GRANTED`", self.text)
        required = ("Human decision", "Immutable source", "Controller and migration identity", "Required capabilities", "Quiescence", "Backup and restore", "Staged installation", "Canary launch", "Fault injection", "Evidence and post-state diff", "Rollback and completion", "General rollout prohibition")
        for heading in required:
            self.assertRegex(self.text, re.compile(rf"^## [0-9]+\. .*{re.escape(heading)}", re.MULTILINE), heading)

    def test_all_identity_placeholders_are_unfilled_and_structured(self):
        placeholders = re.findall(r"<[A-Z][A-Z0-9_./:-]*>", self.text)
        self.assertGreaterEqual(len(placeholders), 25)
        for field in ("CANARY_PROJECT_ID", "SOURCE_COMMIT_OID", "CONTROLLER_BUILD_ID", "MIGRATION_ID", "INVENTORY_MANIFEST_ID", "RESOLUTION_PLAN_ID", "BACKUP_MANIFEST_ID", "FAULT_CORPUS_ID"):
            self.assertIn(f"<{field}>", placeholders)
        self.assertNotRegex(self.text, r"(?:sk-[A-Za-z0-9]{20,}|-----BEGIN .*PRIVATE KEY-----|ghp_[A-Za-z0-9]{20,})")

    def test_commands_are_display_only_and_never_shell_executable(self):
        self.assertGreaterEqual(self.text.count("```text"), 4)
        self.assertNotIn("```bash", self.text)
        self.assertNotIn("```sh", self.text)
        for line in self.text.splitlines():
            stripped = line.strip()
            self.assertFalse(stripped.startswith(("pi-host ", "./install.sh", "docker run", "tmux kill", "herdr ")), stripped)
        self.assertIn("DISPLAY ONLY:", self.text)
        self.assertNotIn("subprocess.run", self.text)
        self.assertNotIn("os.system", self.text)

    def test_general_rollout_and_execution_are_explicitly_prohibited(self):
        self.assertIn("General rollout is prohibited", self.text)
        self.assertRegex(self.text, r"No\s+command in this document has been executed")
        self.assertIn("new explicit user request in `pi-host`", self.text)
        self.assertIn("dual writers", self.text.lower())

if __name__ == "__main__":
    unittest.main()
