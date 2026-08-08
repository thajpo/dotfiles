"""Fail-closed host-owned activation latch projection."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Mapping

from .errors import ActivationMismatchError, ControlPlaneError
from .models import canonical_json

LATCH_SCHEMA_VERSION = 1
_LATCH_KEYS = {"schemaVersion", "projectGitIdentity", "projectId", "mode", "activationResourceVersion", "expectedDbPath", "controllerSchemaVersion", "buildId", "migrationId", "expectedProjectVersion", "manifestDigest"}


class ActivationUnavailableError(ControlPlaneError):
    code = "CP_ACTIVATION_UNAVAILABLE"


@dataclass(frozen=True)
class ActivationLatch:
    payload: dict[str, Any]
    manifest_digest: str
    path: str | None = None

    def as_dict(self) -> dict[str, Any]: return dict(self.payload)


def default_latch_path(state_root: os.PathLike[str] | str | None = None) -> Path:
    if state_root is None:
        root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "pi-control"
    else:
        root = Path(state_root).expanduser()
    return root / "activation.v1.json"


def _safe_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor or os.sep)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink(): raise ActivationUnavailableError("activation path contains a symlink")


def _digest(payload: Mapping[str, Any]) -> str:
    value = {key: payload[key] for key in payload if key != "manifestDigest"}
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


def plan_latch(*, project_git_identity: Mapping[str, Any], project_id: str, mode: str, activation_resource_version: int, expected_db_path: os.PathLike[str] | str, controller_schema_version: int, build_id: str | None, migration_id: str | None, expected_project_version: int) -> ActivationLatch:
    if mode not in {"legacy", "shadow", "controller"}: raise ActivationMismatchError("activation mode is invalid")
    if not isinstance(project_git_identity, Mapping) or not project_git_identity: raise ActivationMismatchError("project Git identity is missing")
    payload = {"schemaVersion": 1, "projectGitIdentity": dict(project_git_identity), "projectId": project_id, "mode": mode, "activationResourceVersion": activation_resource_version, "expectedDbPath": str(Path(expected_db_path).expanduser().absolute()), "controllerSchemaVersion": controller_schema_version, "buildId": build_id, "migrationId": migration_id, "expectedProjectVersion": expected_project_version}
    payload["manifestDigest"] = _digest(payload)
    return ActivationLatch(payload, payload["manifestDigest"])


def write_latch(latch: ActivationLatch, path: os.PathLike[str] | str | None = None) -> ActivationLatch:
    target = Path(path).expanduser().absolute() if path is not None else default_latch_path()
    _safe_components(target)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(parent, 0o700)
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode): raise ActivationUnavailableError("activation latch is not a regular file")
    body = canonical_json(latch.payload).encode() + b"\n"
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(parent))
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    except BaseException:
        try: temporary.unlink()
        except FileNotFoundError: pass
        raise
    return ActivationLatch(latch.payload, latch.manifest_digest, str(target))


def read_latch(path: os.PathLike[str] | str | None = None) -> ActivationLatch:
    target = Path(path).expanduser().absolute() if path is not None else default_latch_path()
    try:
        _safe_components(target)
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ActivationUnavailableError("activation latch permissions or type are invalid")
        value = json.loads(target.read_text(encoding="utf-8"))
    except ActivationUnavailableError: raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ActivationUnavailableError("activation latch is unavailable") from error
    if not isinstance(value, dict) or set(value) != _LATCH_KEYS or value.get("schemaVersion") != 1:
        raise ActivationUnavailableError("activation latch schema is invalid")
    digest = _digest(value)
    if value.get("manifestDigest") != digest:
        raise ActivationMismatchError("activation latch digest does not match its record")
    if canonical_json(value) + "\n" != target.read_text(encoding="utf-8"):
        raise ActivationMismatchError("activation latch is not canonical JSON")
    return ActivationLatch(value, digest, str(target))


def verify_latch(latch: ActivationLatch, *, expected_db_path: os.PathLike[str] | str, project: Mapping[str, Any], build: Mapping[str, Any] | None = None, migration: Mapping[str, Any] | None = None) -> None:
    value = latch.payload
    if value.get("expectedDbPath") != str(Path(expected_db_path).expanduser().absolute()): raise ActivationMismatchError("activation database path does not match")
    if value.get("projectId") != project.get("project_id") or value.get("expectedProjectVersion") != project.get("resource_version"): raise ActivationMismatchError("activation project binding does not match")
    if value.get("mode") == "legacy":
        if value.get("buildId") is not None or value.get("migrationId") is not None: raise ActivationMismatchError("legacy latch contains controller bindings")
        return
    if build is None or migration is None: raise ActivationUnavailableError("activation build or migration evidence is unavailable")
    if value.get("buildId") != build.get("build_id") or value.get("migrationId") != migration.get("migration_id"): raise ActivationMismatchError("activation build or migration binding does not match")
    if value.get("mode") == "controller" and (build.get("status") != "active" or migration.get("state") != "succeeded"): raise ActivationMismatchError("controller activation predicates are not satisfied")


__all__ = ["ActivationLatch", "ActivationUnavailableError", "default_latch_path", "plan_latch", "read_latch", "verify_latch", "write_latch"]
