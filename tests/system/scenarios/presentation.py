from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-009", "HA-010", "HA-011", "HA-012", "HA-013"])
def run(*, capability: bool = False): return run_group("presentation", action_ids=["HA-009", "HA-010", "HA-011", "HA-012", "HA-013"], capability=capability)
