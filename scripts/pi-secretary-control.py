#!/usr/bin/env python3
"""Secure, host-owned project secretary state and constrained workstreams.

This module deliberately keeps its public vocabulary small.  It does not execute
model supplied commands or accept model supplied filesystem destinations.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime
import errno
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator

# Reuse workspace policy/classify so precedence stays exactly aligned.
_WORKSPACE_SPEC = importlib.util.spec_from_file_location(
    "pi_workspace",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi-workspace.py"),
)
_WS = importlib.util.module_from_spec(_WORKSPACE_SPEC)  # type: ignore[arg-type]
assert _WORKSPACE_SPEC and _WORKSPACE_SPEC.loader
_WORKSPACE_SPEC.loader.exec_module(_WS)

MAX_JSON = 64 * 1024
MAX_BRIEF = 16 * 1024
MAX_TITLE = 200
MAX_ROLE = 80
SUPPORTED_OBJECT_FORMATS = {"sha1": 40, "sha256": 64}
WORKSTREAM_ROLES = {"feature", "research", "analysis", "review", "integration"}
GIT_READ_OPERATIONS = {"status", "log", "diff", "show", "branch", "rev-parse", "remote", "tag", "worktree"}
GIT_READ_BLOCKED_ARGS = ("-c", "--config", "--config-env", "--exec-path", "--git-dir", "--work-tree", "-C", "--output", "--ext-diff", "--textconv", "--no-index")
GIT_WRITE_OPERATIONS = {"commit", "push", "commit-and-push"}
CLEANUP_PLAN_VERSION = 1
CLEANUP_SOURCE_PREFIXES = ("benchmark/", "side-agent/")
CLEANUP_DESTINATION_PREFIX = "feature/"
CLEANUP_BRANCH_RE = re.compile(
    r"^(?=.{1,240}$)(?![-.])(?!.*(?:\.\.|//|@\{))(?!.*[./]$)"
    r"(?!.*[ ~^:?*\\\[\]])[A-Za-z0-9_][A-Za-z0-9._/@-]*$"
)
MAX_CLEANUP_ITEMS = 256
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_COMMIT_MESSAGE = 4 * 1024
MAX_COMMIT_PATHS = 128
MAX_COMMIT_PATH = 1024
GIT_BRANCH_READ_FLAGS = {
    "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--no-abbrev",
    "--column", "--no-column", "-i", "--ignore-case", "--contains", "--no-contains",
    "--merged", "--no-merged", "--points-at", "--list",
}
GIT_BRANCH_READ_PREFIXES = ("--contains=", "--no-contains=", "--merged=", "--no-merged=", "--points-at=", "--sort=", "--format=", "--color=", "--column=", "--abbrev=")
GIT_TAG_READ_FLAGS = {
    "-l", "--list", "--column", "--no-column", "-i", "--ignore-case", "--contains",
    "--no-contains", "--merged", "--no-merged", "--points-at",
}
GIT_TAG_READ_PREFIXES = ("--contains=", "--no-contains=", "--merged=", "--no-merged=", "--points-at=", "--sort=", "--format=", "--color=", "--column=")
PROJECT_FIELDS = {
    "schemaVersion", "projectId", "gitCommonDir", "gitCommonDevice", "gitCommonInode",
    "objectFormat", "capabilityHash",
}
REGISTRY_FIELDS = {
    "schemaVersion", "projectId", "alias", "primaryRepository", "secretarySessionId",
    "registeredAt",
}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
BRANCH_RE = re.compile(r"^pi/[a-z0-9][a-z0-9-]{0,62}$")

class SecretaryError(RuntimeError):
    pass


def _env() -> dict[str, str]:
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0"}


def run(command: list[str], cwd: Path | None = None, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=str(cwd) if cwd else None, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=_env())
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise SecretaryError(f"{command[0]} failed: {detail[:300]}")
    return result


def _git(cwd: Path, *args: str) -> str:
    return run(["git", *args], cwd).stdout.strip()


def _git_path(cwd: Path, *args: str) -> Path:
    value = _git(cwd, *args)
    path = Path(value)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve(strict=True)


def _owner(mode: int, *, directory: bool) -> None:
    expected = 0o700 if directory else 0o600
    if stat.S_IMODE(mode) != expected:
        raise SecretaryError(f"unsafe permissions (expected {expected:04o})")


def _safe_lstat(path: Path, *, directory: bool | None = None, missing: bool = False, secure: bool = True) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise SecretaryError(f"missing state: {path}")
    except OSError as error:
        raise SecretaryError(f"cannot inspect state: {path}: {error}") from error
    if stat.S_ISLNK(info.st_mode):
        raise SecretaryError(f"symlink is not allowed: {path}")
    if directory is True and not stat.S_ISDIR(info.st_mode):
        raise SecretaryError(f"state path is not a directory: {path}")
    if directory is False and not stat.S_ISREG(info.st_mode):
        raise SecretaryError(f"state path is not a regular file: {path}")
    if secure and info.st_uid != os.getuid():
        raise SecretaryError(f"state path is not owned by invoking user: {path}")
    if secure and directory is not None:
        _owner(info.st_mode, directory=directory)
    return info


def _no_symlink_path(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise SecretaryError(f"cannot inspect path: {current}") from error
        if stat.S_ISLNK(info.st_mode):
            raise SecretaryError(f"symlink path escape: {current}")


def _ensure_dir(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        info = _safe_lstat(current, directory=True, missing=True, secure=False)
        if info is None:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            os.chmod(current, 0o700)
    _safe_lstat(absolute, directory=True)
    return absolute


def _state_root() -> Path:
    base = Path(os.path.expanduser(os.environ.get("XDG_STATE_HOME", "~/.local/state")))
    if not base.is_absolute():
        raise SecretaryError("XDG_STATE_HOME must be absolute")
    return _ensure_dir(base / "pi-secretary")


def within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _canonical_repo(repository: str | Path) -> Path:
    supplied = Path(os.path.expanduser(str(repository)))
    if not supplied.is_absolute():
        supplied = Path.cwd() / supplied
    _safe_lstat(supplied, directory=True, secure=False)
    try:
        return Path(_git(supplied, "rev-parse", "--show-toplevel")).resolve(strict=True)
    except (OSError, SecretaryError) as error:
        raise SecretaryError(f"not a Git repository: {supplied}") from error


def project_identity(repository: str | Path) -> tuple[str, Path, str]:
    repo = _canonical_repo(repository)
    common = _git_path(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    object_format = _git(repo, "rev-parse", "--show-object-format")
    if object_format not in SUPPORTED_OBJECT_FORMATS:
        raise SecretaryError("unsupported Git object format")
    return hashlib.sha256(str(common).encode("utf-8")).hexdigest(), common, object_format


def _common_identity(common: Path) -> tuple[int, int]:
    info = _safe_lstat(common, directory=True, secure=False)
    assert info is not None
    return info.st_dev, info.st_ino


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _default_alias(repo: Path) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", repo.name).strip("-._")[:48]
    return value if value and re.match(r"^[A-Za-z0-9]", value) else "project"


def _timestamp(value: Any, label: str = "timestamp") -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise SecretaryError(f"invalid {label}")
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError as error:
        raise SecretaryError(f"invalid {label}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SecretaryError(f"invalid {label}")
    return value


def _record_dir(root: Path, project_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", project_id):
        raise SecretaryError("invalid project id")
    return root / "projects" / project_id


def _assert_state_root_not_repo(root: Path, repo: Path) -> None:
    if within(repo, root):
        raise SecretaryError("state root must not be inside the repository")


def _atomic(path: Path, data: str, mode: int = 0o600) -> None:
    parent = _ensure_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(parent))
    temporary = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        _safe_lstat(path, directory=False)
        dfd = os.open(str(parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _read_json(path: Path, allowed: set[str], *, required: set[str] = set()) -> dict[str, Any]:
    info = _safe_lstat(path, directory=False)
    assert info is not None
    if info.st_size > MAX_JSON:
        raise SecretaryError("JSON record is too large")
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SecretaryError(f"malformed JSON record: {path}") from error
    if not isinstance(value, dict) or set(value) - allowed or not required.issubset(value):
        raise SecretaryError(f"invalid JSON record shape: {path}")
    return value


# --- Policy (delegated to workspace module for strict alignment) ---

def _load_policy_and_classify(repo: Path) -> tuple[dict[str, Any], bool, Path]:
    """Load policy via workspace module, classify repo, return policy, trusted-live flag, worktreeRoot."""
    policy = _WS.load_policy()
    if not policy.get("policyValid"):
        raise SecretaryError("repository policy is invalid or missing")
    policy_root = Path(policy["worktreeRoot"]).resolve(strict=True)
    mode, _ = _WS.classify(repo, policy)
    return policy, mode == "trusted-live", policy_root


# --- Capability ---

def _capability_path(root: Path, project_id: str) -> Path:
    return root / "capabilities" / f"{project_id}.token"


def _foundation(project: Path) -> None:
    for name in ("briefs", "workstreams", "workstream-runtime", "events", "events/inbox", "reviews", "reviews/requests", "reviews/receipts", "operations", "operations/facts"):
        _ensure_dir(project / name)
    inbox = project / "events/inbox.jsonl"
    if inbox.exists() or inbox.is_symlink():
        _safe_lstat(inbox, directory=False)
    else:
        _atomic(inbox, "")


def _fact_path(project: Path, operation: str, identifier: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z-]{0,63}", operation):
        raise SecretaryError("invalid operation fact")
    if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
        raise SecretaryError("invalid operation fact")
    digest = hashlib.sha256(f"{operation}\0{identifier}".encode()).hexdigest()
    return project / "operations" / "facts" / f"{digest}.json"


def _validate_fact(path: Path) -> tuple[str, str]:
    value = _read_json(path, {"operation", "id", "createdAt"},
                       required={"operation", "id", "createdAt"})
    operation = value.get("operation")
    identifier = value.get("id")
    if not isinstance(operation, str) or not isinstance(identifier, str):
        raise SecretaryError("malformed operation fact")
    if _fact_path(path.parents[2], operation, identifier) != path:
        raise SecretaryError("operation fact path does not match content")
    _timestamp(value.get("createdAt"), "operation timestamp")
    return operation, identifier


def _read_fact_keys(project: Path) -> set[tuple[str, str]]:
    directory = project / "operations" / "facts"
    _safe_lstat(directory, directory=True)
    seen: set[tuple[str, str]] = set()
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json":
            raise SecretaryError("unexpected operation fact")
        key = _validate_fact(path)
        if key in seen:
            raise SecretaryError("duplicate operation fact")
        seen.add(key)
    return seen


def _append_fact_locked(project: Path, operation: str, identifier: str) -> None:
    path = _fact_path(project, operation, identifier)
    if path.exists() or path.is_symlink():
        if _validate_fact(path) != (operation, identifier):
            raise SecretaryError("operation fact does not match")
        return
    value = {"operation": operation, "id": identifier, "createdAt": _utc_now()}
    _atomic(path, json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def _reconcile_facts_locked(project: Path, project_id: str) -> None:
    """Repair a record committed immediately before process interruption."""
    seen = _read_fact_keys(project)
    expected: list[tuple[str, str]] = [("project-initialized", project_id)]
    for directory_name, suffix, operation in (
        ("briefs", ".md", "brief-created"),
        ("workstreams", ".json", "workstream-created"),
    ):
        directory = project / directory_name
        _safe_lstat(directory, directory=True)
        for entry in sorted(directory.iterdir()):
            if entry.suffix != suffix:
                raise SecretaryError(f"unexpected {directory_name} record")
            _safe_lstat(entry, directory=False)
            _id(entry.stem, f"{directory_name} id")
            expected.append((operation, entry.stem))
    for operation, identifier in expected:
        if (operation, identifier) not in seen:
            _append_fact_locked(project, operation, identifier)


def _stored_capability(root: Path, project_id: str) -> str:
    path = _capability_path(root, project_id)
    info = _safe_lstat(path, directory=False)
    assert info is not None
    if info.st_size > 257:
        raise SecretaryError("capability rejected")
    try:
        stored = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise SecretaryError("capability rejected") from error
    if not stored or len(stored) > 256:
        raise SecretaryError("capability rejected")
    return stored


def _check_capability(root: Path, project_id: str, supplied: str | None, expected_hash: str) -> None:
    stored = _stored_capability(root, project_id)
    if (not isinstance(supplied, str) or not supplied or len(supplied) > 256 or
            not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
        raise SecretaryError("capability rejected")
    if (not hmac.compare_digest(hashlib.sha256(stored.encode()).hexdigest(), expected_hash) or
            not hmac.compare_digest(stored, supplied)):
        raise SecretaryError("capability rejected")


# --- Lock ---

@contextlib.contextmanager
def _project_lock(project: Path) -> Iterator[None]:
    _ensure_dir(project)
    path = project / ".lock"
    if path.exists() or path.is_symlink():
        _safe_lstat(path, directory=False)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def _review_launch_lock(project: Path, request_id: str) -> Iterator[None]:
    directory = project / "operations"
    _ensure_dir(directory)
    path = directory / "cleanup.lock"
    if path.exists() or path.is_symlink():
        _safe_lstat(path, directory=False)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def _workstream_launch_lock(project: Path, workstream_id: str) -> Iterator[None]:
    directory = project / "operations"
    _ensure_dir(directory)
    path = directory / "cleanup.lock"
    if path.exists() or path.is_symlink():
        _safe_lstat(path, directory=False)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def _workstream_cleanup_lock(project: Path) -> Iterator[None]:
    directory = project / "operations"
    _ensure_dir(directory)
    path = directory / "cleanup.lock"
    if path.exists() or path.is_symlink():
        _safe_lstat(path, directory=False)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# --- Project context (revalidates identity, capability, policy) ---

def _validate_project_record(path: Path, project_id: str, common: Path,
                             object_format: str) -> dict[str, Any]:
    record = _read_json(path, PROJECT_FIELDS, required=PROJECT_FIELDS)
    device, inode = _common_identity(common)
    if (record.get("schemaVersion") != 1 or record.get("projectId") != project_id or
            record.get("gitCommonDir") != str(common) or
            record.get("gitCommonDevice") != device or record.get("gitCommonInode") != inode or
            record.get("objectFormat") != object_format):
        raise SecretaryError("project identity does not match current repository")
    cap_hash = record.get("capabilityHash")
    if not isinstance(cap_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", cap_hash):
        raise SecretaryError("malformed capability hash in project record")
    # Registry lookup is always a capability integrity check, even when the
    # secret is intentionally not returned to the caller.
    stored = _stored_capability(path.parents[2], project_id)
    if not hmac.compare_digest(hashlib.sha256(stored.encode()).hexdigest(), cap_hash):
        raise SecretaryError("capability rejected")
    return record


def _project_context(repository: str | Path, capability: str | None, *,
                     require_capability: bool = True,
                     reconcile_facts: bool = True) -> tuple[Path, Path, dict[str, Any], Path]:
    repo = _canonical_repo(repository)
    project_id, common, object_format = project_identity(repo)
    root = _state_root()
    _assert_state_root_not_repo(root, repo)
    project = _record_dir(root, project_id)
    _safe_lstat(project, directory=True)
    record = _validate_project_record(project / "project.json", project_id, common, object_format)
    if require_capability:
        _check_capability(root, project_id, capability, record["capabilityHash"])
    if reconcile_facts:
        with _project_lock(project):
            _foundation(project)
            _reconcile_facts_locked(project, project_id)
    return root, project, record, repo


# --- Init / Status ---

def init_project(repository: str | Path) -> dict[str, Any]:
    repo = _canonical_repo(repository)
    project_id, common, object_format = project_identity(repo)
    common_device, common_inode = _common_identity(common)
    root = _state_root()
    _assert_state_root_not_repo(root, repo)
    project = _record_dir(root, project_id)
    capability_path = _capability_path(root, project_id)
    _ensure_dir(root / "projects")
    _ensure_dir(root / "capabilities")
    with _project_lock(project):
        record_path = project / "project.json"
        if record_path.exists() or record_path.is_symlink():
            record = _validate_project_record(record_path, project_id, common, object_format)
            stored_capability = _stored_capability(root, project_id)
            _check_capability(root, project_id, stored_capability, record["capabilityHash"])
            _foundation(project)
            _reconcile_facts_locked(project, project_id)
            return {"projectId": project_id, "initialized": False, "capability": None}
        capability = secrets.token_urlsafe(48)
        capability_hash = hashlib.sha256(capability.encode()).hexdigest()
        try:
            _atomic(record_path, json.dumps({"schemaVersion": 1, "projectId": project_id,
                                             "gitCommonDir": str(common),
                                             "gitCommonDevice": common_device,
                                             "gitCommonInode": common_inode,
                                             "objectFormat": object_format,
                                             "capabilityHash": capability_hash},
                                            sort_keys=True, separators=(",", ":")) + "\n")
            _atomic(capability_path, capability + "\n")
            _foundation(project)
            _append_fact_locked(project, "project-initialized", project_id)
        except Exception:
            for created in (record_path, capability_path):
                with contextlib.suppress(OSError):
                    created.unlink()
            raise
        return {"projectId": project_id, "initialized": True, "capability": capability}


def status(repository: str | Path, capability: str | None = None) -> dict[str, Any]:
    repo = _canonical_repo(repository)
    project_id, _, _ = project_identity(repo)
    root = _state_root()
    _assert_state_root_not_repo(root, repo)
    project = _record_dir(root, project_id)
    if not project.exists() and not project.is_symlink():
        return {"projectId": project_id, "initialized": False}
    _project_context(repo, capability)
    return {"projectId": project_id, "initialized": True}


def _git_read_command_args(operation: str, git_args: list[str]) -> list[str]:
    """Constrain multi-mode Git subcommands to listing/query forms only."""
    if operation == "branch":
        for value in git_args:
            if value.startswith("-") and value not in GIT_BRANCH_READ_FLAGS and not value.startswith(GIT_BRANCH_READ_PREFIXES):
                raise SecretaryError("git branch option is not a read-only listing option")
        return ["--list", *git_args]
    if operation == "tag":
        for value in git_args:
            numbered_lines = re.fullmatch(r"-n\d*", value) is not None
            if value.startswith("-") and not numbered_lines and value not in GIT_TAG_READ_FLAGS and not value.startswith(GIT_TAG_READ_PREFIXES):
                raise SecretaryError("git tag option is not a read-only listing option")
        return ["--list", *git_args]
    if operation == "remote":
        if not git_args or all(value in {"-v", "--verbose"} for value in git_args):
            return git_args
        if git_args[0] == "get-url" and len(git_args) >= 2:
            options = git_args[1:-1]
            if all(value in {"--all", "--push"} for value in options) and not git_args[-1].startswith("-"):
                return git_args
        if git_args[0] == "show" and len(git_args) in {2, 3}:
            name = git_args[-1]
            if not name.startswith("-") and (len(git_args) == 2 or git_args[1] == "-n"):
                return ["show", "-n", name]
        raise SecretaryError("git remote operation is not a read-only local query")
    if operation == "worktree":
        if not git_args or git_args[0] != "list" or any(
                value not in {"--porcelain", "-z", "-v", "--verbose"} for value in git_args[1:]):
            raise SecretaryError("only git worktree list is supported")
    if operation in {"diff", "log", "show"}:
        return ["--no-ext-diff", "--no-textconv", *git_args]
    return git_args


def git_read(project_id: str, operation: str, git_args: list[str]) -> dict[str, Any]:
    """Run one bounded, read-only Git operation in the registered repository."""
    if operation not in GIT_READ_OPERATIONS:
        raise SecretaryError("Git operation is not read-only or not supported")
    if not isinstance(git_args, list):
        raise SecretaryError("invalid Git arguments")
    if git_args[:1] == ["--"]:
        git_args = git_args[1:]
    if len(git_args) > 32:
        raise SecretaryError("too many Git arguments")
    for value in git_args:
        if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 for c in value):
            raise SecretaryError("invalid Git argument")
        if value in GIT_READ_BLOCKED_ARGS or any(value.startswith(f"{prefix}=") for prefix in GIT_READ_BLOCKED_ARGS):
            raise SecretaryError("Git configuration and repository overrides are not allowed")
        if value in {"--exit-code", "--quiet", "--no-optional-locks", "-d", "-D", "--delete", "--force"}:
            raise SecretaryError("Git argument is not supported")
    command_args = _git_read_command_args(operation, git_args)
    info = launch_info(project_id)
    repo = Path(info["primaryRepository"])
    environment = _env()
    environment.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "PAGER": "cat"})
    command = [
        "git", "--no-pager", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "gpg.program=/bin/false", "-c", "gpg.ssh.program=/bin/false",
        operation, *command_args,
    ]
    result = subprocess.run(command, cwd=str(repo), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=environment)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise SecretaryError(f"git {operation} failed: {detail[:500]}")
    if len(result.stdout.encode()) > MAX_JSON or len(result.stderr.encode()) > MAX_JSON:
        raise SecretaryError("Git output is too large")
    return {"projectId": project_id, "operation": operation, "args": git_args,
            "stdout": result.stdout, "stderr": result.stderr}


def _git_write_path(repo: Path, value: str) -> str:
    if (not isinstance(value, str) or not value or len(value.encode()) > MAX_COMMIT_PATH or
            value.startswith(("/", "\\")) or value.startswith(("-", "!", "^")) or
            any(ord(char) < 32 for char in value) or any(char in value for char in "*?[]:") or
            any(part in {"", ".", ".."} for part in value.split("/"))):
        raise SecretaryError("Git commit paths must be explicit relative paths")
    candidate = (repo / value).resolve(strict=False)
    if not within(repo, candidate):
        raise SecretaryError("Git commit path escapes the registered repository")
    return value


def _git_write_paths(repo: Path, paths: list[str] | None) -> list[str]:
    if not isinstance(paths, list) or not paths or len(paths) > MAX_COMMIT_PATHS:
        raise SecretaryError("Git commit requires a non-empty explicit path list")
    result = [_git_write_path(repo, value) for value in paths]
    if len(set(result)) != len(result):
        raise SecretaryError("Git commit path list contains duplicates")
    return result


def _git_write_message(message: str | None) -> str:
    if not isinstance(message, str) or not message or len(message.encode()) > MAX_COMMIT_MESSAGE or not message.strip():
        raise SecretaryError("Git commit requires an explicit non-empty message")
    if any(ord(char) < 32 and char not in "\n\t" for char in message):
        raise SecretaryError("invalid Git commit message")
    return message


def _current_branch(repo: Path) -> str:
    result = run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], repo, check=False)
    branch = result.stdout.strip()
    if (result.returncode != 0 or not branch or len(branch.encode()) > 512 or
            any(char.isspace() for char in branch) or branch.startswith("-") or
            branch.endswith(("/", ".")) or ".." in branch or "@{" in branch or "//" in branch):
        raise SecretaryError("Git write requires a checked-out branch")
    full_ref = run(["git", "symbolic-ref", "--quiet", "HEAD"], repo, check=False).stdout.strip()
    if full_ref != f"refs/heads/{branch}":
        raise SecretaryError("Git write requires the current checked-out branch")
    return branch


def _git_write_command(repo: Path, operation: str, command: list[str], *, index_file: Path | None = None) -> None:
    environment = _env()
    # Use the host process's normal user Git configuration and credential
    # helpers, and preserve only the host SSH-agent socket when present. Never
    # pass model data as an auth setting, run repository hooks, or allow prompts.
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat"})
    ssh_auth_sock = os.environ.get("SSH_AUTH_SOCK", "")
    if ssh_auth_sock.startswith("/") and "\n" not in ssh_auth_sock and "\x00" not in ssh_auth_sock:
        environment["SSH_AUTH_SOCK"] = ssh_auth_sock
    if index_file is not None:
        environment["GIT_INDEX_FILE"] = str(index_file)
    result = subprocess.run(command, cwd=str(repo), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=environment)
    if result.returncode:
        # Git may include remote URLs or helper diagnostics in either stream;
        # never return those details to the secretary or model.
        raise SecretaryError(f"git {operation} failed (exit {result.returncode})")


@contextlib.contextmanager
def _temporary_git_index(repo: Path, common: Path) -> Iterator[Path]:
    index_value = _git(repo, "rev-parse", "--git-path", "index")
    index_path = Path(index_value)
    if not index_path.is_absolute():
        index_path = (repo / index_path).resolve(strict=False)
    fd, temporary = tempfile.mkstemp(prefix="pi-secretary-index-", dir=str(common))
    os.close(fd)
    temporary_path = Path(temporary)
    try:
        if index_path.is_file():
            temporary_path.write_bytes(index_path.read_bytes())
        yield temporary_path
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def _refresh_git_index(repo: Path, paths: list[str]) -> None:
    result = subprocess.run(["git", "reset", "HEAD", "--", *paths], cwd=str(repo),
                            env=_env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, check=False)
    if result.returncode:
        raise SecretaryError("Git index refresh failed")


@contextlib.contextmanager
def _git_write_lock(common: Path) -> Iterator[None]:
    path = common / "pi-secretary-git-write.lock"
    if path.exists() or path.is_symlink():
        _safe_lstat(path, directory=False)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def _git_worktree_index_lock(worktree: Path) -> Iterator[Path]:
    index_value = _git(worktree, "rev-parse", "--git-path", "index")
    index_path = Path(index_value)
    if not index_path.is_absolute():
        index_path = (worktree / index_path).resolve(strict=False)
    lock_path = Path(f"{index_path}.lock")
    token = secrets.token_hex(16).encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(lock_path), flags, 0o600)
    except FileExistsError as error:
        raise SecretaryError("Git worktree is busy") from error
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, token)
        os.fsync(fd)
        yield index_path
    finally:
        os.close(fd)
        try:
            if lock_path.read_bytes() == token:
                lock_path.unlink()
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _temporary_worktree_index(index_path: Path) -> Iterator[Path]:
    fd, raw_path = tempfile.mkstemp(prefix=".pi-secretary-index-", dir=str(index_path.parent))
    os.close(fd)
    temporary = Path(raw_path)
    try:
        if index_path.is_file():
            temporary.write_bytes(index_path.read_bytes())
        yield temporary
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _repair_landed_worktree_index(target: Path, index_path: Path, candidate: str) -> None:
    with _temporary_worktree_index(index_path) as temporary_index:
        environment = _env()
        environment["GIT_INDEX_FILE"] = str(temporary_index)
        read_tree = subprocess.run(["git", "-C", str(target), "read-tree", "--reset", candidate],
                                    env=environment, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, check=False)
        if read_tree.returncode:
            raise SecretaryError("could not reconstruct landed target index")
        clean = subprocess.run(["git", "-C", str(target), "status", "--porcelain=v1", "--untracked-files=all"],
                               env=environment, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, check=False)
        if clean.returncode or clean.stdout:
            raise SecretaryError("landed target worktree is not clean at the candidate commit")
        os.replace(temporary_index, index_path)


def git_write(project_id: str, operation: str, message: str | None = None,
              paths: list[str] | None = None) -> dict[str, Any]:
    """Commit and/or push only the registered repository's current branch."""
    if operation not in GIT_WRITE_OPERATIONS:
        raise SecretaryError("Git write operation is not supported")
    if operation == "push" and (message is not None or paths):
        raise SecretaryError("Git push does not accept commit arguments")

    initial = _require_secretary(project_id)
    initial_repo = _canonical_repo(initial["primaryRepository"])
    _, initial_common, _ = project_identity(initial_repo)
    with _git_write_lock(initial_common):
        current = _require_secretary(project_id)
        repo = _canonical_repo(current["primaryRepository"])
        _, common, _ = project_identity(repo)
        if repo != initial_repo or common != initial_common:
            raise SecretaryError("registered project changed during Git write authorization")
        # Revalidate durable project identity and capability while holding the
        # same common-dir lock used for the Git mutation.
        _project_context(repo, current["capability"])
        branch = _current_branch(repo)
        if operation in {"push", "commit-and-push"}:
            remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(repo),
                                    env=_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    text=True, check=False)
            if remote.returncode:
                raise SecretaryError("Git push requires the existing origin remote")
        commit_oid: str | None = None
        if operation in {"commit", "commit-and-push"}:
            commit_message = _git_write_message(message)
            commit_paths = _git_write_paths(repo, paths)
            # Stage and commit through a temporary index. This preserves any
            # unrelated pre-staged paths and leaves the real index unchanged
            # if validation or Git commit fails.
            with _temporary_git_index(repo, common) as temporary_index:
                if temporary_index.stat().st_size == 0:
                    _git_write_command(repo, "initialize-index", ["git", "read-tree", "--empty"], index_file=temporary_index)
                _git_write_command(repo, "stage", [
                    "git", "-c", "core.hooksPath=/dev/null", "add", "--", *commit_paths,
                ], index_file=temporary_index)
                _git_write_command(repo, "commit", [
                    "git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                    "commit", "--only", "--no-verify", "-m", commit_message, "--", *commit_paths,
                ], index_file=temporary_index)
                _refresh_git_index(repo, commit_paths)
            if _current_branch(repo) != branch:
                raise SecretaryError("Git branch changed during commit")
            commit_oid = _git(repo, "rev-parse", "HEAD^{commit}").lower()

        if operation in {"push", "commit-and-push"}:
            if _current_branch(repo) != branch:
                raise SecretaryError("Git branch changed before push")
            _git_write_command(repo, "push", [
                "git", "-c", "core.hooksPath=/dev/null", "push", "--no-verify", "origin",
                f"refs/heads/{branch}:refs/heads/{branch}",
            ])

        result: dict[str, Any] = {"projectId": project_id, "operation": operation,
                                  "branch": branch}
        if commit_oid is not None:
            result["commit"] = commit_oid
        if operation in {"push", "commit-and-push"}:
            result["remote"] = "origin"
            result["pushed"] = True
        return result


def _cleanup_branch_name(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not CLEANUP_BRANCH_RE.fullmatch(value) or
            value.startswith("refs/") or
            any(part in {"", ".", ".."} or part.startswith(".") or part.endswith(".") or part.endswith(".lock")
                for part in value.split("/"))):
        raise SecretaryError(f"invalid cleanup {label}")
    return value


def _cleanup_source_branch(value: Any, label: str) -> str:
    branch = _cleanup_branch_name(value, label)
    if not branch.startswith(CLEANUP_SOURCE_PREFIXES):
        raise SecretaryError(f"cleanup {label} is outside the owned namespaces")
    return branch


def _cleanup_destination_branch(value: Any) -> str:
    branch = _cleanup_branch_name(value, "destination branch")
    if not branch.startswith(CLEANUP_DESTINATION_PREFIX):
        raise SecretaryError("cleanup destination branch must use the feature/ namespace")
    return branch


def _cleanup_absolute_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4096 or not os.path.isabs(value):
        raise SecretaryError(f"cleanup {label} must be an absolute path")
    path = Path(value).resolve(strict=False)
    _no_symlink_path(path)
    return path


def _cleanup_artifact_root() -> Path:
    raw = os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent"))
    root = Path(os.path.expanduser(raw))
    if not root.is_absolute():
        raise SecretaryError("Pi agent directory must be absolute")
    return root.resolve(strict=False)


def _cleanup_artifact_allowed(path: Path, kind: str, repository: Path,
                              worktree_root: Path) -> None:
    if kind not in {"subagent-artifact", "workflow-artifact"}:
        raise SecretaryError("invalid cleanup artifact kind")
    if (within(repository, path) or within(path, repository) or
            within(worktree_root, path) or within(path, worktree_root)):
        raise SecretaryError("cleanup artifact cannot be inside the repository or worktree root")
    agent_root = _cleanup_artifact_root()
    sessions = agent_root / "sessions"
    relative = path.relative_to(sessions) if within(sessions, path) else None
    if kind == "workflow-artifact":
        if relative is None or "workflow-artifacts" not in relative.parts:
            raise SecretaryError("workflow artifact is outside the Pi workflow artifact namespace")
        return
    if relative is not None and "subagent-artifacts" in relative.parts:
        return
    temporary_root = Path(tempfile.gettempdir()).resolve(strict=False)
    if within(temporary_root, path):
        temporary_relative = path.relative_to(temporary_root)
        if temporary_relative.parts and temporary_relative.parts[0].startswith("pi-subagents-"):
            return
    raise SecretaryError("subagent artifact is outside the Pi artifact namespaces")


def _cleanup_artifact_identity(info: os.stat_result) -> str:
    # ctime changes when Git moves the file into quarantine; device/inode,
    # size, and mtime remain stable while still pinning the planned content.
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"


def _cleanup_lstat(path: Path, *, directory: bool | None = None,
                   missing: bool = False) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise SecretaryError(f"missing cleanup path: {path}")
    except OSError as error:
        raise SecretaryError(f"cannot inspect cleanup path: {path}") from error
    if stat.S_ISLNK(info.st_mode) or (directory is True and not stat.S_ISDIR(info.st_mode)) or \
            (directory is False and not stat.S_ISREG(info.st_mode)):
        raise SecretaryError(f"unsafe cleanup path: {path}")
    if info.st_uid != os.getuid():
        raise SecretaryError(f"cleanup path is not owned by invoking user: {path}")
    return info


def _cleanup_inode(path: Path, *, directory: bool = False) -> str:
    info = _cleanup_lstat(path, directory=True if directory else False)
    assert info is not None
    return f"{info.st_dev}:{info.st_ino}"


def _cleanup_parent_fd(path: Path, expected_identity: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
        info = os.fstat(fd)
        current = path.lstat()
    except OSError as error:
        raise SecretaryError(f"cannot securely open cleanup artifact directory: {path}") from error
    actual = f"{info.st_dev}:{info.st_ino}"
    current_identity = f"{current.st_dev}:{current.st_ino}"
    if actual != expected_identity or current_identity != expected_identity:
        os.close(fd)
        raise SecretaryError(f"cleanup artifact directory identity changed: {path}")
    return fd


def _cleanup_artifact_digest_fd(fd: int, info: os.stat_result) -> str:
    if info.st_size > MAX_ARTIFACT_BYTES:
        raise SecretaryError("cleanup artifact is too large")
    before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    digest = hashlib.sha256()
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after_info = os.fstat(fd)
    except OSError as error:
        raise SecretaryError("cannot read cleanup artifact") from error
    after = (after_info.st_dev, after_info.st_ino, after_info.st_size,
             after_info.st_mtime_ns, after_info.st_ctime_ns)
    if before != after:
        raise SecretaryError("cleanup artifact changed during hashing")
    return digest.hexdigest()


def _cleanup_artifact_snapshot(path: Path, expected_identity: str,
                               expected_parent_identity: str) -> str:
    parent_fd = _cleanup_parent_fd(path.parent, expected_parent_identity)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            fd = os.open(path.name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise SecretaryError(f"cannot securely open cleanup artifact: {path}") from error
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or _cleanup_artifact_identity(info) != expected_identity:
                raise SecretaryError(f"cleanup artifact identity changed: {path}")
            digest = _cleanup_artifact_digest_fd(fd, info)
            try:
                current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as error:
                raise SecretaryError(f"cannot recheck cleanup artifact: {path}") from error
            if _cleanup_artifact_identity(current) != expected_identity:
                raise SecretaryError(f"cleanup artifact identity changed: {path}")
            return digest
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _rename_cleanup_noreplace(src_dir_fd: int, src_name: str,
                              dst_dir_fd: int, dst_name: str) -> None:
    # A check followed by Path.rename() can overwrite an attacker-created
    # quarantine pathname. Linux renameat2(RENAME_NOREPLACE) gives us the
    # required atomic destination check while the directory descriptors pin
    # both parents. Unsupported platforms fail closed rather than downgrade.
    if not sys.platform.startswith("linux"):
        raise SecretaryError("platform cannot guarantee atomic cleanup quarantine")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                              ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(src_dir_fd, os.fsencode(src_name), dst_dir_fd,
                           os.fsencode(dst_name), 1)
    except AttributeError:
        syscall_numbers = {"x86_64": 316, "amd64": 316, "aarch64": 276,
                           "armv7l": 382, "ppc64le": 357, "s390x": 347}
        number = syscall_numbers.get(os.uname().machine)
        if number is None:
            raise SecretaryError("platform cannot guarantee atomic cleanup quarantine")
        syscall = libc.syscall
        syscall.argtypes = [ctypes.c_long, ctypes.c_int, ctypes.c_char_p,
                            ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        syscall.restype = ctypes.c_long
        result = syscall(number, src_dir_fd, os.fsencode(src_name), dst_dir_fd,
                         os.fsencode(dst_name), 1)
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise SecretaryError("cleanup quarantine destination already exists")
        raise SecretaryError(f"could not atomically quarantine cleanup artifact: {os.strerror(error_number)}")


def _artifact_quarantine_path(path: Path, plan_hash: str) -> Path:
    key = hashlib.sha256(str(path).encode()).hexdigest()[:16]
    return path.with_name(f".{path.name}.cleanup-{plan_hash}-{key}")


def _quarantine_delete_artifact(path: Path, expected_sha256: str,
                                expected_identity: str, expected_parent_identity: str,
                                plan_hash: str, *, owned_quarantine: str | None = None,
                                on_intent: Any | None = None,
                                on_quarantine: Any | None = None) -> None:
    quarantine = _artifact_quarantine_path(path, plan_hash)
    parent_fd = _cleanup_parent_fd(path.parent, expected_parent_identity)
    try:
        q_info: os.stat_result | None
        try:
            q_info = os.stat(quarantine.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            q_info = None
        except OSError as error:
            raise SecretaryError(f"cannot inspect cleanup quarantine: {quarantine}") from error
        source_info = _cleanup_lstat(path, directory=False, missing=True)
        if q_info is not None and source_info is not None:
            raise SecretaryError(f"cleanup artifact source reappeared beside quarantine: {path}")
        if q_info is None and source_info is None:
            raise SecretaryError(f"cleanup artifact source and quarantine are both missing: {path}")
        if q_info is not None:
            if (not stat.S_ISREG(q_info.st_mode) or
                    _cleanup_artifact_identity(q_info) != expected_identity or
                    owned_quarantine != _cleanup_artifact_identity(q_info)):
                raise SecretaryError(f"cleanup found an unowned or changed artifact quarantine: {quarantine}")
        else:
            source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                source_fd = os.open(path.name, source_flags, dir_fd=parent_fd)
            except OSError as error:
                raise SecretaryError(f"could not open cleanup artifact before quarantine: {path}") from error
            try:
                source_info = os.fstat(source_fd)
                if (not stat.S_ISREG(source_info.st_mode) or
                        _cleanup_artifact_identity(source_info) != expected_identity):
                    raise SecretaryError(f"cleanup artifact identity changed: {path}")
                digest = _cleanup_artifact_digest_fd(source_fd, source_info)
                if digest != expected_sha256:
                    raise SecretaryError(f"cleanup artifact changed during apply: {path}")
                if on_intent is not None:
                    on_intent(quarantine, expected_identity)
                _rename_cleanup_noreplace(parent_fd, path.name, parent_fd, quarantine.name)
            finally:
                os.close(source_fd)
            try:
                q_info = os.stat(quarantine.name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as error:
                raise SecretaryError(f"could not inspect quarantined cleanup artifact: {path}") from error
            actual_identity = _cleanup_artifact_identity(q_info)
            if on_quarantine is not None:
                on_quarantine(quarantine, actual_identity)
            if actual_identity != expected_identity:
                raise SecretaryError(f"cleanup quarantined an unexpected artifact: {path}")
        assert q_info is not None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(quarantine.name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise SecretaryError(f"could not open quarantined cleanup artifact: {path}") from error
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or
                    _cleanup_artifact_identity(info) != expected_identity):
                raise SecretaryError("cleanup quarantine identity changed during apply")
            digest = _cleanup_artifact_digest_fd(fd, info)
            if digest != expected_sha256:
                raise SecretaryError(f"cleanup artifact changed during apply: {path}")
            current = os.stat(quarantine.name, dir_fd=parent_fd, follow_symlinks=False)
            if _cleanup_artifact_identity(current) != _cleanup_artifact_identity(info):
                raise SecretaryError("cleanup quarantine identity changed during apply")
            os.unlink(quarantine.name, dir_fd=parent_fd)
        except OSError as error:
            raise SecretaryError(f"could not remove quarantined cleanup artifact: {path}") from error
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _cleanup_worktree_records(repo: Path) -> list[dict[str, Any]]:
    output = _git(repo, "worktree", "list", "--porcelain")
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def finish() -> None:
        nonlocal current
        if current is not None:
            if not isinstance(current.get("path"), Path) or not isinstance(current.get("head"), str):
                raise SecretaryError("malformed Git worktree listing")
            records.append(current)
        current = None

    for line in output.splitlines() + [""]:
        if line.startswith("worktree "):
            finish()
            current = {"path": Path(line[9:]).resolve(strict=False), "head": None,
                       "branch": None, "locked": False, "prunable": False}
        elif current is not None and line.startswith("HEAD "):
            current["head"] = line[5:].strip().lower()
        elif current is not None and line.startswith("branch refs/heads/"):
            current["branch"] = line[len("branch refs/heads/"):]
        elif current is not None and line.startswith("locked"):
            current["locked"] = True
        elif current is not None and line.startswith("prunable"):
            current["prunable"] = True
        elif not line:
            finish()
    return records


def _cleanup_worktree_live_in_registry(project_id: str, path: Path) -> bool:
    project = _record_dir(_state_root(), project_id)
    directory = project / "workstreams"
    if directory.exists():
        _safe_lstat(directory, directory=True)
        for record_path in directory.glob("*.json"):
            record = _read_json(record_path, WORKSTREAM_FIELDS, required=WORKSTREAM_FIELDS)
            if record.get("closedAt") is None and Path(record["workspace"]).resolve(strict=False) == path:
                return True

    registry = _cleanup_artifact_root() / "root-registry.json"
    if not registry.exists() or registry.is_symlink():
        return False
    _safe_lstat(registry, directory=False)
    if registry.stat().st_size > MAX_JSON:
        raise SecretaryError("root registry is too large")
    try:
        value = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SecretaryError("root registry is malformed") from error
    records = value.get("records") if isinstance(value, dict) else None
    if not isinstance(records, list):
        raise SecretaryError("root registry is malformed")
    for record in records:
        if (isinstance(record, dict) and record.get("status") == "active" and
                isinstance(record.get("worktree"), str) and
                Path(record["worktree"]).resolve(strict=False) == path):
            return True
    return False


def _cleanup_worktree_has_live_process(path: Path) -> bool:
    path = path.resolve(strict=False)
    try:
        current = Path.cwd().resolve(strict=False)
    except OSError as error:
        raise SecretaryError("cleanup cannot inspect current process cwd") from error
    if current == path or within(path, current):
        return True
    proc = Path("/proc")
    if not proc.is_dir():
        try:
            result = subprocess.run(["lsof", "-nP", "-a", "-d", "cwd", "-F", "n"],
                                    env=_env(), text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, check=False)
        except FileNotFoundError as error:
            raise SecretaryError("cleanup cannot prove process absence on this platform") from error
        if result.returncode not in {0, 1} or result.stderr.strip():
            raise SecretaryError("cleanup cannot prove process absence on this platform")
        for line in result.stdout.splitlines():
            if not line.startswith("n"):
                continue
            raw_cwd = line[1:]
            if raw_cwd.endswith(" (deleted)"):
                raw_cwd = raw_cwd[:-10]
            try:
                cwd = Path(raw_cwd).resolve(strict=False)
            except OSError as error:
                raise SecretaryError("cleanup received an unreadable process cwd") from error
            if cwd == path or within(path, cwd):
                return True
        return False
    try:
        entries = list(proc.iterdir())
    except OSError as error:
        raise SecretaryError("cleanup cannot inspect process table") from error
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
        except FileNotFoundError:
            continue
        except OSError as error:
            raise SecretaryError("cleanup cannot inspect process ownership") from error
        try:
            raw_cwd = os.readlink(entry / "cwd")
        except FileNotFoundError:
            continue
        except PermissionError as error:
            try:
                command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
            except OSError as command_error:
                raise SecretaryError("cleanup cannot inspect a process cwd") from command_error
            # Linux user-systemd intentionally hides its cwd even from the
            # same UID; it cannot be a Pi worktree process unless its command
            # identity says otherwise. All other unreadable processes fail closed.
            if not ((command.startswith("/usr/lib/systemd/systemd") and "--user" in command) or
                    command.startswith("(sd-pam)")):
                raise SecretaryError("cleanup cannot inspect a process cwd") from error
            continue
        except OSError as error:
            raise SecretaryError("cleanup cannot inspect a process cwd") from error
        if raw_cwd.endswith(" (deleted)"):
            raw_cwd = raw_cwd[:-10]
        try:
            cwd = Path(raw_cwd).resolve(strict=False)
        except OSError as error:
            raise SecretaryError("cleanup received an unreadable process cwd") from error
        if cwd == path or within(path, cwd):
            return True
    return False


def _cleanup_branch_oid(repo: Path, branch: str, object_format: str) -> str | None:
    result = run(["git", "rev-parse", "--verify", "--quiet", "--end-of-options",
                  f"refs/heads/{branch}^{{commit}}"], repo, check=False)
    if result.returncode == 1:
        return None
    if result.returncode:
        raise SecretaryError("could not inspect cleanup branch")
    return _validate_oid(result.stdout.strip(), object_format)


def _normalize_cleanup_plan(raw: Any, object_format: str, *, require_artifact_identity: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {"version", "renames", "deletions", "worktrees", "artifacts"}:
        raise SecretaryError("invalid cleanup plan shape")
    if raw.get("version", CLEANUP_PLAN_VERSION) != CLEANUP_PLAN_VERSION:
        raise SecretaryError("unsupported cleanup plan version")

    def entries(name: str) -> list[Any]:
        value = raw.get(name, [])
        if not isinstance(value, list) or len(value) > MAX_CLEANUP_ITEMS:
            raise SecretaryError(f"invalid cleanup plan {name}")
        return value

    renames: list[dict[str, str]] = []
    for item in entries("renames"):
        if not isinstance(item, dict) or set(item) != {"from", "to", "expectedOid"}:
            raise SecretaryError("invalid cleanup branch rename")
        renames.append({
            "from": _cleanup_source_branch(item["from"], "source branch"),
            "to": _cleanup_destination_branch(item["to"]),
            "expectedOid": _validate_oid(item["expectedOid"], object_format),
        })

    deletions: list[dict[str, str]] = []
    for item in entries("deletions"):
        if not isinstance(item, dict) or set(item) != {"branch", "expectedOid"}:
            raise SecretaryError("invalid cleanup branch deletion")
        deletions.append({
            "branch": _cleanup_source_branch(item["branch"], "branch"),
            "expectedOid": _validate_oid(item["expectedOid"], object_format),
        })

    worktrees: list[dict[str, str]] = []
    for item in entries("worktrees"):
        if not isinstance(item, dict) or set(item) != {"path", "branch", "expectedOid"}:
            raise SecretaryError("invalid cleanup worktree removal")
        worktrees.append({
            "path": str(_cleanup_absolute_path(item["path"], "worktree path")),
            "branch": _cleanup_source_branch(item["branch"], "worktree branch"),
            "expectedOid": _validate_oid(item["expectedOid"], object_format),
        })

    artifacts: list[dict[str, str]] = []
    for item in entries("artifacts"):
        allowed = {"path", "kind", "expectedSha256", "expectedIdentity", "expectedParentIdentity"}
        if not isinstance(item, dict) or not set(item) <= allowed or not {"path", "kind", "expectedSha256"} <= set(item):
            raise SecretaryError("invalid cleanup artifact")
        path = _cleanup_absolute_path(item["path"], "artifact path")
        kind = item["kind"]
        if kind not in {"subagent-artifact", "workflow-artifact"}:
            raise SecretaryError("invalid cleanup artifact kind")
        digest = item["expectedSha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise SecretaryError("invalid cleanup artifact digest")
        expected_identity = item.get("expectedIdentity")
        expected_parent_identity = item.get("expectedParentIdentity")
        if require_artifact_identity and (not isinstance(expected_identity, str) or
                                           not isinstance(expected_parent_identity, str)):
            raise SecretaryError("cleanup apply requires pinned artifact identities")
        if expected_identity is None:
            info = _cleanup_lstat(path, directory=False)
            assert info is not None
            expected_identity = _cleanup_artifact_identity(info)
        if expected_parent_identity is None:
            expected_parent_identity = _cleanup_inode(path.parent, directory=True)
        if (not isinstance(expected_identity, str) or
                not re.fullmatch(r"[0-9]+:[0-9]+:[0-9]+:[0-9]+", expected_identity) or
                not isinstance(expected_parent_identity, str) or
                not re.fullmatch(r"[0-9]+:[0-9]+", expected_parent_identity)):
            raise SecretaryError("invalid cleanup artifact identity")
        artifacts.append({"path": str(path), "kind": kind, "expectedSha256": digest.lower(),
                          "expectedIdentity": expected_identity,
                          "expectedParentIdentity": expected_parent_identity})

    if not renames and not deletions and not worktrees and not artifacts:
        raise SecretaryError("cleanup plan is empty")
    if len({item["from"] for item in renames}) != len(renames) or len({item["to"] for item in renames}) != len(renames):
        raise SecretaryError("cleanup plan contains duplicate branch rename")
    if len({item["branch"] for item in deletions}) != len(deletions):
        raise SecretaryError("cleanup plan contains duplicate branch deletion")
    if len({item["path"] for item in worktrees}) != len(worktrees):
        raise SecretaryError("cleanup plan contains duplicate worktree")
    if len({item["path"] for item in artifacts}) != len(artifacts):
        raise SecretaryError("cleanup plan contains duplicate artifact")
    if ({item["from"] for item in renames} & {item["branch"] for item in deletions} or
            {item["to"] for item in renames} & ({item["from"] for item in renames} | {item["branch"] for item in deletions})):
        raise SecretaryError("cleanup plan has overlapping branch operations")
    return {"version": CLEANUP_PLAN_VERSION, "renames": sorted(renames, key=lambda item: item["from"]),
            "deletions": sorted(deletions, key=lambda item: item["branch"]),
            "worktrees": sorted(worktrees, key=lambda item: item["path"]),
            "artifacts": sorted(artifacts, key=lambda item: item["path"])}


def _cleanup_plan_hash(plan: dict[str, Any]) -> str:
    encoded = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _inspect_cleanup_plan(project_id: str, repo: Path, plan: dict[str, Any],
                          object_format: str, policy_root: Path,
                          *, require_artifact_identity: bool = False) -> dict[str, Any]:
    normalized = _normalize_cleanup_plan(plan, object_format,
                                         require_artifact_identity=require_artifact_identity)
    records = _cleanup_worktree_records(repo)
    by_path = {record["path"]: record for record in records}
    by_branch = {record["branch"]: record for record in records if record.get("branch")}
    planned_worktree_branches = {item["branch"] for item in normalized["worktrees"]}
    protected = set(_load_policy_and_classify(repo)[0].get("protectedBranches", []))
    actions: list[dict[str, Any]] = []

    for item in normalized["renames"]:
        source_oid = _cleanup_branch_oid(repo, item["from"], object_format)
        if source_oid != item["expectedOid"]:
            raise SecretaryError(f"cleanup branch OID changed: {item['from']}")
        if item["from"] in protected or item["to"] in protected:
            raise SecretaryError("cleanup cannot rename a protected branch")
        if _cleanup_branch_oid(repo, item["to"], object_format) is not None:
            raise SecretaryError(f"cleanup destination branch already exists: {item['to']}")
        record = by_branch.get(item["from"])
        if record is not None and item["from"] not in planned_worktree_branches:
            raise SecretaryError(f"cleanup branch is checked out: {item['from']}")
        actions.append({"kind": "branch-rename", "from": item["from"], "to": item["to"], "oid": source_oid})

    for item in normalized["deletions"]:
        branch = item["branch"]
        source_oid = _cleanup_branch_oid(repo, branch, object_format)
        if source_oid != item["expectedOid"]:
            raise SecretaryError(f"cleanup branch OID changed: {branch}")
        if branch in protected:
            raise SecretaryError("cleanup cannot delete a protected branch")
        record = by_branch.get(branch)
        if record is not None and branch not in planned_worktree_branches:
            raise SecretaryError(f"cleanup branch is checked out: {branch}")
        actions.append({"kind": "branch-delete", "branch": branch, "oid": source_oid})

    for item in normalized["worktrees"]:
        path = Path(item["path"])
        if not within(policy_root, path) or within(repo, path) or within(path, repo):
            raise SecretaryError("cleanup worktree is outside the configured worktree root")
        record = by_path.get(path)
        if record is None:
            raise SecretaryError(f"cleanup worktree is not registered: {path}")
        if record.get("branch") != item["branch"] or record.get("head") != item["expectedOid"]:
            raise SecretaryError(f"cleanup worktree identity changed: {path}")
        if record.get("locked") or record.get("prunable"):
            raise SecretaryError(f"cleanup worktree is locked or prunable: {path}")
        if not path.is_dir() or path.is_symlink():
            raise SecretaryError(f"cleanup worktree is missing or unsafe: {path}")
        if _cleanup_worktree_live_in_registry(project_id, path) or _cleanup_worktree_has_live_process(path):
            raise SecretaryError(f"cleanup worktree is live or owned by an active session: {path}")
        if _git(path, "status", "--porcelain=v1", "--untracked-files=all"):
            raise SecretaryError(f"cleanup worktree is dirty: {path}")
        actions.append({"kind": "worktree-remove", "path": str(path), "branch": item["branch"], "oid": item["expectedOid"]})

    for item in normalized["artifacts"]:
        path = Path(item["path"])
        _cleanup_artifact_allowed(path, item["kind"], repo, policy_root)
        if not path.is_file() or path.is_symlink():
            raise SecretaryError(f"cleanup artifact is missing or not a regular file: {path}")
        digest = _cleanup_artifact_snapshot(path, item["expectedIdentity"],
                                            item["expectedParentIdentity"])
        if digest != item["expectedSha256"]:
            raise SecretaryError(f"cleanup artifact changed: {path}")
        actions.append({"kind": "artifact-delete", "path": str(path), "sha256": digest,
                        "identity": item["expectedIdentity"]})

    plan_hash = _cleanup_plan_hash(normalized)
    return {"plan": normalized, "planHash": plan_hash, "actions": actions,
            "counts": {"renames": len(normalized["renames"]), "deletions": len(normalized["deletions"]),
                       "worktrees": len(normalized["worktrees"]), "artifacts": len(normalized["artifacts"])} }


def _apply_cleanup_ref_transaction(repo: Path, plan: dict[str, Any]) -> None:
    updates = plan["renames"] + plan["deletions"]
    if not updates:
        return
    commands = ["start"]
    for item in plan["renames"]:
        commands.append(f"create refs/heads/{item['to']} {item['expectedOid']}")
        commands.append(f"delete refs/heads/{item['from']} {item['expectedOid']}")
    for item in plan["deletions"]:
        commands.append(f"delete refs/heads/{item['branch']} {item['expectedOid']}")
    commands.extend(["prepare", "commit", ""])
    result = subprocess.run(["git", "update-ref", "--stdin"], cwd=str(repo), input="\n".join(commands),
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=_env())
    if result.returncode:
        raise SecretaryError("Git cleanup ref transaction failed")


def _cleanup_recovery_path(project: Path, kind: str, identifier: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,95}", identifier):
        raise SecretaryError("invalid cleanup recovery identifier")
    return project / "operations" / f"cleanup-{kind}-{identifier}.json"


def _write_cleanup_recovery(path: Path, *, kind: str, identifier: str, plan_hash: str,
                            phase: str, completed_worktrees: list[str] | None = None,
                            pending_worktrees: list[str] | None = None,
                            worktree_metadata: dict[str, dict[str, Any]] | None = None,
                            completed_artifacts: list[str] | None = None,
                            pending_artifacts: list[str] | None = None,
                            quarantine_artifacts: dict[str, str] | None = None,
                            refs_applied: bool = False, refs_pending: bool = False,
                            error: str | None = None) -> None:
    if (quarantine_artifacts is None or worktree_metadata is None) and path.exists() and not path.is_symlink():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if quarantine_artifacts is None and isinstance(previous, dict) and isinstance(previous.get("quarantineArtifacts"), dict):
                quarantine_artifacts = {str(key): str(value) for key, value in previous["quarantineArtifacts"].items()}
            if worktree_metadata is None and isinstance(previous, dict) and isinstance(previous.get("worktreeMetadata"), dict):
                worktree_metadata = {str(key): dict(value) for key, value in previous["worktreeMetadata"].items()
                                     if isinstance(value, dict)}
        except (OSError, UnicodeError, json.JSONDecodeError):
            quarantine_artifacts = {} if quarantine_artifacts is None else quarantine_artifacts
            worktree_metadata = {} if worktree_metadata is None else worktree_metadata
    value = {"schemaVersion": 1, "kind": kind, "identifier": identifier,
             "planHash": plan_hash, "phase": phase,
             "completedWorktrees": sorted(completed_worktrees or []),
             "pendingWorktrees": sorted(pending_worktrees or []),
             "worktreeMetadata": worktree_metadata or {},
             "completedArtifacts": sorted(completed_artifacts or []),
             "pendingArtifacts": sorted(pending_artifacts or []),
             "quarantineArtifacts": quarantine_artifacts or {},
             "refsApplied": refs_applied, "refsPending": refs_pending, "updatedAt": _utc_now()}
    if error:
        value["error"] = error[:500]
    _atomic(path, json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def _read_cleanup_recovery(path: Path, *, kind: str, identifier: str, plan_hash: str) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    fields = {"schemaVersion", "kind", "identifier", "planHash", "phase", "completedWorktrees",
              "pendingWorktrees", "worktreeMetadata", "completedArtifacts", "pendingArtifacts",
              "quarantineArtifacts", "refsApplied", "refsPending", "updatedAt", "error"}
    value = _read_json(path, fields, required={"schemaVersion", "kind", "identifier", "planHash", "phase",
                                                 "completedWorktrees", "pendingWorktrees", "completedArtifacts",
                                                 "pendingArtifacts", "quarantineArtifacts", "refsApplied",
                                                 "refsPending", "updatedAt"})
    value.setdefault("worktreeMetadata", {})
    if (value["schemaVersion"] != 1 or value["kind"] != kind or value["identifier"] != identifier or
            value["planHash"] != plan_hash or value["phase"] not in {"prepared", "worktrees", "reviews",
            "candidate", "worktree-pending", "refs-pending", "refs-applied", "artifact-pending",
            "artifacts", "branch-deleted", "complete", "error"} or
            not isinstance(value["completedWorktrees"], list) or
            not all(isinstance(item, str) for item in value["completedWorktrees"]) or
            not isinstance(value["pendingWorktrees"], list) or
            not all(isinstance(item, str) for item in value["pendingWorktrees"]) or
            not isinstance(value["worktreeMetadata"], dict) or
            not all(isinstance(key, str) and isinstance(item, dict)
                    for key, item in value["worktreeMetadata"].items()) or
            not isinstance(value["completedArtifacts"], list) or
            not all(isinstance(item, str) for item in value["completedArtifacts"]) or
            not isinstance(value["pendingArtifacts"], list) or
            not all(isinstance(item, str) for item in value["pendingArtifacts"]) or
            not isinstance(value["quarantineArtifacts"], dict) or
            not all(isinstance(key, str) and isinstance(item, str)
                    for key, item in value["quarantineArtifacts"].items()) or
            not isinstance(value["refsApplied"], bool) or not isinstance(value["refsPending"], bool)):
        raise SecretaryError("invalid cleanup recovery manifest")
    return value


def _resolve_cleanup_recovery_refs(repo: Path, normalized: dict[str, Any], object_format: str,
                                   recovery: dict[str, Any]) -> dict[str, Any]:
    if not recovery["refsPending"] or recovery["refsApplied"]:
        return recovery
    applied_states: list[bool] = []
    for item in normalized["renames"]:
        source = _cleanup_branch_oid(repo, item["from"], object_format)
        destination = _cleanup_branch_oid(repo, item["to"], object_format)
        applied_states.append(source is None and destination == item["expectedOid"])
        if not applied_states[-1] and not (source == item["expectedOid"] and destination is None):
            raise SecretaryError("cleanup recovery found an ambiguous branch rename state")
    for item in normalized["deletions"]:
        source = _cleanup_branch_oid(repo, item["branch"], object_format)
        applied_states.append(source is None)
        if not applied_states[-1] and source != item["expectedOid"]:
            raise SecretaryError("cleanup recovery found an ambiguous branch deletion state")
    if applied_states and all(applied_states):
        recovery["refsApplied"] = True
    elif applied_states and not any(applied_states):
        recovery["refsApplied"] = False
    elif applied_states:
        raise SecretaryError("cleanup recovery found partially applied branch refs")
    recovery["refsPending"] = False
    return recovery


def _inspect_cleanup_recovery(project_id: str, repo: Path, original_plan: dict[str, Any],
                              object_format: str, policy_root: Path, recovery: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_cleanup_plan(original_plan, object_format)
    completed_worktrees = set(recovery["completedWorktrees"])
    pending_worktrees = set(recovery["pendingWorktrees"])
    completed_artifacts = set(recovery["completedArtifacts"])
    pending_artifacts = set(recovery["pendingArtifacts"])
    quarantine_artifacts = recovery["quarantineArtifacts"]
    registered_worktree_paths = {record["path"] for record in _cleanup_worktree_records(repo)}
    for pending in pending_worktrees:
        if not Path(pending).exists() and pending in registered_worktree_paths:
            raise SecretaryError(f"cleanup recovery retains stale Git worktree metadata: {pending}")
    owned_quarantines: set[str] = set()
    for artifact_path in pending_artifacts:
        quarantine = _artifact_quarantine_path(Path(artifact_path), recovery["planHash"])
        info = _cleanup_lstat(quarantine, directory=False, missing=True)
        if (info is not None and
                quarantine_artifacts.get(artifact_path) == _cleanup_artifact_identity(info)):
            owned_quarantines.add(artifact_path)
    # If the process died after deleting the exact quarantined inode but
    # before recording completion, the durable intent plus absence of both
    # source and quarantine is an idempotent completed state. No new pathname
    # is touched; this only prevents a safe cleanup from being stranded.
    for item in normalized["artifacts"]:
        if item["path"] not in pending_artifacts or item["path"] in completed_artifacts:
            continue
        quarantine = _artifact_quarantine_path(Path(item["path"]), recovery["planHash"])
        if quarantine_artifacts.get(item["path"]) != item["expectedIdentity"]:
            continue
        source_info = _cleanup_lstat(Path(item["path"]), directory=False, missing=True)
        quarantine_info = _cleanup_lstat(quarantine, directory=False, missing=True)
        if source_info is None and quarantine_info is None:
            completed_artifacts.add(item["path"])

    remaining = {"version": 1,
                 "renames": [] if recovery["refsApplied"] else normalized["renames"],
                 "deletions": [] if recovery["refsApplied"] else normalized["deletions"],
                 "worktrees": [item for item in normalized["worktrees"]
                               if item["path"] not in completed_worktrees and
                               not (item["path"] in pending_worktrees and not Path(item["path"]).exists())],
                 "artifacts": [item for item in normalized["artifacts"]
                               if item["path"] not in completed_artifacts]}
    ref_and_worktree_plan = {"version": 1, "renames": remaining["renames"],
                             "deletions": remaining["deletions"],
                             "worktrees": remaining["worktrees"], "artifacts": []}
    if ref_and_worktree_plan["renames"] or ref_and_worktree_plan["deletions"] or ref_and_worktree_plan["worktrees"]:
        inspected = _inspect_cleanup_plan(project_id, repo, ref_and_worktree_plan, object_format, policy_root)
        actions: list[dict[str, Any]] = inspected["actions"]
    else:
        actions = []
    for item in remaining["artifacts"]:
        path = Path(item["path"])
        _cleanup_artifact_allowed(path, item["kind"], repo, policy_root)
        quarantine = _artifact_quarantine_path(path, recovery["planHash"])
        if item["path"] in pending_artifacts and item["path"] in owned_quarantines:
            if _cleanup_lstat(path, directory=False, missing=True) is not None:
                raise SecretaryError(f"cleanup recovery found a reappeared artifact source: {path}")
            digest = _cleanup_artifact_snapshot(quarantine, item["expectedIdentity"],
                                                item["expectedParentIdentity"])
        elif path.is_file() and not path.is_symlink():
            digest = _cleanup_artifact_snapshot(path, item["expectedIdentity"],
                                                item["expectedParentIdentity"])
        else:
            raise SecretaryError(f"cleanup recovery artifact is missing: {path}")
        if digest != item["expectedSha256"]:
            raise SecretaryError(f"cleanup recovery artifact changed: {path}")
        actions.append({"kind": "artifact-delete", "path": str(path), "sha256": item["expectedSha256"],
                        "identity": item["expectedIdentity"]})
    recovery["completedArtifacts"] = sorted(completed_artifacts)
    recovery["pendingArtifacts"] = sorted(pending_artifacts - completed_artifacts)
    return {"plan": remaining, "reportedPlan": normalized, "planHash": recovery["planHash"],
            "actions": actions, "counts": {"renames": len(remaining["renames"]),
            "deletions": len(remaining["deletions"]), "worktrees": len(remaining["worktrees"]),
            "artifacts": len(remaining["artifacts"])}}


def _apply_cleanup(repo: Path, inspected: dict[str, Any], recovery_path: Path,
                   recovery: dict[str, Any] | None = None) -> dict[str, Any]:
    plan = inspected["plan"]
    reported_plan = inspected.get("reportedPlan", plan)
    completed_worktrees: list[str] = list(recovery["completedWorktrees"] if recovery else [])
    pending_worktrees: list[str] = list(recovery["pendingWorktrees"] if recovery else [])
    completed_artifacts: list[str] = list(recovery["completedArtifacts"] if recovery else [])
    pending_artifacts: list[str] = list(recovery["pendingArtifacts"] if recovery else [])
    quarantine_artifacts: dict[str, str] = dict(recovery["quarantineArtifacts"] if recovery else {})
    refs_applied = bool(recovery and recovery["refsApplied"])
    refs_pending = bool(recovery and recovery["refsPending"])
    registered_worktree_paths = {record["path"] for record in _cleanup_worktree_records(repo)}
    for pending in list(pending_worktrees):
        if pending not in completed_worktrees and not Path(pending).exists():
            if pending in registered_worktree_paths:
                raise SecretaryError(f"cleanup recovery retains stale Git worktree metadata: {pending}")
            completed_worktrees.append(pending)
            pending_worktrees.remove(pending)
    _write_cleanup_recovery(recovery_path, kind="git", identifier=inspected["planHash"],
                            plan_hash=inspected["planHash"], phase="worktrees",
                            completed_worktrees=completed_worktrees,
                            pending_worktrees=pending_worktrees,
                            completed_artifacts=completed_artifacts,
                            pending_artifacts=pending_artifacts,
                            refs_applied=refs_applied, refs_pending=refs_pending)
    for item in plan["worktrees"]:
        worktree = Path(item["path"])
        try:
            if item["path"] not in completed_worktrees and item["path"] not in pending_worktrees:
                pending_worktrees.append(item["path"])
                _write_cleanup_recovery(recovery_path, kind="git", identifier=inspected["planHash"],
                                        plan_hash=inspected["planHash"], phase="worktree-pending",
                                        completed_worktrees=completed_worktrees,
                                        pending_worktrees=pending_worktrees,
                                        completed_artifacts=completed_artifacts,
                                        pending_artifacts=pending_artifacts,
                                        refs_applied=refs_applied, refs_pending=refs_pending)
            if _cleanup_worktree_has_live_process(worktree):
                raise SecretaryError(f"cleanup worktree became live during apply: {worktree}")
            if _git(worktree, "status", "--porcelain=v1", "--untracked-files=all"):
                raise SecretaryError(f"cleanup worktree became dirty during apply: {worktree}")
            result = run(["git", "worktree", "remove", item["path"]], repo, check=False)
            if result.returncode:
                raise SecretaryError(f"could not remove owned cleanup worktree: {item['path']}")
            if item["path"] not in completed_worktrees:
                completed_worktrees.append(item["path"])
            if item["path"] in pending_worktrees:
                pending_worktrees.remove(item["path"])
            _write_cleanup_recovery(recovery_path, kind="git", identifier=inspected["planHash"],
                                    plan_hash=inspected["planHash"], phase="worktrees",
                                    completed_worktrees=completed_worktrees,
                                    pending_worktrees=pending_worktrees,
                                    completed_artifacts=completed_artifacts,
                                    pending_artifacts=pending_artifacts,
                                    refs_applied=refs_applied, refs_pending=refs_pending)
        except Exception as error:
            _write_cleanup_recovery(recovery_path, kind="git", identifier=inspected["planHash"],
                                    plan_hash=inspected["planHash"], phase="error",
                                    completed_worktrees=completed_worktrees,
                                    pending_worktrees=pending_worktrees,
                                    completed_artifacts=completed_artifacts,
                                    pending_artifacts=pending_artifacts,
                                    refs_applied=refs_applied, refs_pending=refs_pending,
                                    error=str(error))
            raise
    if not refs_applied:
        refs_pending = True
        _write_cleanup_recovery(recovery_path, kind="git", identifier=inspected["planHash"],
                                plan_hash=inspected["planHash"], phase="refs-pending",
                                completed_worktrees=completed_worktrees,
                                pending_worktrees=pending_worktrees,
                                completed_artifacts=completed_artifacts,
                                pending_artifacts=pending_artifacts,
                                refs_applied=refs_applied, refs_pending=refs_pending)
        try:
            _apply_cleanup_ref_transaction(repo, plan)
        except Exception as error:
            _write_cleanup_recovery(recovery_path, kind="git", identifier=inspected["planHash"],
                                    plan_hash=inspected["planHash"], phase="error",
                                    completed_worktrees=completed_worktrees,
                                    pending_worktrees=pending_worktrees,
                                    completed_artifacts=completed_artifacts,
                                    pending_artifacts=pending_artifacts,
                                    refs_applied=refs_applied, refs_pending=refs_pending,
                                    error=str(error))
            raise
        refs_applied = True
        refs_pending = False
    _write_cleanup_recovery(recovery_path, kind="git", identifier=inspected["planHash"],
                            plan_hash=inspected["planHash"], phase="refs-applied",
                            completed_worktrees=completed_worktrees,
                            pending_worktrees=pending_worktrees,
                            completed_artifacts=completed_artifacts,
                            pending_artifacts=pending_artifacts,
                            refs_applied=refs_applied, refs_pending=refs_pending)
    for item in plan["artifacts"]:
        path = Path(item["path"])
        try:
            if item["path"] not in completed_artifacts and item["path"] not in pending_artifacts:
                pending_artifacts.append(item["path"])
                _write_cleanup_recovery(recovery_path, kind="git", identifier=inspected["planHash"],
                                        plan_hash=inspected["planHash"], phase="artifact-pending",
                                        completed_worktrees=completed_worktrees,
                                        pending_worktrees=pending_worktrees,
                                        completed_artifacts=completed_artifacts,
                                        pending_artifacts=pending_artifacts,
                                        refs_applied=refs_applied, refs_pending=refs_pending)
            def mark_quarantine(_quarantine: Path, identity: str) -> None:
                quarantine_artifacts[item["path"]] = identity
                _write_cleanup_recovery(recovery_path, kind="git", identifier=inspected["planHash"],
                                        plan_hash=inspected["planHash"], phase="artifact-pending",
                                        completed_worktrees=completed_worktrees,
                                        pending_worktrees=pending_worktrees,
                                        completed_artifacts=completed_artifacts,
                                        pending_artifacts=pending_artifacts,
                                        quarantine_artifacts=quarantine_artifacts,
                                        refs_applied=refs_applied, refs_pending=refs_pending)
            _quarantine_delete_artifact(
                path, item["expectedSha256"], item["expectedIdentity"],
                item["expectedParentIdentity"], inspected["planHash"],
                owned_quarantine=quarantine_artifacts.get(item["path"]),
                on_intent=mark_quarantine, on_quarantine=mark_quarantine)
            if item["path"] not in completed_artifacts:
                completed_artifacts.append(item["path"])
            if item["path"] in pending_artifacts:
                pending_artifacts.remove(item["path"])
            quarantine_artifacts.pop(item["path"], None)
            _write_cleanup_recovery(recovery_path, kind="git", identifier=inspected["planHash"],
                                    plan_hash=inspected["planHash"], phase="artifacts",
                                    completed_worktrees=completed_worktrees,
                                    pending_worktrees=pending_worktrees,
                                    completed_artifacts=completed_artifacts,
                                    pending_artifacts=pending_artifacts,
                                    quarantine_artifacts=quarantine_artifacts,
                                    refs_applied=refs_applied, refs_pending=refs_pending)
        except Exception as error:
            _write_cleanup_recovery(recovery_path, kind="git", identifier=inspected["planHash"],
                                    plan_hash=inspected["planHash"], phase="error",
                                    completed_worktrees=completed_worktrees,
                                    pending_worktrees=pending_worktrees,
                                    completed_artifacts=completed_artifacts,
                                    pending_artifacts=pending_artifacts,
                                    refs_applied=refs_applied, refs_pending=refs_pending,
                                    error=str(error))
            raise
    _write_cleanup_recovery(recovery_path, kind="git", identifier=inspected["planHash"],
                            plan_hash=inspected["planHash"], phase="complete",
                            completed_worktrees=completed_worktrees,
                            pending_worktrees=pending_worktrees,
                            completed_artifacts=completed_artifacts,
                            pending_artifacts=pending_artifacts,
                            quarantine_artifacts=quarantine_artifacts,
                            refs_applied=refs_applied, refs_pending=refs_pending)
    return {"applied": True, "planHash": inspected["planHash"],
            "recoveryPath": str(recovery_path), "recovered": recovery is not None,
            "renamedBranches": [item["from"] + " -> " + item["to"] for item in reported_plan["renames"]],
            "deletedBranches": [item["branch"] for item in reported_plan["deletions"]],
            "removedWorktrees": [item["path"] for item in reported_plan["worktrees"]],
            "removedArtifacts": [item["path"] for item in reported_plan["artifacts"]]}


def git_cleanup(project_id: str, operation: str, plan: dict[str, Any],
                plan_hash: str | None = None) -> dict[str, Any]:
    if operation not in {"plan", "apply"}:
        raise SecretaryError("cleanup operation must be plan or apply")
    info = _require_secretary(project_id)
    repo = _canonical_repo(info["primaryRepository"])
    _, common, object_format = project_identity(repo)
    _policy, trusted_live, policy_root = _load_policy_and_classify(repo)
    if not trusted_live:
        raise SecretaryError("Git cleanup requires a trusted-live repository")
    normalized_for_hash = _normalize_cleanup_plan(plan, object_format,
                                                  require_artifact_identity=operation == "apply")
    expected_plan_hash = _cleanup_plan_hash(normalized_for_hash)
    if operation == "plan":
        inspected = _inspect_cleanup_plan(project_id, repo, plan, object_format, policy_root)
        return {"projectId": project_id, "operation": "plan", **inspected}
    if not isinstance(plan_hash, str) or not hmac.compare_digest(plan_hash, expected_plan_hash):
        raise SecretaryError("cleanup plan hash does not match")
    project = _record_dir(_state_root(), project_id)
    recovery_path = _cleanup_recovery_path(project, "git", plan_hash)
    with _git_write_lock(common):
        # Re-read all OIDs, worktree state, and artifact digests while holding
        # the common-dir lock so an approved plan cannot silently drift. The
        # durable recovery manifest is written before the first deletion and
        # after each phase; a failed apply therefore has an explicit resume /
        # inspection point instead of an unexplained partial state.
        recovery = _read_cleanup_recovery(recovery_path, kind="git", identifier=plan_hash, plan_hash=plan_hash)
        reported_plan = _normalize_cleanup_plan(plan, object_format)
        if recovery is not None:
            recovery = _resolve_cleanup_recovery_refs(repo, reported_plan, object_format, recovery)
        if recovery is not None and recovery["phase"] == "complete":
            return {"projectId": project_id, "operation": "apply", "applied": True,
                    "recovered": True, "planHash": plan_hash, "recoveryPath": str(recovery_path),
                    "renamedBranches": [item["from"] + " -> " + item["to"] for item in reported_plan["renames"]],
                    "deletedBranches": [item["branch"] for item in reported_plan["deletions"]],
                    "removedWorktrees": [item["path"] for item in reported_plan["worktrees"]],
                    "removedArtifacts": [item["path"] for item in reported_plan["artifacts"]]}
        if recovery is None:
            current = _inspect_cleanup_plan(project_id, repo, plan, object_format, policy_root,
                                            require_artifact_identity=True)
        else:
            current = _inspect_cleanup_recovery(project_id, repo, plan, object_format, policy_root, recovery)
        if not hmac.compare_digest(current["planHash"], plan_hash):
            raise SecretaryError("cleanup plan changed before apply")
        return {"projectId": project_id, "operation": "apply",
                **_apply_cleanup(repo, current, recovery_path, recovery)}


@contextlib.contextmanager
def _registry_lock(root: Path) -> Iterator[None]:
    path = root / ".registry.lock"
    if path.exists() or path.is_symlink():
        _safe_lstat(path, directory=False)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _registry_path(root: Path, project_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", project_id):
        raise SecretaryError("invalid project id")
    return root / "registry" / f"{project_id}.json"


def _validate_registry_record(root: Path, path: Path) -> dict[str, Any]:
    record = _read_json(path, REGISTRY_FIELDS, required=REGISTRY_FIELDS)
    project_id = record.get("projectId")
    if not isinstance(project_id, str) or path != _registry_path(root, project_id):
        raise SecretaryError("registry path does not match project")
    alias = record.get("alias")
    session_id = record.get("secretarySessionId")
    primary = record.get("primaryRepository")
    if (record.get("schemaVersion") != 1 or not isinstance(alias, str) or
            not ALIAS_RE.fullmatch(alias) or not isinstance(session_id, str) or
            not SESSION_RE.fullmatch(session_id) or not isinstance(primary, str) or
            not Path(primary).is_absolute()):
        raise SecretaryError("malformed project registry record")
    _timestamp(record.get("registeredAt"), "registeredAt")
    repo = _canonical_repo(primary)
    current_id, common, object_format = project_identity(repo)
    if current_id != project_id:
        raise SecretaryError("project identity does not match registry path")
    _validate_project_record(_record_dir(root, project_id) / "project.json",
                             project_id, common, object_format)
    return record


def _registry_records(root: Path) -> list[dict[str, Any]]:
    registry = root / "registry"
    if not registry.exists():
        return []
    _safe_lstat(registry, directory=True)
    result: list[dict[str, Any]] = []
    for path in sorted(registry.iterdir()):
        if path.suffix != ".json":
            raise SecretaryError("unexpected project registry entry")
        result.append(_validate_registry_record(root, path))
    return result


def register_project(repository: str | Path, alias: str) -> dict[str, Any]:
    alias = _line(alias, "project alias", 48)
    if not ALIAS_RE.fullmatch(alias):
        raise SecretaryError("invalid project alias")
    repo = _canonical_repo(repository)
    project_id, _, _ = project_identity(repo)
    root = _state_root()
    _assert_state_root_not_repo(root, repo)
    _ensure_dir(root / "registry")
    init_project(repo)
    with _registry_lock(root):
        for other in _registry_records(root):
            if other["alias"] == alias and other["projectId"] != project_id:
                raise SecretaryError("project alias already registered")
        path = _registry_path(root, project_id)
        if path.exists() or path.is_symlink():
            current = _validate_registry_record(root, path)
            secretary_session = current["secretarySessionId"]
            registered_at = current["registeredAt"]
        else:
            secretary_session = "sec-" + secrets.token_hex(24)
            registered_at = _utc_now()
        record = {"schemaVersion": 1, "projectId": project_id, "alias": alias,
                  "primaryRepository": str(repo), "secretarySessionId": secretary_session,
                  "registeredAt": registered_at}
        _atomic(path, json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    return {key: record[key] for key in
            ("projectId", "alias", "primaryRepository", "secretarySessionId", "registeredAt")}


def registry_list() -> list[dict[str, Any]]:
    root = _state_root()
    return [{key: record[key] for key in
             ("projectId", "alias", "primaryRepository", "secretarySessionId", "registeredAt")}
            for record in _registry_records(root)]


def launch_info(project_id: str, *, internal: bool = False) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", project_id):
        raise SecretaryError("invalid project id")
    root = _state_root()
    record = _validate_registry_record(root, _registry_path(root, project_id))
    result = {key: record[key] for key in
              ("projectId", "alias", "primaryRepository", "secretarySessionId", "registeredAt")}
    if internal:
        result["capability"] = _stored_capability(root, project_id)
    return result


# --- IDs and text validation ---

def _id(value: str, label: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise SecretaryError(f"invalid {label}")
    return value


def _text(value: str, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > limit or any(
            ord(c) < 32 and c not in "\n\t" for c in value):
        raise SecretaryError(f"invalid {label}")
    return value


def _line(value: str, label: str, limit: int) -> str:
    value = _text(value, label, limit)
    if "\n" in value or "\r" in value:
        raise SecretaryError(f"invalid {label}")
    return value


def _oid_len(object_format: str) -> int:
    try:
        return SUPPORTED_OBJECT_FORMATS[object_format]
    except (KeyError, TypeError) as error:
        raise SecretaryError("unsupported Git object format") from error


def _validate_oid(oid: str, object_format: str) -> str:
    expected = _oid_len(object_format)
    if not isinstance(oid, str) or not re.fullmatch(rf"[0-9a-fA-F]{{{expected}}}", oid):
        raise SecretaryError(f"OID does not match object format {object_format}")
    return oid.lower()


# --- Briefs ---

EVENT_KINDS = {"needs-user", "review-requested", "referral", "process-exit"}


def _event_path(project: Path, event_id: str) -> Path:
    return project / "events" / "inbox" / f"{_id(event_id, 'event id')}.json"


def _validate_event(path: Path, project_id: str) -> dict[str, Any]:
    fields = {"schemaVersion", "eventId", "projectId", "workstreamId", "kind", "summary",
              "details", "source", "createdAt", "acknowledgedAt"}
    value = _read_json(path, fields, required=fields)
    if (value.get("schemaVersion") != 1 or value.get("projectId") != project_id or
            value.get("eventId") != path.stem or value.get("kind") not in EVENT_KINDS or
            value.get("source") not in {"agent", "host"}):
        raise SecretaryError("malformed attention event")
    _id(value.get("workstreamId"), "workstream id")
    _line(value.get("summary"), "event summary", 500)
    if not isinstance(value.get("details"), str) or len(value["details"].encode()) > 4096:
        raise SecretaryError("malformed attention event")
    _timestamp(value.get("createdAt"), "event createdAt")
    if value.get("acknowledgedAt") is not None:
        _timestamp(value["acknowledgedAt"], "event acknowledgedAt")
    return value


def _require_workstream(project_id: str, workstream_id: str) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    info = launch_info(project_id, internal=True)
    repo = Path(info["primaryRepository"])
    record = open_workstream(repo, info["capability"], workstream_id)
    return info, repo, record


def _revalidate_workstream_unlocked(project_id: str, workstream_id: str, repo: Path) -> dict[str, Any]:
    project = _record_dir(_state_root(), project_id)
    actual_project_id, common, object_format = project_identity(repo)
    if actual_project_id != project_id:
        raise SecretaryError("workstream project identity changed")
    project_record = _validate_project_record(project / "project.json", project_id, common, object_format)
    return _validate_workstream_record(_workstream_path(project, workstream_id), project, repo,
                                       project_record["objectFormat"], project_record["gitCommonDir"])


def _validate_feature_route(record: dict[str, Any]) -> None:
    route_path = os.environ.get("PI_TASK_ROUTE_FILE", "")
    capability = os.environ.get("PI_TASK_ROUTE_CAPABILITY", "")
    if not route_path or not capability:
        raise SecretaryError("feature route is required")
    path = Path(route_path)
    info = _safe_lstat(path, directory=False)
    assert info is not None
    if info.st_size > MAX_JSON:
        raise SecretaryError("feature route is too large")
    try:
        route = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SecretaryError("malformed feature route") from error
    expected = hashlib.sha256(capability.encode()).hexdigest()
    if (not isinstance(route, dict) or route.get("uid") != os.getuid() or
            not hmac.compare_digest(str(route.get("capabilityHash", "")), expected) or
            route.get("readOnly") is True):
        raise SecretaryError("feature route rejected")
    worktree = route.get("worktree")
    if not isinstance(worktree, str) or Path(worktree).resolve(strict=True) != Path(record["workspace"]).resolve(strict=True):
        raise SecretaryError("feature route does not own workstream")


def _review_request_path(project: Path, request_id: str) -> Path:
    return project / "reviews" / "requests" / f"{_id(request_id, 'review request id')}.json"


def _review_receipt_path(project: Path, receipt_id: str) -> Path:
    return project / "reviews" / "receipts" / f"{_id(receipt_id, 'review receipt id')}.json"


def _validate_review_receipt(path: Path, request: dict[str, Any] | None = None) -> dict[str, Any]:
    fields = {"schemaVersion", "receiptId", "projectId", "requestId", "workstreamId", "candidateOid",
              "candidateTree", "baseOid", "reviewerSessionId", "verdict", "summary", "findings", "reviewedAt"}
    value = _read_json(path, fields, required=fields)
    if (value.get("schemaVersion") != 1 or value.get("receiptId") != path.stem or
            value.get("verdict") not in {"accept", "reject"} or
            not re.fullmatch(r"[0-9a-f]{64}", str(value.get("projectId", ""))) or
            not SESSION_RE.fullmatch(str(value.get("reviewerSessionId", "")))):
        raise SecretaryError("malformed review receipt")
    for name in ("requestId", "workstreamId"):
        _id(value.get(name), f"review receipt {name}")
    for name in ("candidateOid", "candidateTree", "baseOid"):
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(value.get(name, ""))):
            raise SecretaryError("malformed review receipt")
    _line(value.get("summary"), "review summary", 1000)
    if not isinstance(value.get("findings"), str) or len(value["findings"].encode()) > 16 * 1024:
        raise SecretaryError("malformed review receipt")
    _timestamp(value.get("reviewedAt"), "review reviewedAt")
    if request is not None:
        bindings = {"projectId": "projectId", "requestId": "requestId", "workstreamId": "workstreamId",
                    "candidateOid": "candidateOid", "candidateTree": "candidateTree", "baseOid": "baseOid",
                    "reviewerSessionId": "reviewerSessionId"}
        if any(value[left] != request[right] for left, right in bindings.items()):
            raise SecretaryError("review receipt does not bind exact assignment")
    return value


def _find_orphan_review_receipt(project: Path, request: dict[str, Any]) -> dict[str, Any] | None:
    directory = project / "reviews" / "receipts"
    matches: list[dict[str, Any]] = []
    for receipt_path in sorted(directory.glob("*.json")):
        receipt = _validate_review_receipt(receipt_path)
        if receipt["requestId"] == request["requestId"]:
            matches.append(_validate_review_receipt(receipt_path, request))
    if len(matches) > 1:
        raise SecretaryError("multiple orphan review receipts exist for one request")
    return matches[0] if matches else None


def _validate_review_request(path: Path, project_id: str) -> dict[str, Any]:
    fields = {"schemaVersion", "requestId", "projectId", "workstreamId", "candidateOid",
              "candidateTree", "baseOid", "requestedAt", "reviewerSessionId", "reviewWorkspace",
              "reviewerTmuxSocket", "launchState", "receiptId"}
    value = _read_json(path, fields, required=fields - {"reviewerTmuxSocket", "launchState"})
    value.setdefault("reviewerTmuxSocket", None)
    value.setdefault("launchState", "pending")
    if value.get("schemaVersion") != 1 or value.get("projectId") != project_id or value.get("requestId") != path.stem:
        raise SecretaryError("malformed review request")
    _id(value.get("workstreamId"), "workstream id")
    for name in ("candidateOid", "candidateTree", "baseOid"):
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(value.get(name, ""))):
            raise SecretaryError("malformed review request")
    _timestamp(value.get("requestedAt"), "review requestedAt")
    if value.get("reviewerSessionId") is not None and not SESSION_RE.fullmatch(str(value["reviewerSessionId"])):
        raise SecretaryError("malformed review request")
    if value.get("reviewWorkspace") is not None and not Path(str(value["reviewWorkspace"])).is_absolute():
        raise SecretaryError("malformed review request")
    if value.get("reviewerTmuxSocket") is not None and (not isinstance(value["reviewerTmuxSocket"], str) or not value["reviewerTmuxSocket"].startswith("/")):
        raise SecretaryError("malformed review request")
    if value.get("launchState") not in {"pending", "launched", "uncertain"}:
        raise SecretaryError("malformed review request")
    if value.get("receiptId") is not None:
        _id(value["receiptId"], "review receipt id")
    return value


def _validate_review_workspace(request: dict[str, Any], *, require_clean: bool = True) -> Path:
    raw = request.get("reviewWorkspace")
    if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
        raise SecretaryError("review request has no valid workspace")
    workspace_path = Path(raw)
    if workspace_path.is_symlink():
        raise SecretaryError("review checkout path is a symlink")
    workspace = workspace_path.resolve(strict=True)
    if workspace != workspace_path:
        raise SecretaryError("review checkout path is not canonical")
    if _git(workspace, "rev-parse", "HEAD^{commit}").lower() != request["candidateOid"]:
        raise SecretaryError("review checkout moved from assigned commit")
    if _git(workspace, "rev-parse", "HEAD^{tree}").lower() != request["candidateTree"]:
        raise SecretaryError("review tree differs from assignment")
    if require_clean and _git(workspace, "status", "--porcelain=v1", "--untracked-files=all"):
        raise SecretaryError("review checkout is dirty")
    return workspace


def _assert_review_candidate_ready(record: dict[str, Any]) -> None:
    workspace = Path(record["workspace"])
    if _git(workspace, "status", "--porcelain=v1", "--untracked-files=all"):
        raise SecretaryError("review candidate worktree is dirty")
    for state in ("rebase-merge", "rebase-apply"):
        raw = _git(workspace, "rev-parse", "--git-path", state)
        path = Path(raw)
        if not path.is_absolute():
            path = workspace / path
        if path.exists() or path.is_symlink():
            raise SecretaryError("review candidate worktree has an unfinished rebase")


def _create_review_request(project_id: str, workstream_id: str, record: dict[str, Any]) -> dict[str, Any]:
    project = _record_dir(_state_root(), project_id)
    workspace = Path(record["workspace"])
    with _git_worktree_index_lock(workspace):
        _assert_review_candidate_ready(record)
        candidate = _git(workspace, "rev-parse", "HEAD^{commit}").lower()
        tree = _git(workspace, "rev-parse", "HEAD^{tree}").lower()
    request_id = "rr-" + secrets.token_hex(16)
    value = {"schemaVersion": 1, "requestId": request_id, "projectId": project_id,
             "workstreamId": workstream_id, "candidateOid": candidate, "candidateTree": tree,
             "baseOid": record["baseOid"], "requestedAt": _utc_now(), "reviewerSessionId": None,
             "reviewWorkspace": None, "reviewerTmuxSocket": None, "launchState": "pending", "receiptId": None}
    _atomic(_review_request_path(project, request_id), json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    return value


def append_event(project_id: str, workstream_id: str, kind: str, summary: str,
                 details: str = "", *, source: str = "agent", validate_route: bool = True) -> dict[str, Any]:
    if kind not in EVENT_KINDS or (source == "agent" and kind == "process-exit"):
        raise SecretaryError("invalid event kind")
    _, repo, record = _require_workstream(project_id, workstream_id)
    summary = _line(summary, "event summary", 500)
    if not isinstance(details, str) or len(details.encode()) > 4096:
        raise SecretaryError("invalid event details")
    project = _record_dir(_state_root(), project_id)
    event_id = "evt-" + secrets.token_hex(16)
    value = {"schemaVersion": 1, "eventId": event_id, "projectId": project_id,
             "workstreamId": workstream_id, "kind": kind, "summary": summary,
             "details": details, "source": source, "createdAt": _utc_now(), "acknowledgedAt": None}
    with _project_lock(project):
        current_record = _revalidate_workstream_unlocked(project_id, workstream_id, repo)
        if validate_route:
            _validate_feature_route(current_record)
        if kind == "review-requested":
            request = _create_review_request(project_id, workstream_id, current_record)
            value["details"] = json.dumps({"reviewRequestId": request["requestId"]}, separators=(",", ":"))
        _atomic(_event_path(project, event_id), json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    return value


def list_events(project_id: str, *, include_acknowledged: bool = False) -> list[dict[str, Any]]:
    launch_info(project_id)
    project = _record_dir(_state_root(), project_id)
    directory = project / "events" / "inbox"
    _safe_lstat(directory, directory=True)
    result = [_validate_event(path, project_id) for path in sorted(directory.glob("*.json"))]
    return result if include_acknowledged else [item for item in result if item["acknowledgedAt"] is None]


def acknowledge_event(project_id: str, event_id: str) -> dict[str, Any]:
    info = launch_info(project_id, internal=True)
    supplied = os.environ.get("PI_SECRETARY_CAPABILITY", "")
    if not supplied or not hmac.compare_digest(supplied, info["capability"]):
        raise SecretaryError("secretary capability required")
    project = _record_dir(_state_root(), project_id)
    path = _event_path(project, event_id)
    with _project_lock(project):
        value = _validate_event(path, project_id)
        if value["acknowledgedAt"] is None:
            value["acknowledgedAt"] = _utc_now()
            _atomic(path, json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    return value


def _brief_path(project: Path, brief_id: str) -> Path:
    return project / "briefs" / f"{_id(brief_id, 'brief id')}.md"


def create_brief(repository: str | Path, capability: str, title: str,
                 text: str, brief_id: str | None = None) -> dict[str, Any]:
    root, project, _, repo = _project_context(repository, capability)
    title = _line(title, "brief title", MAX_TITLE)
    text = _text(text, "brief text", MAX_BRIEF)
    identity = brief_id or "brief-" + secrets.token_hex(12)
    _id(identity, "brief id")
    with _project_lock(project):
        _foundation(project)
        path = _brief_path(project, identity)
        if path.exists() or path.is_symlink():
            raise SecretaryError("brief id already exists")
        _ensure_dir(path.parent)
        content = f"# {title}\n\n{text}\n"
        try:
            _atomic(path, content)
            _append_fact_locked(project, "brief-created", identity)
        except Exception:
            with contextlib.suppress(OSError):
                path.unlink()
            raise
    return {"projectId": project.name, "briefId": identity, "title": title}


def _read_brief_file(path: Path, brief_id: str) -> dict[str, Any]:
    info = _safe_lstat(path, directory=False)
    assert info is not None
    if info.st_size > MAX_BRIEF + MAX_TITLE + 16:
        raise SecretaryError("brief is too large")
    raw = path.read_bytes()
    try:
        content = raw.decode("utf-8")
    except UnicodeError as error:
        raise SecretaryError("brief is not UTF-8") from error
    lines = content.splitlines()
    if len(lines) < 3 or not lines[0].startswith("# ") or not lines[0][2:].strip() or lines[1] != "":
        raise SecretaryError("malformed brief")
    title = _text(lines[0][2:], "brief title", MAX_TITLE)
    rest = "\n".join(lines[2:])
    if rest.endswith("\n"):
        rest = rest[:-1]
    _text(rest, "brief text", MAX_BRIEF)
    return {"briefId": brief_id, "title": title, "text": rest}


def read_brief(repository: str | Path, capability: str, brief_id: str) -> dict[str, Any]:
    _, project, _, _ = _project_context(repository, capability)
    return _read_brief_file(_brief_path(project, brief_id), brief_id)


def list_briefs(repository: str | Path, capability: str) -> list[dict[str, Any]]:
    _, project, _, _ = _project_context(repository, capability)
    directory = project / "briefs"
    if not directory.exists():
        return []
    _safe_lstat(directory, directory=True)
    result = []
    for entry in sorted(directory.iterdir()):
        if entry.suffix != ".md":
            raise SecretaryError("unexpected brief record")
        result.append(_read_brief_file(entry, entry.stem))
    return result


# --- Workstreams ---

WORKSTREAM_FIELDS = {"schemaVersion", "workstreamId", "title", "role", "briefId", "targetRef",
                     "baseOid", "workspace", "branch", "createdAt", "closedAt"}


def _workstream_path(project: Path, workstream_id: str) -> Path:
    return project / "workstreams" / f"{_id(workstream_id, 'workstream id')}.json"


def _current_tmux_socket() -> str | None:
    value = os.environ.get("TMUX", "")
    if not value:
        return None
    pieces = value.rsplit(",", 2)
    if len(pieces) != 3 or not pieces[0].startswith("/") or "\n" in pieces[0] or "\x00" in pieces[0]:
        raise SecretaryError("malformed tmux server identity")
    return pieces[0]


def _default_tmux_socket() -> str:
    raw_dir = os.environ.get("TMUX_TMPDIR", "/tmp")
    if not raw_dir.startswith("/") or "\n" in raw_dir or "\x00" in raw_dir:
        raise SecretaryError("malformed tmux temporary directory")
    return str(Path(raw_dir) / f"tmux-{os.getuid()}" / "default")


def _workstream_runtime_path(project: Path, workstream_id: str) -> Path:
    return project / "workstream-runtime" / f"{_id(workstream_id, 'workstream id')}.json"


def _workstream_runtime(project: Path, repo: Path, record: dict[str, Any]) -> dict[str, Any]:
    path = _workstream_runtime_path(project, record["workstreamId"])
    fields = {"schemaVersion", "workstreamId", "piSessionId", "tmuxSession", "tmuxWindow", "tmuxSocket", "launchState", "seededAt"}
    if path.exists() or path.is_symlink():
        value = _read_json(path, fields, required=fields - {"tmuxSocket", "launchState"})
        value.setdefault("tmuxSocket", None)
        value.setdefault("launchState", "pending")
        if (value.get("schemaVersion") != 1 or value.get("workstreamId") != record["workstreamId"] or
                not isinstance(value.get("piSessionId"), str) or
                not SESSION_RE.fullmatch(value["piSessionId"]) or
                not isinstance(value.get("tmuxSession"), str) or
                not re.fullmatch(r"[A-Za-z0-9_-]{1,300}", value["tmuxSession"]) or
                not isinstance(value.get("tmuxWindow"), str) or
                not re.fullmatch(r"[A-Za-z0-9_-]{1,300}", value["tmuxWindow"]) or
                (value.get("tmuxSocket") is not None and
                 (not isinstance(value.get("tmuxSocket"), str) or not value["tmuxSocket"].startswith("/"))) or
                value.get("launchState") not in {"pending", "launched", "uncertain"} or
                (value.get("seededAt") is not None and not isinstance(value.get("seededAt"), str))):
            raise SecretaryError("malformed workstream runtime record")
        if value["seededAt"] is not None:
            _timestamp(value["seededAt"], "seededAt")
        return value
    workspace = Path(record["workspace"]).resolve(strict=True)
    common = _git_path(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    repo_name = re.sub(r"[^A-Za-z0-9_-]+", "-", repo.name).strip("-") or "repo"
    worktree_name = re.sub(r"[^A-Za-z0-9_-]+", "-", workspace.name).strip("-") or "worktree"
    common_hash = hashlib.sha256(str(common).encode()).hexdigest()
    worktree_hash = hashlib.sha256(str(workspace).encode()).hexdigest()
    value = {"schemaVersion": 1, "workstreamId": record["workstreamId"],
             "piSessionId": "ws-" + secrets.token_hex(24),
             "tmuxSession": f"pi-{repo_name}-{common_hash[:12]}",
             "tmuxWindow": f"w-{worktree_name}-{worktree_hash[:12]}",
             "tmuxSocket": _current_tmux_socket(), "launchState": "pending", "seededAt": None}
    _atomic(path, json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    return value


def _workstream_id(title: str, role: str, brief_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "workstream"
    slug = slug[:32].strip("-") or "workstream"
    suffix = hashlib.sha256(f"{title}\0{role}\0{brief_id}".encode()).hexdigest()[:16]
    return f"ws-{slug}-{suffix}"[:63]


def _target_oid(repo: Path, target_ref: str, object_format: str) -> str:
    if not isinstance(target_ref, str) or not target_ref or len(target_ref) > 512 or any(
            c.isspace() for c in target_ref) or target_ref.startswith("-"):
        raise SecretaryError("invalid target ref")
    result = run(["git", "rev-parse", "--verify", "--end-of-options", f"{target_ref}^{{commit}}"], repo, check=False)
    if result.returncode:
        raise SecretaryError("target ref is not a committed revision")
    oid = result.stdout.strip()
    return _validate_oid(oid, object_format)


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = run(["git", "merge-base", "--is-ancestor", ancestor, descendant], repo, check=False)
    return result.returncode == 0


def _check_branch_absent(repo: Path, branch: str) -> bool:
    """Return True if the branch does not exist."""
    result = run(["git", "branch", "--list", branch], repo, check=False)
    return result.returncode == 0 and not result.stdout.strip()


def _registered_worktrees(repo: Path) -> list[tuple[Path, str | None]] | None:
    result = run(["git", "worktree", "list", "--porcelain"], repo, check=False)
    if result.returncode:
        return None
    entries: list[tuple[Path, str | None]] = []
    current_path: Path | None = None
    current_branch: str | None = None
    for line in result.stdout.splitlines() + [""]:
        if line.startswith("worktree "):
            if current_path is not None:
                entries.append((current_path, current_branch))
            current_path = Path(line[9:]).resolve(strict=False)
            current_branch = None
        elif line.startswith("branch ") and current_path is not None:
            ref = line[7:]
            current_branch = ref.removeprefix("refs/heads/")
        elif not line and current_path is not None:
            entries.append((current_path, current_branch))
            current_path = None
            current_branch = None
    return entries


def _worktree_registered(repo: Path, workspace: Path, branch: str) -> bool | None:
    expected = workspace.resolve(strict=False)
    entries = _registered_worktrees(repo)
    if entries is None:
        return None
    return any(path == expected and current_branch == branch for path, current_branch in entries)


def _assert_worktree_registration_absent(repo: Path, workspace: Path, label: str) -> None:
    entries = _registered_worktrees(repo)
    if entries is None:
        raise SecretaryError(f"cleanup cannot prove {label} Git worktree absence")
    expected = workspace.resolve(strict=False)
    if any(path == expected for path, _ in entries):
        raise SecretaryError(f"cleanup found stale {label} Git worktree registration: {workspace}")


def _branch_registered(repo: Path, branch: str) -> bool | None:
    entries = _registered_worktrees(repo)
    if entries is None:
        return None
    return any(current_branch == branch for _, current_branch in entries)


def _branch_exists(repo: Path, branch: str) -> bool | None:
    result = run(["git", "branch", "--list", branch], repo, check=False)
    if result.returncode:
        return None
    return bool(result.stdout.strip())


def _branch_points_to(repo: Path, branch: str, oid: str) -> bool | None:
    result = run(["git", "rev-parse", "--verify", "--end-of-options", f"{branch}^{{commit}}"],
                 repo, check=False)
    if result.returncode:
        return None
    return result.stdout.strip().lower() == oid.lower()


def _cleanup_git_resources(repo: Path, workspace: Path, branch: str, *,
                           branch_oid: str, branch_created: bool, worktree_created: bool) -> None:
    """Remove only Git resources proven to belong to this failed attempt."""
    # A post-command exception can occur after Git registered the worktree.  The
    # exact registration plus our proven-new branch is sufficient ownership
    # evidence; an arbitrary directory at this path is not.
    if not worktree_created and branch_created:
        registered = _worktree_registered(repo, workspace, branch)
        if registered is None:
            return
        worktree_created = registered
    if worktree_created:
        registered = _worktree_registered(repo, workspace, branch)
        if registered is None:
            return
        if registered:
            run(["git", "worktree", "remove", "--force", str(workspace)], repo, check=False)
    # Never delete a branch that was not successfully created by this call, and
    # never delete one still registered to a worktree after uncertain cleanup.
    if not branch_created:
        return
    exists = _branch_exists(repo, branch)
    registered = _branch_registered(repo, branch)
    points_to_expected = _branch_points_to(repo, branch, branch_oid) if exists else False
    if exists is True and registered is False and points_to_expected is True:
        run(["git", "branch", "-D", branch], repo, check=False)


def create_workstream(repository: str | Path, capability: str, title: str, role: str, brief_id: str,
                      target_ref: str = "HEAD", workstream_id: str | None = None) -> dict[str, Any]:
    root, project, record, repo = _project_context(repository, capability)
    # Validate policy and trusted-live classification
    policy, trusted_live, worktree_root = _load_policy_and_classify(repo)
    if not trusted_live:
        raise SecretaryError("workstream allocation requires trusted-live repository policy")
    object_format = record["objectFormat"]
    common = Path(record["gitCommonDir"]).resolve(strict=True)
    title = _line(title, "workstream title", MAX_TITLE)
    role = _line(role, "workstream role", MAX_ROLE)
    if role not in WORKSTREAM_ROLES:
        raise SecretaryError("invalid workstream role")
    brief = _read_brief_file(_brief_path(project, brief_id), brief_id)
    worktree_root = Path(policy["worktreeRoot"]).resolve(strict=True)
    _safe_lstat(worktree_root, directory=True)
    oid = _target_oid(repo, target_ref, object_format)
    identity = workstream_id or _workstream_id(title, role, brief_id)
    _id(identity, "workstream id")
    branch = f"pi/{identity}"
    if not BRANCH_RE.fullmatch(branch):
        raise SecretaryError("derived branch is invalid")
    repo_component = re.sub(r"[^a-zA-Z0-9_.-]+", "-", repo.name).strip("-.") or "repo"
    project_prefix = record["projectId"][:12]
    workspace = worktree_root / repo_component / project_prefix / identity
    workspace_parent = workspace.parent
    if not within(worktree_root, workspace) or within(repo, workspace) or within(workspace, repo):
        raise SecretaryError("derived workspace escapes configured worktree root")
    workstream_record = {"schemaVersion": 1, "workstreamId": identity, "title": title, "role": role,
                         "briefId": brief_id, "targetRef": target_ref, "baseOid": oid,
                         "workspace": str(workspace), "branch": branch,
                         "createdAt": _utc_now(), "closedAt": None}
    with _project_lock(project):
        # Reject a policy change rather than allocating under a stale root.
        policy_now, trusted_live_now, worktree_root_now = _load_policy_and_classify(repo)
        if (not trusted_live_now or policy_now.get("policyHash") != policy.get("policyHash") or
                worktree_root_now != worktree_root):
            raise SecretaryError("repository policy changed during workstream creation")
        _foundation(project)
        record_path = _workstream_path(project, identity)
        if record_path.exists() or record_path.is_symlink() or workspace.exists() or workspace.is_symlink():
            raise SecretaryError("workstream id or workspace already exists")
        _ensure_dir(workspace_parent)
        branch_created = False
        worktree_created = False
        try:
            if not _check_branch_absent(repo, branch):
                raise SecretaryError("derived branch already exists")
            # Create the branch separately so an add failure cannot make cleanup
            # mistake a pre-existing branch for one owned by this invocation.
            run(["git", "branch", branch, oid], repo)
            branch_created = True
            run(["git", "worktree", "add", str(workspace), branch], repo)
            worktree_created = True
            actual = _canonical_repo(workspace)
            actual_common = _git_path(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")
            actual_format = _git(workspace, "rev-parse", "--show-object-format")
            if (actual != workspace.resolve(strict=True) or actual_common != common or
                    actual_format != object_format or
                    _git(workspace, "branch", "--show-current") != branch or
                    _git(workspace, "rev-parse", "HEAD^{commit}").lower() != oid):
                raise SecretaryError("created worktree failed verification")
            _ensure_dir(record_path.parent)
            _atomic(record_path, json.dumps(workstream_record, sort_keys=True, separators=(",", ":")) + "\n")
            runtime = _workstream_runtime(project, repo, workstream_record)
            _append_fact_locked(project, "workstream-created", identity)
        except Exception:
            with contextlib.suppress(OSError):
                record_path.unlink()
            with contextlib.suppress(OSError):
                _workstream_runtime_path(project, identity).unlink()
            _cleanup_git_resources(repo, workspace, branch, branch_oid=oid,
                                   branch_created=branch_created,
                                   worktree_created=worktree_created)
            raise
    return {"projectId": project.name, **workstream_record, **runtime, "currentOid": oid}


def _validate_workstream_record(path: Path, project: Path, repo: Path,
                                object_format: str, common_dir: str,
                                *, allow_missing_workspace: bool = False) -> dict[str, Any]:
    fields = {"schemaVersion", "workstreamId", "title", "role", "briefId", "targetRef",
              "baseOid", "workspace", "branch", "createdAt", "closedAt"}
    record = _read_json(path, fields, required=fields)
    if record["schemaVersion"] != 1 or _id(record["workstreamId"], "workstream id") != path.stem:
        raise SecretaryError("malformed workstream record")
    for key, limit in (("title", MAX_TITLE), ("role", MAX_ROLE), ("briefId", 63),
                       ("targetRef", 512), ("workspace", 4096), ("branch", 80)):
        if not isinstance(record[key], str) or not record[key] or len(record[key].encode()) > limit:
            raise SecretaryError("malformed workstream record")
    if not isinstance(record["baseOid"], str) or not BRANCH_RE.fullmatch(record["branch"]):
        raise SecretaryError("malformed workstream record")
    if record["branch"] != f"pi/{record['workstreamId']}":
        raise SecretaryError("malformed workstream record")
    _line(record["title"], "workstream title", MAX_TITLE)
    _line(record["role"], "workstream role", MAX_ROLE)
    if record["role"] not in WORKSTREAM_ROLES:
        raise SecretaryError("malformed workstream record")
    _id(record["briefId"], "brief id")
    _timestamp(record["createdAt"], "workstream createdAt")
    if record["closedAt"] is not None:
        _timestamp(record["closedAt"], "workstream closedAt")
    if any(c.isspace() for c in record["targetRef"]) or record["targetRef"].startswith("-"):
        raise SecretaryError("malformed workstream record")
    # Validate OID length against object format
    oid = _validate_oid(record["baseOid"], object_format)
    workspace_raw = Path(record["workspace"])
    if not workspace_raw.is_absolute():
        raise SecretaryError("workstream workspace must be absolute")
    _no_symlink_path(workspace_raw)
    ws_path = workspace_raw.resolve(strict=not allow_missing_workspace)
    # Re-validate policy
    _, trusted_live, policy_root = _load_policy_and_classify(repo)
    if not trusted_live:
        raise SecretaryError("repository is not trusted-live under current policy")
    if not within(policy_root, ws_path) or within(repo, ws_path):
        raise SecretaryError("workstream workspace escapes policy root")
    if allow_missing_workspace and not ws_path.exists():
        registered = _registered_worktrees(repo)
        if registered is None or any(path == ws_path for path, _ in registered):
            raise SecretaryError("missing workstream workspace still has a Git registration")
        base_exists = run(["git", "cat-file", "-e", f"{record['baseOid']}^{{commit}}"], repo, check=False)
        if base_exists.returncode:
            raise SecretaryError("recorded workstream base commit is unavailable")
        branch_ref = f"refs/heads/{record['branch']}"
        branch = run(["git", "rev-parse", "--verify", "--quiet", "--end-of-options",
                      f"{branch_ref}^{{commit}}"], repo, check=False)
        if branch.returncode == 0:
            current_oid = branch.stdout.strip().lower()
            _validate_oid(current_oid, object_format)
            if not _is_ancestor(repo, oid, current_oid):
                raise SecretaryError("recorded baseOid is not an ancestor of workstream branch")
            return {**record, "currentOid": current_oid}
        if branch.returncode != 1:
            raise SecretaryError("could not inspect workstream branch")
        return {**record, "currentOid": None}
    # Verify actual worktree: exact canonical repo, common directory and
    # object format, exact branch, base is ancestor of current HEAD.
    if _canonical_repo(ws_path) != ws_path:
        raise SecretaryError("workstream worktree is not the expected repository")
    actual_common = _git_path(ws_path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    actual_format = _git(ws_path, "rev-parse", "--show-object-format")
    if actual_common != Path(common_dir).resolve(strict=True) or actual_format != object_format:
        raise SecretaryError("workstream Git identity does not match project")
    current_branch = _git(ws_path, "branch", "--show-current")
    if current_branch != record["branch"]:
        raise SecretaryError("workstream branch does not match record")
    current_oid = _git(ws_path, "rev-parse", "HEAD^{commit}").lower()
    _validate_oid(current_oid, object_format)
    if not _is_ancestor(repo, oid, current_oid):
        raise SecretaryError("recorded baseOid is not an ancestor of workstream HEAD")
    return {**record, "currentOid": current_oid}


def open_workstream(repository: str | Path, capability: str, workstream_id: str) -> dict[str, Any]:
    _, project, record, repo = _project_context(repository, capability)
    validated = _validate_workstream_record(_workstream_path(project, workstream_id), project, repo,
                                            record["objectFormat"], record["gitCommonDir"])
    runtime = _workstream_runtime(project, repo, validated)
    return {"projectId": project.name, **validated, **runtime}


def list_workstreams(repository: str | Path, capability: str) -> list[dict[str, Any]]:
    _, project, record, repo = _project_context(repository, capability)
    directory = project / "workstreams"
    if not directory.exists():
        return []
    _safe_lstat(directory, directory=True)
    result = []
    for entry in sorted(directory.iterdir()):
        if entry.suffix != ".json":
            raise SecretaryError("unexpected workstream record")
        validated = _validate_workstream_record(entry, project, repo, record["objectFormat"],
                                                 record["gitCommonDir"])
        runtime = _workstream_runtime(project, repo, validated)
        result.append({"projectId": project.name, **validated, **runtime})
    return result


def create_reviewer(project_id: str, event_id: str) -> dict[str, Any]:
    info = launch_info(project_id, internal=True)
    if not hmac.compare_digest(os.environ.get("PI_SECRETARY_CAPABILITY", ""), info["capability"]):
        raise SecretaryError("secretary capability required")
    project = _record_dir(_state_root(), project_id)
    event_path = _event_path(project, event_id)
    event = _validate_event(event_path, project_id)
    if event["kind"] != "review-requested" or event["acknowledgedAt"] is not None:
        raise SecretaryError("event is not an open review request")
    try:
        request_id = json.loads(event["details"])["reviewRequestId"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise SecretaryError("review event is malformed") from error
    request_path = _review_request_path(project, request_id)
    before = _validate_review_request(request_path, project_id)
    current_tmux_socket = _current_tmux_socket()
    reviewer_tmux_socket = current_tmux_socket or before["reviewerTmuxSocket"] or _default_tmux_socket()
    if (before["reviewerTmuxSocket"] is not None and current_tmux_socket is not None and
            before["reviewerTmuxSocket"] != current_tmux_socket):
        raise SecretaryError("reviewer belongs to a different tmux server")
    _, project, project_record, repo = _project_context(info["primaryRepository"], info["capability"])
    with _review_launch_lock(project, request_id):
        with _project_lock(project):
            current_event = _validate_event(event_path, project_id)
            if current_event["kind"] != "review-requested" or current_event["acknowledgedAt"] is not None:
                raise SecretaryError("event is not an open review request")
            try:
                current_request_id = json.loads(current_event["details"])["reviewRequestId"]
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                raise SecretaryError("review event is malformed") from error
            if current_request_id != request_id:
                raise SecretaryError("review event changed concurrently")
            request = _validate_review_request(request_path, project_id)
            if request["reviewerSessionId"] is None:
                workstream = _validate_workstream_record(_workstream_path(project, request["workstreamId"]), project, repo,
                                                          project_record["objectFormat"], project_record["gitCommonDir"])
                parent = Path(workstream["workspace"]).parent
                workspace = parent / f"review-{request_id}"
                if workspace.exists() or workspace.is_symlink():
                    if workspace.is_symlink() or not workspace.is_dir():
                        raise SecretaryError("review workspace already exists and is unsafe")
                    candidate_request = {**request, "reviewWorkspace": str(workspace.resolve(strict=True))}
                    registered = _registered_worktrees(repo)
                    if registered is None or not any(path == workspace.resolve(strict=True) and branch is None for path, branch in registered):
                        raise SecretaryError("review workspace already exists without an exact Git worktree registration")
                    _validate_review_worktree_identity(candidate_request, project, project_record, repo,
                                                       require_clean=True)
                else:
                    created = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(workspace), request["candidateOid"]],
                                             env=_env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
                    if created.returncode:
                        raise SecretaryError("could not create exact review worktree")
                request["reviewerSessionId"] = "rv-" + secrets.token_hex(24)
                request["reviewWorkspace"] = str(workspace.resolve(strict=True))
                request["reviewerTmuxSocket"] = reviewer_tmux_socket
                request["launchState"] = "pending"
                _atomic(request_path, json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n")
            else:
                if request["reviewWorkspace"] is None:
                    raise SecretaryError("review assignment is incomplete")
                _validate_review_worktree_identity(request, project, project_record, repo, require_clean=False)
                if reviewer_tmux_socket is not None:
                    if request["reviewerTmuxSocket"] not in (None, reviewer_tmux_socket):
                        raise SecretaryError("reviewer tmux server changed concurrently")
                    if request["reviewerTmuxSocket"] is None:
                        request["reviewerTmuxSocket"] = reviewer_tmux_socket
                if request["launchState"] in {"launched", "uncertain"}:
                    live = _reviewer_process_live(request, repo)
                    if live is True:
                        request["launchState"] = "launched"
                        _atomic(request_path, json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n")
                        return request
                    if live is None:
                        request["launchState"] = "uncertain"
                        _atomic(request_path, json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n")
                        raise SecretaryError("cannot prove reviewer process state")
                request["launchState"] = "pending"
                _atomic(request_path, json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n")

        environment = _env()
        for name in ("TERM", "COLORTERM", "PI_CODING_AGENT_DIR", "TMUX_TMPDIR"):
            if os.environ.get(name): environment[name] = os.environ[name]
        environment["TMUX"] = f"{request['reviewerTmuxSocket']},0,0"
        environment["PI_PIDEV_DETACHED"] = "1"
        environment.update({"PI_PIDEV_SESSION_ID": request["reviewerSessionId"],
                            "PI_PIDEV_REVIEW_PROJECT_ID": project_id,
                            "PI_PIDEV_REVIEW_REQUEST_ID": request_id,
                            "PI_PIDEV_CONTROL": str(Path(__file__).resolve())})
        try:
            result = subprocess.run([str(_pidev_path())], cwd=request["reviewWorkspace"], env=environment,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        except (OSError, SecretaryError) as error:
            result = None
            launch_error = error
        else:
            launch_error = None
        if launch_error is None and result is not None and result.returncode == 0:
            live = _wait_for_reviewer_process(request, repo)
            if live is not True:
                with _project_lock(project):
                    current = _validate_review_request(request_path, project_id)
                    current["launchState"] = "uncertain" if live is None else "pending"
                    _atomic(request_path, json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n")
                if live is None:
                    raise SecretaryError("cannot prove reviewer process state")
                raise SecretaryError("review launch did not become live")
        with _project_lock(project):
            current = _validate_review_request(request_path, project_id)
            immutable = ("projectId", "requestId", "workstreamId", "candidateOid", "candidateTree", "baseOid",
                         "reviewerSessionId", "reviewWorkspace")
            if any(current[name] != request[name] for name in immutable):
                raise SecretaryError("review assignment changed during launch")
            if launch_error is not None or result is None or result.returncode:
                live = _reviewer_process_live(request, repo)
                current["launchState"] = "launched" if live is True else "uncertain" if live is None else "pending"
                _atomic(request_path, json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n")
                if live is True:
                    return current
                if isinstance(launch_error, SecretaryError):
                    raise launch_error
                if launch_error is not None:
                    raise SecretaryError(f"review launch failed: {launch_error}") from launch_error
                if live is None:
                    raise SecretaryError(f"review launch failed with uncertain process state: {(result.stderr or result.stdout).strip()[:260]}")
                raise SecretaryError(f"review launch failed: {(result.stderr or result.stdout).strip()[:300]}")
            current["launchState"] = "launched"
            _atomic(request_path, json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n")
            return current


def review_launch_info(project_id: str, request_id: str) -> dict[str, Any]:
    info = launch_info(project_id, internal=True)
    _, project, project_record, repo = _project_context(info["primaryRepository"], info["capability"],
                                                         reconcile_facts=False)
    request = _validate_review_request(_review_request_path(project, request_id), project_id)
    if (request["reviewerSessionId"] is None or request["reviewWorkspace"] is None or
            request["reviewerTmuxSocket"] is None):
        raise SecretaryError("review request is not assigned to an authoritative tmux server")
    _validate_review_worktree_identity(request, project, project_record, repo, require_clean=False)
    return {**request, "capability": info["capability"]}


def submit_review(project_id: str, request_id: str, verdict: str, summary: str, findings: str) -> dict[str, Any]:
    request = review_launch_info(project_id, request_id)
    if verdict not in {"accept", "reject"}:
        raise SecretaryError("invalid review verdict")
    summary = _line(summary, "review summary", 1000)
    if not isinstance(findings, str) or len(findings.encode()) > 16 * 1024:
        raise SecretaryError("invalid review findings")
    if (not hmac.compare_digest(os.environ.get("PI_REVIEW_CAPABILITY", ""), request["capability"]) or
            os.environ.get("PI_REVIEW_SESSION_ID") != request["reviewerSessionId"]):
        raise SecretaryError("reviewer capability required")
    info = launch_info(project_id, internal=True)
    _, project, project_record, repo = _project_context(info["primaryRepository"], info["capability"],
                                                         reconcile_facts=False)
    request_path = _review_request_path(project, request_id)
    with _project_lock(project):
        current = _validate_review_request(request_path, project_id)
        immutable = ("projectId", "requestId", "workstreamId", "candidateOid", "candidateTree", "baseOid",
                     "reviewerSessionId", "reviewWorkspace", "reviewerTmuxSocket")
        if any(current[name] != request[name] for name in immutable):
            raise SecretaryError("review assignment changed during receipt submission")
        if current["receiptId"] is not None:
            existing = _validate_review_receipt(_review_receipt_path(project, current["receiptId"]), current)
            if (existing["verdict"], existing["summary"], existing["findings"]) != (verdict, summary, findings):
                raise SecretaryError("conflicting review receipt already exists")
            return existing
        orphan = _find_orphan_review_receipt(project, current)
        if orphan is not None:
            if (orphan["verdict"], orphan["summary"], orphan["findings"]) != (verdict, summary, findings):
                raise SecretaryError("conflicting orphan review receipt already exists")
            current["receiptId"] = orphan["receiptId"]
            _atomic(request_path, json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n")
            return orphan
        # The pre-lock validation above is only an early failure. Revalidate the
        # exact detached checkout while holding both the assignment lock and
        # its Git index lock immediately before creating the immutable receipt.
        review_workspace = Path(current["reviewWorkspace"])
        with _git_worktree_index_lock(review_workspace):
            _validate_review_workspace(current, require_clean=True)
            _validate_review_worktree_identity(current, project, project_record, repo, require_clean=True)
            receipt_id = "receipt-" + secrets.token_hex(16)
            receipt = {"schemaVersion": 1, "receiptId": receipt_id, "projectId": project_id,
                       "requestId": request_id, "workstreamId": current["workstreamId"],
                       "candidateOid": current["candidateOid"], "candidateTree": current["candidateTree"],
                       "baseOid": current["baseOid"], "reviewerSessionId": current["reviewerSessionId"],
                       "verdict": verdict, "summary": summary, "findings": findings, "reviewedAt": _utc_now()}
            _atomic(_review_receipt_path(project, receipt_id), json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
            current["receiptId"] = receipt_id
            _atomic(request_path, json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n")
    return receipt


def review_status(project_id: str, request_id: str) -> dict[str, Any]:
    request = review_launch_info(project_id, request_id)
    _, _, workstream = _require_workstream(project_id, request["workstreamId"])
    _assert_review_candidate_ready(workstream)
    current_oid = _git(Path(workstream["workspace"]), "rev-parse", "HEAD^{commit}").lower()
    receipt = None
    if request["receiptId"] is not None:
        receipt = _validate_review_receipt(_review_receipt_path(_record_dir(_state_root(), project_id), request["receiptId"]), request)
    return {**request, "capability": None, "currentOid": current_oid,
            "stale": current_oid != request["candidateOid"], "receipt": receipt}


def _require_secretary(project_id: str) -> dict[str, Any]:
    info = launch_info(project_id, internal=True)
    if not hmac.compare_digest(os.environ.get("PI_SECRETARY_CAPABILITY", ""), info["capability"]):
        raise SecretaryError("secretary capability required")
    return info


def _target_worktree(repo: Path, target_ref: str) -> Path:
    result = _git(repo, "worktree", "list", "--porcelain")
    matches: list[Path] = []
    current: Path | None = None
    for line in result.splitlines():
        if line.startswith("worktree "):
            current = Path(line[9:]).resolve(strict=True)
        elif line == f"branch {target_ref}" and current is not None:
            matches.append(current)
    if len(matches) != 1:
        raise SecretaryError("target branch is not checked out in exactly one worktree")
    return matches[0]


def _record_landing(project: Path, project_id: str, workstream_id: str, request_id: str,
                    receipt_id: str, target_ref: str, expected: str, candidate: str) -> dict[str, Any]:
    existing = _landing_for(project, workstream_id)
    if existing is not None and existing["requestId"] == request_id and existing["landedOid"] == candidate:
        if (existing["projectId"] != project_id or existing["receiptId"] != receipt_id or
                existing["targetRef"] != target_ref or existing["expectedTargetOid"] != expected):
            raise SecretaryError("existing landing operation does not match exact review assignment")
        return {**existing, "landed": True, "requiresIntegration": False, "recovered": True}
    operation = {"schemaVersion": 1, "operationId": "land-" + secrets.token_hex(16),
                 "kind": "landing", "projectId": project_id, "workstreamId": workstream_id,
                 "requestId": request_id, "receiptId": receipt_id, "targetRef": target_ref,
                 "expectedTargetOid": expected, "landedOid": candidate, "landedAt": _utc_now()}
    with _project_lock(project):
        _atomic(project / "operations" / f"{operation['operationId']}.json",
                json.dumps(operation, sort_keys=True, separators=(",", ":")) + "\n")
    return {**operation, "landed": True, "requiresIntegration": False}


def land_reviewed(project_id: str, request_id: str) -> dict[str, Any]:
    info = _require_secretary(project_id)
    initial = review_status(project_id, request_id)
    initial_receipt = initial.get("receipt")
    if not isinstance(initial_receipt, dict) or initial_receipt.get("verdict") != "accept" or initial["stale"]:
        raise SecretaryError("an exact current ACCEPT receipt is required")
    _, project, project_record, repo = _project_context(info["primaryRepository"], info["capability"])
    initial_workstream = open_workstream(repo, info["capability"], initial["workstreamId"])
    candidate_workspace = Path(initial_workstream["workspace"])
    lock_path = Path(project_record["gitCommonDir"]) / "pi-secretary-target.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Hold the candidate worktree's Git index lock while revalidating the
        # receipt and performing the target fast-forward. A concurrent worker
        # commit therefore fails closed instead of landing an unreviewed tip.
        with _git_worktree_index_lock(candidate_workspace):
            status = review_status(project_id, request_id)
            receipt = status.get("receipt")
            if not isinstance(receipt, dict) or receipt.get("verdict") != "accept" or status["stale"]:
                raise SecretaryError("an exact current ACCEPT receipt is required")
            workstream = open_workstream(repo, info["capability"], status["workstreamId"])
            if Path(workstream["workspace"]) != candidate_workspace:
                raise SecretaryError("workstream workspace changed during landing")
            target_name = workstream["targetRef"]
            target_ref = _git(repo, "rev-parse", "--symbolic-full-name", target_name)
            if not target_ref.startswith("refs/heads/"):
                raise SecretaryError("landing target is not a branch")
            target = _target_worktree(repo, target_ref)
            candidate = status["candidateOid"]
            expected = status["baseOid"]
            with _git_worktree_index_lock(target) as target_index:
                actual = _git(repo, "rev-parse", f"{target_ref}^{{commit}}").lower()
                if actual == candidate:
                    if _git(target, "branch", "--show-current") != target_ref.removeprefix("refs/heads/"):
                        raise SecretaryError("target worktree branch changed")
                    _repair_landed_worktree_index(target, target_index, candidate)
                    return _record_landing(project, project_id, workstream["workstreamId"], request_id,
                                           receipt["receiptId"], target_ref, expected, candidate)
                if actual != expected:
                    return {"landed": False, "requiresIntegration": True, "reason": "target-moved",
                            "expectedTargetOid": expected, "actualTargetOid": actual, "candidateOid": candidate}
                if not _is_ancestor(repo, actual, candidate):
                    return {"landed": False, "requiresIntegration": True, "reason": "not-fast-forward",
                            "expectedTargetOid": expected, "actualTargetOid": actual, "candidateOid": candidate}
                if _git(target, "status", "--porcelain=v1"):
                    raise SecretaryError("target worktree is dirty")
                if _git(target, "branch", "--show-current") != target_ref.removeprefix("refs/heads/"):
                    raise SecretaryError("target worktree branch changed")
                # Git refuses to use the real index while its lock is held, so
                # merge through a private index and atomically install it while
                # the target index lock excludes concurrent checkout/commit.
                with _temporary_worktree_index(target_index) as temporary_index:
                    merge_env = _env()
                    merge_env["GIT_INDEX_FILE"] = str(temporary_index)
                    merged = subprocess.run(["git", "-C", str(target), "merge", "--ff-only", "--no-edit", candidate],
                                            env=merge_env, text=True, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, check=False)
                    if merged.returncode:
                        raise SecretaryError("fast-forward landing failed")
                    os.replace(temporary_index, target_index)
                if _git(repo, "rev-parse", f"{target_ref}^{{commit}}").lower() != candidate:
                    raise SecretaryError("fast-forward landing failed")
                return _record_landing(project, project_id, workstream["workstreamId"], request_id,
                                       receipt["receiptId"], target_ref, expected, candidate)
    finally:
        os.close(fd)


def create_integration(project_id: str, request_id: str) -> dict[str, Any]:
    info = _require_secretary(project_id)
    status = review_status(project_id, request_id)
    if status["stale"] or not isinstance(status.get("receipt"), dict) or status["receipt"].get("verdict") != "accept":
        raise SecretaryError("integration requires a current exact ACCEPT receipt")
    _, _, original = _require_workstream(project_id, status["workstreamId"])
    repo = Path(info["primaryRepository"])
    actual = _git(repo, "rev-parse", f"{original['targetRef']}^{{commit}}").lower()
    title = f"Integrate {original['title']}"
    text = (f"# Integration brief\n\nIntegrate reviewed candidate `{status['candidateOid']}` into target "
            f"`{original['targetRef']}` currently at `{actual}`.\n\nDo not rewrite or delete the original workstream. "
            "Resolve semantics in this separate workstream, preserve behavior, test, commit, and request a new exact-OID review.")
    brief = create_brief(repo, info["capability"], title, text)
    record = create_workstream(repo, info["capability"], title, "integration", brief["briefId"],
                               target_ref=original["targetRef"])
    launch_workstream(project_id, record["workstreamId"])
    return record


def _landing_for(project: Path, workstream_id: str) -> dict[str, Any] | None:
    directory = project / "operations"
    found = []
    fields = {"schemaVersion", "operationId", "kind", "projectId", "workstreamId", "requestId",
              "receiptId", "targetRef", "expectedTargetOid", "landedOid", "landedAt"}
    for path in directory.glob("land-*.json"):
        value = _read_json(path, fields, required=fields)
        if value.get("workstreamId") != workstream_id:
            continue
        if (value.get("schemaVersion") != 1 or value.get("operationId") != path.stem or
                value.get("kind") != "landing" or value.get("projectId") != project.name or
                not re.fullmatch(r"refs/heads/[A-Za-z0-9._/@-]{1,240}", str(value.get("targetRef", ""))) or
                not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(value.get("expectedTargetOid", ""))) or
                not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(value.get("landedOid", "")))):
            raise SecretaryError("malformed landing operation")
        _id(value["requestId"], "landing request id")
        _id(value["receiptId"], "landing receipt id")
        _timestamp(value["landedAt"], "landing landedAt")
        found.append(value)
    return sorted(found, key=lambda value: value["landedAt"])[-1] if found else None


def _validate_landing_provenance(project: Path, project_id: str, repo: Path,
                                 record: dict[str, Any], landing: dict[str, Any]) -> None:
    target_ref = _git(repo, "rev-parse", "--symbolic-full-name", record["targetRef"])
    if landing["projectId"] != project_id or landing["targetRef"] != target_ref:
        raise SecretaryError("landing provenance does not match the workstream target")
    request = _validate_review_request(_review_request_path(project, landing["requestId"]), project_id)
    if (request["workstreamId"] != record["workstreamId"] or request["receiptId"] != landing["receiptId"] or
            request["candidateOid"] != landing["landedOid"] or request["baseOid"] != landing["expectedTargetOid"]):
        raise SecretaryError("landing provenance does not match the review request")
    receipt = _validate_review_receipt(_review_receipt_path(project, landing["receiptId"]), request)
    if receipt["verdict"] != "accept" or receipt["candidateOid"] != landing["landedOid"]:
        raise SecretaryError("landing provenance is not an exact ACCEPT receipt")
    _validate_oid(landing["expectedTargetOid"], record["objectFormat"] if "objectFormat" in record else _git(repo, "rev-parse", "--show-object-format"))
    _validate_oid(landing["landedOid"], record["objectFormat"] if "objectFormat" in record else _git(repo, "rev-parse", "--show-object-format"))


def _validate_review_worktree_identity(request: dict[str, Any], project: Path,
                                       project_record: dict[str, Any], repo: Path,
                                       *, require_clean: bool) -> Path:
    workspace = _validate_review_workspace(request, require_clean=require_clean)
    _, trusted_live, policy_root = _load_policy_and_classify(repo)
    if not trusted_live or not within(policy_root, workspace) or within(repo, workspace):
        raise SecretaryError("review checkout escapes policy root")
    if _canonical_repo(workspace) != workspace:
        raise SecretaryError("review checkout is not the expected repository")
    actual_common = _git_path(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")
    actual_format = _git(workspace, "rev-parse", "--show-object-format")
    if actual_common != Path(project_record["gitCommonDir"]).resolve(strict=True) or actual_format != project_record["objectFormat"]:
        raise SecretaryError("review checkout Git identity does not match project")
    registered = _registered_worktrees(repo)
    if registered is None or not any(path == workspace and branch is None for path, branch in registered):
        raise SecretaryError("review checkout is not an exact detached Git worktree")
    return workspace


def _validate_review_worktree_for_cleanup(request: dict[str, Any], project: Path,
                                          project_record: dict[str, Any], repo: Path) -> Path:
    return _validate_review_worktree_identity(request, project, project_record, repo, require_clean=True)


def _worktree_quarantine_path(workspace: Path, label: str) -> Path:
    workspace = workspace.resolve(strict=False)
    key = hashlib.sha256(f"{label}\0{workspace}".encode()).hexdigest()[:24]
    return workspace.parent / f".pi-secretary-{label}-quarantine-{key}"


def _remove_worktree_with_process_guard(repo: Path, workspace: Path, label: str,
                                        on_quarantine: Any, *,
                                        on_quarantine_state: Any | None = None) -> None:
    if _cleanup_worktree_has_live_process(workspace):
        raise SecretaryError(f"cleanup refuses a live {label} process")
    quarantine = _worktree_quarantine_path(workspace, label)
    if quarantine.exists() or quarantine.is_symlink():
        raise SecretaryError(f"cleanup quarantine already exists for {label}: {quarantine}")
    if on_quarantine_state is not None:
        on_quarantine_state(quarantine, "planned")
    moved = subprocess.run(["git", "-C", str(repo), "worktree", "move", str(workspace), str(quarantine)],
                           env=_env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if moved.returncode:
        raise SecretaryError(f"could not quarantine clean {label} worktree")
    if on_quarantine_state is not None:
        on_quarantine_state(quarantine, "moved")
    try:
        if workspace.exists() or workspace.is_symlink():
            raise SecretaryError(f"cleanup found a reappeared {label} path during quarantine")
        if _cleanup_worktree_has_live_process(workspace) or _cleanup_worktree_has_live_process(quarantine):
            raise SecretaryError(f"cleanup found a live {label} process during quarantine")
        registered = _registered_worktrees(repo)
        if registered is None or not any(path == quarantine for path, _ in registered):
            raise SecretaryError(f"cleanup lost {label} quarantine registration")
        if on_quarantine_state is not None:
            on_quarantine_state(quarantine, "remove-pending")
        removed = subprocess.run(["git", "-C", str(repo), "worktree", "remove", str(quarantine)],
                                 env=_env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if removed.returncode:
            raise SecretaryError(f"could not remove quarantined {label} worktree")
        if on_quarantine_state is not None:
            on_quarantine_state(quarantine, "removed")
        if workspace.exists() or workspace.is_symlink() or _cleanup_worktree_has_live_process(quarantine):
            raise SecretaryError(f"cleanup found a {label} process or path after removal")
    except Exception as error:
        on_quarantine(quarantine, str(error))
        raise


def _resume_quarantined_worktree(repo: Path, quarantine: Path, policy_root: Path,
                                  expected: dict[str, Any]) -> bool:
    required = {"kind", "originalPath", "workstreamId", "requestId", "branch", "expectedOid",
                "tmuxSession", "tmuxWindow", "tmuxSocket", "sessionId", "state"}
    if set(expected) != required or expected["kind"] not in {"candidate", "review"} or \
            not isinstance(expected["originalPath"], str) or not isinstance(expected["workstreamId"], str) or \
            (expected["requestId"] is not None and not isinstance(expected["requestId"], str)) or \
            (expected["branch"] is not None and not isinstance(expected["branch"], str)) or \
            not isinstance(expected["expectedOid"], str) or not re.fullmatch(r"[0-9a-f]{40,64}", expected["expectedOid"]) or \
            not isinstance(expected["tmuxSession"], str) or not isinstance(expected["tmuxWindow"], str) or \
            not isinstance(expected["tmuxSocket"], str) or not isinstance(expected["sessionId"], str) or \
            expected["state"] not in {"planned", "moved", "remove-pending", "removed"}:
        raise SecretaryError("cleanup recovery has invalid bound worktree metadata")
    original = Path(expected["originalPath"])
    quarantine = Path(quarantine)
    if not original.is_absolute() or not quarantine.is_absolute():
        raise SecretaryError("cleanup recovery worktree paths must be absolute")
    _no_symlink_path(original)
    _no_symlink_path(quarantine)
    original = original.resolve(strict=False)
    quarantine = quarantine.resolve(strict=False)
    if quarantine != _worktree_quarantine_path(original, expected["kind"]):
        raise SecretaryError("cleanup recovery quarantine is not bound to its original worktree")
    if (not within(policy_root, quarantine) or within(repo, quarantine) or
            within(quarantine, repo) or quarantine.is_symlink()):
        raise SecretaryError(f"cleanup recovery quarantine is outside the managed worktree root: {quarantine}")
    if original.exists() or original.is_symlink():
        if quarantine.exists() or quarantine.is_symlink():
            raise SecretaryError("cleanup recovery found both original and quarantined worktrees")
    records = _cleanup_worktree_records(repo)
    by_path = {item["path"]: item for item in records}
    original_record = by_path.get(original)
    quarantine_record = by_path.get(quarantine)
    if quarantine_record is None:
        if quarantine.exists() or quarantine.is_symlink():
            raise SecretaryError(f"cleanup recovery found an unregistered quarantine: {quarantine}")
        if original_record is not None:
            if (expected["state"] != "planned" or original_record.get("branch") != expected["branch"] or
                    original_record.get("head") != expected["expectedOid"]):
                raise SecretaryError("cleanup recovery found a changed original worktree")
            return False
        if original.exists() or original.is_symlink() or expected["state"] not in {"remove-pending", "removed"}:
            raise SecretaryError("cleanup recovery cannot prove the worktree quarantine outcome")
        if _tmux_window_live(expected["tmuxSession"], expected["tmuxWindow"], expected["tmuxSocket"]):
            raise SecretaryError("cleanup recovery refuses an established worktree window")
        if _cleanup_worktree_has_live_process(original):
            raise SecretaryError("cleanup recovery refuses a live removed-worktree process")
        return True
    if original.exists() or original.is_symlink():
        raise SecretaryError("cleanup recovery found a reappeared original worktree")
    if (quarantine_record.get("branch") != expected["branch"] or
            quarantine_record.get("head") != expected["expectedOid"] or
            quarantine_record.get("locked") or quarantine_record.get("prunable") or
            not quarantine.is_dir() or quarantine.is_symlink()):
        raise SecretaryError("cleanup recovery quarantine identity changed")
    # Recovery is an established process state, not a fresh launch. A missing
    # or unreadable tmux socket therefore remains uncertain and blocks removal.
    if _tmux_window_live(expected["tmuxSession"], expected["tmuxWindow"], expected["tmuxSocket"]):
        raise SecretaryError("cleanup recovery refuses a live quarantined worktree window")
    if _cleanup_worktree_has_live_process(quarantine):
        raise SecretaryError("cleanup recovery refuses a live quarantined worktree process")
    with _git_worktree_index_lock(quarantine):
        if _git(quarantine, "status", "--porcelain=v1", "--untracked-files=all"):
            raise SecretaryError("cleanup recovery refuses a dirty quarantined worktree")
        removed = subprocess.run(["git", "-C", str(repo), "worktree", "remove", str(quarantine)],
                                 env=_env(), text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, check=False)
        if removed.returncode:
            raise SecretaryError("cleanup recovery could not remove quarantined worktree")
        if quarantine.exists() or quarantine.is_symlink():
            raise SecretaryError("cleanup recovery found a reappeared quarantine")
        _assert_worktree_registration_absent(repo, quarantine, "recovered")
    return True


def _tmux_window_live(session: str, window: str, socket_path: str | None) -> bool:
    if socket_path is None or not socket_path.startswith("/") or "\n" in socket_path or "\x00" in socket_path:
        raise SecretaryError("cleanup lacks authoritative tmux server identity")
    environment = _env()
    try:
        sessions = subprocess.run(["tmux", "-S", socket_path, "list-sessions", "-F", "#{session_name}"],
                                  env=environment, text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, check=False)
    except FileNotFoundError as error:
        raise SecretaryError("cleanup cannot prove tmux resource absence") from error
    if sessions.returncode != 0:
        raise SecretaryError("cleanup cannot prove tmux resource absence")
    names = sessions.stdout.splitlines()
    if any(not name or "\t" in name or "\x00" in name for name in names):
        raise SecretaryError("cleanup received malformed tmux state")
    if session not in names:
        return False
    windows = subprocess.run(["tmux", "-S", socket_path, "list-windows", "-t", f"={session}", "-F", "#{window_name}"],
                             env=environment, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, check=False)
    if windows.returncode != 0:
        raise SecretaryError("cleanup cannot inspect tmux workstream windows")
    values = windows.stdout.splitlines()
    if any(not name or "\t" in name or "\x00" in name for name in values):
        raise SecretaryError("cleanup received malformed tmux state")
    return window in values


def _managed_process_live(socket: str | None, session: str, window: str,
                          workspace: Path, session_id: str) -> bool | None:
    if not isinstance(socket, str) or not socket.startswith("/") or "\n" in socket or "\x00" in socket:
        return None
    workspace = workspace.resolve(strict=False)
    try:
        if not _tmux_window_live(session, window, socket):
            return False
    except SecretaryError:
        return None
    panes = subprocess.run(["tmux", "-S", socket, "list-panes", "-t", f"={session}:{window}",
                            "-F", "#{pane_pid}\t#{pane_current_path}"],
                           env=_env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if panes.returncode != 0:
        return None
    roots: list[int] = []
    for line in panes.stdout.splitlines():
        fields = line.split("\t", 1)
        if len(fields) != 2 or not fields[0].isdigit():
            return None
        try:
            pane_path = Path(fields[1]).resolve(strict=False)
        except (OSError, RuntimeError):
            return None
        if pane_path == workspace:
            roots.append(int(fields[0]))
    if not roots:
        return False
    processes = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], env=_env(), text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if processes.returncode != 0:
        return None
    parents: dict[int, int] = {}
    args_by_pid: dict[int, str] = {}
    for line in processes.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.strip().split(None, 2)
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            return None
        pid, parent = int(fields[0]), int(fields[1])
        parents[pid] = parent
        args_by_pid[pid] = fields[2]
    allowed_launchers = {
        str(Path(__file__).resolve().parents[1] / "bin" / "pidev"),
        str(Path.home() / ".local" / "bin" / "pidev"),
    }
    pending = list(roots)
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        args = args_by_pid.get(pid)
        if args is None:
            return None
        try:
            tokens = shlex.split(args)
        except ValueError:
            return None
        launcher_present = any(token in allowed_launchers for token in tokens)
        if (launcher_present and "--launch" in tokens and "--session-id" in tokens and
                any(tokens[index + 1] == session_id for index, token in enumerate(tokens[:-1])
                    if token == "--session-id")):
            return True
        pending.extend(child for child, parent in parents.items() if parent == pid)
    return False


def _reviewer_process_live(request: dict[str, Any], repo: Path) -> bool | None:
    socket = request.get("reviewerTmuxSocket")
    workspace_raw = request.get("reviewWorkspace")
    session_id = request.get("reviewerSessionId")
    if not isinstance(workspace_raw, str) or not isinstance(session_id, str):
        return None
    common = _git_path(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common_hash = hashlib.sha256(str(common).encode()).hexdigest()
    repo_name = re.sub(r"[^A-Za-z0-9_-]+", "-", repo.name).strip("-") or "repo"
    workspace = Path(workspace_raw).resolve(strict=False)
    worktree_name = re.sub(r"[^A-Za-z0-9_-]+", "-", workspace.name).strip("-") or "worktree"
    return _managed_process_live(socket, f"pi-{repo_name}-{common_hash[:12]}",
                                 f"w-{worktree_name}-{hashlib.sha256(str(workspace).encode()).hexdigest()[:12]}",
                                 workspace, session_id)


def _wait_for_managed_process(socket: str | None, session: str, window: str,
                              workspace: Path, session_id: str, repo: Path,
                              timeout: float = 10.0) -> bool | None:
    if not isinstance(socket, str):
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        live = _managed_process_live(socket, session, window, workspace, session_id)
        if live is True:
            return True
        if live is None:
            return None
        time.sleep(0.1)
    return _managed_process_live(socket, session, window, workspace, session_id)


def _wait_for_reviewer_process(request: dict[str, Any], repo: Path, timeout: float = 10.0) -> bool | None:
    socket = request.get("reviewerTmuxSocket")
    workspace_raw = request.get("reviewWorkspace")
    session_id = request.get("reviewerSessionId")
    if not isinstance(socket, str) or not isinstance(workspace_raw, str) or not isinstance(session_id, str):
        return None
    common = _git_path(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common_hash = hashlib.sha256(str(common).encode()).hexdigest()
    repo_name = re.sub(r"[^A-Za-z0-9_-]+", "-", repo.name).strip("-") or "repo"
    workspace = Path(workspace_raw).resolve(strict=False)
    worktree_name = re.sub(r"[^A-Za-z0-9_-]+", "-", workspace.name).strip("-") or "worktree"
    return _wait_for_managed_process(socket, f"pi-{repo_name}-{common_hash[:12]}",
                                     f"w-{worktree_name}-{hashlib.sha256(str(workspace).encode()).hexdigest()[:12]}",
                                     workspace, session_id, repo, timeout)


def _revalidate_cleanup_state(project_id: str, workstream_id: str, project: Path,
                              project_record: dict[str, Any], repo: Path,
                              expected_record: dict[str, Any],
                              expected_reviews: dict[str, dict[str, Any]],
                              *, allow_closed: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    current = _validate_workstream_record(_workstream_path(project, workstream_id), project, repo,
                                          project_record["objectFormat"], project_record["gitCommonDir"],
                                          allow_missing_workspace=True)
    immutable = ("schemaVersion", "workstreamId", "title", "role", "briefId", "targetRef",
                 "baseOid", "workspace", "branch", "createdAt")
    if ((allow_closed and (current["closedAt"] is None or current["closedAt"] != expected_record["closedAt"])) or
            (not allow_closed and current["closedAt"] is not None) or
            any(current[name] != expected_record[name] for name in immutable)):
        raise SecretaryError("workstream state changed during cleanup")
    landing = _landing_for(project, workstream_id)
    if landing is None:
        raise SecretaryError("cleanup lost its landing operation")
    _validate_landing_provenance(project, project_id, repo, current, landing)
    target_oid = _git(repo, "rev-parse", f"{current['targetRef']}^{{commit}}").lower()
    if not _is_ancestor(repo, landing["landedOid"], target_oid):
        raise SecretaryError("landed commit is no longer reachable from target")

    workspace = Path(current["workspace"])
    if workspace.exists() or workspace.is_symlink():
        raise SecretaryError("cleanup found a reappeared candidate worktree")
    if _cleanup_worktree_has_live_process(workspace):
        raise SecretaryError("cleanup found a live candidate process")
    _assert_worktree_registration_absent(repo, workspace, "candidate")

    seen_reviews: set[str] = set()
    for request_path in (project / "reviews" / "requests").glob("*.json"):
        request = _validate_review_request(request_path, project_id)
        if request["workstreamId"] != workstream_id:
            continue
        request_id = request["requestId"]
        expected = expected_reviews.get(request_id)
        if expected is None:
            raise SecretaryError("cleanup found a new review assignment")
        seen_reviews.add(request_id)
        for name in ("projectId", "requestId", "workstreamId", "candidateOid", "candidateTree", "baseOid",
                     "requestedAt", "reviewerSessionId", "reviewWorkspace", "reviewerTmuxSocket", "receiptId"):
            if request[name] != expected[name]:
                raise SecretaryError("review assignment changed during cleanup")
        if request["reviewWorkspace"] is not None:
            review = Path(request["reviewWorkspace"])
            if review.exists() or review.is_symlink():
                raise SecretaryError("cleanup found a reappeared review worktree")
            if _cleanup_worktree_has_live_process(review):
                raise SecretaryError("cleanup found a live review process")
            _assert_worktree_registration_absent(repo, review, "review")
    if seen_reviews != set(expected_reviews):
        raise SecretaryError("review assignment disappeared during cleanup")

    branch_ref = f"refs/heads/{current['branch']}"
    branch = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", branch_ref],
                            env=_env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if branch.returncode == 0:
        raise SecretaryError("cleanup found a reappeared workstream branch")
    if branch.returncode != 1:
        raise SecretaryError("could not inspect owned branch")
    return current, landing


def cleanup_workstream(project_id: str, workstream_id: str) -> dict[str, Any]:
    info = _require_secretary(project_id)
    repo = _canonical_repo(info["primaryRepository"])
    _, common, _ = project_identity(repo)
    project = _record_dir(_state_root(), project_id)
    with _workstream_cleanup_lock(project):
        with _git_write_lock(common):
            return _cleanup_workstream_locked(project_id, workstream_id)


def _cleanup_workstream_locked(project_id: str, workstream_id: str) -> dict[str, Any]:
    info = _require_secretary(project_id)
    _, project, project_record, repo = _project_context(info["primaryRepository"], info["capability"])
    record_path = _workstream_path(project, workstream_id)
    record = _validate_workstream_record(record_path, project, repo, project_record["objectFormat"],
                                         project_record["gitCommonDir"], allow_missing_workspace=True)
    recovery_path = _cleanup_recovery_path(project, "workstream", workstream_id)
    plan_hash = hashlib.sha256(f"workstream:{workstream_id}".encode()).hexdigest()
    previous_recovery = _read_cleanup_recovery(recovery_path, kind="workstream", identifier=workstream_id,
                                               plan_hash=plan_hash)
    recovered_worktrees: list[str] = list(previous_recovery["completedWorktrees"] if previous_recovery else [])
    if record["closedAt"] is not None and previous_recovery and previous_recovery["pendingWorktrees"]:
        raise SecretaryError("closed cleanup has an unresolved bound worktree quarantine")
    if record["closedAt"] is not None:
        expected_reviews: dict[str, dict[str, Any]] = {}
        for request_path in (project / "reviews" / "requests").glob("*.json"):
            request = _validate_review_request(request_path, project_id)
            if request["workstreamId"] == workstream_id:
                expected_reviews[request["requestId"]] = dict(request)
        with _project_lock(project):
            _revalidate_cleanup_state(project_id, workstream_id, project, project_record, repo,
                                      record, expected_reviews, allow_closed=True)
        return {"workstreamId": workstream_id, "closedAt": record["closedAt"], "alreadyClosed": True}
    landing = _landing_for(project, workstream_id)
    if landing is None:
        raise SecretaryError("cleanup refuses unlanded workstream")
    _validate_landing_provenance(project, project_id, repo, record, landing)
    target_oid = _git(repo, "rev-parse", f"{record['targetRef']}^{{commit}}").lower()
    if not _is_ancestor(repo, landing["landedOid"], target_oid):
        raise SecretaryError("landed commit is no longer reachable from target")

    workspace = Path(record["workspace"])
    if workspace.exists() or workspace.is_symlink():
        _assert_review_candidate_ready(record)
        if record["currentOid"] != landing["landedOid"]:
            raise SecretaryError("cleanup refuses dirty or moved workstream")
        runtime = _workstream_runtime(project, repo, record)
        if _tmux_window_live(runtime["tmuxSession"], runtime["tmuxWindow"], runtime["tmuxSocket"]):
            raise SecretaryError("cleanup refuses a live workstream window")
        if _cleanup_worktree_has_live_process(workspace):
            raise SecretaryError("cleanup refuses a live workstream process")
    else:
        if _cleanup_worktree_has_live_process(workspace):
            raise SecretaryError("cleanup refuses a live workstream process")
        _assert_worktree_registration_absent(repo, workspace, "candidate")

    _, trusted_live, policy_root = _load_policy_and_classify(repo)
    if not trusted_live:
        raise SecretaryError("cleanup recovery requires a trusted-live repository")
    recovery_assignments: dict[str, dict[str, Any]] = {}
    runtime_for_recovery = _workstream_runtime(project, repo, record)
    candidate_original = str(Path(record["workspace"]).resolve(strict=False))
    recovery_assignments[candidate_original] = {
        "kind": "candidate", "originalPath": candidate_original,
        "workstreamId": workstream_id, "requestId": None,
        "branch": record["branch"], "expectedOid": record["currentOid"],
        "tmuxSession": runtime_for_recovery["tmuxSession"],
        "tmuxWindow": runtime_for_recovery["tmuxWindow"],
        "tmuxSocket": runtime_for_recovery["tmuxSocket"],
        "sessionId": runtime_for_recovery["piSessionId"],
    }
    common_hash = hashlib.sha256(str(Path(project_record["gitCommonDir"])).encode()).hexdigest()
    repo_name = re.sub(r"[^A-Za-z0-9_-]+", "-", repo.name).strip("-") or "repo"
    for request_path in (project / "reviews" / "requests").glob("*.json"):
        request = _validate_review_request(request_path, project_id)
        if request["workstreamId"] != workstream_id or request["reviewWorkspace"] is None:
            continue
        review_original = str(Path(request["reviewWorkspace"]).resolve(strict=False))
        review_name = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(review_original).name).strip("-") or "worktree"
        recovery_assignments[review_original] = {
            "kind": "review", "originalPath": review_original,
            "workstreamId": workstream_id, "requestId": request["requestId"],
            "branch": None, "expectedOid": request["candidateOid"],
            "tmuxSession": f"pi-{repo_name}-{common_hash[:12]}",
            "tmuxWindow": f"w-{review_name}-{hashlib.sha256(review_original.encode()).hexdigest()[:12]}",
            "tmuxSocket": request["reviewerTmuxSocket"],
            "sessionId": request["reviewerSessionId"],
        }
    completed_worktrees: list[str] = list(dict.fromkeys(recovered_worktrees))
    pending_quarantines: list[str] = []
    active_metadata: dict[str, Any] | None = None
    worktree_metadata: dict[str, dict[str, Any]] = {}
    if previous_recovery and previous_recovery["pendingWorktrees"]:
        old_pending = list(previous_recovery["pendingWorktrees"])
        old_metadata = previous_recovery["worktreeMetadata"]
        for index, pending_path in enumerate(old_pending):
            raw_pending = Path(pending_path)
            if not raw_pending.is_absolute():
                raise SecretaryError("cleanup recovery has a non-absolute quarantine path")
            _no_symlink_path(raw_pending)
            canonical_pending = str(raw_pending.resolve(strict=False))
            metadata = old_metadata.get(pending_path) or old_metadata.get(canonical_pending)
            if not isinstance(metadata, dict):
                raise SecretaryError("cleanup recovery lacks bound worktree metadata")
            original_raw = metadata.get("originalPath")
            if not isinstance(original_raw, str) or not Path(original_raw).is_absolute():
                raise SecretaryError("cleanup recovery lacks a bound original worktree")
            _no_symlink_path(Path(original_raw))
            original_key = str(Path(original_raw).resolve(strict=False))
            assignment = recovery_assignments.get(original_key)
            if assignment is None:
                raise SecretaryError("cleanup recovery worktree is not assigned to this workstream")
            bound = dict(assignment)
            bound["state"] = metadata.get("state")
            if any(metadata.get(key) != value for key, value in assignment.items()):
                raise SecretaryError("cleanup recovery worktree identity does not match assignment")
            try:
                result = _resume_quarantined_worktree(repo, raw_pending, policy_root, bound)
            except Exception as error:
                remaining_metadata = {str(item): old_metadata[item] for item in old_pending[index:]
                                      if item in old_metadata and isinstance(old_metadata[item], dict)}
                _write_cleanup_recovery(recovery_path, kind="workstream", identifier=workstream_id,
                                        plan_hash=plan_hash, phase="worktree-pending",
                                        completed_worktrees=completed_worktrees,
                                        pending_worktrees=old_pending[index:],
                                        worktree_metadata=remaining_metadata, error=str(error))
                raise
            if result:
                completed_worktrees.append(assignment["originalPath"])
            # A planned move that did not happen is retried by the normal
            # cleanup path; a completed quarantine is already gone.
        _write_cleanup_recovery(recovery_path, kind="workstream", identifier=workstream_id,
                                plan_hash=plan_hash, phase="worktrees",
                                completed_worktrees=completed_worktrees, pending_worktrees=[],
                                worktree_metadata={})
    _write_cleanup_recovery(recovery_path, kind="workstream", identifier=workstream_id,
                            plan_hash=plan_hash, phase="prepared", worktree_metadata={})
    def quarantine_state(path: Path, state: str) -> None:
        if active_metadata is None:
            raise SecretaryError("cleanup quarantine state has no assigned worktree")
        key = str(path.resolve(strict=False))
        if key not in pending_quarantines:
            pending_quarantines.append(key)
        metadata = dict(active_metadata)
        metadata["state"] = state
        worktree_metadata[key] = metadata
        _write_cleanup_recovery(recovery_path, kind="workstream", identifier=workstream_id,
                                plan_hash=plan_hash, phase="worktree-pending",
                                completed_worktrees=completed_worktrees,
                                pending_worktrees=pending_quarantines,
                                worktree_metadata=worktree_metadata)

    def quarantine_failure(path: Path, error: str) -> None:
        quarantine_state(path, worktree_metadata.get(str(path.resolve(strict=False)), {}).get("state", "moved"))
        _write_cleanup_recovery(recovery_path, kind="workstream", identifier=workstream_id,
                                plan_hash=plan_hash, phase="worktree-pending",
                                completed_worktrees=completed_worktrees,
                                pending_worktrees=pending_quarantines,
                                worktree_metadata=worktree_metadata, error=error)

    def clear_quarantine(path: Path) -> None:
        key = str(path.resolve(strict=False))
        if key in pending_quarantines:
            pending_quarantines.remove(key)
        worktree_metadata.pop(key, None)

    def clear_active_quarantine() -> None:
        if active_metadata is None:
            return
        for key in list(worktree_metadata):
            if worktree_metadata[key].get("originalPath") == active_metadata.get("originalPath"):
                clear_quarantine(Path(key))

    removed_review_paths: list[str] = []
    expected_reviews: dict[str, dict[str, Any]] = {}
    for request_path in (project / "reviews" / "requests").glob("*.json"):
        request = _validate_review_request(request_path, project_id)
        if request["workstreamId"] != workstream_id:
            continue
        expected_reviews[request["requestId"]] = dict(request)
        if request["reviewWorkspace"] is None:
            continue
        review = Path(request["reviewWorkspace"])
        if not review.exists() and not review.is_symlink():
            if _cleanup_worktree_has_live_process(review):
                raise SecretaryError("cleanup refuses a live review process")
            _assert_worktree_registration_absent(repo, review, "review")
            continue
        _validate_review_worktree_for_cleanup(request, project, project_record, repo)
        if _cleanup_worktree_has_live_process(review):
            raise SecretaryError("cleanup refuses a live review process")
        with _git_worktree_index_lock(review):
            if (_git(review, "rev-parse", "HEAD^{commit}").lower() != request["candidateOid"] or
                    _git(review, "status", "--porcelain=v1", "--untracked-files=all")):
                raise SecretaryError("cleanup refuses dirty or moved review checkout")
            common_hash = hashlib.sha256(str(Path(project_record["gitCommonDir"])).encode()).hexdigest()
            repo_name = re.sub(r"[^A-Za-z0-9_-]+", "-", repo.name).strip("-") or "repo"
            review_name = re.sub(r"[^A-Za-z0-9_-]+", "-", review.name).strip("-") or "worktree"
            session = f"pi-{repo_name}-{common_hash[:12]}"
            window = f"w-{review_name}-{hashlib.sha256(str(review).encode()).hexdigest()[:12]}"
            if _tmux_window_live(session, window, request["reviewerTmuxSocket"]):
                raise SecretaryError("cleanup refuses a live review window")
            if _cleanup_worktree_has_live_process(review):
                raise SecretaryError("cleanup refuses a live review process")
            active_metadata = dict(recovery_assignments[str(review.resolve(strict=False))])
            _remove_worktree_with_process_guard(repo, review, "review", quarantine_failure,
                                                on_quarantine_state=quarantine_state)
            clear_active_quarantine()
            active_metadata = None
            removed_review_paths.append(str(review))
            completed_worktrees.append(str(review))
            _write_cleanup_recovery(recovery_path, kind="workstream", identifier=workstream_id,
                                    plan_hash=hashlib.sha256(f"workstream:{workstream_id}".encode()).hexdigest(),
                                    phase="reviews", completed_worktrees=completed_worktrees,
                                    pending_worktrees=pending_quarantines,
                                    worktree_metadata=worktree_metadata)
    if workspace.exists() or workspace.is_symlink():
        with _git_worktree_index_lock(workspace):
            record = _validate_workstream_record(record_path, project, repo, project_record["objectFormat"], project_record["gitCommonDir"])
            _assert_review_candidate_ready(record)
            if record["currentOid"] != landing["landedOid"]:
                raise SecretaryError("cleanup refuses dirty or moved workstream")
            runtime = _workstream_runtime(project, repo, record)
            if _tmux_window_live(runtime["tmuxSession"], runtime["tmuxWindow"], runtime["tmuxSocket"]):
                raise SecretaryError("cleanup refuses a live workstream window")
            if _cleanup_worktree_has_live_process(workspace):
                raise SecretaryError("cleanup refuses a live workstream process")
            active_metadata = dict(recovery_assignments[candidate_original])
            _remove_worktree_with_process_guard(repo, workspace, "candidate", quarantine_failure,
                                                on_quarantine_state=quarantine_state)
            clear_active_quarantine()
            active_metadata = None
            completed_worktrees.append(str(workspace))
            _write_cleanup_recovery(recovery_path, kind="workstream", identifier=workstream_id,
                                    plan_hash=hashlib.sha256(f"workstream:{workstream_id}".encode()).hexdigest(),
                                    phase="candidate", completed_worktrees=completed_worktrees,
                                    pending_worktrees=pending_quarantines,
                                    worktree_metadata=worktree_metadata)
    else:
        _assert_worktree_registration_absent(repo, workspace, "candidate")
    branch_ref = f"refs/heads/{record['branch']}"
    branch = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", branch_ref], env=_env(),
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if branch.returncode == 0:
        if branch.stdout.strip().lower() != landing["landedOid"]:
            raise SecretaryError("cleanup refuses moved owned branch")
        deleted = subprocess.run(["git", "-C", str(repo), "update-ref", "-d", branch_ref, landing["landedOid"]],
                                 env=_env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if deleted.returncode:
            raise SecretaryError("could not delete exact landed branch")
    elif branch.returncode != 1:
        raise SecretaryError("could not inspect owned branch")
    _write_cleanup_recovery(recovery_path, kind="workstream", identifier=workstream_id,
                            plan_hash=hashlib.sha256(f"workstream:{workstream_id}".encode()).hexdigest(),
                            phase="branch-deleted", completed_worktrees=completed_worktrees,
                            refs_applied=True)
    with _project_lock(project):
        current, _ = _revalidate_cleanup_state(project_id, workstream_id, project, project_record, repo,
                                                record, expected_reviews)
        persisted = {name: current[name] for name in WORKSTREAM_FIELDS}
        closed_at = _utc_now()
        persisted["closedAt"] = closed_at
        _atomic(record_path, json.dumps(persisted, sort_keys=True, separators=(",", ":")) + "\n")
    _write_cleanup_recovery(recovery_path, kind="workstream", identifier=workstream_id,
                            plan_hash=hashlib.sha256(f"workstream:{workstream_id}".encode()).hexdigest(),
                            phase="complete", completed_worktrees=completed_worktrees,
                            refs_applied=True)
    return {"workstreamId": workstream_id, "closedAt": closed_at,
            "landedOid": landing["landedOid"], "removedReviewWorktrees": len(removed_review_paths),
            "recoveryPath": str(recovery_path)}


def record_process_exit(workspace: str | Path, exit_code: int) -> dict[str, Any]:
    workspace_path = Path(workspace).resolve(strict=True)
    matches: list[tuple[str, str]] = []
    root = _state_root()
    for registry in _registry_records(root):
        info = launch_info(registry["projectId"], internal=True)
        _, project, project_record, repo = _project_context(info["primaryRepository"], info["capability"])
        directory = project / "workstreams"
        if not directory.is_dir():
            continue
        for entry in directory.glob("*.json"):
            record = _validate_workstream_record(entry, project, repo, project_record["objectFormat"], project_record["gitCommonDir"])
            if Path(record["workspace"]).resolve(strict=True) == workspace_path and record["closedAt"] is None:
                matches.append((registry["projectId"], record["workstreamId"]))
    if len(matches) != 1:
        raise SecretaryError("workspace is not owned by exactly one open workstream")
    project_id, workstream_id = matches[0]
    return append_event(project_id, workstream_id, "process-exit",
                        f"Full agent process exited with status {exit_code}",
                        source="host", validate_route=False)


def _pidev_path() -> Path:
    candidates = [Path(__file__).resolve().parents[1] / "bin" / "pidev",
                  Path.home() / ".local" / "bin" / "pidev"]
    for candidate in candidates:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if (info.st_uid == os.getuid() and stat.S_ISREG(info.st_mode) and
                not stat.S_ISLNK(info.st_mode) and not (info.st_mode & 0o022) and
                info.st_mode & stat.S_IXUSR):
            return candidate
    raise SecretaryError("managed pidev launcher is unavailable")


def launch_workstream(project_id: str, workstream_id: str) -> dict[str, Any]:
    info = launch_info(project_id, internal=True)
    repo = Path(info["primaryRepository"])
    project = _record_dir(_state_root(), project_id)
    with _workstream_launch_lock(project, workstream_id):
        record = open_workstream(repo, info["capability"], workstream_id)
        current_socket = _current_tmux_socket()
        runtime_path = _workstream_runtime_path(project, workstream_id)
        with _project_lock(project):
            runtime = _workstream_runtime(project, repo, record)
            if (runtime["tmuxSocket"] is not None and current_socket is not None and
                    runtime["tmuxSocket"] != current_socket):
                raise SecretaryError("workstream belongs to a different tmux server")
            desired_socket = runtime["tmuxSocket"] or current_socket or _default_tmux_socket()
            runtime["tmuxSocket"] = desired_socket
            if runtime["launchState"] in {"launched", "uncertain"}:
                live = _managed_process_live(desired_socket, runtime["tmuxSession"], runtime["tmuxWindow"],
                                              Path(record["workspace"]), runtime["piSessionId"])
                if live is True:
                    runtime["launchState"] = "launched"
                    _atomic(runtime_path, json.dumps(runtime, sort_keys=True, separators=(",", ":")) + "\n")
                    record.update(runtime)
                    return record
                if live is None:
                    runtime["launchState"] = "uncertain"
                    _atomic(runtime_path, json.dumps(runtime, sort_keys=True, separators=(",", ":")) + "\n")
                    raise SecretaryError("cannot prove workstream process state")
            runtime["launchState"] = "pending"
            _atomic(runtime_path, json.dumps(runtime, sort_keys=True, separators=(",", ":")) + "\n")
        record.update(runtime)
        brief_path = _brief_path(project, record["briefId"])
        _safe_lstat(brief_path, directory=False)
        environment = _env()
        for name in ("TERM", "COLORTERM", "PI_CODING_AGENT_DIR", "TMUX_TMPDIR"):
            if os.environ.get(name):
                environment[name] = os.environ[name]
        environment["TMUX"] = f"{runtime['tmuxSocket']},0,0"
        environment["PI_PIDEV_DETACHED"] = "1"
        environment.update({"PI_PIDEV_SESSION_ID": record["piSessionId"],
                            "PI_PIDEV_WORKSTREAM_ID": workstream_id,
                            "PI_PIDEV_PROJECT_ID": project_id,
                            "PI_PIDEV_BRIEF_PATH": str(brief_path),
                            "PI_PIDEV_CONTROL": str(Path(__file__).resolve())})
        try:
            result = subprocess.run([str(_pidev_path())], cwd=record["workspace"], env=environment,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        except (OSError, SecretaryError) as error:
            result = None
            launch_error = error
        else:
            launch_error = None
        if launch_error is None and result is not None and result.returncode == 0:
            live = _wait_for_managed_process(runtime["tmuxSocket"], runtime["tmuxSession"], runtime["tmuxWindow"],
                                              Path(record["workspace"]), runtime["piSessionId"], repo)
            if live is not True:
                with _project_lock(project):
                    current = _workstream_runtime(project, repo, record)
                    current["launchState"] = "uncertain" if live is None else "pending"
                    _atomic(runtime_path, json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n")
                if live is None:
                    raise SecretaryError("cannot prove workstream process state")
                raise SecretaryError("workstream launch did not become live")
        with _project_lock(project):
            current = _workstream_runtime(project, repo, record)
            if any(current[name] != runtime[name] for name in ("piSessionId", "tmuxSession", "tmuxWindow", "tmuxSocket")):
                raise SecretaryError("workstream launch state changed concurrently")
            if launch_error is not None or result is None or result.returncode:
                live = _managed_process_live(runtime["tmuxSocket"], runtime["tmuxSession"], runtime["tmuxWindow"],
                                              Path(record["workspace"]), runtime["piSessionId"])
                current["launchState"] = "launched" if live is True else "uncertain" if live is None else "pending"
                _atomic(runtime_path, json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n")
                if live is True:
                    record.update(current)
                    return record
                if isinstance(launch_error, SecretaryError):
                    raise launch_error
                if launch_error is not None:
                    raise SecretaryError(f"workstream launch failed: {launch_error}") from launch_error
                if live is None:
                    raise SecretaryError(f"workstream launch failed with uncertain process state: {(result.stderr or result.stdout).strip()[:260]}")
                raise SecretaryError(f"workstream launch failed: {(result.stderr or result.stdout).strip()[:300]}")
            current["launchState"] = "launched"
            _atomic(runtime_path, json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n")
            record.update(current)
            return record


def record_idea(project_id: str, title: str, brief_text: str) -> dict[str, Any]:
    info = launch_info(project_id, internal=True)
    return create_brief(info["primaryRepository"], info["capability"], title, brief_text)


def promote_workstream(project_id: str, title: str, brief_text: str, role: str,
                       brief_id: str | None = None, workstream_id: str | None = None) -> dict[str, Any]:
    info = launch_info(project_id, internal=True)
    repo = Path(info["primaryRepository"])
    capability = info["capability"]
    if brief_id is None:
        brief = create_brief(repo, capability, title, brief_text)
        brief_id = brief["briefId"]
    else:
        read_brief(repo, capability, brief_id)
    record = create_workstream(repo, capability, title, role, brief_id,
                               workstream_id=workstream_id)
    launch_workstream(project_id, record["workstreamId"])
    return record


# --- CLI ---

def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=".")
    parser.add_argument("--capability", default=os.environ.get("PI_SECRETARY_CAPABILITY"))
    parser.add_argument("--print-capability", action="store_true",
                        help="explicit bootstrap/test hook; never used by normal output")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    p = sub.add_parser("register"); p.add_argument("--alias", required=True)
    p.add_argument("--repository", default=argparse.SUPPRESS)
    sub.add_parser("registry-list")
    p = sub.add_parser("launch-info"); p.add_argument("--project-id", required=True)
    p.add_argument("--internal-launch", action="store_true")
    sub.add_parser("status")
    p = sub.add_parser("git-read"); p.add_argument("--project-id", required=True)
    p.add_argument("--operation", required=True); p.add_argument("git_args", nargs=argparse.REMAINDER)
    p = sub.add_parser("git-write"); p.add_argument("--project-id", required=True)
    p.add_argument("--operation", required=True); p.add_argument("--message")
    p.add_argument("--path", dest="paths", action="append", default=[])
    p = sub.add_parser("git-cleanup"); p.add_argument("--project-id", required=True)
    p.add_argument("--operation", choices=("plan", "apply"), required=True)
    p.add_argument("--plan-json", required=True); p.add_argument("--plan-hash")
    p = sub.add_parser("brief-create"); p.add_argument("title"); p.add_argument("text")
    p = sub.add_parser("brief-read"); p.add_argument("brief_id")
    sub.add_parser("brief-list")
    p = sub.add_parser("workstream-create"); p.add_argument("title"); p.add_argument("role")
    p.add_argument("brief_id"); p.add_argument("--target-ref", default="HEAD"); p.add_argument("--workstream-id")
    p = sub.add_parser("workstream-open"); p.add_argument("workstream_id")
    sub.add_parser("workstream-list")
    p = sub.add_parser("record-idea"); p.add_argument("--project-id", required=True)
    p.add_argument("--title", required=True); p.add_argument("--brief", required=True)
    p = sub.add_parser("promote"); p.add_argument("--project-id", required=True)
    p.add_argument("--title", required=True); p.add_argument("--brief", required=True)
    p.add_argument("--role", required=True); p.add_argument("--brief-id"); p.add_argument("--workstream-id")
    p = sub.add_parser("focus-workstream"); p.add_argument("--project-id", required=True); p.add_argument("--workstream-id", required=True)
    p = sub.add_parser("project-workstreams"); p.add_argument("--project-id", required=True)
    p = sub.add_parser("notify"); p.add_argument("--project-id", required=True); p.add_argument("--workstream-id", required=True)
    p.add_argument("--kind", required=True); p.add_argument("--summary", required=True); p.add_argument("--details", default="")
    p = sub.add_parser("events-list"); p.add_argument("--project-id", required=True); p.add_argument("--all", action="store_true")
    p = sub.add_parser("event-ack"); p.add_argument("--project-id", required=True); p.add_argument("--event-id", required=True)
    p = sub.add_parser("process-exit"); p.add_argument("--workspace", required=True); p.add_argument("--exit-code", required=True, type=int)
    p = sub.add_parser("review-create"); p.add_argument("--project-id", required=True); p.add_argument("--event-id", required=True)
    p = sub.add_parser("review-launch-info"); p.add_argument("--project-id", required=True); p.add_argument("--request-id", required=True)
    p.add_argument("--internal-launch", action="store_true")
    p = sub.add_parser("review-submit"); p.add_argument("--project-id", required=True); p.add_argument("--request-id", required=True)
    p.add_argument("--verdict", required=True); p.add_argument("--summary", required=True); p.add_argument("--findings", default="")
    p = sub.add_parser("review-status"); p.add_argument("--project-id", required=True); p.add_argument("--request-id", required=True)
    p = sub.add_parser("land-reviewed"); p.add_argument("--project-id", required=True); p.add_argument("--request-id", required=True)
    p = sub.add_parser("integration-create"); p.add_argument("--project-id", required=True); p.add_argument("--request-id", required=True)
    p = sub.add_parser("workstream-cleanup"); p.add_argument("--project-id", required=True); p.add_argument("--workstream-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = init_project(args.repository)
            if args.print_capability:
                project_id, _, _ = project_identity(args.repository)
                capability_path = _capability_path(_state_root(), project_id)
                _safe_lstat(capability_path, directory=False)
                result["capability"] = capability_path.read_text(encoding="utf-8").strip()
            else:
                result.pop("capability", None)
        elif args.command == "register":
            result = register_project(args.repository, args.alias)
        elif args.command == "registry-list":
            result = registry_list()
        elif args.command == "launch-info":
            result = launch_info(args.project_id, internal=args.internal_launch)
        elif args.command == "status":
            result = status(args.repository, args.capability)
        elif args.command == "git-read":
            result = git_read(args.project_id, args.operation, args.git_args)
        elif args.command == "git-write":
            result = git_write(args.project_id, args.operation, args.message, args.paths)
        elif args.command == "git-cleanup":
            try:
                cleanup_plan = json.loads(args.plan_json)
            except (TypeError, json.JSONDecodeError) as error:
                raise SecretaryError("cleanup plan JSON is malformed") from error
            result = git_cleanup(args.project_id, args.operation, cleanup_plan, args.plan_hash)
        elif args.command == "brief-create":
            result = create_brief(args.repository, args.capability, args.title, args.text)
        elif args.command == "brief-read":
            result = read_brief(args.repository, args.capability, args.brief_id)
        elif args.command == "brief-list":
            result = list_briefs(args.repository, args.capability)
        elif args.command == "workstream-create":
            result = create_workstream(args.repository, args.capability, args.title, args.role,
                                       args.brief_id, args.target_ref, args.workstream_id)
        elif args.command == "workstream-open":
            result = open_workstream(args.repository, args.capability, args.workstream_id)
        elif args.command == "workstream-list":
            result = list_workstreams(args.repository, args.capability)
        elif args.command == "record-idea":
            result = record_idea(args.project_id, args.title, args.brief)
        elif args.command == "promote":
            result = promote_workstream(args.project_id, args.title, args.brief, args.role,
                                        args.brief_id, args.workstream_id)
        elif args.command == "focus-workstream":
            result = launch_workstream(args.project_id, args.workstream_id)
        elif args.command == "project-workstreams":
            info = launch_info(args.project_id, internal=True)
            result = list_workstreams(info["primaryRepository"], info["capability"])
        elif args.command == "notify":
            result = append_event(args.project_id, args.workstream_id, args.kind, args.summary, args.details)
        elif args.command == "events-list":
            result = list_events(args.project_id, include_acknowledged=args.all)
        elif args.command == "event-ack":
            result = acknowledge_event(args.project_id, args.event_id)
        elif args.command == "process-exit":
            result = record_process_exit(args.workspace, args.exit_code)
        elif args.command == "review-create":
            result = create_reviewer(args.project_id, args.event_id)
        elif args.command == "review-launch-info":
            if not args.internal_launch:
                raise SecretaryError("review launch info is internal-only")
            result = review_launch_info(args.project_id, args.request_id)
        elif args.command == "review-submit":
            result = submit_review(args.project_id, args.request_id, args.verdict, args.summary, args.findings)
        elif args.command == "review-status":
            result = review_status(args.project_id, args.request_id)
        elif args.command == "land-reviewed":
            result = land_reviewed(args.project_id, args.request_id)
        elif args.command == "integration-create":
            result = create_integration(args.project_id, args.request_id)
        else:
            result = cleanup_workstream(args.project_id, args.workstream_id)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except SecretaryError as error:
        print(json.dumps({"error": str(error)}, sort_keys=True, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
