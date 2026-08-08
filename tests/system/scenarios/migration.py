from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-100", "HA-101", "HA-102", "HA-103"])
def run(*, capability: bool = False): return run_group("migration", action_ids=["HA-100", "HA-101", "HA-102", "HA-103"], capability=capability)
