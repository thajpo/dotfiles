"""Current runtime surface materialization helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .adapters import HarnessAdapter, RuntimeSurfaceArtifacts
from .models import InvalidRequestError


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


def materialize_current_surface(
    store: Any,
    harness: HarnessAdapter,
    scope: Mapping[str, Any],
) -> tuple[Any, RuntimeSurfaceArtifacts | None, dict[str, Any]]:
    materialized_scope = dict(scope)
    current = getattr(harness, "current_runtime_surface", None)
    prepare = getattr(harness, "prepare_runtime_surface", None)
    legacy_build = getattr(harness, "build_runtime_release", None)
    if callable(current):
        surface = current()
    elif callable(prepare):
        surface = prepare()
    elif callable(legacy_build):
        surface = legacy_build()
    else:
        surface = None
    materialized_scope = fleet_scope_paths(store, materialized_scope)
    materialize = getattr(harness, "materialize_profile")
    try:
        artifacts = materialize(materialized_scope, surface)
    except TypeError:
        artifacts = materialize(materialized_scope)
    generation = getattr(artifacts, "generation_sha256", None)
    return artifacts, surface, materialized_scope


materialize_active_release = materialize_current_surface
materialize_active_surface = materialize_current_surface
