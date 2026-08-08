"""Per-working-copy package environment identity and isolation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import new_id, utc_now, validate_id
from .package_diff import package_identity


def environment_identity(store: Any, *, working_copy_id: str, platform: str, image_config_id: str) -> dict[str, Any]:
    validate_id(working_copy_id, prefix="wc")
    wc = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (working_copy_id,)).fetchone()
    if wc is None:
        raise ValueError("working copy not found")
    identity = package_identity(wc["path"], platform=platform, image_config_id=image_config_id)
    environment_id = new_id("pkg")
    root = Path(store.state_root) / "environments" / working_copy_id / (identity["manifestDigest"] or "no-manifest").removeprefix("sha256:")
    root.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    with store.transaction():
        existing = store.conn.execute("SELECT * FROM package_environments WHERE working_copy_id=? AND manifest_digest IS ? AND lock_digest IS ? AND platform=? AND image_config_id=?", (working_copy_id, identity["manifestDigest"], identity["lockDigest"], platform, image_config_id)).fetchone()
        if existing is not None:
            return dict(existing)
        store.conn.execute("INSERT INTO package_environments(environment_id,working_copy_id,manifest_digest,lock_digest,ecosystem,platform,image_config_id,environment_path,cache_scope,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (environment_id, working_copy_id, identity["manifestDigest"] or "none", identity["lockDigest"], identity["ecosystem"], platform, image_config_id, str(root), f"download-cache:{identity['ecosystem']}", now, now))
        return dict(store.conn.execute("SELECT * FROM package_environments WHERE environment_id=?", (environment_id,)).fetchone())


def assert_environment_isolated(store: Any, *, working_copy_id: str, environment_path: str) -> None:
    validate_id(working_copy_id, prefix="wc")
    row = store.conn.execute("SELECT environment_path FROM package_environments WHERE working_copy_id=? AND environment_path=?", (working_copy_id, environment_path)).fetchone()
    if row is None:
        raise ValueError("environment is not owned by working copy")


__all__ = ["assert_environment_isolated", "environment_identity"]
