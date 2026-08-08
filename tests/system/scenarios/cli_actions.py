from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-060"])
def run(*, capability: bool = False): return run_group("cli-actions", action_ids=["HA-060"], capability=capability)
