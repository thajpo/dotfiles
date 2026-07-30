#!/usr/bin/env python3
"""Secure, host-owned project secretary state and constrained workstreams.

This module deliberately keeps its public vocabulary small.  It does not execute
model supplied commands or accept model supplied filesystem destinations.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
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
                     require_capability: bool = True) -> tuple[Path, Path, dict[str, Any], Path]:
    repo = _canonical_repo(repository)
    project_id, common, object_format = project_identity(repo)
    root = _state_root()
    _assert_state_root_not_repo(root, repo)
    project = _record_dir(root, project_id)
    _safe_lstat(project, directory=True)
    record = _validate_project_record(project / "project.json", project_id, common, object_format)
    if require_capability:
        _check_capability(root, project_id, capability, record["capabilityHash"])
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
    if operation == "worktree" and (not git_args or git_args[0] != "list"):
        raise SecretaryError("only git worktree list is supported")
    info = launch_info(project_id)
    repo = Path(info["primaryRepository"])
    environment = _env()
    environment.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0"})
    result = subprocess.run(["git", operation, *git_args], cwd=str(repo), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=environment)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise SecretaryError(f"git {operation} failed: {detail[:500]}")
    if len(result.stdout.encode()) > MAX_JSON or len(result.stderr.encode()) > MAX_JSON:
        raise SecretaryError("Git output is too large")
    return {"projectId": project_id, "operation": operation, "args": git_args,
            "stdout": result.stdout, "stderr": result.stderr}


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


def _validate_review_request(path: Path, project_id: str) -> dict[str, Any]:
    fields = {"schemaVersion", "requestId", "projectId", "workstreamId", "candidateOid",
              "candidateTree", "baseOid", "requestedAt", "reviewerSessionId", "reviewWorkspace", "reviewerTmuxSocket", "receiptId"}
    value = _read_json(path, fields, required=fields - {"reviewerTmuxSocket"})
    value.setdefault("reviewerTmuxSocket", None)
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
    if value.get("receiptId") is not None:
        _id(value["receiptId"], "review receipt id")
    return value


def _create_review_request(project_id: str, workstream_id: str, record: dict[str, Any]) -> dict[str, Any]:
    project = _record_dir(_state_root(), project_id)
    workspace = Path(record["workspace"])
    candidate = _git(workspace, "rev-parse", "HEAD^{commit}").lower()
    tree = _git(workspace, "rev-parse", "HEAD^{tree}").lower()
    request_id = "rr-" + secrets.token_hex(16)
    value = {"schemaVersion": 1, "requestId": request_id, "projectId": project_id,
             "workstreamId": workstream_id, "candidateOid": candidate, "candidateTree": tree,
             "baseOid": record["baseOid"], "requestedAt": _utc_now(), "reviewerSessionId": None,
             "reviewWorkspace": None, "reviewerTmuxSocket": None, "receiptId": None}
    _atomic(_review_request_path(project, request_id), json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    return value


def append_event(project_id: str, workstream_id: str, kind: str, summary: str,
                 details: str = "", *, source: str = "agent", validate_route: bool = True) -> dict[str, Any]:
    if kind not in EVENT_KINDS or (source == "agent" and kind == "process-exit"):
        raise SecretaryError("invalid event kind")
    _, _, record = _require_workstream(project_id, workstream_id)
    if validate_route:
        _validate_feature_route(record)
    summary = _line(summary, "event summary", 500)
    if not isinstance(details, str) or len(details.encode()) > 4096:
        raise SecretaryError("invalid event details")
    project = _record_dir(_state_root(), project_id)
    event_id = "evt-" + secrets.token_hex(16)
    value = {"schemaVersion": 1, "eventId": event_id, "projectId": project_id,
             "workstreamId": workstream_id, "kind": kind, "summary": summary,
             "details": details, "source": source, "createdAt": _utc_now(), "acknowledgedAt": None}
    with _project_lock(project):
        if kind == "review-requested":
            request = _create_review_request(project_id, workstream_id, record)
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


def _workstream_runtime_path(project: Path, workstream_id: str) -> Path:
    return project / "workstream-runtime" / f"{_id(workstream_id, 'workstream id')}.json"


def _workstream_runtime(project: Path, repo: Path, record: dict[str, Any]) -> dict[str, Any]:
    path = _workstream_runtime_path(project, record["workstreamId"])
    fields = {"schemaVersion", "workstreamId", "piSessionId", "tmuxSession", "tmuxWindow", "tmuxSocket", "seededAt"}
    if path.exists() or path.is_symlink():
        value = _read_json(path, fields, required=fields - {"tmuxSocket"})
        value.setdefault("tmuxSocket", None)
        if (value.get("schemaVersion") != 1 or value.get("workstreamId") != record["workstreamId"] or
                not isinstance(value.get("piSessionId"), str) or
                not SESSION_RE.fullmatch(value["piSessionId"]) or
                not isinstance(value.get("tmuxSession"), str) or
                not re.fullmatch(r"[A-Za-z0-9_-]{1,300}", value["tmuxSession"]) or
                not isinstance(value.get("tmuxWindow"), str) or
                not re.fullmatch(r"[A-Za-z0-9_-]{1,300}", value["tmuxWindow"]) or
                (value.get("tmuxSocket") is not None and
                 (not isinstance(value.get("tmuxSocket"), str) or not value["tmuxSocket"].startswith("/"))) or
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
             "tmuxSocket": _current_tmux_socket(), "seededAt": None}
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
                                object_format: str, common_dir: str) -> dict[str, Any]:
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
    ws_path = workspace_raw.resolve(strict=True)
    # Re-validate policy
    _, trusted_live, policy_root = _load_policy_and_classify(repo)
    if not trusted_live:
        raise SecretaryError("repository is not trusted-live under current policy")
    if not within(policy_root, ws_path) or within(repo, ws_path):
        raise SecretaryError("workstream workspace escapes policy root")
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
    event = _validate_event(_event_path(project, event_id), project_id)
    if event["kind"] != "review-requested" or event["acknowledgedAt"] is not None:
        raise SecretaryError("event is not an open review request")
    try:
        request_id = json.loads(event["details"])["reviewRequestId"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise SecretaryError("review event is malformed") from error
    request_path = _review_request_path(project, request_id)
    before = _validate_review_request(request_path, project_id)
    reviewer_tmux_socket = _current_tmux_socket()
    if (before["reviewerTmuxSocket"] is not None and reviewer_tmux_socket is not None and
            before["reviewerTmuxSocket"] != reviewer_tmux_socket):
        raise SecretaryError("reviewer belongs to a different tmux server")
    workstream = None
    if before["reviewerSessionId"] is None:
        _, _, workstream = _require_workstream(project_id, before["workstreamId"])
    with _project_lock(project):
        request = _validate_review_request(request_path, project_id)
        if request["reviewerSessionId"] is None:
            if workstream is None or workstream["workstreamId"] != request["workstreamId"]:
                raise SecretaryError("review assignment changed concurrently")
            parent = Path(workstream["workspace"]).parent
            workspace = parent / f"review-{request_id}"
            if workspace.exists() or workspace.is_symlink():
                raise SecretaryError("review workspace already exists")
            repo = Path(info["primaryRepository"])
            created = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(workspace), request["candidateOid"]],
                                     env=_env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if created.returncode:
                raise SecretaryError("could not create exact review worktree")
            request["reviewerSessionId"] = "rv-" + secrets.token_hex(24)
            request["reviewWorkspace"] = str(workspace.resolve(strict=True))
            request["reviewerTmuxSocket"] = reviewer_tmux_socket
            _atomic(request_path, json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n")
        elif reviewer_tmux_socket is not None:
            if request["reviewerTmuxSocket"] not in (None, reviewer_tmux_socket):
                raise SecretaryError("reviewer tmux server changed concurrently")
            if request["reviewerTmuxSocket"] is None:
                request["reviewerTmuxSocket"] = reviewer_tmux_socket
                _atomic(request_path, json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n")
    environment = _env()
    for name in ("TMUX", "TERM", "COLORTERM", "PI_CODING_AGENT_DIR"):
        if os.environ.get(name): environment[name] = os.environ[name]
    environment.update({"PI_PIDEV_SESSION_ID": request["reviewerSessionId"],
                        "PI_PIDEV_REVIEW_PROJECT_ID": project_id,
                        "PI_PIDEV_REVIEW_REQUEST_ID": request_id,
                        "PI_PIDEV_CONTROL": str(Path(__file__).resolve())})
    result = subprocess.run([str(_pidev_path())], cwd=request["reviewWorkspace"], env=environment,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise SecretaryError(f"review launch failed: {(result.stderr or result.stdout).strip()[:300]}")
    return request


def review_launch_info(project_id: str, request_id: str) -> dict[str, Any]:
    info = launch_info(project_id, internal=True)
    project = _record_dir(_state_root(), project_id)
    request = _validate_review_request(_review_request_path(project, request_id), project_id)
    if request["reviewerSessionId"] is None or request["reviewWorkspace"] is None:
        raise SecretaryError("review request is not assigned")
    workspace = Path(request["reviewWorkspace"]).resolve(strict=True)
    if _git(workspace, "rev-parse", "HEAD^{commit}").lower() != request["candidateOid"]:
        raise SecretaryError("review checkout moved from assigned commit")
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
    workspace = Path(request["reviewWorkspace"])
    if _git(workspace, "rev-parse", "HEAD^{tree}").lower() != request["candidateTree"]:
        raise SecretaryError("review tree differs from assignment")
    if _git(workspace, "status", "--porcelain=v1"):
        raise SecretaryError("review checkout is dirty")
    project = _record_dir(_state_root(), project_id)
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
        receipt_id = "receipt-" + secrets.token_hex(16)
        receipt = {"schemaVersion": 1, "receiptId": receipt_id, "projectId": project_id,
                   "requestId": request_id, "workstreamId": request["workstreamId"],
                   "candidateOid": request["candidateOid"], "candidateTree": request["candidateTree"],
                   "baseOid": request["baseOid"], "reviewerSessionId": request["reviewerSessionId"],
                   "verdict": verdict, "summary": summary, "findings": findings, "reviewedAt": _utc_now()}
        _atomic(_review_receipt_path(project, receipt_id), json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
        current["receiptId"] = receipt_id
        _atomic(request_path, json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n")
    return receipt


def review_status(project_id: str, request_id: str) -> dict[str, Any]:
    request = review_launch_info(project_id, request_id)
    _, _, workstream = _require_workstream(project_id, request["workstreamId"])
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
    status = review_status(project_id, request_id)
    receipt = status.get("receipt")
    if not isinstance(receipt, dict) or receipt.get("verdict") != "accept" or status["stale"]:
        raise SecretaryError("an exact current ACCEPT receipt is required")
    _, project, project_record, repo = _project_context(info["primaryRepository"], info["capability"])
    workstream = open_workstream(repo, info["capability"], status["workstreamId"])
    target_name = workstream["targetRef"]
    target_ref = _git(repo, "rev-parse", "--symbolic-full-name", target_name)
    if not target_ref.startswith("refs/heads/"):
        raise SecretaryError("landing target is not a branch")
    target = _target_worktree(repo, target_ref)
    candidate = status["candidateOid"]
    expected = status["baseOid"]
    lock_path = Path(project_record["gitCommonDir"]) / "pi-secretary-target.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        actual = _git(repo, "rev-parse", f"{target_ref}^{{commit}}").lower()
        if actual == candidate:
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
        merged = subprocess.run(["git", "-C", str(target), "merge", "--ff-only", "--no-edit", candidate],
                                env=_env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if merged.returncode or _git(repo, "rev-parse", f"{target_ref}^{{commit}}").lower() != candidate:
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
    for path in directory.glob("land-*.json"):
        fields = {"schemaVersion", "operationId", "kind", "projectId", "workstreamId", "requestId",
                  "receiptId", "targetRef", "expectedTargetOid", "landedOid", "landedAt"}
        value = _read_json(path, fields, required=fields)
        if value.get("kind") == "landing" and value.get("workstreamId") == workstream_id:
            found.append(value)
    return sorted(found, key=lambda value: value["landedAt"])[-1] if found else None


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


def cleanup_workstream(project_id: str, workstream_id: str) -> dict[str, Any]:
    project = _record_dir(_state_root(), project_id)
    lock_path = project / "operations" / "cleanup.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _cleanup_workstream_locked(project_id, workstream_id)
    finally:
        os.close(fd)


def _cleanup_workstream_locked(project_id: str, workstream_id: str) -> dict[str, Any]:
    info = _require_secretary(project_id)
    _, project, project_record, repo = _project_context(info["primaryRepository"], info["capability"])
    record_path = _workstream_path(project, workstream_id)
    raw = _read_json(record_path, WORKSTREAM_FIELDS, required=WORKSTREAM_FIELDS)
    if raw["closedAt"] is not None:
        return {"workstreamId": workstream_id, "closedAt": raw["closedAt"], "alreadyClosed": True}
    landing = _landing_for(project, workstream_id)
    if landing is None:
        raise SecretaryError("cleanup refuses unlanded workstream")
    target_oid = _git(repo, "rev-parse", f"{raw['targetRef']}^{{commit}}").lower()
    if not _is_ancestor(repo, landing["landedOid"], target_oid):
        raise SecretaryError("landed commit is no longer reachable from target")

    workspace = Path(raw["workspace"])
    if workspace.exists() or workspace.is_symlink():
        record = _validate_workstream_record(record_path, project, repo, project_record["objectFormat"], project_record["gitCommonDir"])
        if record["currentOid"] != landing["landedOid"] or _git(workspace, "status", "--porcelain=v1"):
            raise SecretaryError("cleanup refuses dirty or moved workstream")
        runtime = _workstream_runtime(project, repo, record)
        if _tmux_window_live(runtime["tmuxSession"], runtime["tmuxWindow"], runtime["tmuxSocket"]):
            raise SecretaryError("cleanup refuses a live workstream window")

    review_paths: list[Path] = []
    for request_path in (project / "reviews" / "requests").glob("*.json"):
        request = _validate_review_request(request_path, project_id)
        if request["workstreamId"] != workstream_id or request["reviewWorkspace"] is None:
            continue
        review = Path(request["reviewWorkspace"])
        if not review.exists() and not review.is_symlink():
            continue
        if (_git(review, "rev-parse", "HEAD^{commit}").lower() != request["candidateOid"] or
                _git(review, "status", "--porcelain=v1")):
            raise SecretaryError("cleanup refuses dirty or moved review checkout")
        common_hash = hashlib.sha256(str(Path(project_record["gitCommonDir"])).encode()).hexdigest()
        repo_name = re.sub(r"[^A-Za-z0-9_-]+", "-", repo.name).strip("-") or "repo"
        review_name = re.sub(r"[^A-Za-z0-9_-]+", "-", review.name).strip("-") or "worktree"
        session = f"pi-{repo_name}-{common_hash[:12]}"
        window = f"w-{review_name}-{hashlib.sha256(str(review).encode()).hexdigest()[:12]}"
        if _tmux_window_live(session, window, request["reviewerTmuxSocket"]):
            raise SecretaryError("cleanup refuses a live review window")
        review_paths.append(review)

    for review in review_paths:
        removed = subprocess.run(["git", "-C", str(repo), "worktree", "remove", str(review)], env=_env(),
                                 text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if removed.returncode:
            raise SecretaryError("could not remove clean owned review worktree")
    if workspace.exists() or workspace.is_symlink():
        removed = subprocess.run(["git", "-C", str(repo), "worktree", "remove", str(workspace)], env=_env(),
                                 text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if removed.returncode:
            raise SecretaryError("could not remove clean owned worktree")
    branch_ref = f"refs/heads/{raw['branch']}"
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
    with _project_lock(project):
        current = _read_json(record_path, WORKSTREAM_FIELDS, required=WORKSTREAM_FIELDS)
        if current["closedAt"] is not None or current["workspace"] != raw["workspace"] or current["branch"] != raw["branch"]:
            raise SecretaryError("workstream state changed during cleanup")
        current["closedAt"] = _utc_now()
        _atomic(record_path, json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n")
    return {"workstreamId": workstream_id, "closedAt": current["closedAt"],
            "landedOid": landing["landedOid"], "removedReviewWorktrees": len(review_paths)}


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
    record = open_workstream(repo, info["capability"], workstream_id)
    project = _record_dir(_state_root(), project_id)
    current_socket = _current_tmux_socket()
    if record["tmuxSocket"] is not None and current_socket is not None and record["tmuxSocket"] != current_socket:
        raise SecretaryError("workstream belongs to a different tmux server")
    if record["tmuxSocket"] is None and current_socket is not None:
        runtime_path = _workstream_runtime_path(project, workstream_id)
        with _project_lock(project):
            runtime = _workstream_runtime(project, repo, record)
            if runtime["tmuxSocket"] not in (None, current_socket):
                raise SecretaryError("workstream tmux server changed concurrently")
            runtime["tmuxSocket"] = current_socket
            _atomic(runtime_path, json.dumps(runtime, sort_keys=True, separators=(",", ":")) + "\n")
        record["tmuxSocket"] = current_socket
    brief_path = _brief_path(project, record["briefId"])
    _safe_lstat(brief_path, directory=False)
    environment = _env()
    for name in ("TMUX", "TERM", "COLORTERM", "PI_CODING_AGENT_DIR"):
        if os.environ.get(name):
            environment[name] = os.environ[name]
    environment.update({"PI_PIDEV_SESSION_ID": record["piSessionId"],
                        "PI_PIDEV_WORKSTREAM_ID": workstream_id,
                        "PI_PIDEV_PROJECT_ID": project_id,
                        "PI_PIDEV_BRIEF_PATH": str(brief_path),
                        "PI_PIDEV_CONTROL": str(Path(__file__).resolve())})
    result = subprocess.run([str(_pidev_path())], cwd=record["workspace"], env=environment,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise SecretaryError(f"workstream launch failed: {(result.stderr or result.stdout).strip()[:300]}")
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
