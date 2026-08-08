from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

try:
    from . import validate_plan_docs as validator
except ImportError:  # unittest discovery with tests/system as top-level start dir
    import validate_plan_docs as validator


ROOT = Path(__file__).resolve().parents[2]
BRIEFS = ROOT / "pi" / "control-plane" / "IMPLEMENTATION_SLICE_BRIEFS.md"
REQUIRED = validator.BRIEF_HEADINGS


class SliceBriefTests(unittest.TestCase):
    def test_all_forty_briefs_have_required_contract(self):
        report = validator.validate_slice_briefs(ROOT)
        self.assertEqual(report["briefCount"], 40)

    def test_missing_heading_is_rejected(self):
        text = BRIEFS.read_text()
        text = text.replace("#### Stop and escalate\n", "", 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "briefs.md"
            path.write_text(text)
            with self.assertRaises(validator.ValidationFailure) as caught:
                validator.validate_slice_briefs(ROOT, path)
            self.assertIn("missing Stop and escalate", str(caught.exception))

    def test_missing_acceptance_command_is_rejected(self):
        text = BRIEFS.read_text()
        match = re.search(r"(### C0a — .*?)(?=\n### )", text, re.S)
        self.assertIsNotNone(match)
        block = match.group(1)
        replacement = re.sub(
            r"#### Acceptance commands\n.*?\n#### Stop and escalate",
            "#### Acceptance commands\n\n#### Stop and escalate",
            block,
            count=1,
            flags=re.S,
        )
        self.assertNotEqual(block, replacement)
        text = text[: match.start()] + replacement + text[match.end() :]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "briefs.md"
            path.write_text(text)
            with self.assertRaises(validator.ValidationFailure) as caught:
                validator.validate_slice_briefs(ROOT, path)
            self.assertIn("C0a has no acceptance command", str(caught.exception))

    def test_self_prerequisite_is_rejected(self):
        text = BRIEFS.read_text()
        match = re.search(r"(### C0b — .*?)(?=\n### )", text, re.S)
        self.assertIsNotNone(match)
        block = match.group(1).replace("C0a accepted.", "C0b accepted.", 1)
        text = text[: match.start()] + block + text[match.end() :]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "briefs.md"
            path.write_text(text)
            with self.assertRaises(validator.ValidationFailure) as caught:
                validator.validate_slice_briefs(ROOT, path)
            self.assertIn("C0b has a self-prerequisite", str(caught.exception))

    def test_acceptance_sections_are_not_all_inherited(self):
        text = BRIEFS.read_text()
        starts = list(re.finditer(r"^### (C(?:\d+[a-z]?\d?|11)) — ", text, re.M))
        self.assertEqual(len(starts), 40)
        for index, start in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
            block = text[start.start() : end]
            for heading in REQUIRED:
                self.assertIn(heading, block, start.group(1))


if __name__ == "__main__":
    unittest.main()
