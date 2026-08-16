"""Bounded evidence envelopes and truthful 0/1/2/77 aggregation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
STATUSES = {"PASS", "FAIL", "STOP", "SKIP"}
DEFAULT_ACTION_MANIFEST = Path(__file__).with_name("action-manifest.v1.json")


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
    installed_product_action_observed: bool = False
    production_mutation_performed: bool = False
    remote_provider_contacted: bool = False

    def as_dict(self) -> dict[str, Any]:
        value = {"schemaVersion": 1, "scenarioId": self.scenario_id, "actionIds": list(self.action_ids), "status": self.status, "tier": self.tier, "fixtureId": self.fixture_id, "sourceBuildId": self.source_build_id, "buildId": self.build_id, "before": dict(self.before or {}), "after": dict(self.after or {}), "capability": dict(self.capability or {}), "faultSeed": self.fault_seed, "installedProductActionObserved": self.installed_product_action_observed, "productionMutationPerformed": self.production_mutation_performed, "remoteProviderContacted": self.remote_provider_contacted, "assertions": dict(self.assertions), "commands": list(self.commands)}
        if self.reason is not None: value["reason"] = self.reason
        return value


def _action_catalog(manifest_path: str | Path) -> dict[str, Mapping[str, Any]]:
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("action manifest is unavailable or invalid") from error
    actions = manifest.get("actions") if isinstance(manifest, Mapping) else None
    if not isinstance(actions, list):
        raise ValueError("action manifest is unavailable or invalid")
    catalog: dict[str, Mapping[str, Any]] = {}
    for action in actions:
        if not isinstance(action, Mapping) or not isinstance(action.get("actionId"), str):
            raise ValueError("action manifest is unavailable or invalid")
        catalog[action["actionId"]] = action
    return catalog


def validate_evidence(value: Mapping[str, Any], *, manifest_path: str | Path = DEFAULT_ACTION_MANIFEST, release: bool = False) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schemaVersion") != 1:
        raise ValueError("evidence schema version is invalid")
    required = {"schemaVersion", "scenarioId", "actionIds", "status", "tier", "fixtureId", "sourceBuildId", "buildId", "before", "after", "capability", "faultSeed", "installedProductActionObserved", "productionMutationPerformed", "remoteProviderContacted", "assertions", "commands"}
    allowed = required | {"reason"}
    commands_valid = isinstance(value.get("commands"), list) and all(isinstance(item, Mapping) and isinstance(item.get("argv"), list) and all(isinstance(argument, str) for argument in item.get("argv", [])) and isinstance(item.get("returncode"), int) and not isinstance(item.get("returncode"), bool) and isinstance(item.get("stdoutDigest"), str) and isinstance(item.get("stderrDigest"), str) for item in value.get("commands", []))
    action_ids = value.get("actionIds")
    action_ids_valid = isinstance(action_ids, list) and bool(action_ids) and all(isinstance(item, str) and re.fullmatch(r"HA-[0-9]{3}", item) is not None for item in action_ids) and len(set(action_ids)) == len(action_ids)
    status = value.get("status")
    observation_fields = ("installedProductActionObserved", "productionMutationPerformed", "remoteProviderContacted")
    if not required.issubset(value) or not set(value).issubset(allowed) or ("reason" in value and not isinstance(value["reason"], str)) or not isinstance(value["scenarioId"], str) or not value["scenarioId"] or not action_ids_valid or not isinstance(status, str) or status not in STATUSES or any(not isinstance(value[key], str) or not value[key] for key in ("tier", "fixtureId", "sourceBuildId", "buildId", "faultSeed")) or any(not isinstance(value.get(key), bool) for key in observation_fields) or not isinstance(value["before"], Mapping) or not isinstance(value["after"], Mapping) or not isinstance(value["capability"], Mapping) or not isinstance(value["assertions"], Mapping) or not commands_valid or (status == "PASS" and (not value["before"] or not value["after"] or not value["capability"] or not value["assertions"] or not value["commands"])):
        raise ValueError("evidence envelope is malformed")
    catalog = _action_catalog(manifest_path)
    for action_id in value["actionIds"]:
        action = catalog.get(action_id)
        if action is None:
            raise ValueError(f"evidence references unknown action: {action_id}")
        if value["scenarioId"] not in action.get("scenarios", []):
            raise ValueError(f"evidence scenario is not declared for action: {action_id}")
        if value["tier"] not in action.get("tiers", []):
            raise ValueError(f"evidence tier is not declared for action: {action_id}")
        if release and action.get("status") != "implemented-source":
            raise ValueError(f"release evidence references a non-implemented action: {action_id}")
    if release and status == "PASS" and value["installedProductActionObserved"] is not True:
        raise ValueError("release PASS requires an installed product action observation")
    if release:
        incomplete = sorted(action_id for action_id, action in catalog.items() if action.get("status") != "implemented-source")
        if incomplete:
            raise ValueError(f"release action catalog contains planned or excluded actions: {', '.join(incomplete)}")
    return dict(value)


def validate_release_evidence(value: Mapping[str, Any], *, manifest_path: str | Path = DEFAULT_ACTION_MANIFEST) -> dict[str, Any]:
    return validate_evidence(value, manifest_path=manifest_path, release=True)


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


__all__ = ["Evidence", "aggregate", "validate_evidence", "validate_release_evidence", "write_evidence"]
