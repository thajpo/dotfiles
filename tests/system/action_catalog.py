from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / "tests/system/action-manifest.v1.json").read_text(encoding="utf-8"))
ACTIONS = {row["actionId"]: row for row in MANIFEST["actions"]}
_RANGE = re.compile(r"^(?P<prefix>.+?)-(?P<start>[0-9]+)\.\.(?P<end>[0-9]+)$")


def expand_reference(reference: str) -> list[str]:
    match = _RANGE.fullmatch(reference)
    if not match:
        return [reference]
    return [f"{match.group('prefix')}-{index:03d}" for index in range(int(match.group("start")), int(match.group("end")) + 1)]


def scenarios_for_actions(action_ids: list[str]) -> list[dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for action_id in action_ids:
        row = ACTIONS[action_id]
        for reference in row.get("scenarios", []):
            for scenario_id in expand_reference(reference):
                entry = values.setdefault(scenario_id, {"scenarioId": scenario_id, "actionIds": [], "tier": "T2", "statuses": []})
                if action_id not in entry["actionIds"]:
                    entry["actionIds"].append(action_id)
                entry["statuses"].append(row.get("status", "unknown"))
    return [values[key] for key in sorted(values)]


def action_row(action_id: str) -> dict[str, Any]:
    return ACTIONS[action_id]


def action_ids_for_group(group: str) -> list[str]:
    groups = {
        "launch-session-presentation": [f"HA-{index:03d}" for index in range(1, 14)],
        "parent-secretary-workstream": [f"HA-{index:03d}" for index in range(20, 31)] + [f"HA-{index:03d}" for index in range(40, 51)],
        "controller-change-ui": [f"HA-{index:03d}" for index in range(60, 92) if f"HA-{index:03d}" in ACTIONS],
        "migration-admin": [f"HA-{index:03d}" for index in range(100, 113) if f"HA-{index:03d}" in ACTIONS] + [f"HA-{index:03d}" for index in range(120, 129)],
    }
    if group not in groups:
        raise KeyError(group)
    return groups[group]


__all__ = ["ACTIONS", "action_ids_for_group", "action_row", "scenarios_for_actions", "expand_reference"]
