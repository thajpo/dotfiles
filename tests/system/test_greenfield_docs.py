from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    from . import validate_plan_docs
    from .test_action_manifest import copy_validation_fixture
except ImportError:
    import validate_plan_docs
    from test_action_manifest import copy_validation_fixture


ROOT = Path(__file__).resolve().parents[2]


class GreenfieldDocumentationTests(unittest.TestCase):
    def test_canonical_docs_exist_and_known_retired_docs_are_absent(self):
        self.assertEqual(validate_plan_docs.validate(ROOT), [])

    def test_retired_plan_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            retired = root / "pi/MIGRATION.md"
            retired.parent.mkdir(parents=True, exist_ok=True)
            retired.write_text("# Retired\n", encoding="utf-8")

            errors = validate_plan_docs.validate(root)

        self.assertIn("retired non-greenfield document returned: pi/MIGRATION.md", errors)

    def test_container_resident_conversational_pi_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            contract = root / "pi/control-plane/EXECUTION_CONTRACT.md"
            contract.write_text(contract.read_text(encoding="utf-8") + "\nThe conversational Pi runs in the writer container.\n", encoding="utf-8")

            errors = validate_plan_docs.validate(root)

        self.assertIn("canonical documents contain contradictory runtime topology", errors)

    def test_target_pi_task_compatibility_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            contract = root / "pi/control-plane/EXECUTION_CONTRACT.md"
            contract.write_text(contract.read_text(encoding="utf-8") + "\nPI_TASK_ROUTE_FILE is supported target compatibility behavior.\n", encoding="utf-8")

            errors = validate_plan_docs.validate(root)

        self.assertIn("target PI_TASK compatibility appears in canonical document: pi/control-plane/EXECUTION_CONTRACT.md", errors)

    def test_all_phases_and_status_vocabulary_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            plan = root / "PI_GREENFIELD_IMPLEMENTATION_PLAN.md"
            text = plan.read_text(encoding="utf-8")
            text = text.replace("| P12 | `installed-passed` |", "| PX | `queued` |", 1)
            plan.write_text(text, encoding="utf-8")

            errors = validate_plan_docs.validate(root)

        self.assertIn("canonical plan must contain exactly one current-status row for P0-P12", errors)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            plan = root / "PI_GREENFIELD_IMPLEMENTATION_PLAN.md"
            plan.write_text(plan.read_text(encoding="utf-8").replace("`installed-passed`", "`installed-complete`"), encoding="utf-8")

            errors = validate_plan_docs.validate(root)

        self.assertIn("canonical plan is missing mechanical status vocabulary: installed-passed", errors)

    def test_p1_progress_requires_p0_and_later_phases_remain_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            plan = root / "PI_GREENFIELD_IMPLEMENTATION_PLAN.md"
            text = plan.read_text(encoding="utf-8")
            text = text.replace("| P0 | `source-passed` |", "| P0 | `not-started` |", 1)
            text = text.replace("| P1 | `not-started` |", "| P1 | `source-passed` |", 1)
            plan.write_text(text, encoding="utf-8")
            self.assertIn("P1 cannot advance before P0 source-passed", validate_plan_docs.validate(root))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            plan = root / "PI_GREENFIELD_IMPLEMENTATION_PLAN.md"
            text = plan.read_text(encoding="utf-8")
            text = text.replace("| P2 | `cumulative-passed` |", "| P2 | `source-passed` |", 1)
            text = text.replace("| P3 | `not-started` |", "| P3 | `source-passed` |", 1)
            plan.write_text(text, encoding="utf-8")
            self.assertIn("P3 cannot advance before P1 and P2 cumulative-passed", validate_plan_docs.validate(root))

    def test_contradictory_docker_owner_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            contract = root / "pi/control-plane/EXECUTION_CONTRACT.md"
            contract.write_text(contract.read_text(encoding="utf-8") + "\npi-sandbox-control is the sole Docker lifecycle owner.\n", encoding="utf-8")

            errors = validate_plan_docs.validate(root)

        self.assertIn("canonical documents contain contradictory runtime topology", errors)

    def test_linux_npm_python_and_tty_decisions_are_required(self):
        decisions = (
            "Release 1 is Linux-only.",
            "npm `package-lock.json`",
            "Python `uv.lock` plus hash-pinned requirements",
            "separate TTY-bound host CLI",
        )
        for decision in decisions:
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                copy_validation_fixture(root)
                plan = root / "PI_GREENFIELD_IMPLEMENTATION_PLAN.md"
                plan.write_text(plan.read_text(encoding="utf-8").replace(decision, "removed decision"), encoding="utf-8")

                errors = validate_plan_docs.validate(root)

                self.assertIn(f"canonical plan is missing accepted decision: {decision}", errors)

    def test_contract_cannot_duplicate_program_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            contract = root / "pi/control-plane/PRODUCT_CONTRACT.md"
            contract.write_text(contract.read_text(encoding="utf-8") + "\nCurrent release is `release-passed`.\n", encoding="utf-8")

            errors = validate_plan_docs.validate(root)

        self.assertIn("contract duplicates program status vocabulary: pi/control-plane/PRODUCT_CONTRACT.md", errors)


if __name__ == "__main__":
    unittest.main()
