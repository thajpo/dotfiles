from __future__ import annotations
import importlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
MODULES = ["launchers", "sessions", "personal", "secretary", "presentation", "tools", "children", "workstreams", "host_command_feedback", "cleanup_publication", "cli_actions", "projects", "runs", "changes", "reviews_integration", "continuity_observability", "migration", "installation", "recovery_security", "docker"]

class ScenarioTests(unittest.TestCase):
    def test_manifest_scenario_references_are_declared_exactly(self):
        from tests.system.action_catalog import ACTIONS, expand_reference
        expected = {(scenario_id, action_id) for action_id, row in ACTIONS.items() for reference in row.get("scenarios", []) for scenario_id in expand_reference(reference)}
        actual = set()
        for name in MODULES:
            module = importlib.import_module(f"tests.system.scenarios.{name}")
            for scenario in module.SCENARIOS:
                for action_id in scenario["actionIds"]:
                    actual.add((scenario["scenarioId"], action_id))
        self.assertEqual(expected, actual)

    def test_every_catalog_action_has_a_declared_nonlive_scenario(self):
        actions = {row["actionId"] for row in json.loads((ROOT / "tests/system/action-manifest.v1.json").read_text())["actions"]}
        declared = set()
        for name in MODULES:
            module = importlib.import_module(f"tests.system.scenarios.{name}")
            for scenario in module.SCENARIOS:
                declared.update(scenario["actionIds"])
        self.assertEqual(actions - declared, set())
        self.assertTrue(declared - actions == set())

    def test_scenarios_stop_without_process_capability(self):
        module = importlib.import_module("tests.system.scenarios.workstreams")
        self.assertEqual(module.run(capability=False)["status"], "STOP")

    def test_fault_corpus_covers_each_recovery_boundary(self):
        module = importlib.import_module("tests.system.scenarios.recovery_security")
        result = module.run(capability=False)
        self.assertEqual(result["status"], "STOP")
        self.assertGreaterEqual(len(result["faults"]), 12)
        self.assertIn("after-external", {item["fault"] for item in result["faults"]})

if __name__ == "__main__": unittest.main()
