"""Strict adapter-neutral Pisec configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from .adapters import validate_adapter_id
from .models import InvalidRequestError, parse_json_strict


class PisecConfig(dict[str, Any]):
    """Validated configuration for orchestrators and optional worker routes."""

    @property
    def harness_id(self) -> str:
        return str(self["harness"]["id"])

    @property
    def workspace_id(self) -> str:
        return str(self["workspace"]["id"])

    @property
    def harness_config(self) -> Mapping[str, Any]:
        return self["harness"]["config"]

    @property
    def workspace_config(self) -> Mapping[str, Any]:
        return self["workspace"]["config"]

    @property
    def worker_routing(self) -> Mapping[str, Any]:
        return self.get("workerRouting", {})

    @property
    def worker_harnesses(self) -> Mapping[str, Any]:
        return self.get("workerHarnesses", {})


def default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return Path(os.environ.get("PISEC_CONFIG", base / "pisec" / "config.json"))


def _expand_path(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise InvalidRequestError(f"{name} must be a path")
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        raise InvalidRequestError(f"{name} must be absolute or home-relative")
    return str(expanded.absolute())


def _validate_worker_routing(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) != {"defaultModel", "fallbackHarness", "routes"}:
        raise InvalidRequestError("worker routing configuration fields are invalid")
    default_model = value["defaultModel"]
    fallback = value["fallbackHarness"]
    routes = value["routes"]
    if not isinstance(default_model, str) or not default_model or any(ord(char) < 0x20 for char in default_model):
        raise InvalidRequestError("worker routing defaultModel is invalid")
    if not isinstance(fallback, str):
        raise InvalidRequestError("worker routing fallbackHarness is invalid")
    validate_adapter_id(fallback)
    if not isinstance(routes, dict):
        raise InvalidRequestError("worker routing routes are invalid")
    normalized: dict[str, dict[str, Any]] = {}
    for model, route in routes.items():
        if not isinstance(model, str) or not model or any(ord(char) < 0x20 for char in model):
            raise InvalidRequestError("worker routing model key is invalid")
        if not isinstance(route, dict) or set(route) != {"harness", "model", "reasoningEffort"}:
            raise InvalidRequestError("worker routing route fields are invalid")
        harness = route["harness"]
        native_model = route["model"]
        effort = route["reasoningEffort"]
        validate_adapter_id(harness)
        if not isinstance(native_model, str) or not native_model or any(ord(char) < 0x20 for char in native_model):
            raise InvalidRequestError("worker routing native model is invalid")
        if effort not in {"low", "medium", "high", "xhigh"}:
            raise InvalidRequestError("worker routing reasoningEffort is invalid")
        normalized[model] = {"harness": harness, "model": native_model, "reasoningEffort": effort}
    return {"defaultModel": default_model, "fallbackHarness": fallback, "routes": normalized}


def _validate_worker_harnesses(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidRequestError("worker harnesses are invalid")
    result: dict[str, Any] = {}
    for key, envelope in value.items():
        validate_adapter_id(key)
        if not isinstance(envelope, dict) or set(envelope) != {"id", "config"} or envelope["id"] != key or not isinstance(envelope["config"], dict):
            raise InvalidRequestError("worker harness envelope is invalid")
        result[key] = {"id": key, "config": dict(envelope["config"])}
    return result

def _validate_envelope(value: Any) -> PisecConfig:
    allowed = {"schemaVersion", "fencePath", "harness", "workspace", "workerRouting", "workerHarnesses"}
    if not isinstance(value, dict) or not set(value).issubset(allowed) or not {"schemaVersion", "fencePath", "harness", "workspace"}.issubset(value):
        raise InvalidRequestError("Pisec configuration fields are invalid")
    if value.get("schemaVersion") != 3:
        raise InvalidRequestError("Pisec configuration schemaVersion must be 3")
    harness = value["harness"]
    workspace = value["workspace"]
    if not isinstance(harness, dict) or set(harness) != {"id", "config"} or not isinstance(harness["config"], dict):
        raise InvalidRequestError("harness configuration envelope is invalid")
    if not isinstance(workspace, dict) or set(workspace) != {"id", "config"} or not isinstance(workspace["config"], dict):
        raise InvalidRequestError("workspace configuration envelope is invalid")
    result: dict[str, Any] = {
        "schemaVersion": 3,
        "fencePath": _expand_path(value["fencePath"], "fencePath"),
        "harness": {"id": validate_adapter_id(harness["id"]), "config": dict(harness["config"])},
        "workspace": {"id": validate_adapter_id(workspace["id"]), "config": dict(workspace["config"])},
    }
    worker_routing = _validate_worker_routing(value.get("workerRouting"))
    worker_harnesses = _validate_worker_harnesses(value.get("workerHarnesses"))
    if worker_routing:
        result["workerRouting"] = worker_routing
    if worker_harnesses:
        result["workerHarnesses"] = worker_harnesses
    return PisecConfig(result)


def load_config(path: Path | str | None = None) -> PisecConfig:
    selected = Path(path) if path is not None else default_config_path()
    try:
        value = parse_json_strict(selected.read_bytes(), max_bytes=256 * 1024)
    except (OSError, UnicodeError, InvalidRequestError) as error:
        raise InvalidRequestError("Pisec configuration is unavailable or invalid", detail={"path": str(selected)}) from error
    return _validate_envelope(value)
