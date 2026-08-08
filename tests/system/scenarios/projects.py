from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-061", "HA-062"])
def run(*, capability: bool = False): return run_group("projects", action_ids=["HA-061", "HA-062"], capability=capability)
