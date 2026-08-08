from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-070"])
def run(*, capability: bool = False): return run_group("changes", action_ids=["HA-070"], capability=capability)
