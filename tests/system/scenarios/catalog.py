from __future__ import annotations

def declarations(prefix: str, action_start: int | list[int], action_end: int | None = None, tier: str = "T0"):
    values = action_start if isinstance(action_start, list) else list(range(action_start, (action_end or action_start) + 1))
    return [{"scenarioId": f"{prefix}-{index:03d}", "actionIds": [f"HA-{index:03d}"], "tier": tier} for index in values]

def run_group(name: str, *, action_ids: list[str] | None = None, capability: bool = False, required: str = "required process/staged capability is unavailable"):
    return {
        "schemaVersion": 1,
        "scenarioId": name,
        "actionIds": list(action_ids or ["SCENARIO-UNBOUND"]),
        "status": "PASS" if capability else "STOP",
        "tier": "T2",
        "fixtureId": "disposable-process-fixture",
        "sourceBuildId": "source-only",
        "buildId": "disposable-process-fixture",
        "before": {"namespaceDigest": "unavailable"},
        "after": {"namespaceDigest": "unavailable"},
        "capability": {"processFixture": capability, "liveAction": False},
        "faultSeed": "none",
        "noLiveAction": True,
        "assertions": {"authority": capability, "filesystem": capability, "git": capability, "process": capability, "presentation": capability, "noLiveAction": True},
        "commands": [],
        "reason": None if capability else required,
    }
