from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-106"])
def run(*, capability: bool = False): return run_group("docker-runtime", action_ids=["HA-106"], capability=capability, required="Docker daemon and pinned image are unavailable")
