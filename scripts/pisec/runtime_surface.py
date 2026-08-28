"""The single verified runtime-surface snapshot boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import tempfile
from typing import Any, Mapping

from .adapters import HarnessAdapter, RuntimeSurfaceArtifacts
from .models import InvalidRequestError, NeedsAttentionError, canonical_json, validate_sha256


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        digest.update(b"missing\0")
        return digest.hexdigest()
    for path in [root, *sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())]:
        relative = "." if path == root else path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise NeedsAttentionError("runtime surface contains a symlink")
        if stat.S_ISDIR(info.st_mode):
            digest.update(f"d\0{relative}\0".encode("utf-8"))
        elif stat.S_ISREG(info.st_mode):
            digest.update(f"f\0{relative}\0{stat.S_IMODE(info.st_mode) & 0o700:o}\0".encode("utf-8"))
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            digest.update(b"\0")
        else:
            raise NeedsAttentionError("runtime surface contains an unsupported file")
    return digest.hexdigest()


def verify_surface(surface: RuntimeSurfaceArtifacts) -> RuntimeSurfaceArtifacts:
    """Verify the captured root without recapturing a newer surface."""
    validate_sha256(surface.content_sha256, "runtime surface digest")
    raw_root = Path(surface.root_path)
    if raw_root.is_symlink() or not raw_root.is_absolute() or raw_root.resolve(strict=False) != raw_root:
        raise NeedsAttentionError("runtime surface root was replaced or is not canonical")
    if not raw_root.is_dir() or _tree_digest(raw_root) != surface.content_sha256:
        raise NeedsAttentionError("runtime surface changed during the operation")
    return surface


def capture_runtime_surface(harness: HarnessAdapter) -> RuntimeSurfaceArtifacts:
    """Capture exactly one immutable surface snapshot for an operation."""
    value = harness.current_runtime_surface()
    if not isinstance(value, RuntimeSurfaceArtifacts):
        raise NeedsAttentionError("harness returned an invalid runtime surface")
    raw_root = Path(value.root_path)
    if raw_root.is_symlink() or not raw_root.is_absolute() or raw_root.resolve(strict=False) != raw_root:
        raise NeedsAttentionError("harness returned a non-canonical runtime surface root")
    manifest = json.loads(value.manifest_json)
    snapshot = RuntimeSurfaceArtifacts(value.content_sha256, manifest, str(raw_root))
    return verify_surface(snapshot)


def fleet_scope_paths(store: Any, scope: Mapping[str, Any]) -> dict[str, Any]:
    del store
    return dict(scope)


def materialize_current_surface(
    store: Any,
    harness: HarnessAdapter,
    scope: Mapping[str, Any],
    *,
    surface: RuntimeSurfaceArtifacts | None = None,
) -> tuple[Any, RuntimeSurfaceArtifacts, dict[str, Any]]:
    """Stage and activate using one captured surface; callers never reacquire it."""
    materialized_scope = fleet_scope_paths(store, scope)
    surface = capture_runtime_surface(harness) if surface is None else verify_surface(surface)
    staging = materialized_scope.get("stagingRoot")
    if not isinstance(staging, str) or not staging:
        staging = tempfile.mkdtemp(prefix="pisec-profile-stage-")
    materialized_scope = {
        **materialized_scope,
        "operationId": str(materialized_scope.get("operationId", "op_surface")),
        "stagingRoot": str(Path(staging).resolve(strict=False)),
    }
    staged = harness.stage_profile(materialized_scope, surface, Path(staging))
    verify_surface(surface)
    artifacts = harness.activate_profile(materialized_scope, staged)
    if not hasattr(artifacts, "generation_sha256"):
        raise InvalidRequestError("harness activation returned invalid artifacts")
    desired = harness.desired_generation(materialized_scope, surface)
    if artifacts.generation_sha256 != desired:
        raise NeedsAttentionError("activated runtime generation does not match the captured surface")
    verify_surface(surface)
    return artifacts, surface, materialized_scope
