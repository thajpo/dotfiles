from pathlib import Path
import unittest

from scripts.pisec.first_mate import FIRST_MATE_BRIEF, FIRST_MATE_RESPONSE_CONTRACT


ROOT = Path(__file__).resolve().parents[1]


class FirstMatePromptTests(unittest.TestCase):
    def test_default_response_contract_is_medium_detail_and_action_oriented(self):
        contract = FIRST_MATE_RESPONSE_CONTRACT
        for section in (
            "Goal and current position",
            "Implementation completed",
            "How the important parts work",
            "Verification and confidence",
            "Remaining work, risks, and next action",
        ):
            self.assertIn(section, contract)
        for required in ("cross-project activity", "platform problems", "ownership", "consequences", "required decisions"):
            self.assertIn(required, contract)
        self.assertNotIn("short screen", contract)
        self.assertNotIn("implementation narration", contract)

    def test_brief_preserves_safety_and_response_contract(self):
        self.assertIn(FIRST_MATE_RESPONSE_CONTRACT, FIRST_MATE_BRIEF)
        for safety_rule in (
            "configured First Mate fleet scope",
            "explicit project IDs",
            "Never self-approve worker creation or workstream acceptance",
            "never write project files",
            "register projects",
            "Do not change lifecycle, Git, or host authority rules",
        ):
            self.assertIn(safety_rule, FIRST_MATE_BRIEF)
    def test_extension_reinforces_default_response_contract(self):
        source = (ROOT / "omp" / "extensions" / "pisec-prompts.ts").read_text()
        self.assertIn("medium-detail senior-engineering briefing", source)
        self.assertIn("Goal and current position", source)
        self.assertIn("Remaining work, risks, and next action", source)
        self.assertNotIn("Default replies must fit a short screen", source)
        self.assertNotIn("use only Status, Needs attention, and Next action", source)

    def test_first_mate_brief_has_no_worker_creation_tool_instructions(self):
        self.assertIn("Route engineering work to the correct in-scope project Secretary", FIRST_MATE_BRIEF)
        self.assertNotIn("pisec_prepare_workstream", FIRST_MATE_BRIEF)
        self.assertNotIn("pisec_create_workstream", FIRST_MATE_BRIEF)


if __name__ == "__main__":
    unittest.main()
