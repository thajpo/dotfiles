from pathlib import Path
import unittest

from scripts.pisec.first_mate import FIRST_MATE_BRIEF, FIRST_MATE_RESPONSE_CONTRACT


ROOT = Path(__file__).resolve().parents[1]


class FirstMatePromptTests(unittest.TestCase):
    def test_default_response_contract_is_concise_and_action_oriented(self):
        contract = FIRST_MATE_RESPONSE_CONTRACT
        for heading in ("Status", "Needs attention", "Next action"):
            self.assertIn(heading, contract)
        for suppressed in ("healthy or idle project listings", "raw metadata", "timestamps", "event history", "implementation narration"):
            self.assertIn(suppressed, contract)
        self.assertIn("If nothing needs action, say so in one sentence", contract)
        self.assertIn("detailed evidence only when the user explicitly asks for a drill-down", contract)

    def test_brief_preserves_safety_and_response_contract(self):
        self.assertIn(FIRST_MATE_RESPONSE_CONTRACT, FIRST_MATE_BRIEF)
        for safety_rule in (
            "explicit project IDs",
            "Never self-approve worker creation or merges",
            "never write project files",
            "register projects",
            "Do not change lifecycle, Git, or host authority rules",
        ):
            self.assertIn(safety_rule, FIRST_MATE_BRIEF)
    def test_extension_reinforces_default_response_contract(self):
        source = (ROOT / "omp" / "extensions" / "pisec.ts").read_text()
        self.assertIn("Default replies must fit a short screen", source)
        self.assertIn("use only Status, Needs attention, and Next action", source)
        self.assertIn("If nothing needs action, say so in one sentence", source)
        self.assertIn("Give detailed evidence only for explicit drill-down requests", source)


if __name__ == "__main__":
    unittest.main()
