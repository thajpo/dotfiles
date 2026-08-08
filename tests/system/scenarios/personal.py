from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-005", "HA-068"])
def run(*, capability: bool = False): return run_group("personal", action_ids=["HA-005", "HA-068"], capability=capability)
