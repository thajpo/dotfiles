from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-027", "HA-028", "HA-029", "HA-030"])
def run(*, capability: bool = False): return run_group("host-command-feedback", action_ids=["HA-027", "HA-028", "HA-029", "HA-030"], capability=capability)
