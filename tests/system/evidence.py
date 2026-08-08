"""Bounded evidence envelopes and truthful 0/1/2/77 aggregation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
STATUSES = {"PASS", "FAIL", "STOP", "SKIP"}


@dataclass(frozen=True)
class Evidence:
    scenario_id: str
    action_ids: tuple[str, ...]
    status: str
    tier: str
    assertions: Mapping[str, Any]
    commands: tuple[Mapping[str, Any], ...] = ()
    reason: str | None = None
    fixture_id: str = "unbound"
    source_build_id: str = "unbound"
    build_id: str = "unbound"
    before: Mapping[str, Any] = None  # type: ignore[assignment]
    after: Mapping[str, Any] = None  # type: ignore[assignment]
    capability: Mapping[str, Any] = None  # type: ignore[assignment]
    fault_seed: str = "none"
    no_live_action: bool = True

    def as_dict(self) -> dict[str, Any]:
        value = {"schemaVersion": 1, "scenarioId": self.scenario_id, "actionIds": list(self.action_ids), "status": self.status, "tier": self.tier, "fixtureId": self.fixture_id, "sourceBuildId": self.source_build_id, "buildId": self.build_id, "before": dict(self.before or {}), "after": dict(self.after or {}), "capability": dict(self.capability or {}), "faultSeed": self.fault_seed, "noLiveAction": self.no_live_action, "assertions": dict(self.assertions), "commands": list(self.commands)}
        if self.reason is not None: value["reason"] = self.reason
        return value


def validate_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schemaVersion") != 1:
        raise ValueError("evidence schema version is invalid")
    required = {"schemaVersion", "scenarioId", "actionIds", "status", "tier", "fixtureId", "sourceBuildId", "buildId", "before", "after", "capability", "faultSeed", "noLiveAction", "assertions", "commands"}
    commands_valid = isinstance(value.get("commands"), list) and all(isinstance(item, Mapping) and isinstance(item.get("argv"), list) and isinstance(item.get("returncode"), int) and isinstance(item.get("stdoutDigest"), str) and isinstance(item.get("stderrDigest"), str) for item in value.get("commands", []))
    if not required.issubset(value) or not isinstance(value["scenarioId"], str) or not isinstance(value["actionIds"], list) or not value["actionIds"] or value["status"] not in STATUSES or any(not isinstance(value[key], str) or not value[key] for key in ("fixtureId", "sourceBuildId", "buildId", "faultSeed")) or not isinstance(value["before"], Mapping) or not isinstance(value["after"], Mapping) or not isinstance(value["capability"], Mapping) or value["noLiveAction"] is not True or not isinstance(value["assertions"], Mapping) or not commands_valid or (value["status"] == "PASS" and (not value["before"] or not value["after"] or not value["commands"])):
        raise ValueError("evidence envelope is malformed")
    return dict(value)


def write_evidence(value: Mapping[str, Any], destination: str | Path) -> Path:
    checked = validate_evidence(value)
    body = json.dumps(checked, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
    if len(body) > MAX_EVIDENCE_BYTES: raise ValueError("evidence exceeds its size bound")
    path = Path(destination).expanduser().absolute()
    if path.exists() or path.is_symlink(): raise FileExistsError(str(path))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = path.open("xb", buffering=0)
    try:
        fd.write(body); fd.flush()
    finally: fd.close()
    path.chmod(0o600)
    return path


def aggregate(statuses: Iterable[str]) -> int:
    values = list(statuses)
    if not values: return 77
    if any(value == "FAIL" for value in values): return 1
    if any(value in {"STOP", "SKIP"} for value in values): return 77
    if any(value not in STATUSES for value in values): return 2
    return 0


__all__ = ["Evidence", "aggregate", "validate_evidence", "write_evidence"]
