from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-023", "HA-024", "HA-025"])
def run(*, capability: bool = False): return run_group("children", action_ids=["HA-023", "HA-024", "HA-025"], capability=capability)
