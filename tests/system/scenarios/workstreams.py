from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-042", "HA-043", "HA-044", "HA-045", "HA-069"])
def run(*, capability: bool = False): return run_group("workstreams", action_ids=["HA-042", "HA-043", "HA-044", "HA-045", "HA-069"], capability=capability)
