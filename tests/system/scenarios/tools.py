from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-020", "HA-021", "HA-022", "HA-026"])
def run(*, capability: bool = False): return run_group("tools", action_ids=["HA-020", "HA-021", "HA-022", "HA-026"], capability=capability)
