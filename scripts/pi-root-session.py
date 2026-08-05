#!/usr/bin/env python3
"""Durable Pi root-session registry and legacy-session migration.

Root conversations are deliberately separate from pi-subagents runs.  Active
root JSONL files live directly under ``sessions/root`` so Pi's global session
selector can see them; child runs use ``sessions/subagent`` and are not roots.
The registry is host-owned, atomic, and never inferred from the current cwd.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterable

SCHEMA_VERSION = 1
SESSION_VERSION = 3
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
BRANCH_RE = re.compile(r"^pi/root-[A-Za-z0-9][A-Za-z0-9._-]{0,90}$")
# Dirty in-place roots retain the user's existing branch (often main/master,
# but it may be a feature branch). Keep it ref-safe while reserving the
# managed-root invariant for branches allocated by this helper.
GIT_BRANCH_RE = re.compile(r"^(?=.{1,240}$)(?![-.])(?!.*(?:\.\.|//|@\{))(?!.*[./]$)(?!.*[ ~^:?*\\\[\]])[A-Za-z0-9_][A-Za-z0-9._/@-]*$")
LEGACY_BRANCH_RE = re.compile(r"^pi/[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8,64}$")


class RootSessionError(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _safe_id(value: str, label: str = "conversation id") -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise RootSessionError(f"invalid {label}")
    return value


def _safe_profile(value: str) -> str:
    if not isinstance(value, str) or not PROFILE_RE.fullmatch(value):
        raise RootSessionError("invalid root profile")
    return value


def _lstat(path: Path, *, directory: bool | None = None, missing: bool = False, secure: bool = True) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise RootSessionError(f"missing root-session state: {path}")
    if stat.S_ISLNK(info.st_mode):
        raise RootSessionError(f"symlink is not allowed in root-session state: {path}")
    if directory is True and not stat.S_ISDIR(info.st_mode):
        raise RootSessionError(f"not a directory: {path}")
    if directory is False and not stat.S_ISREG(info.st_mode):
        raise RootSessionError(f"not a regular file: {path}")
    if secure and info.st_uid != os.getuid():
        raise RootSessionError(f"root-session state is not user-owned: {path}")
    return info


def ensure_dir(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = _lstat(current, directory=True, missing=True, secure=False)
        if info is None:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            os.chmod(current, 0o700)
        elif info.st_uid == os.getuid():
            os.chmod(current, 0o700)
    _lstat(path, directory=True)
    return path


def atomic_write(path: Path, data: str, mode: int = 0o600) -> None:
    parent = ensure_dir(path.parent)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(parent))
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        _lstat(path, directory=False)
        directory_fd = os.open(str(parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _agent_dir(value: str | None = None) -> Path:
    raw = value or os.environ.get("PI_CODING_AGENT_DIR") or "~/.pi/agent"
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        raise RootSessionError("PI_CODING_AGENT_DIR must be absolute")
    return ensure_dir(path)


def paths(agent_dir: Path) -> tuple[Path, Path, Path, Path]:
    sessions = ensure_dir(agent_dir / "sessions")
    root = ensure_dir(sessions / "root")
    archive = ensure_dir(root / "archive")
    return root, archive, agent_dir / "root-registry.json", agent_dir / "root-registry.lock"


@contextlib.contextmanager
def registry_lock(path: Path):
    ensure_dir(path.parent)
    if path.exists() or path.is_symlink():
        _lstat(path, directory=False)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load_registry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    _lstat(path, directory=False)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RootSessionError(f"malformed root-session registry: {path}") from error
    if not isinstance(value, dict) or value.get("schemaVersion") != SCHEMA_VERSION or not isinstance(value.get("records"), list):
        raise RootSessionError("malformed root-session registry")
    records: list[dict[str, Any]] = []
    for record in value["records"]:
        if not isinstance(record, dict):
            raise RootSessionError("malformed root-session registry record")
        required = {"conversationId", "profile", "sessionFile", "sessionDir", "worktree", "status", "createdAt", "updatedAt"}
        if not required.issubset(record):
            raise RootSessionError("incomplete root-session registry record")
        _safe_id(record["conversationId"])
        _safe_profile(record["profile"])
        if record["status"] not in {"active", "archived"}:
            raise RootSessionError("invalid root-session status")
        for key in ("sessionFile", "sessionDir", "worktree"):
            if not isinstance(record[key], str) or not Path(record[key]).is_absolute():
                raise RootSessionError(f"invalid root-session {key}")
        root_dir = (path.parent / "sessions" / "root").resolve(strict=False)
        session_file = Path(record["sessionFile"]).resolve(strict=False)
        session_dir = Path(record["sessionDir"]).resolve(strict=False)
        if session_dir != root_dir or not (is_within(root_dir, session_file) and session_file.suffix == ".jsonl"):
            raise RootSessionError("root-session session path escapes the flat root directory")
        if session_file.exists() or session_file.is_symlink():
            _lstat(session_file, directory=False)
        if record.get("repository") is not None and (
            not isinstance(record["repository"], str) or not Path(record["repository"]).is_absolute()
        ):
            raise RootSessionError("invalid root-session repository")
        if record.get("branch") is not None and (
            not isinstance(record["branch"], str) or not GIT_BRANCH_RE.fullmatch(record["branch"])
        ):
            raise RootSessionError("invalid root-session branch")
        records.append(record)
    return records


def save_registry(path: Path, records: Iterable[dict[str, Any]]) -> None:
    ordered = sorted(records, key=lambda item: (item.get("status") != "active", item["conversationId"]))
    atomic_write(path, json.dumps({"schemaVersion": SCHEMA_VERSION, "records": ordered}, indent=2, sort_keys=True) + "\n")


def git(cwd: Path, *args: str, check: bool = True) -> str:
    environment = os.environ.copy()
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat"})
    result = subprocess.run(["git", *args], cwd=str(cwd), env=environment, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if check and result.returncode:
        detail = (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")[:300]
        raise RootSessionError(f"git {args[0] if args else 'command'} failed: {detail}")
    return result.stdout.strip()


def canonical_repository(path: Path) -> Path | None:
    try:
        value = git(path, "rev-parse", "--path-format=absolute", "--show-toplevel")
    except RootSessionError:
        return None
    candidate = Path(value)
    try:
        return candidate.resolve(strict=True)
    except OSError:
        return None


def is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def worktree_root_from_policy(agent_dir: Path, override: str | None = None) -> Path:
    raw = override or os.environ.get("PI_ROOT_WORKTREE_ROOT")
    if not raw:
        policy_path = Path(os.path.expanduser(os.environ.get("PI_ROOT_POLICY", "~/.config/pi/repository-policy.json")))
        if policy_path.is_file() and not policy_path.is_symlink():
            try:
                policy = json.loads(policy_path.read_text(encoding="utf-8"))
                raw = policy.get("worktreeRoot") if isinstance(policy, dict) else None
            except (OSError, UnicodeError, json.JSONDecodeError):
                raw = None
    raw = raw or "~/.local/share/pi/worktrees"
    root = Path(os.path.expanduser(str(raw)))
    if not root.is_absolute():
        raise RootSessionError("root worktree root must be absolute")
    root = ensure_dir(root)
    if root == agent_dir or is_within(agent_dir, root):
        raise RootSessionError("root worktree root must not be inside the Pi agent directory")
    return root


def repository_identity(repository: Path) -> tuple[str, Path, str]:
    common = Path(git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
    object_format = git(repository, "rev-parse", "--show-object-format")
    identity = hashlib.sha256(f"{common}\0{object_format}".encode()).hexdigest()
    return identity, common, object_format


def primary_repository(repository: Path) -> Path:
    """Return the main worktree path so records survive pruning old linked worktrees."""
    try:
        entries = worktree_entries(repository)
        if entries:
            candidate = entries[0][0]
            if candidate.is_dir() and canonical_repository(candidate) is not None:
                return candidate.resolve(strict=True)
    except RootSessionError:
        pass
    return repository.resolve(strict=True)


def current_repository_state(repository: Path) -> tuple[str, str, str]:
    oid = git(repository, "rev-parse", "--verify", "HEAD^{commit}")
    branch = git(repository, "branch", "--show-current")
    status = git(repository, "status", "--porcelain=v1", "--untracked-files=all")
    return oid, branch, status


def safe_component(value: str, fallback: str = "repo") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return cleaned[:64] or fallback


def derive_branch(conversation_id: str, profile: str, repository_id: str) -> str:
    slug = safe_component(f"{profile}-{conversation_id}", "root")[:48]
    suffix = hashlib.sha256(f"{repository_id}\0{profile}\0{conversation_id}".encode()).hexdigest()[:16]
    branch = f"pi/root-{slug}-{suffix}"
    if not BRANCH_RE.fullmatch(branch):
        raise RootSessionError("derived root branch is invalid")
    return branch


def worktree_entries(repository: Path) -> list[tuple[Path, str | None]]:
    output = git(repository, "worktree", "list", "--porcelain")
    result: list[tuple[Path, str | None]] = []
    current_path: Path | None = None
    current_branch: str | None = None
    for line in output.splitlines() + [""]:
        if line.startswith("worktree "):
            if current_path is not None:
                result.append((current_path, current_branch))
            current_path = Path(line[9:]).resolve(strict=False)
            current_branch = None
        elif line.startswith("branch ") and current_path is not None:
            current_branch = line[7:].removeprefix("refs/heads/")
        elif not line and current_path is not None:
            result.append((current_path, current_branch))
            current_path = None
            current_branch = None
    return result


def stale_worktree_admin_entries(repository: Path, stale_entries: list[dict[str, Any]], managed_root: Path) -> list[dict[str, str]]:
    """Find only prunable Git admin dirs whose worktree is managed by Pi.

    `git worktree prune` has no path filter and would remove stale metadata for
    unrelated user worktrees. Resolve Git's `gitdir` marker ourselves and
    remove only an admin directory whose corresponding path is both selected
    as a stale legacy root and inside the configured managed root.
    """
    if not stale_entries:
        return []
    common = Path(git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
    admin_root = common / "worktrees"
    if not admin_root.exists():
        return []
    _lstat(admin_root, directory=True)
    stale_by_path = {Path(item["path"]).resolve(strict=False): item for item in stale_entries}
    result: list[dict[str, str]] = []
    try:
        children = list(admin_root.iterdir())
    except OSError as error:
        raise RootSessionError(f"cannot inspect Git worktree metadata: {admin_root}") from error
    for admin in children:
        if not admin.is_dir():
            continue
        _lstat(admin, directory=True)
        marker = admin / "gitdir"
        if not marker.exists():
            continue
        _lstat(marker, directory=False)
        try:
            raw = marker.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise RootSessionError(f"cannot read Git worktree metadata: {marker}") from error
        if not raw:
            continue
        target = Path(raw)
        if not target.is_absolute():
            target = admin / target
        target = target.resolve(strict=False)
        if target.name != ".git":
            continue
        worktree = target.parent
        item = stale_by_path.get(worktree)
        if item is None or not is_within(managed_root, worktree) or target.exists():
            continue
        result.append({"path": str(worktree), "branch": str(item.get("branch") or ""), "admin": str(admin)})
    return result


def branch_exists(repository: Path, branch: str) -> bool:
    return subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=str(repository),
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def ensure_worktree(repository: Path, conversation_id: str, profile: str, root_worktrees: Path,
                    existing: dict[str, Any] | None = None) -> tuple[Path, str, str, str]:
    repository = repository.resolve(strict=True)
    repository_id, common, object_format = repository_identity(repository)
    if root_worktrees == repository or is_within(repository, root_worktrees):
        raise RootSessionError("root worktree root must not be inside the repository")
    if existing:
        worktree = Path(existing["worktree"]).resolve(strict=False)
        branch = existing.get("branch") or ""
        if not worktree.exists():
            if not branch or not BRANCH_RE.fullmatch(branch) or not branch_exists(repository, branch):
                raise RootSessionError(f"root worktree is missing and its branch cannot be recovered: {worktree}")
            ensure_dir(worktree.parent)
            git(repository, "worktree", "add", str(worktree), branch)
        actual_id, _actual_common, _actual_format = repository_identity(worktree)
        if actual_id != repository_id:
            raise RootSessionError(f"root worktree repository changed: {worktree}")
        actual_branch = git(worktree, "branch", "--show-current")
        if branch and actual_branch != branch:
            raise RootSessionError(f"root worktree branch changed: {worktree}")
        return worktree, actual_branch, repository_id, object_format

    oid, branch, status = current_repository_state(repository)
    if status:
        # Never stash, reset, copy, or discard an existing checkout. A dirty
        # checkout is already the user's durable workspace; record it exactly
        # rather than manufacturing a clean worktree that silently omits the
        # user's uncommitted work. Clean protected checkouts still receive a
        # private linked worktree below.
        return repository, branch, repository_id, object_format
    branch = derive_branch(conversation_id, profile, repository_id)
    destination = root_worktrees / safe_component(repository.name) / "root" / repository_id[:16] / safe_component(conversation_id)
    if destination.exists() or destination.is_symlink():
        raise RootSessionError(f"root worktree destination already exists: {destination}")
    registered = {path: registered_branch for path, registered_branch in worktree_entries(repository)}
    if branch_exists(repository, branch):
        raise RootSessionError(f"derived root branch already exists outside the registry: {branch}")
    ensure_dir(destination.parent)
    try:
        git(repository, "worktree", "add", "-b", branch, str(destination), oid)
    except Exception:
        with contextlib.suppress(OSError):
            destination.rmdir()
        raise
    actual_id, _actual_common, _actual_format = repository_identity(destination)
    if actual_id != repository_id or git(destination, "branch", "--show-current") != branch:
        raise RootSessionError("created root worktree failed identity verification")
    if Path(destination).resolve() in registered:
        raise RootSessionError("created root worktree was already registered")
    return destination.resolve(strict=True), branch, repository_id, object_format


def session_header(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    _lstat(path, directory=False)
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                value = json.loads(line)
                return value if isinstance(value, dict) and value.get("type") == "session" else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return None


def new_header(conversation_id: str, worktree: Path, *, parent_session: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": "session", "version": SESSION_VERSION, "id": conversation_id,
        "timestamp": now_iso(), "cwd": str(worktree),
    }
    if parent_session is not None:
        value["parentSession"] = parent_session
    return value


def ensure_session_file(path: Path, conversation_id: str, worktree: Path) -> None:
    header = session_header(path)
    if header is not None:
        if header.get("id") != conversation_id:
            raise RootSessionError(f"root session id does not match registry: {path}")
        return
    if path.exists() and path.stat().st_size:
        raise RootSessionError(f"root session is not a valid Pi session: {path}")
    atomic_write(path, json.dumps(new_header(conversation_id, worktree), ensure_ascii=False, separators=(",", ":")) + "\n")


def record_for(records: list[dict[str, Any]], conversation_id: str) -> dict[str, Any] | None:
    return next((record for record in records if record["conversationId"] == conversation_id), None)


def managed_profile_for_id(conversation_id: str) -> str | None:
    """Return the launcher-owned profile implied by a legacy stable ID."""
    if conversation_id.startswith("personal-"):
        return "personal"
    if conversation_id.startswith("sec-") or conversation_id.startswith("secretary-"):
        return "secretary"
    return None


def context_for(cwd: Path, repository_arg: str | None) -> tuple[Path | None, Path]:
    try:
        supplied = Path(os.path.expanduser(repository_arg)).resolve(strict=True) if repository_arg else cwd.resolve(strict=True)
    except OSError as error:
        raise RootSessionError("root cwd or repository is missing") from error
    repository = canonical_repository(supplied)
    return repository, supplied


def ensure_record(agent_dir: Path, conversation_id: str, profile: str, cwd: Path,
                  repository_arg: str | None = None, worktree_override: str | None = None) -> dict[str, Any]:
    conversation_id = _safe_id(conversation_id)
    profile = _safe_profile(profile)
    root, _archive, registry_path, lock_path = paths(agent_dir)
    with registry_lock(lock_path):
        records = load_registry(registry_path)
        existing = record_for(records, conversation_id)
        if existing is not None:
            if existing["profile"] != profile:
                # Early durable-registry generations could register a managed
                # personal/secretary session as generic `root` before the
                # launcher profile reached the lifecycle extension. Repair
                # only that one-way legacy case, with both the reserved stable
                # ID prefix and exact repository identity as proof. All other
                # profile changes remain forbidden.
                implied_profile = managed_profile_for_id(conversation_id)
                if existing["profile"] != "root" or profile != implied_profile:
                    raise RootSessionError(f"conversation id already belongs to profile {existing['profile']}")
                try:
                    stored_repository = Path(existing["repository"]).resolve(strict=True) if existing.get("repository") else None
                    requested_path = Path(os.path.expanduser(repository_arg)).resolve(strict=True) if repository_arg else cwd.resolve(strict=True)
                    requested_repository = canonical_repository(requested_path)
                except OSError as error:
                    raise RootSessionError("root registry repository is missing") from error
                if (stored_repository is None or requested_repository is None or
                        repository_identity(stored_repository)[0] != repository_identity(requested_repository)[0]):
                    raise RootSessionError("legacy managed profile repair requires the exact registered repository")
                existing["profile"] = profile
            try:
                repository = Path(existing["repository"]).resolve(strict=True) if existing.get("repository") else None
            except OSError as error:
                raise RootSessionError("root registry repository is missing") from error
            if repository_arg and repository is not None:
                requested = canonical_repository(Path(os.path.expanduser(repository_arg)).resolve(strict=True))
                if requested is None or repository_identity(requested)[0] != repository_identity(repository)[0]:
                    raise RootSessionError("conversation id belongs to a different repository")
            if repository is not None:
                worktree, branch, repository_id, object_format = ensure_worktree(
                    repository, conversation_id, profile, worktree_root_from_policy(agent_dir, worktree_override), existing)
                existing.update({"worktree": str(worktree), "branch": branch, "repositoryId": repository_id, "objectFormat": object_format})
            session_path = Path(existing["sessionFile"]).resolve()
            ensure_session_file(session_path, conversation_id, Path(existing["worktree"]))
            existing["status"] = "active"
            existing["updatedAt"] = now_iso()
            save_registry(registry_path, records)
            return dict(existing, sessionDir=str(root))

        repository, supplied = context_for(cwd, repository_arg)
        stored_repository = primary_repository(repository) if repository is not None else None
        branch = ""
        repository_id = ""
        object_format = ""
        if repository is not None:
            worktree, branch, repository_id, object_format = ensure_worktree(
                repository, conversation_id, profile, worktree_root_from_policy(agent_dir, worktree_override))
        else:
            worktree = supplied
            if not worktree.is_dir():
                raise RootSessionError("non-Git root cwd is not a directory")
        session_path = root / f"{conversation_id}.jsonl"
        if session_path.exists() or session_path.is_symlink():
            raise RootSessionError(f"root session path already exists outside the registry: {session_path}")
        created = now_iso()
        record: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION, "conversationId": conversation_id, "profile": profile,
            "sessionFile": str(session_path), "sessionDir": str(root), "repository": str(stored_repository) if stored_repository else None,
            "repositoryId": repository_id or None, "objectFormat": object_format or None,
            "worktree": str(worktree.resolve()), "branch": branch or None,
            "status": "active", "createdAt": created, "updatedAt": created,
        }
        ensure_session_file(session_path, conversation_id, worktree.resolve())
        records.append(record)
        try:
            save_registry(registry_path, records)
        except Exception:
            with contextlib.suppress(OSError):
                session_path.unlink()
            raise
        return dict(record, sessionDir=str(root))


def lookup_latest(agent_dir: Path, cwd: Path) -> dict[str, Any] | None:
    root, _archive, registry_path, _lock = paths(agent_dir)
    records = load_registry(registry_path)
    repository = canonical_repository(cwd.resolve(strict=True))
    repository_id = repository_identity(repository)[0] if repository is not None else None
    candidates = [record for record in records if record.get("status") == "active"]
    if repository_id is not None:
        candidates = [record for record in candidates if record.get("repositoryId") == repository_id]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.get("updatedAt", ""), reverse=True)
    result = dict(candidates[0])
    result["sessionDir"] = str(root)
    return result


def decode_source_session(path: Path) -> tuple[dict[str, Any], list[str]] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict) and value.get("type") == "session" and isinstance(value.get("id"), str):
                return value, lines
            return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return None


def rewrite_session(path: Path, destination: Path, conversation_id: str, worktree: Path) -> None:
    decoded = decode_source_session(path)
    if decoded is None:
        raise RootSessionError(f"cannot migrate invalid session: {path}")
    header, lines = decoded
    header = dict(header)
    header.update({"type": "session", "version": SESSION_VERSION, "id": conversation_id,
                   "cwd": str(worktree), "parentSession": str(path.resolve())})
    first = next(index for index, line in enumerate(lines) if line.strip())
    lines[first] = json.dumps(header, ensure_ascii=False, separators=(",", ":"))
    atomic_write(destination, "\n".join(lines) + "\n")


def infer_profile(source: Path, override: str | None) -> str:
    if override:
        return _safe_profile(override)
    parts = set(source.parts)
    if "secretary" in parts:
        return "secretary"
    if "tmux" in parts:
        return "personal"
    return "root"


def machine_value(name: str) -> str | None:
    path = Path(os.path.expanduser(os.environ.get("DOTFILES_MACHINE_CONFIG", "~/.config/dotfiles/machine.env")))
    if not path.is_file() or path.is_symlink():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                return os.path.expandvars(value.strip().strip('"').strip("'"))
    except (OSError, UnicodeError):
        return None
    return None


def inferred_repository(header: dict[str, Any], source: Path, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    cwd_value = header.get("cwd")
    if isinstance(cwd_value, str) and Path(cwd_value).is_dir() and canonical_repository(Path(cwd_value)) is not None:
        return cwd_value
    # Managed personal panes have stable logical labels even when their old
    # generated worktree has already been removed.
    stem = header.get("id", "")
    if isinstance(stem, str) and stem.startswith("personal-"):
        suffix = stem.removeprefix("personal-")
        env_name = {"mlre-transition": "PI_PERSONAL_MLRE_DIR", "financials": "PI_PERSONAL_FINANCIALS_DIR",
                    "dotfiles": "PI_PERSONAL_DOTFILES_DIR"}.get(suffix)
        if env_name and (os.environ.get(env_name) or machine_value(env_name)):
            return os.environ.get(env_name) or machine_value(env_name)
        if suffix == "dotfiles" and (Path.home() / "dotfiles").is_dir():
            return str(Path.home() / "dotfiles")
    # Secretary project records retain the canonical repository independently
    # from the old nested session directory.
    try:
        if "secretary" in source.parts:
            project_id = next(part for index, part in enumerate(source.parts) if part == "secretary" and index + 1 < len(source.parts) for part in [source.parts[index + 1]] if re.fullmatch(r"[0-9a-f]{64}", part))
            state_root = Path(os.path.expanduser(os.environ.get("XDG_STATE_HOME", "~/.local/state"))) / "pi-secretary" / "registry"
            record_path = state_root / f"{project_id}.json"
            if record_path.is_file():
                value = json.loads(record_path.read_text(encoding="utf-8"))
                if isinstance(value, dict) and isinstance(value.get("primaryRepository"), str):
                    return value["primaryRepository"]
    except (StopIteration, OSError, UnicodeError, json.JSONDecodeError):
        pass
    return None


def discover_legacy_sessions(agent_dir: Path, source_root: Path) -> list[Path]:
    sessions = agent_dir / "sessions" if source_root == Path("__default__") else source_root
    if not sessions.exists():
        return []
    found: list[Path] = []
    for path in sessions.rglob("*.jsonl"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(sessions)
        if not relative.parts:
            continue
        first = relative.parts[0]
        if first in {"root", "subagent", "workflow-artifacts"} or "subagent-artifacts" in relative.parts or "archive" in relative.parts:
            continue
        if first not in {"tmux", "secretary"} and not first.startswith("--"):
            continue
        # Legacy roots were exactly one file below their namespace. Anything
        # deeper is a child run/artifact and must remain private.
        expected_depth = 3 if first in {"tmux", "secretary"} else 2
        if len(relative.parts) != expected_depth:
            continue
        if decode_source_session(path) is not None:
            found.append(path)
    return sorted(found, key=lambda item: item.stat().st_mtime, reverse=True)


def migrate(agent_dir: Path, source_root_arg: str | None, profile: str | None, repository: str | None,
            dry_run: bool) -> list[dict[str, Any]]:
    source_root = Path("__default__") if source_root_arg is None else Path(os.path.expanduser(source_root_arg)).resolve(strict=True)
    candidates = discover_legacy_sessions(agent_dir, source_root)
    root, archive, registry_path, lock_path = paths(agent_dir)
    plans: list[dict[str, Any]] = []
    with registry_lock(lock_path):
        records = load_registry(registry_path)
        groups: dict[tuple[str, str], list[Path]] = {}
        metadata: dict[Path, tuple[dict[str, Any], str | None, str]] = {}
        for source in candidates:
            decoded = decode_source_session(source)
            assert decoded is not None
            header, _ = decoded
            raw_id = header["id"]
            try:
                _safe_id(raw_id)
                conversation_id = raw_id
            except RootSessionError:
                conversation_id = f"legacy-{hashlib.sha256(str(source).encode()).hexdigest()[:24]}"
            repo_arg = inferred_repository(header, source, repository)
            repo_key = ""
            if repo_arg:
                repo = canonical_repository(Path(os.path.expanduser(repo_arg)).resolve(strict=True))
                repo_key = str(repo) if repo else repo_arg
            key = (conversation_id, repo_key)
            groups.setdefault(key, []).append(source)
            metadata[source] = (header, repo_arg, infer_profile(source, profile))
        for (base_id, _repo_key), sources in groups.items():
            sources.sort(key=lambda item: item.stat().st_mtime, reverse=True)
            for index, source in enumerate(sources):
                header, repo_arg, source_profile = metadata[source]
                # The newest copy is the selected survivor. Other copies are
                # retained as independently archived conversations.
                active_candidate = index == 0
                conversation_id = base_id if active_candidate else f"archived-{base_id}-{hashlib.sha256(str(source).encode()).hexdigest()[:16]}"
                existing = record_for(records, conversation_id)
                if existing is not None:
                    active_candidate = False
                    conversation_id = f"archived-{base_id}-{hashlib.sha256((str(source) + conversation_id).encode()).hexdigest()[:16]}"
                plan = {"source": str(source), "conversationId": conversation_id,
                        "profile": source_profile, "status": "active" if active_candidate else "archived",
                        "repository": repo_arg}
                plans.append(plan)
                if dry_run:
                    continue
                if not repo_arg:
                    raise RootSessionError(f"cannot infer repository for legacy session; pass --repository: {source}")
                repo = canonical_repository(Path(os.path.expanduser(repo_arg)).resolve(strict=True))
                if repo is None:
                    raise RootSessionError(f"legacy session cwd is not a Git repository: {repo_arg}")
                stored_repo = primary_repository(repo)
                # Allocate a stable workspace without creating a competing
                # session header, then copy the selected history into its exact
                # destination. The source is never removed.
                worktree, branch, repo_id, object_format = ensure_worktree(
                    repo, conversation_id, source_profile, worktree_root_from_policy(agent_dir))
                destination = root / f"{conversation_id}.jsonl" if active_candidate else archive / f"{conversation_id}.jsonl"
                if destination.exists():
                    raise RootSessionError(f"migration destination already exists: {destination}")
                rewrite_session(source, destination, conversation_id, worktree)
                created = now_iso()
                records.append({
                    "schemaVersion": SCHEMA_VERSION, "conversationId": conversation_id, "profile": source_profile,
                    "sessionFile": str(destination), "sessionDir": str(root), "repository": str(stored_repo),
                    "repositoryId": repo_id, "objectFormat": object_format, "worktree": str(worktree),
                    "branch": branch, "status": "active" if active_candidate else "archived",
                    "createdAt": created, "updatedAt": created, "legacySource": str(source),
                })
        if not dry_run:
            save_registry(registry_path, records)
    return plans


def register_existing(agent_dir: Path, session_file_arg: str, profile: str, worktree_arg: str,
                      conversation_id_arg: str | None = None) -> dict[str, Any]:
    profile = _safe_profile(profile)
    root, _archive, registry_path, lock_path = paths(agent_dir)
    session_file = Path(session_file_arg).expanduser().resolve(strict=False)
    worktree = Path(worktree_arg).expanduser().resolve(strict=True)
    _lstat(session_file.parent, directory=True)
    if session_file.parent != root or session_file.suffix != ".jsonl":
        raise RootSessionError("session-event registration accepts only a direct root JSONL")
    header = session_header(session_file)
    if header is None:
        if session_file.exists() and session_file.stat().st_size:
            raise RootSessionError("session-event registration requires a valid Pi session header")
        conversation_id = _safe_id(conversation_id_arg or "")
    else:
        if not isinstance(header.get("id"), str):
            raise RootSessionError("session-event registration requires a valid Pi session header")
        conversation_id = _safe_id(header["id"])
    repository = canonical_repository(worktree)
    repository_id = object_format = ""
    branch = ""
    if repository is not None:
        repository_id, _common, object_format = repository_identity(worktree)
        branch = git(worktree, "branch", "--show-current")
        repository = primary_repository(repository)
    with registry_lock(lock_path):
        records = load_registry(registry_path)
        existing = record_for(records, conversation_id)
        created = existing.get("createdAt", now_iso()) if existing else now_iso()
        record = existing or {
            "schemaVersion": SCHEMA_VERSION, "conversationId": conversation_id, "profile": profile,
            "sessionFile": str(session_file), "sessionDir": str(root), "repository": str(repository) if repository else None,
            "repositoryId": repository_id or None, "objectFormat": object_format or None,
            "worktree": str(worktree), "branch": branch or None, "status": "active",
            "createdAt": created, "updatedAt": created,
        }
        if existing is not None:
            if existing["sessionFile"] != str(session_file):
                raise RootSessionError("root conversation id is already registered to another session file")
            if existing["profile"] != profile:
                raise RootSessionError(f"root conversation already belongs to profile {existing['profile']}")
            record.update({"worktree": str(worktree), "repository": str(repository) if repository else None,
                           "repositoryId": repository_id or None, "objectFormat": object_format or None,
                           "branch": branch or None, "status": "active", "updatedAt": now_iso()})
        if existing is None:
            records.append(record)
        save_registry(registry_path, records)
        return dict(record, sessionDir=str(root))


def archive_record(agent_dir: Path, conversation_id: str) -> dict[str, Any]:
    _safe_id(conversation_id)
    root, archive, registry_path, lock_path = paths(agent_dir)
    with registry_lock(lock_path):
        records = load_registry(registry_path)
        record = record_for(records, conversation_id)
        if record is None:
            raise RootSessionError("unknown root conversation")
        if record["status"] == "archived":
            return record
        source = Path(record["sessionFile"])
        _lstat(source, directory=False)
        destination = archive / source.name
        if destination.exists():
            destination = archive / f"{conversation_id}-{secrets.token_hex(6)}.jsonl"
        os.replace(source, destination)
        record["sessionFile"] = str(destination)
        record["status"] = "archived"
        record["updatedAt"] = now_iso()
        save_registry(registry_path, records)
        return dict(record, sessionDir=str(root))


def cleanup_git(agent_dir: Path, repository_arg: str, apply: bool) -> dict[str, Any]:
    repository = canonical_repository(Path(os.path.expanduser(repository_arg)).resolve(strict=True))
    if repository is None:
        raise RootSessionError("cleanup target is not a Git repository")
    root, _archive, registry_path, _lock = paths(agent_dir)
    managed_worktree_root = worktree_root_from_policy(agent_dir)
    records = load_registry(registry_path)
    protected_branches = {record.get("branch") for record in records if record.get("status") == "active"}
    protected_paths = {str(Path(record["worktree"]).resolve()) for record in records if record.get("status") == "active"}
    entries = worktree_entries(repository)
    stale_entries = [
        {"path": str(path), "branch": branch}
        for path, branch in entries
        if branch and LEGACY_BRANCH_RE.fullmatch(branch) and is_within(managed_worktree_root, path) and str(path) not in protected_paths and branch not in protected_branches
    ]
    branches = [line.strip() for line in git(repository, "for-each-ref", "--format=%(refname:short)", "refs/heads/pi").splitlines() if line.strip()]
    obsolete = []
    head = git(repository, "rev-parse", "HEAD^{commit}")
    registered_branches = {branch for _path, branch in entries if branch}
    stale_branches = {item["branch"] for item in stale_entries if item.get("branch")}
    for branch in branches:
        if (LEGACY_BRANCH_RE.fullmatch(branch) and branch in stale_branches and branch not in protected_branches and
                (branch not in registered_branches or branch in stale_branches) and
                git(repository, "rev-parse", f"{branch}^{{commit}}", check=False) == head):
            obsolete.append(branch)
    prunable = stale_worktree_admin_entries(repository, stale_entries, managed_worktree_root)
    result = {"repository": str(repository), "staleWorktrees": stale_entries,
              "prunableWorktrees": prunable, "obsoleteBranches": obsolete, "applied": False}
    if apply:
        # Do not call `git worktree prune`: it has no path filter and can
        # delete stale metadata for worktrees outside Pi's managed root.
        for item in prunable:
            admin = Path(item["admin"])
            _lstat(admin, directory=True)
            shutil.rmtree(admin)
        remaining_branches = {branch for _path, branch in worktree_entries(repository) if branch}
        for branch in obsolete:
            if branch not in remaining_branches:
                git(repository, "branch", "-D", branch)
        result["applied"] = True
    return result


def output(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-dir")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ensure")
    p.add_argument("--conversation-id", required=True); p.add_argument("--profile", default="root")
    p.add_argument("--cwd", default="."); p.add_argument("--repository"); p.add_argument("--worktree-root")
    p = sub.add_parser("latest"); p.add_argument("--cwd", default=".")
    sub.add_parser("list")
    sub.add_parser("session-dir")
    p = sub.add_parser("migrate"); p.add_argument("--source-root"); p.add_argument("--profile"); p.add_argument("--repository"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("register-existing"); p.add_argument("--session-file", required=True); p.add_argument("--conversation-id"); p.add_argument("--profile", default="root"); p.add_argument("--worktree", required=True)
    p = sub.add_parser("archive"); p.add_argument("--conversation-id", required=True)
    p = sub.add_parser("cleanup"); p.add_argument("--repository", required=True); p.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        agent_dir = _agent_dir(args.agent_dir)
        if args.command == "ensure":
            result = ensure_record(agent_dir, args.conversation_id, args.profile, Path(args.cwd).resolve(), args.repository, args.worktree_root)
        elif args.command == "latest":
            result = lookup_latest(agent_dir, Path(args.cwd).resolve())
            if result is None:
                return 1
        elif args.command == "list":
            _root, _archive, registry_path, _lock = paths(agent_dir)
            result = load_registry(registry_path)
        elif args.command == "session-dir":
            result = {"sessionDir": str(paths(agent_dir)[0])}
        elif args.command == "migrate":
            result = migrate(agent_dir, args.source_root, args.profile, args.repository, args.dry_run)
        elif args.command == "register-existing":
            result = register_existing(agent_dir, args.session_file, args.profile, args.worktree, args.conversation_id)
        elif args.command == "archive":
            result = archive_record(agent_dir, args.conversation_id)
        elif args.command == "cleanup":
            result = cleanup_git(agent_dir, args.repository, args.apply)
        else:
            raise RootSessionError("unknown command")
        output(result)
        return 0
    except RootSessionError as error:
        print(f"pi root-session: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli(sys.argv[1:]))
