from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-104", "HA-105"])
def run(*, capability: bool = False): return run_group("installation", action_ids=["HA-104", "HA-105"], capability=capability, required="staged install capability is unavailable")
