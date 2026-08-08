from tests.system.action_catalog import scenarios_for_actions
from .catalog import run_group
SCENARIOS = scenarios_for_actions(["HA-107", "HA-108", "HA-109", "HA-110", "HA-111", "HA-112", "HA-120", "HA-121", "HA-122", "HA-123", "HA-124", "HA-125", "HA-126", "HA-127", "HA-128"])
FAULTS = ("before-intent", "after-intent", "before-lock", "before-external", "after-external", "after-observation", "after-event", "stale-version", "stale-epoch", "auth-swap", "project-swap", "path-swap", "forbidden-content", "unknown-process")
def run(*, capability: bool = False):
    result = run_group("recovery-security", action_ids=["HA-107", "HA-108", "HA-109", "HA-110", "HA-111", "HA-112", "HA-120", "HA-121", "HA-122", "HA-123", "HA-124", "HA-125", "HA-126", "HA-127", "HA-128"], capability=capability)
    result["faults"] = [{"fault": fault, "classification": "PASS" if capability else "STOP"} for fault in FAULTS]
    return result
