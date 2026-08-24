"""Immutable runtime software releases and explicit activation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from .adapters import HarnessAdapter, RuntimeReleaseArtifacts
from .events import append_event_in_transaction
from .models import ConflictError, InvalidRequestError, canonical_json, utc_now, validate_id


def _release_artifacts(harness: HarnessAdapter) -> RuntimeReleaseArtifacts:
    builder = getattr(harness, "build_runtime_release", None)
    if callable(builder):
        artifacts = builder()
    else:
        manifest = {
            "schemaVersion": 1,
            "adapter": harness.manifest.adapter_id,
            "adapterVersion": harness.manifest.version_label,
        }
        digest = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
        artifacts = RuntimeReleaseArtifacts(digest, manifest)
    if not isinstance(artifacts, RuntimeReleaseArtifacts) or len(artifacts.content_sha256) != 64:
        raise InvalidRequestError("harness returned an invalid runtime release")
    if artifacts.root_path is not None and not Path(artifacts.root_path).is_absolute():
        raise InvalidRequestError("runtime release root must be absolute")
    return artifacts


def build_runtime_release(store: Any, harness: HarnessAdapter) -> dict[str, Any]:
    artifacts = _release_artifacts(harness)
    release_id = "rel_" + artifacts.content_sha256[:32]
    manifest_json = canonical_json(artifacts.manifest, max_bytes=256 * 1024, max_text=64 * 1024)
    existing = store.conn.execute("SELECT * FROM runtime_releases WHERE content_sha256=?", (artifacts.content_sha256,)).fetchone()
    if existing is not None:
        row = dict(existing)
        if row["manifest_json"] != manifest_json or row["root_path"] != artifacts.root_path:
            raise ConflictError("runtime release digest is bound to different contents")
        return {**row, "reused": True}
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "INSERT INTO runtime_releases(release_id,harness_id,adapter_version,content_sha256,manifest_json,root_path,created_at) VALUES(?,?,?,?,?,?,?)",
            (release_id, harness.manifest.adapter_id, harness.manifest.version_label, artifacts.content_sha256, manifest_json, artifacts.root_path, now),
        )
        append_event_in_transaction(store.conn, kind="runtime.release.built", payload={"releaseId": release_id, "contentSha256": artifacts.content_sha256, "harnessId": harness.manifest.adapter_id})
    return {**dict(store.conn.execute("SELECT * FROM runtime_releases WHERE release_id=?", (release_id,)).fetchone()), "reused": False}


def activate_runtime_release(store: Any, release_id: str) -> dict[str, Any]:
    release_id = validate_id(release_id, prefix="rel")
    release = store.conn.execute("SELECT * FROM runtime_releases WHERE release_id=?", (release_id,)).fetchone()
    if release is None:
        raise InvalidRequestError("runtime release was not found")
    current = store.conn.execute("SELECT release_id FROM runtime_release_channels WHERE channel='current'").fetchone()
    if current is not None and current["release_id"] == release_id:
        return {**dict(release), "activated": False}
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "INSERT INTO runtime_release_channels(channel,release_id,activated_at) VALUES('current',?,?) ON CONFLICT(channel) DO UPDATE SET release_id=excluded.release_id,activated_at=excluded.activated_at",
            (release_id, now),
        )
        store.conn.execute(
            "UPDATE runtime_bindings SET desired_release_id=?,updated_at=? WHERE workstream_id IN (SELECT w.workstream_id FROM workstreams w JOIN projects p USING(project_id) WHERE p.active=1 AND w.desired_state='active')",
            (release_id, now),
        )
        append_event_in_transaction(store.conn, kind="runtime.release.activated", payload={"releaseId": release_id, "contentSha256": release["content_sha256"]})
    return {**dict(release), "activated": True}


def active_runtime_release(store: Any, harness: HarnessAdapter, *, bootstrap: bool = True) -> dict[str, Any]:
    row = store.conn.execute(
        "SELECT r.* FROM runtime_release_channels c JOIN runtime_releases r USING(release_id) WHERE c.channel='current'"
    ).fetchone()
    if row is not None and row["harness_id"] == harness.manifest.adapter_id:
        return dict(row)
    if row is not None:
        compatible = store.conn.execute(
            "SELECT * FROM runtime_releases WHERE harness_id=? ORDER BY created_at DESC,release_id LIMIT 1",
            (harness.manifest.adapter_id,),
        ).fetchone()
        if compatible is not None:
            return dict(compatible)
    if row is None:
        if not bootstrap:
            raise ConflictError("no runtime release is active")
        built = build_runtime_release(store, harness)
        activate_runtime_release(store, str(built["release_id"]))
        row = store.conn.execute("SELECT * FROM runtime_releases WHERE release_id=?", (built["release_id"],)).fetchone()
    else:
        built = build_runtime_release(store, harness)
        row = store.conn.execute("SELECT * FROM runtime_releases WHERE release_id=?", (built["release_id"],)).fetchone()
    if row is None or row["harness_id"] != harness.manifest.adapter_id:
        raise ConflictError("active runtime release does not match the configured harness")
    return dict(row)


def release_scope(scope: Mapping[str, Any], release: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(scope)
    result.update(
        {
            "runtimeReleaseId": str(release["release_id"]),
            "runtimeReleaseSha256": str(release["content_sha256"]),
            "runtimeReleaseRoot": release.get("root_path"),
        }
    )
    return result


def fleet_scope_paths(store: Any, scope: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(scope)
    if scope.get("executionProfile") != "first-mate":
        return result
    from .projects import fleet_project_ids

    worktrees_root = Path(str(scope["fleetWorktreesDir"]))
    git_objects_root = Path(str(scope["fleetGitObjectsDir"]))
    project_ids = fleet_project_ids(store)
    result["fleetProjectWorktrees"] = [str((worktrees_root / project_id).absolute()) for project_id in project_ids]
    result["fleetProjectGitObjects"] = [str((git_objects_root / project_id).absolute()) for project_id in project_ids]
    return result


def materialize_active_release(store: Any, harness: HarnessAdapter, scope: Mapping[str, Any]) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    release = active_runtime_release(store, harness)
    bound_scope = fleet_scope_paths(store, release_scope(scope, release))
    return harness.materialize_profile(bound_scope), release, bound_scope
