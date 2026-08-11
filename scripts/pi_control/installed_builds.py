"""Registration of exact, pre-existing P1 staged generations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .greenfield_install import RESOURCE_INVENTORY, verify_stage
from .models import canonical_json, utc_now
from .operations import update_operation_in_transaction
from .staged_build import load_build_manifest


class InstalledBuildError(ValueError):
    pass


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def verify_registered_build(store: Any, build_id: str) -> Mapping[str, Any]:
    """Reverify one registered generation from its complete P1 stage tree."""

    if not isinstance(build_id, str) or not build_id:
        raise InstalledBuildError("an exact registered build ID is required")
    row = store.conn.execute("SELECT * FROM installed_builds WHERE build_id=? AND status IN ('staged','active')", (build_id,)).fetchone()
    if row is None:
        raise InstalledBuildError("run build is not a registered verified staged generation")
    manifest_path = Path(row["build_manifest_path"])
    resources_path = Path(row["resource_manifest_path"])
    if manifest_path.is_symlink() or resources_path.is_symlink() or not manifest_path.is_file() or not resources_path.is_file():
        raise InstalledBuildError("registered build manifests are unavailable")
    root = manifest_path.parent
    if root.is_symlink() or resources_path.parent != root or manifest_path.name != "build-manifest.json" or resources_path.name != RESOURCE_INVENTORY:
        raise InstalledBuildError("registered build manifests do not identify one canonical stage root")
    try:
        verified = verify_stage(root)
        manifest = load_build_manifest(manifest_path)
        resource_digest = _file_digest(resources_path)
    except (OSError, RuntimeError, ValueError) as error:
        raise InstalledBuildError("registered staged generation failed exact reverification") from error
    expected = (row["build_id"], row["build_manifest_digest"], row["resource_manifest_digest"], row["pi_version"])
    actual = (verified["buildId"], manifest.digest, resource_digest, verified["piVersion"])
    if actual != expected or verified["manifestDigest"] != row["build_manifest_digest"]:
        raise InstalledBuildError("registered staged generation identity no longer matches its row")
    return row


def register_staged_build(store: Any, staged_root: str | Path) -> dict[str, Any]:
    supplied = Path(staged_root).expanduser().absolute()
    if supplied.is_symlink() or not supplied.is_dir():
        raise InstalledBuildError("staged build root is unsafe")
    root = supplied.resolve(strict=True)
    verified = verify_stage(root)
    manifest_path = root / "build-manifest.json"
    resources_path = root / RESOURCE_INVENTORY
    manifest = load_build_manifest(manifest_path)
    try:
        resources = json.loads(resources_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InstalledBuildError("release resource inventory is invalid") from error
    resource_digest = _file_digest(resources_path)
    operation = store.create_operation(idempotency_key="build.register:" + manifest.build_id, kind="build.register", resource_type="installed-build", resource_id=manifest.build_id, actor_type="controller", request={"buildId": manifest.build_id, "buildManifestDigest": manifest.digest, "resourceManifestDigest": resource_digest})
    existing = store.conn.execute("SELECT * FROM installed_builds WHERE build_id=?", (manifest.build_id,)).fetchone()
    identity = (str(manifest_path), manifest.digest, str(resources_path), resource_digest, verified["piVersion"])
    if existing is not None:
        actual = (existing["build_manifest_path"], existing["build_manifest_digest"], existing["resource_manifest_path"], existing["resource_manifest_digest"], existing["pi_version"])
        if actual != identity:
            raise InstalledBuildError("registered build identity differs from staged manifests")
        if operation.state != "succeeded":
            store.complete_operation(operation.operation_id, result={"buildId": manifest.build_id}, step="build-registered")
        return dict(existing)
    payload = manifest.payload
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "INSERT INTO installed_builds(build_id,source_commit,source_tree_hash,build_manifest_path,build_manifest_digest,resource_manifest_path,resource_manifest_digest,pi_version,package_lock_hash,status,installed_at,activated_at,rollback_path,verification_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (manifest.build_id, payload.get("sourceCommit"), payload.get("sourceTreeHash") or payload["sourceDigest"], str(manifest_path), manifest.digest, str(resources_path), resource_digest, verified["piVersion"], payload.get("packageLockSha256") or payload["sourceDigest"], "staged", now, None, None, canonical_json({"verified": True, "resourceSchemaVersion": resources["schemaVersion"]})),
        )
        update_operation_in_transaction(store.conn, operation.operation_id, state="succeeded", step="build-registered", result={"buildId": manifest.build_id})
    return dict(store.conn.execute("SELECT * FROM installed_builds WHERE build_id=?", (manifest.build_id,)).fetchone())


__all__ = ["InstalledBuildError", "register_staged_build", "verify_registered_build"]
