from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-040", "HA-041", "HA-046", "HA-047", "HA-048", "HA-071", "HA-072", "HA-073", "HA-074", "HA-075"])
def run(*, capability: bool = False): return run_group("reviews-integration", action_ids=["HA-040", "HA-041", "HA-046", "HA-047", "HA-048", "HA-071", "HA-072", "HA-073", "HA-074", "HA-075"], capability=capability)
