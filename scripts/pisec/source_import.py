"""Import clean, committed Git work into a Pisec-owned worker."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from typing import Any, Mapping

from .git_runner import git_text, run_git
from .models import InvalidRequestError, NeedsAttentionError, bounded_text, validate_git_oid


IMPORT_PATCH_MAX_BYTES = 16 * 1024 * 1024
IMPORT_PATHS_MAX_BYTES = 128 * 1024


def _oid(repository: Path, revision: str) -> str:
    value = validate_git_oid(
        git_text(repository, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"),
        "Git commit object id",
    )
    return value.lower()


def _tree_oid(repository: Path, revision: str) -> str:
    value = validate_git_oid(
        git_text(repository, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{tree}}"),
        "Git tree object id",
    )
    return value.lower()


def _git_common_dir(repository: Path) -> Path:
    value = Path(git_text(repository, "rev-parse", "--git-common-dir"))
    if not value.is_absolute():
        value = repository / value
    return value.resolve(strict=True)


def _canonical_source_root(project_root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError("source worktree path must be a non-empty path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise InvalidRequestError("source worktree path must be absolute")
    try:
        info = candidate.lstat()
    except FileNotFoundError as error:
        raise InvalidRequestError("source worktree path does not exist") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InvalidRequestError("source worktree path must be a non-symlink directory")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise InvalidRequestError("source worktree path is not user-owned")
    try:
        root = candidate.resolve(strict=True)
        Path(git_text(root, "rev-parse", "--show-toplevel")).resolve(strict=True)
    except (OSError, InvalidRequestError) as error:
        raise InvalidRequestError("source worktree is not a usable Git checkout") from error
    if _git_common_dir(root) != _git_common_dir(project_root):
        raise InvalidRequestError("source checkout must share the project's Git repository; import a registered project ref")
    status = run_git(root, ("status", "--porcelain=v1", "--untracked-files=all"), max_bytes=IMPORT_PATHS_MAX_BYTES).stdout
    if status:
        raise InvalidRequestError("source checkout must be clean; commit the work before importing")
    return root


def _safe_ref(value: Any) -> str:
    ref = bounded_text(value, name="source ref", limit=512)
    if ref.startswith("-") or any(ord(char) < 0x20 for char in ref):
        raise InvalidRequestError("source ref contains unsafe characters")
    return ref


def normalize_source_request(project_root: Path, source: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if source is None:
        return None
    if not isinstance(source, Mapping):
        raise InvalidRequestError("source must be an object")
    keys = set(source)
    if keys - {"ref", "path"} or not keys & {"ref", "path"} or {"ref", "path"}.issubset(keys):
        raise InvalidRequestError("source must contain exactly one of ref or path")
    if "ref" in source:
        return {"kind": "project_ref", "ref": _safe_ref(source["ref"])}
    return {"kind": "git_worktree", "path": str(_canonical_source_root(project_root, source["path"]))}


def _source_selector(project_root: Path, source: Mapping[str, Any]) -> dict[str, Any] | None:
    if source.get("kind") == "project_ref" and "ref" in source and "path" not in source:
        return {"kind": "project_ref", "ref": _safe_ref(source["ref"])}
    if source.get("kind") == "git_worktree" and "path" in source and "ref" not in source:
        return {"kind": "git_worktree", "path": str(_canonical_source_root(project_root, source["path"]))}
    return normalize_source_request(project_root, source)


def _source_repository(project_root: Path, source: Mapping[str, Any]) -> tuple[Path, str]:
    normalized = _source_selector(project_root, source)
    if normalized is None:
        raise InvalidRequestError("an import source is required")
    if normalized["kind"] == "project_ref":
        return project_root, str(normalized["ref"])
    return Path(str(normalized["path"])), "HEAD"


def _changed_paths(repository: Path, base: str, source: str) -> list[str]:
    names = run_git(
        repository,
        ("diff", "--name-only", "--no-ext-diff", "--no-renames", "-z", base, source),
        max_bytes=IMPORT_PATHS_MAX_BYTES,
    ).stdout
    paths = [item for item in names.split("\x00") if item]
    if any(not path or len(path) > 4096 or any(ord(char) < 0x20 for char in path) for path in paths):
        raise InvalidRequestError("source changed-path manifest is invalid")
    return sorted(set(paths))


def _source_document(project_root: Path, target_oid: str, source: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _source_selector(project_root, source)
    if normalized is None:
        raise InvalidRequestError("an import source is required")
    source_repository, source_ref = _source_repository(project_root, normalized)
    source_oid = _oid(source_repository, source_ref)
    source_tree_oid = _tree_oid(source_repository, source_oid)
    tree = run_git(source_repository, ("ls-tree", "-r", "-t", source_oid), max_bytes=IMPORT_PATHS_MAX_BYTES).stdout
    if any(line.startswith("160000 ") for line in tree.splitlines()):
        raise InvalidRequestError("source Git submodules and gitlinks are unsupported")
    try:
        merge_base = validate_git_oid(git_text(project_root, "merge-base", target_oid, source_oid), "source merge base").lower()
    except InvalidRequestError as error:
        raise InvalidRequestError("source work must share history with the project target") from error
    patch = run_git(
        project_root,
        ("diff", "--binary", "--no-ext-diff", "--no-color", merge_base, source_oid),
        max_bytes=IMPORT_PATCH_MAX_BYTES,
    ).stdout
    if not patch:
        raise InvalidRequestError("source import contains no changes relative to the target")
    return {
        **normalized,
        "sourceCommitOid": source_oid,
        "sourceTreeOid": source_tree_oid,
        "mergeBaseOid": merge_base,
        "patchSha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "changedPaths": _changed_paths(project_root, merge_base, source_oid),
    }


def inspect_import_source(project_root: Path, target_oid: str, source: Mapping[str, Any]) -> dict[str, Any]:
    target_oid = validate_git_oid(target_oid, "source target commit").lower()
    return _source_document(project_root, target_oid, source)


def _same_source(expected: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    keys = ("kind", "ref", "path", "sourceCommitOid", "sourceTreeOid", "mergeBaseOid", "patchSha256", "changedPaths")
    return all(expected.get(key) == current.get(key) for key in keys)


def materialize_import(
    *,
    project_root: Path,
    target_oid: str,
    worker: Path,
    source: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize one approved clean source commit into the worker branch."""
    target_oid = validate_git_oid(target_oid, "import target commit").lower()
    expected = dict(source)
    current = inspect_import_source(project_root, target_oid, expected)
    if not _same_source(expected, current):
        raise NeedsAttentionError("approved import source moved; prepare a new worker proposal")
    if _oid(worker, "HEAD") != target_oid:
        raise NeedsAttentionError("worker branch moved before import materialization")

    source_repository, _source_ref = _source_repository(project_root, current)
    temporary_ref = "refs/pisec/import-candidate"
    normalized = False
    try:
        run_git(
            worker,
            ("fetch", "--no-tags", "--no-write-fetch-head", "--", str(source_repository), f"{current['sourceCommitOid']}:{temporary_ref}"),
            role="worker",
            timeout=120,
        )
        if _tree_oid(worker, temporary_ref) != current["sourceTreeOid"]:
            raise NeedsAttentionError("imported source tree did not match the approved commit")
        patch = run_git(
            worker,
            ("diff", "--binary", "--no-ext-diff", "--no-color", current["mergeBaseOid"], temporary_ref),
            max_bytes=IMPORT_PATCH_MAX_BYTES,
        ).stdout
        if hashlib.sha256(patch.encode("utf-8")).hexdigest() != current["patchSha256"]:
            raise NeedsAttentionError("imported source patch did not match the approved snapshot")
        applied = run_git(
            worker,
            ("apply", "--index", "--3way"),
            input_text=patch,
            accepted=(0, 1),
            max_bytes=128 * 1024,
        )
        if applied.returncode != 0:
            raise NeedsAttentionError("approved source does not apply cleanly to the target; rebase the source first")
        run_git(worker, ("commit", "-qm", "adopt external work into Pisec"), role="worker")
        normalized = True
        return {"sourceCommitOid": current["sourceCommitOid"], "sourceTreeOid": current["sourceTreeOid"], "normalized": True}
    finally:
        if not normalized:
            run_git(worker, ("reset", "--hard", "HEAD"), role="worker", accepted=(0,))
        run_git(worker, ("update-ref", "-d", temporary_ref), role="worker", accepted=(0, 1))
