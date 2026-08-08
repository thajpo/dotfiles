from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-006", "HA-007", "HA-008"])
def run(*, capability: bool = False): return run_group("secretary", action_ids=["HA-006", "HA-007", "HA-008"], capability=capability)
