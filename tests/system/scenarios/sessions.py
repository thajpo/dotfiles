from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-002", "HA-003"])
def run(*, capability: bool = False): return run_group("sessions", action_ids=["HA-002", "HA-003"], capability=capability)
