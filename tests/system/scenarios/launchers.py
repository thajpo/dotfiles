from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-001", "HA-004"])
def run(*, capability: bool = False): return run_group("launchers", action_ids=["HA-001", "HA-004"], capability=capability)
