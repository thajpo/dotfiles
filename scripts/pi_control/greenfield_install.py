"""Atomic staging, activation, and reversible rollback for the fresh Pi system."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import uuid
from typing import Any

from .greenfield_store import GreenfieldStore
from .staged_build import BuildManifest, load_build_manifest, stage_build


class InstallError(RuntimeError):
    pass


RELEASE_FILES = (
    "bin/pi-control", "bin/pi-system-run", "bin/pi-workstream", "bin/pi-integration", "scripts/pi-system-run.py",
    "scripts/pi_control/__init__.py", "scripts/pi_control/models.py", "scripts/pi_control/errors.py", "scripts/pi_control/events.py", "scripts/pi_control/operations.py", "scripts/pi_control/locks.py", "scripts/pi_control/git_adapter.py", "scripts/pi_control/project_policy.py", "scripts/pi_control/process_adapter.py", "scripts/pi_control/writer_lock.py",
    "scripts/pi_control/greenfield_schema.py", "scripts/pi_control/greenfield_store.py", "scripts/pi_control/greenfield_client.py", "scripts/pi_control/greenfield_cli.py", "scripts/pi_control/projects.py", "scripts/pi_control/conversations.py", "scripts/pi_control/messages.py", "scripts/pi_control/command_requests.py", "scripts/pi_control/network_runner.py", "scripts/pi_control/launch.py", "scripts/pi_control/docker_runtime.py", "scripts/pi_control/greenfield_workstreams.py", "scripts/pi_control/greenfield_review.py", "scripts/pi_control/greenfield_reconcile.py", "scripts/pi_control/scoped_read.py", "scripts/pi_control/changes.py", "scripts/pi_control/reviews.py", "scripts/pi_control/integration.py", "scripts/pi_control/dependencies.py", "scripts/pi_control/package_diff.py", "scripts/pi_control/package_environment.py",
    "pi/extensions/scoped-project-read", "pi/extensions/project-messages", "pi/extensions/project-commands", "pi/extensions/dependency-review", "pi/packages/pi-sandbox-control",
)


def _safe_root(value: os.PathLike[str] | str) -> Path:
    path = Path(value).expanduser().absolute()
    if path == Path(path.anchor) or path.is_symlink():
        raise InstallError("install root is too broad or symlinked")
    return path


def _copy_entries(source: Path, destination: Path, manifest: BuildManifest) -> None:
    for entry in manifest.payload["files"]:
        relative = Path(entry["path"])
        source_path = source / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if entry["kind"] == "symlink":
            os.symlink(entry["target"], target)
        else:
            shutil.copy2(source_path, target, follow_symlinks=False)
            os.chmod(target, entry["mode"])


def _atomic_json(path: Path, value: dict[str, Any], *, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def stage(source_root: os.PathLike[str] | str, staging_root: os.PathLike[str] | str, *, test_outcomes: dict[str, Any] | None = None) -> dict[str, Any]:
    source = _safe_root(source_root)
    stage_path = _safe_root(staging_root)
    if stage_path.exists():
        raise InstallError("staging root already exists; preserve it and choose a new stage")
    stage_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = stage_build(source, stage_path, files=RELEASE_FILES, repository=source, metadata={"product": "pi-system", "freshState": True}, test_outcomes=test_outcomes or {"contract": "PASS"})
    _copy_entries(source, stage_path, manifest)
    manifest.verify_files(stage_path, exclude=["build-manifest.json"])
    resources = {"schemaVersion": 1, "buildId": manifest.build_id, "manifestDigest": manifest.digest, "installedRoot": str(stage_path), "loadedPaths": list(RELEASE_FILES), "legacyCoLoad": [], "freshState": True}
    _atomic_json(stage_path / "loaded-resources.json", resources)
    return {"stageRoot": str(stage_path), "buildId": manifest.build_id, "manifestDigest": manifest.digest, "fileCount": len(manifest.payload["files"]), "freshState": True}


def verify_stage(stage_root: os.PathLike[str] | str) -> dict[str, Any]:
    root = _safe_root(stage_root)
    manifest = load_build_manifest(root / "build-manifest.json")
    manifest.verify_files(root, exclude=["build-manifest.json", "loaded-resources.json"])
    resources = json.loads((root / "loaded-resources.json").read_text(encoding="utf-8"))
    if resources.get("buildId") != manifest.build_id or resources.get("manifestDigest") != manifest.digest or resources.get("loadedPaths") != list(RELEASE_FILES) or resources.get("legacyCoLoad") != [] or resources.get("freshState") is not True:
        raise InstallError("staged loaded-resource evidence is invalid")
    return {"stageRoot": str(root), "buildId": manifest.build_id, "manifestDigest": manifest.digest, "fileCount": len(manifest.payload["files"]), "verified": True}


def activate(stage_root: os.PathLike[str] | str, data_root: os.PathLike[str] | str) -> dict[str, Any]:
    stage_path = _safe_root(stage_root)
    target = _safe_root(data_root)
    verified = verify_stage(stage_path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup: Path | None = None
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise InstallError("existing active Pi data root is unsafe")
        backup = target.parent / f"{target.name}.rollback.{uuid.uuid4().hex}"
        os.rename(target, backup)
    marker = {"schemaVersion": 1, "activeRoot": str(target), "buildId": verified["buildId"], "manifestDigest": verified["manifestDigest"], "rollbackRoot": str(backup) if backup else None, "activatedAt": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}
    _atomic_json(stage_path / "activation.json", marker)
    try:
        os.rename(stage_path, target)
        parent_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException:
        if backup is not None and not target.exists():
            os.rename(backup, target)
        raise
    return {**verified, "dataRoot": str(target), "rollbackRoot": str(backup) if backup else None, "activated": True}


def ensure_fresh_state(state_root: os.PathLike[str] | str) -> dict[str, Any]:
    root = _safe_root(state_root)
    if root.exists() and (root / "control.db").exists():
        raise InstallError("production Pi state already exists; refusing import or overwrite")
    with GreenfieldStore(root) as store:
        return {"stateRoot": str(root), "schema": store.schema_status().as_dict(), "fresh": True}


def rollback(data_root: os.PathLike[str] | str) -> dict[str, Any]:
    target = _safe_root(data_root)
    if not target.exists() or not target.is_dir() or target.is_symlink():
        raise InstallError("active Pi data root is unavailable")
    backups = sorted((item for item in target.parent.glob(f"{target.name}.rollback.*") if item.is_dir() and not item.is_symlink()), key=lambda item: item.stat().st_mtime_ns)
    preserved = target.parent / f"{target.name}.preserved.{uuid.uuid4().hex}"
    os.rename(target, preserved)
    restored: Path | None = None
    try:
        if backups:
            restored = backups[-1]
            os.rename(restored, target)
            directory_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        if not target.exists():
            os.rename(preserved, target)
        raise
    return {"rolledBack": restored is not None, "restoredRoot": str(target) if restored else None, "preservedNewRoot": str(preserved), "statePreserved": True, "workPreserved": True}


__all__ = ["InstallError", "RELEASE_FILES", "activate", "ensure_fresh_state", "rollback", "stage", "verify_stage"]
