"""Host-owned project policy parsing and trust narrowing for Phase 3."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import canonical_json

_POLICY_VERSION = 1
_ALLOWED_KEYS = frozenset({
    "version", "defaultMode", "trustedRoots", "isolatedRoots",
    "controlPlaneRepositories", "protectedBranches", "worktreeRoot",
})
_MODES = frozenset({"trusted", "isolated"})
_EFFECTIVE_MODES = ("trusted-live", "isolated", "read-only")


class PolicyError(ValueError):
    """Policy is malformed, unsupported, or cannot be safely normalized."""


def _path_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PolicyError(f"{field} must be a non-empty path")
    if len(value) > 4096:
        raise PolicyError(f"{field} is too long")
    return value


def _canonical_path(value: str, *, base: Path) -> Path:
    expanded = Path(os.path.expanduser(value))
    if not expanded.is_absolute():
        expanded = base / expanded
    # resolve(strict=False) canonicalizes existing aliases but does not require
    # a root to exist during policy load.  Registration separately observes the
    # repository and refuses missing paths.
    return expanded.resolve(strict=False)


def _path_is_under(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


@dataclass(frozen=True)
class ProjectPolicy:
    version: int
    default_mode: str
    trusted_roots: tuple[Path, ...]
    isolated_roots: tuple[Path, ...]
    control_plane_repositories: tuple[Path, ...]
    protected_branches: tuple[str, ...]
    worktree_root: Path | None
    normalized: Mapping[str, Any]
    policy_hash: str

    def trust_for_repository(self, repository: Path) -> str:
        candidate = repository.resolve(strict=False)
        if _path_is_under(candidate, self.trusted_roots) or _path_is_under(candidate, self.control_plane_repositories):
            return "trusted"
        if _path_is_under(candidate, self.isolated_roots):
            return "isolated"
        return self.default_mode

    def effective_mode(self, project_trust: str, requested: str | None = None) -> str:
        if requested is None:
            requested = "trusted-live" if project_trust == "trusted" else "isolated"
        if requested not in _EFFECTIVE_MODES:
            raise PolicyError("unknown effective working-copy mode")
        rank = {"trusted-live": 2, "isolated": 1, "read-only": 0}
        allowed = "trusted-live" if project_trust == "trusted" else "isolated"
        if rank[requested] > rank[allowed]:
            raise PolicyError("working-copy mode would broaden project trust")
        return requested

    def branch_is_protected(self, branch_ref: str | None) -> bool:
        if branch_ref is None:
            return False
        short = branch_ref.removeprefix("refs/heads/")
        return short in self.protected_branches or branch_ref in self.protected_branches

    def as_dict(self) -> dict[str, Any]:
        return dict(self.normalized)


def load_policy(source: os.PathLike[str] | str | Mapping[str, Any] | None = None, *, base_dir: os.PathLike[str] | str | None = None) -> ProjectPolicy:
    base = Path(base_dir).expanduser().resolve() if base_dir is not None else Path.cwd().resolve()
    if source is None:
        configured = os.environ.get("PI_SYSTEM_PROJECT_POLICY")
        config_root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        user_policy = config_root / "pi" / "repository-policy.json"
        bundled_policy = Path(__file__).resolve().parents[2] / "pi" / "repository-policy.json"
        source = Path(configured).expanduser() if configured else (user_policy if user_policy.is_file() and not user_policy.is_symlink() else bundled_policy)
    if isinstance(source, Mapping):
        data = dict(source)
    else:
        path = Path(source).expanduser()
        if path.is_symlink() or not path.is_file():
            raise PolicyError("policy must be a regular non-symlink file")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PolicyError("policy cannot be read") from error
    if not isinstance(data, dict):
        raise PolicyError("policy must be a JSON object")
    unknown = set(data) - _ALLOWED_KEYS
    if unknown:
        raise PolicyError("policy contains unknown fields: " + ",".join(sorted(unknown)))
    if data.get("version", data.get("schemaVersion", 1)) != _POLICY_VERSION:
        raise PolicyError("unsupported policy version")
    default_mode = data.get("defaultMode")
    if default_mode not in _MODES:
        raise PolicyError("defaultMode must be trusted or isolated")

    def paths(field: str) -> tuple[Path, ...]:
        values = data.get(field, [])
        if not isinstance(values, list) or len(values) > 256:
            raise PolicyError(f"{field} must be a bounded array")
        return tuple(_canonical_path(_path_text(value, field=field), base=base) for value in values)

    trusted = paths("trustedRoots")
    isolated = paths("isolatedRoots")
    control = paths("controlPlaneRepositories")
    branches = data.get("protectedBranches", [])
    if not isinstance(branches, list) or any(not isinstance(item, str) or not item or len(item) > 256 for item in branches):
        raise PolicyError("protectedBranches must be bounded strings")
    worktree_value = data.get("worktreeRoot")
    worktree = None if worktree_value is None else _canonical_path(_path_text(worktree_value, field="worktreeRoot"), base=base)
    normalized = {
        "version": _POLICY_VERSION,
        "defaultMode": default_mode,
        "trustedRoots": [str(item) for item in trusted],
        "isolatedRoots": [str(item) for item in isolated],
        "controlPlaneRepositories": [str(item) for item in control],
        "protectedBranches": list(branches),
        "worktreeRoot": str(worktree) if worktree is not None else None,
    }
    digest = "sha256:" + hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()
    return ProjectPolicy(_POLICY_VERSION, default_mode, trusted, isolated, control, tuple(branches), worktree, normalized, digest)


parse_policy = load_policy

__all__ = ["PolicyError", "ProjectPolicy", "load_policy", "parse_policy"]
