"""Strict adapter-neutral Pisec configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from .adapters import validate_adapter_id
from .models import InvalidRequestError, parse_json_strict


class PisecConfig(dict[str, Any]):
    """Validated configuration retaining only epoch-three fields."""

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

def _validate_envelope(value: Any) -> PisecConfig:
    if not isinstance(value, dict) or set(value) != {"schemaVersion", "fencePath", "harness", "workspace"}:
        raise InvalidRequestError("Pisec configuration fields are invalid")
    if value.get("schemaVersion") != 3:
        raise InvalidRequestError("Pisec configuration schemaVersion must be 3")
    harness = value["harness"]
    workspace = value["workspace"]
    if not isinstance(harness, dict) or set(harness) != {"id", "config"} or not isinstance(harness["config"], dict):
        raise InvalidRequestError("harness configuration envelope is invalid")
    if not isinstance(workspace, dict) or set(workspace) != {"id", "config"} or not isinstance(workspace["config"], dict):
        raise InvalidRequestError("workspace configuration envelope is invalid")
    return PisecConfig(
        {
            "schemaVersion": 3,
            "fencePath": _expand_path(value["fencePath"], "fencePath"),
            "harness": {"id": validate_adapter_id(harness["id"]), "config": dict(harness["config"])},
            "workspace": {"id": validate_adapter_id(workspace["id"]), "config": dict(workspace["config"])},
        }
    )


def load_config(path: Path | str | None = None) -> PisecConfig:
    selected = Path(path) if path is not None else default_config_path()
    try:
        value = parse_json_strict(selected.read_bytes(), max_bytes=256 * 1024)
    except (OSError, UnicodeError, InvalidRequestError) as error:
        raise InvalidRequestError("Pisec configuration is unavailable or invalid", detail={"path": str(selected)}) from error
    return _validate_envelope(value)
