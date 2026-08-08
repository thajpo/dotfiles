from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-067"])
def run(*, capability: bool = False): return run_group("runs", action_ids=["HA-067"], capability=capability)
