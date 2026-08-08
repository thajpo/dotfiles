from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-063", "HA-064", "HA-065", "HA-066", "HA-080", "HA-081", "HA-082", "HA-083", "HA-084", "HA-085", "HA-086", "HA-087", "HA-088", "HA-089", "HA-090", "HA-091"])
def run(*, capability: bool = False): return run_group("continuity-observability", action_ids=["HA-063", "HA-064", "HA-065", "HA-066", "HA-080", "HA-081", "HA-082", "HA-083", "HA-084", "HA-085", "HA-086", "HA-087", "HA-088", "HA-089", "HA-090", "HA-091"], capability=capability)
