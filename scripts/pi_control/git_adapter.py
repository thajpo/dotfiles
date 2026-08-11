"""Bounded, read-only Git observation for Phase 3.

This adapter owns no lifecycle state and has no mutating Git command path.  Git
objects/refs remain the source-content authority; callers only normalize the
observations returned here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from .models import bounded_text, utc_now

_MAX_OUTPUT = 512 * 1024
_MAX_STATUS = 256 * 1024
_MAX_WORKTREES = 256
_OID_RE = re.compile(r"^[0-9a-fA-F]+$")
_READ_ONLY_COMMANDS = frozenset({"rev-parse", "symbolic-ref", "status", "worktree", "config"})


class GitObservationError(RuntimeError):
    """A bounded failure to observe Git; it never implies a missing resource."""

    def __init__(self, message: str, *, kind: str = "error", detail: Mapping[str, Any] | None = None):
        self.kind = kind
        self.detail = dict(detail or {})
        super().__init__(message)


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor or os.sep)
    for component in absolute.parts[1:]:
        current /= component
        if current.is_symlink():
            raise GitObservationError("Git observation path contains a symlink", detail={"path": str(current)})


def _git_executable() -> str:
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise GitObservationError("Git executable is unavailable", detail={"kind": "adapter-unavailable"})
    return executable


def sanitized_git_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Construct a minimal environment that cannot inherit Git injection vars."""

    env = {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_EDITOR": "true",
        "GIT_ASKPASS": "true",
        # macOS Git emits a warning when Darwin's temp directory is not
        # discoverable under the scrubbed HOME.  Keep the observation
        # environment deterministic without leaking the caller's temp path.
        "TMPDIR": tempfile.gettempdir(),
    }
    if extra:
        for key, value in extra.items():
            if key.startswith("GIT_") or key in {"PATH", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH"}:
                continue
            env[key] = str(value)
    return env


def _validate_read_only_args(args: Sequence[str]) -> None:
    if not args or args[0] not in _READ_ONLY_COMMANDS:
        raise GitObservationError("Git command is not allowlisted")
    if any("\x00" in str(arg) for arg in args):
        raise GitObservationError("Git argument contains NUL")
    allowed = {
        ("rev-parse", "--git-common-dir"),
        ("rev-parse", "--git-dir"),
        ("rev-parse", "--show-toplevel"),
        ("rev-parse", "--show-object-format"),
        ("rev-parse", "--is-bare-repository"),
        ("rev-parse", "--verify", "HEAD"),
        ("rev-parse", "--verify", "HEAD^{tree}"),
        ("symbolic-ref", "--quiet", "--short", "HEAD"),
        ("status", "--porcelain=v2", "--branch"),
        ("worktree", "list", "--porcelain"),
        ("config", "--local", "--null", "--list"),
    }
    if tuple(str(item) for item in args) not in allowed:
        raise GitObservationError("Git read-only command is not allowlisted")


def run_git(
    cwd: os.PathLike[str] | str,
    args: Sequence[str],
    *,
    timeout: float = 15.0,
    max_output: int = _MAX_OUTPUT,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded, read-only local Git observation command."""

    _validate_read_only_args(args)
    if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 60:
        raise ValueError("Git observation timeout is outside its bound")
    path = Path(cwd)
    _reject_symlink_components(path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise GitObservationError("Git observation cwd is unavailable", kind="missing", detail={"path": str(path)}) from error
    if not resolved.is_dir():
        raise GitObservationError("Git observation cwd is not a directory", kind="missing", detail={"path": str(path)})
    trusted_noop = shutil.which("true", path=os.defpath) or "true"
    command = [
        _git_executable(),
        "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=/dev/null",
        "-c", "diff.external=" + trusted_noop,
        "-c", "core.sshCommand=",
        "-c", "credential.helper=",
        "-c", "core.attributesFile=/dev/null",
        "-c", "core.excludesFile=/dev/null",
        *[str(argument) for argument in args],
    ]
    try:
        result = subprocess.run(
            command,
            cwd=str(resolved),
            env=sanitized_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise GitObservationError("Git observation timed out", detail={"command": args[0]}) from error
    except OSError as error:
        raise GitObservationError("Git observation process was unavailable", kind="adapter-unavailable", detail={"command": args[0]}) from error
    if len(result.stdout.encode("utf-8")) > max_output or len(result.stderr.encode("utf-8")) > max_output:
        raise GitObservationError("Git observation output exceeded its bound")
    return result


def _require(result: subprocess.CompletedProcess[str], *, command: str, missing_kind: str = "error") -> str:
    if result.returncode != 0:
        stderr = result.stderr.strip()[:512]
        kind = missing_kind if "not a git repository" in stderr.lower() or "does not exist" in stderr.lower() else "error"
        raise GitObservationError("Git observation command failed", kind=kind, detail={"command": command, "stderr": stderr})
    return result.stdout.strip()


def _optional(result: subprocess.CompletedProcess[str], *, command: str) -> str | None:
    if result.returncode == 0:
        return result.stdout.strip() or None
    stderr = result.stderr.lower()
    expected_no_value = (
        "not a symbolic ref" in stderr
        or "is not a symbolic ref" in stderr
        or "needed a single revision" in stderr
        or "ambiguous argument 'head'" in stderr
        or "this operation must be run in a work tree" in stderr
    )
    if expected_no_value:
        return None
    raise GitObservationError("Git observation command failed", detail={"command": command, "stderr": result.stderr.strip()[:512]})


def _validate_oid(value: str | None, object_format: str, *, name: str) -> str | None:
    if value is None:
        return None
    expected = 40 if object_format == "sha1" else 64 if object_format == "sha256" else None
    if expected is None or len(value) != expected or _OID_RE.fullmatch(value) is None:
        raise GitObservationError("Git object ID has an invalid shape", detail={"field": name, "object_format": object_format})
    return value.lower()


def _validate_local_config(cwd: Path) -> None:
    result = run_git(cwd, ["config", "--local", "--null", "--list"], max_output=128 * 1024)
    listing = _require(result, command="config")
    dangerous_prefixes = (
        "filter.", "include.", "includeif.", "credential.", "core.fsmonitor",
        "core.sshcommand", "core.hookspath", "diff.",
    )
    for entry in listing.split("\x00"):
        key = entry.split("\n", 1)[0].strip().lower()
        if key.startswith(dangerous_prefixes):
            raise GitObservationError("repository Git configuration exposes an execution surface", detail={"key": key[:128]})


def _canonical_metadata(cwd: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    _reject_symlink_components(candidate)
    try:
        return candidate.resolve(strict=True)
    except OSError as error:
        raise GitObservationError("Git metadata path is unavailable", detail={"path": str(candidate)}) from error


@dataclass(frozen=True)
class GitWorktreeObservation:
    path: str
    head_oid: str | None
    branch_ref: str | None
    detached: bool
    bare: bool
    exists: bool
    git_dir: str | None = None
    common_dir: str | None = None
    object_format: str | None = None
    status: str | None = None
    state: str = "unknown"
    error: str | None = None
    tree_oid: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "head_oid": self.head_oid, "tree_oid": self.tree_oid, "branch_ref": self.branch_ref, "detached": self.detached, "bare": self.bare, "exists": self.exists, "git_dir": self.git_dir, "common_dir": self.common_dir, "object_format": self.object_format, "status": self.status, "state": self.state, "error": self.error}


@dataclass(frozen=True)
class GitRepositoryObservation:
    repository_path: str
    top_level: str | None
    git_dir: str
    common_dir: str
    object_format: str
    is_bare: bool
    branch_ref: str | None
    head_oid: str | None
    tree_oid: str | None
    status: str
    status_hash: str
    dirty: bool
    worktrees: tuple[GitWorktreeObservation, ...] = field(default_factory=tuple)
    observed_at: str = field(default_factory=utc_now)
    provenance: str = "git-read-only-v1"

    @property
    def device_inode(self) -> tuple[int, int]:
        metadata = Path(self.common_dir).stat()
        return metadata.st_dev, metadata.st_ino

    def as_dict(self) -> dict[str, Any]:
        return {
            "repository_path": self.repository_path,
            "top_level": self.top_level,
            "git_dir": self.git_dir,
            "common_dir": self.common_dir,
            "object_format": self.object_format,
            "is_bare": self.is_bare,
            "branch_ref": self.branch_ref,
            "head_oid": self.head_oid,
            "tree_oid": self.tree_oid,
            "status": self.status,
            "status_hash": self.status_hash,
            "dirty": self.dirty,
            "worktrees": [item.as_dict() for item in self.worktrees],
            "observed_at": self.observed_at,
            "provenance": self.provenance,
        }


def _parse_worktree_list(output: str, *, object_format: str, common_dir: Path) -> list[GitWorktreeObservation]:
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in output.splitlines():
        if not raw:
            if current:
                records.append(current)
                current = None
            continue
        key, separator, value = raw.partition(" ")
        if key == "worktree":
            if current:
                records.append(current)
            current = {"path": value}
        elif current is not None and key == "HEAD":
            current["head_oid"] = _validate_oid(value, object_format, name="worktree HEAD")
        elif current is not None and key == "branch":
            current["branch_ref"] = value
        elif current is not None and key == "detached":
            current["detached"] = True
        elif current is not None and key == "bare":
            current["bare"] = True
    if current:
        records.append(current)
    if len(records) > _MAX_WORKTREES:
        raise GitObservationError("Git worktree inventory exceeded its bound")
    observations: list[GitWorktreeObservation] = []
    for record in records:
        raw_path = Path(str(record.get("path", "")))
        try:
            _reject_symlink_components(raw_path)
        except GitObservationError as unsafe:
            observations.append(GitWorktreeObservation(
                path=str(raw_path), head_oid=record.get("head_oid"), branch_ref=record.get("branch_ref"),
                detached=bool(record.get("detached")), bare=bool(record.get("bare")), exists=False,
                state="error", error="unsafe Git-reported worktree path",
            ))
            continue
        try:
            path = raw_path.resolve(strict=True)
            exists = path.is_dir()
        except OSError:
            path = raw_path.absolute()
            exists = False
        git_dir = common = object_format_value = status = tree_oid = None
        state = "missing" if not exists else "unknown"
        error = None
        if exists and not record.get("bare"):
            try:
                child = observe_repository(path, include_worktrees=False)
                git_dir, common, object_format_value, status, tree_oid = child.git_dir, child.common_dir, child.object_format, child.status, child.tree_oid
                state = "ready" if child.head_oid is not None else "unknown"
            except GitObservationError as failure:
                state = "error" if failure.kind != "missing" else "missing"
                error = str(failure)[:512]
        observations.append(GitWorktreeObservation(
            path=str(path), head_oid=record.get("head_oid"), branch_ref=record.get("branch_ref"),
            detached=bool(record.get("detached")), bare=bool(record.get("bare")), exists=exists,
            git_dir=git_dir, common_dir=common, object_format=object_format_value, status=status,
            state=state, error=error, tree_oid=tree_oid,
        ))
    return observations


def observe_repository(repository: os.PathLike[str] | str, *, include_worktrees: bool = True) -> GitRepositoryObservation:
    """Observe one repository/worktree without writing Git or filesystem state."""

    raw = Path(repository).expanduser()
    _reject_symlink_components(raw)
    try:
        cwd = raw.resolve(strict=True)
    except OSError as error:
        raise GitObservationError("repository path is missing", kind="missing", detail={"path": str(raw)}) from error
    if not cwd.is_dir():
        raise GitObservationError("repository path is not a directory", kind="missing", detail={"path": str(raw)})
    _validate_local_config(cwd)
    git_dir = _canonical_metadata(cwd, _require(run_git(cwd, ["rev-parse", "--git-dir"], max_output=4096), command="git-dir"))
    common_dir = _canonical_metadata(cwd, _require(run_git(cwd, ["rev-parse", "--git-common-dir"], max_output=4096), command="git-common-dir"))
    if git_dir != common_dir and common_dir not in git_dir.parents:
        raise GitObservationError("Git dir is not contained by its common directory", detail={"git_dir": str(git_dir), "common_dir": str(common_dir)})
    top_result = run_git(cwd, ["rev-parse", "--show-toplevel"], max_output=4096)
    top_level = _optional(top_result, command="show-toplevel")
    if top_level:
        top_path = _canonical_metadata(cwd, top_level)
        if cwd != top_path and not cwd.is_relative_to(top_path):
            raise GitObservationError("Git top-level does not contain observation cwd", detail={"top_level": str(top_path), "cwd": str(cwd)})
        top_level = str(top_path)
    object_format = _require(run_git(cwd, ["rev-parse", "--show-object-format"], max_output=128), command="object-format")
    if object_format not in {"sha1", "sha256"}:
        raise GitObservationError("unsupported Git object format", detail={"object_format": object_format})
    is_bare = _require(run_git(cwd, ["rev-parse", "--is-bare-repository"], max_output=32), command="bare") == "true"
    branch_result = run_git(cwd, ["symbolic-ref", "--quiet", "--short", "HEAD"], max_output=4096)
    branch_ref = _optional(branch_result, command="symbolic-ref")
    if branch_ref:
        branch_ref = "refs/heads/" + branch_ref if not branch_ref.startswith("refs/") else branch_ref
    head_result = run_git(cwd, ["rev-parse", "--verify", "HEAD"], max_output=128)
    head_oid = _validate_oid(_optional(head_result, command="HEAD"), object_format, name="HEAD")
    tree_result = run_git(cwd, ["rev-parse", "--verify", "HEAD^{tree}"], max_output=128)
    tree_oid = _validate_oid(_optional(tree_result, command="HEAD tree"), object_format, name="HEAD tree")
    status_result = None if is_bare else run_git(cwd, ["status", "--porcelain=v2", "--branch"], max_output=_MAX_STATUS)
    status = "" if status_result is None else _require(status_result, command="status")
    status_hash = hashlib.sha256(status.encode("utf-8")).hexdigest()
    worktrees: list[GitWorktreeObservation] = []
    if include_worktrees:
        worktree_result = run_git(cwd, ["worktree", "list", "--porcelain"], max_output=_MAX_OUTPUT)
        worktrees = _parse_worktree_list(_require(worktree_result, command="worktree list"), object_format=object_format, common_dir=common_dir)
    return GitRepositoryObservation(
        repository_path=str(cwd), top_level=top_level, git_dir=str(git_dir), common_dir=str(common_dir),
        object_format=object_format, is_bare=is_bare, branch_ref=branch_ref, head_oid=head_oid,
        tree_oid=tree_oid, status=status, status_hash=status_hash, dirty=any(
            line and not line.startswith("# branch.") for line in status.splitlines()
        ), worktrees=tuple(worktrees),
    )


# Friendly aliases used by later phases.
GitAdapter = observe_repository
observe_git_repository = observe_repository

__all__ = [
    "GitAdapter", "GitObservationError", "GitRepositoryObservation", "GitWorktreeObservation",
    "observe_git_repository", "observe_repository", "run_git", "sanitized_git_environment",
]
