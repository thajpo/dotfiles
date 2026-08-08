from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-049", "HA-050", "HA-076", "HA-077"])
def run(*, capability: bool = False): return run_group("cleanup-publication", action_ids=["HA-049", "HA-050", "HA-076", "HA-077"], capability=capability)
