"""Phase 6A immutable Git snapshot capture and reconciliation.

Snapshots deliberately separate source-content authority (Git objects and
refs) from controller lifecycle state.  Capture uses a disposable index, never
the caller's real index; the only durable Git mutation is the namespaced
immutable snapshot ref and its objects.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Mapping, Sequence

from .models import canonical_json, utc_now

SNAPSHOT_SCHEMA_VERSION = 1
_SNAPSHOT_ID = re.compile(r"^snap_[0-9a-f]{32}$")
_MAX_OUTPUT = 512 * 1024
_MAX_FILES = 100_000
_MAX_FILE_BYTES = 256 * 1024 * 1024
_MAX_PATH_BYTES = 4096
_MAX_TOTAL_PATH_BYTES = 4 * 1024 * 1024
_MANIFEST_KEYS = frozenset({
    "schemaVersion", "snapshotId", "repositoryPath", "commonDir", "objectFormat",
    "sourceHeadOid", "sourceTreeOid", "snapshotCommitOid", "snapshotTreeOid",
    "refName", "dirty", "statusHash", "changedPaths", "policy", "createdAt",
    "recovered", "manifestDigest",
})
_POLICY_KEYS = frozenset({
    "includeUntracked", "includeIgnored", "allowSymlinks", "allowSubmodules",
    "maxFiles", "maxFileBytes",
})


class SnapshotError(RuntimeError):
    """Base class for bounded snapshot failures."""


class SnapshotConflictError(SnapshotError):
    """The requested immutable snapshot/ref already exists or raced."""


class SnapshotConcurrentMutationError(SnapshotError):
    """The worktree changed while a snapshot was being captured."""


class SnapshotIntegrityError(SnapshotError):
    """A snapshot manifest and its Git ref disagree."""


@dataclass(frozen=True)
class SnapshotPolicy:
    """Explicit source selection policy for one capture."""

    include_untracked: bool = False
    include_ignored: bool = False
    allow_symlinks: bool = False
    allow_submodules: bool = False
    max_files: int = _MAX_FILES
    max_file_bytes: int = _MAX_FILE_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.max_files, int) or not 1 <= self.max_files <= _MAX_FILES:
            raise ValueError("snapshot max_files is outside its bound")
        if not isinstance(self.max_file_bytes, int) or not 1 <= self.max_file_bytes <= _MAX_FILE_BYTES:
            raise ValueError("snapshot max_file_bytes is outside its bound")

    def as_dict(self) -> dict[str, Any]:
        return {
            "includeUntracked": self.include_untracked,
            "includeIgnored": self.include_ignored,
            "allowSymlinks": self.allow_symlinks,
            "allowSubmodules": self.allow_submodules,
            "maxFiles": self.max_files,
            "maxFileBytes": self.max_file_bytes,
        }


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_id: str
    repository_path: str
    common_dir: str
    object_format: str
    source_head_oid: str | None
    source_tree_oid: str | None
    snapshot_commit_oid: str
    snapshot_tree_oid: str
    ref_name: str
    dirty: bool
    status_hash: str
    changed_paths: tuple[str, ...]
    policy: Mapping[str, Any]
    created_at: str
    manifest_path: str
    manifest_digest: str
    recovered: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
            "snapshotId": self.snapshot_id,
            "repositoryPath": self.repository_path,
            "commonDir": self.common_dir,
            "objectFormat": self.object_format,
            "sourceHeadOid": self.source_head_oid,
            "sourceTreeOid": self.source_tree_oid,
            "snapshotCommitOid": self.snapshot_commit_oid,
            "snapshotTreeOid": self.snapshot_tree_oid,
            "refName": self.ref_name,
            "dirty": self.dirty,
            "statusHash": self.status_hash,
            "changedPaths": list(self.changed_paths),
            "policy": dict(self.policy),
            "createdAt": self.created_at,
            "manifestPath": self.manifest_path,
            "recovered": self.recovered,
            "manifestDigest": self.manifest_digest,
        }


class _NoOpFailpoint:
    def hit(self, _name: str, _context: Mapping[str, str]) -> None:
        return None


def _validate_id(snapshot_id: str) -> str:
    if not isinstance(snapshot_id, str) or _SNAPSHOT_ID.fullmatch(snapshot_id) is None:
        raise ValueError("snapshot_id must be snap_ followed by 32 lowercase hex characters")
    return snapshot_id


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor or os.sep)
    for component in absolute.parts[1:]:
        current /= component
        if current.is_symlink():
            raise SnapshotError(f"snapshot path contains a symlink: {current}")


def _secure_directory(path: Path, *, create: bool = True) -> Path:
    _reject_symlink_components(path)
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise SnapshotError(f"snapshot state path is not a directory: {path}")
    os.chmod(path, 0o700)
    return path.resolve(strict=True)


def _git_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "GIT_EDITOR": "true",
        "GIT_SEQUENCE_EDITOR": "true",
        "GIT_ASKPASS": "true",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    for key, value in (extra or {}).items():
        if key in {"GIT_INDEX_FILE", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"}:
            environment[key] = str(value)
    return environment


def _git(
    repository: Path,
    args: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> str:
    if not args or any("\x00" in str(item) for item in args):
        raise SnapshotError("invalid Git snapshot command")
    command = str(args[0])
    allowed = {"add", "cat-file", "commit-tree", "config", "diff-tree", "ls-files", "read-tree", "rev-parse", "status", "update-ref", "write-tree"}
    if command not in allowed:
        raise SnapshotError(f"Git command is not allowlisted for snapshots: {command}")
    if any(str(item) in {"-c", "--upload-pack", "--exec-path"} or str(item).startswith("--upload-pack=") for item in args[1:]):
        raise SnapshotError("unsafe Git option in snapshot command")
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise SnapshotError("Git executable is unavailable")
    _reject_symlink_components(repository)
    try:
        resolved = repository.resolve(strict=True)
    except OSError as error:
        raise SnapshotError("snapshot repository is unavailable") from error
    result = subprocess.run(
        [executable, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "diff.external=" + (shutil.which("true", path=os.defpath) or "true"), "-c", "core.sshCommand=", "-c", "credential.helper=", *[str(item) for item in args]],
        cwd=str(resolved),
        env=_git_environment(environment),
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL if input_text is None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
        shell=False,
    )
    if len(result.stdout.encode()) > _MAX_OUTPUT or len(result.stderr.encode()) > _MAX_OUTPUT:
        raise SnapshotError("Git snapshot output exceeded its bound")
    if check and result.returncode != 0:
        raise SnapshotError(f"Git snapshot command failed ({command}): {result.stderr.strip()[:512]}")
    return result.stdout


def _optional_git(repository: Path, args: Sequence[str], *, environment: Mapping[str, str] | None = None) -> str | None:
    output = _git(repository, args, environment=environment, check=False)
    # _git does not return stderr, so optional commands use a narrow command
    # where an absent HEAD/ref is represented by an empty stdout and a failed
    # command is retried as a required operation by the caller when needed.
    return output.strip() or None


def _repository_info(repository: Path) -> tuple[str, str, str, str | None, str | None, str]:
    config = _git(repository, ["config", "--local", "--null", "--list"])
    for entry in config.split("\x00"):
        key = entry.split("\n", 1)[0].strip().lower()
        if key.startswith(("filter.", "include.", "includeif.", "credential.", "config.worktree", "core.worktree", "core.fsmonitor", "core.splitindex", "diff.")):
            raise SnapshotError(f"repository Git configuration exposes an execution surface: {key[:128]}")
    top = Path(_git(repository, ["rev-parse", "--show-toplevel"]).strip()).resolve(strict=True)
    common_raw = _git(repository, ["rev-parse", "--git-common-dir"]).strip()
    common_path = Path(common_raw)
    if not common_path.is_absolute():
        common_path = top / common_path
    common = common_path.resolve(strict=True)
    object_format = _git(repository, ["rev-parse", "--show-object-format"]).strip()
    if object_format not in {"sha1", "sha256"}:
        raise SnapshotError("unsupported Git object format")
    head_result = _git(repository, ["rev-parse", "--verify", "HEAD"], check=False)
    head = head_result.strip() or None
    tree_result = _git(repository, ["rev-parse", "--verify", "HEAD^{tree}"], check=False)
    tree = tree_result.strip() or None
    status = _git(repository, ["status", "--porcelain=v2", "--branch", "--untracked-files=all"])
    return str(top), str(common), object_format, head, tree, status


def _hash_bytes(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _filesystem_inventory(repository: Path, policy: SnapshotPolicy) -> tuple[dict[str, Any], ...]:
    entries: list[dict[str, Any]] = []
    total_path_bytes = 0
    stack = [repository]
    while stack:
        directory = stack.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name, reverse=True)
        except OSError as error:
            raise SnapshotError(f"cannot inspect worktree: {directory}") from error
        for item in children:
            if item.name == ".git":
                continue
            path = Path(item.path)
            relative = path.relative_to(repository).as_posix()
            path_bytes = len(os.fsencode(relative))
            if path_bytes > _MAX_PATH_BYTES:
                raise SnapshotError(f"path exceeds snapshot length bound: {relative[:128]}")
            total_path_bytes += path_bytes
            if total_path_bytes > _MAX_TOTAL_PATH_BYTES:
                raise SnapshotError("snapshot path-byte bound exceeded")
            info = item.stat(follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                if not policy.allow_symlinks:
                    raise SnapshotError(f"symlink is not allowed in snapshot: {relative}")
                entries.append({"path": relative, "kind": "symlink", "mode": mode, "target": os.readlink(path)})
            elif stat.S_ISDIR(info.st_mode):
                entries.append({"path": relative, "kind": "directory", "mode": mode})
                stack.append(path)
            elif stat.S_ISREG(info.st_mode):
                if info.st_size > policy.max_file_bytes:
                    raise SnapshotError(f"file exceeds snapshot bound: {relative}")
                entries.append({"path": relative, "kind": "file", "mode": mode, "size": info.st_size, "sha256": _hash_bytes(path)})
            else:
                raise SnapshotError(f"special file is not allowed in snapshot: {relative}")
            if len(entries) > policy.max_files:
                raise SnapshotError("snapshot file bound exceeded")
    return tuple(sorted(entries, key=lambda entry: entry["path"]))


def _status_hash(status: str) -> str:
    return hashlib.sha256(status.encode("utf-8")).hexdigest()


def _real_index_digest(repository: Path) -> str | None:
    git_dir_raw = _git(repository, ["rev-parse", "--git-dir"]).strip()
    git_dir = Path(git_dir_raw)
    if not git_dir.is_absolute():
        git_dir = repository / git_dir
    _reject_symlink_components(git_dir)
    index = git_dir.resolve(strict=True) / "index"
    if not index.exists():
        return None
    if index.is_symlink() or not index.is_file():
        raise SnapshotError("repository index is not a regular file")
    return _hash_bytes(index)


def _dirty(status: str) -> bool:
    return any(line and not line.startswith("# branch.") for line in status.splitlines())


def _manifest_digest(value: Mapping[str, Any]) -> str:
    body = dict(value)
    body.pop("manifestDigest", None)
    return "sha256:" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def _write_manifest(path: Path, value: Mapping[str, Any], *, failpoint: Any) -> str:
    checked = dict(value)
    checked["manifestDigest"] = _manifest_digest(checked)
    failpoint.hit("manifest.write.before", {"snapshot_id": str(checked["snapshotId"])})
    _secure_directory(path.parent)
    if path.exists() or path.is_symlink():
        raise SnapshotConflictError("snapshot manifest already exists")
    payload = canonical_json(checked).encode("utf-8")
    temporary = path.parent / (".manifest-" + next(tempfile._get_candidate_names()))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    failpoint.hit("manifest.write.after", {"snapshot_id": str(checked["snapshotId"])})
    return checked["manifestDigest"]


def _read_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SnapshotIntegrityError("snapshot manifest is missing or symlinked")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SnapshotIntegrityError("snapshot manifest is unreadable") from error
    if not isinstance(value, dict) or set(value) != _MANIFEST_KEYS or value.get("schemaVersion") != SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotIntegrityError("snapshot manifest schema is not exact")
    if not isinstance(value.get("snapshotId"), str) or _SNAPSHOT_ID.fullmatch(value["snapshotId"]) is None:
        raise SnapshotIntegrityError("snapshot manifest ID is invalid")
    policy = value.get("policy")
    if not isinstance(policy, dict) or set(policy) not in (_POLICY_KEYS, {"recovered"}):
        raise SnapshotIntegrityError("snapshot manifest policy is not exact")
    if not isinstance(value.get("changedPaths"), list) or any(not isinstance(item, str) for item in value["changedPaths"]):
        raise SnapshotIntegrityError("snapshot manifest changed paths are invalid")
    if value.get("manifestDigest") != _manifest_digest(value):
        raise SnapshotIntegrityError("snapshot manifest digest mismatch")
    return value


def _changed_paths(repository: Path, commit: str) -> tuple[str, ...]:
    output = _git(repository, ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", commit])
    return tuple(sorted(item for item in output.split("\x00") if item))


def _validate_submodule_repository(nested: Path, expected_head: str) -> None:
    config = _git(nested, ["config", "--local", "--null", "--list"])
    for entry in config.split("\x00"):
        key = entry.split("\n", 1)[0].strip().lower()
        if key.startswith(("filter.", "include.", "includeif.", "credential.", "config.worktree", "core.fsmonitor", "core.splitindex", "diff.")):
            raise SnapshotError(f"submodule Git configuration exposes an execution surface: {key[:128]}")
    nested_head = _git(nested, ["rev-parse", "--verify", "HEAD"]).strip()
    if nested_head != expected_head:
        raise SnapshotError("submodule HEAD does not match the recorded gitlink")
    nested_status = _git(nested, ["status", "--porcelain=v2", "--branch", "--untracked-files=all"])
    if _dirty(nested_status):
        raise SnapshotError("dirty submodule cannot be captured exactly")


def _validate_submodules(repository: Path, index_entries: str, *, allow: bool) -> None:
    for raw_entry in (item for item in index_entries.split("\x00") if item):
        metadata, separator, raw_path = raw_entry.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) < 3 or fields[0] != "160000":
            continue
        if not allow:
            raise SnapshotError("submodule entries require explicit snapshot policy")
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise SnapshotError("submodule path escapes repository")
        nested = repository / relative
        _reject_symlink_components(nested)
        if not nested.is_dir():
            raise SnapshotError("submodule is not initialized")
        _validate_submodule_repository(nested, fields[1])


def _record_from_manifest(value: Mapping[str, Any], *, manifest_path: Path) -> SnapshotRecord:
    return SnapshotRecord(
        snapshot_id=str(value["snapshotId"]),
        repository_path=str(value["repositoryPath"]),
        common_dir=str(value["commonDir"]),
        object_format=str(value["objectFormat"]),
        source_head_oid=value.get("sourceHeadOid"),
        source_tree_oid=value.get("sourceTreeOid"),
        snapshot_commit_oid=str(value["snapshotCommitOid"]),
        snapshot_tree_oid=str(value["snapshotTreeOid"]),
        ref_name=str(value["refName"]),
        dirty=bool(value.get("dirty")),
        status_hash=str(value["statusHash"]),
        changed_paths=tuple(value.get("changedPaths", [])),
        policy=dict(value["policy"]),
        created_at=str(value["createdAt"]),
        manifest_path=str(manifest_path),
        manifest_digest=str(value["manifestDigest"]),
        recovered=bool(value.get("recovered", False)),
    )


def _verify_ref(repository: Path, manifest: Mapping[str, Any]) -> None:
    ref = str(manifest["refName"])
    expected_ref = f"refs/pi/snapshots/{manifest['snapshotId']}"
    if ref != expected_ref:
        raise SnapshotIntegrityError("snapshot ref is not bound to snapshot ID")
    if not re.fullmatch(r"refs/pi/snapshots/snap_[0-9a-f]{32}", ref):
        raise SnapshotIntegrityError("snapshot ref is outside the immutable namespace")
    commit = _git(repository, ["rev-parse", "--verify", ref]).strip()
    if commit != manifest["snapshotCommitOid"]:
        raise SnapshotIntegrityError("snapshot ref does not match manifest commit")
    tree = _git(repository, ["rev-parse", "--verify", f"{ref}^{{tree}}"]).strip()
    if tree != manifest["snapshotTreeOid"]:
        raise SnapshotIntegrityError("snapshot ref does not match manifest tree")


def capture_snapshot(
    repository: os.PathLike[str] | str,
    state_root: os.PathLike[str] | str,
    *,
    snapshot_id: str,
    policy: SnapshotPolicy | None = None,
    failpoint: Any | None = None,
) -> SnapshotRecord:
    """Capture one exact worktree state into an immutable namespaced Git ref."""

    _validate_id(snapshot_id)
    policy = policy or SnapshotPolicy()
    controller = failpoint or _NoOpFailpoint()
    repo = Path(repository).expanduser()
    _reject_symlink_components(repo)
    try:
        repo = repo.resolve(strict=True)
    except OSError as error:
        raise SnapshotError("snapshot repository is unavailable") from error
    if not repo.is_dir():
        raise SnapshotError("snapshot repository is not a directory")
    root = _secure_directory(Path(state_root).expanduser())
    snapshots_root = _secure_directory(root / "snapshots")
    snapshot_dir = snapshots_root / snapshot_id
    if snapshot_dir.exists() or snapshot_dir.is_symlink():
        raise SnapshotConflictError("snapshot ID already exists")
    top, common, object_format, source_head, source_tree, status_before = _repository_info(repo)
    index_before = _real_index_digest(repo)
    before_inventory = _filesystem_inventory(repo, policy)
    temporary_fd, temporary_name = tempfile.mkstemp(prefix=".snapshot-index-", dir=str(root))
    os.close(temporary_fd)
    temporary_index = Path(temporary_name)
    temporary_index.unlink()
    ref_name = f"refs/pi/snapshots/{snapshot_id}"
    try:
        environment = {"GIT_INDEX_FILE": str(temporary_index)}
        if source_head:
            _git(repo, ["read-tree", "--reset", source_head], environment=environment)
        else:
            _git(repo, ["read-tree", "--empty"], environment=environment)
        add_args = ["add"]
        if policy.include_ignored:
            add_args.append("-f")
        add_args.append("-A" if policy.include_untracked else "-u")
        _git(repo, add_args, environment=environment)
        after_add_inventory = _filesystem_inventory(repo, policy)
        if after_add_inventory != before_inventory:
            raise SnapshotConcurrentMutationError("worktree changed while snapshot was being captured")
        index_entries = _git(repo, ["ls-files", "--stage", "-z"], environment=environment)
        _validate_submodules(repo, index_entries, allow=policy.allow_submodules)
        snapshot_tree = _git(repo, ["write-tree"], environment=environment).strip()
        if not snapshot_tree:
            raise SnapshotError("Git did not return a snapshot tree")
        after_tree_inventory = _filesystem_inventory(repo, policy)
        if after_tree_inventory != before_inventory:
            raise SnapshotConcurrentMutationError("worktree changed before snapshot commit")
        commit_args = ["commit-tree", snapshot_tree]
        if source_head:
            commit_args.extend(["-p", source_head])
        commit_environment = dict(environment)
        commit_environment.update({
            "GIT_AUTHOR_NAME": "pi-control snapshot",
            "GIT_AUTHOR_EMAIL": "pi-control@example.invalid",
            "GIT_COMMITTER_NAME": "pi-control snapshot",
            "GIT_COMMITTER_EMAIL": "pi-control@example.invalid",
        })
        snapshot_commit = _git(repo, commit_args, environment=commit_environment, input_text=f"pi-control snapshot {snapshot_id}\n").strip()
        if not snapshot_commit:
            raise SnapshotError("Git did not return a snapshot commit")
        # Re-observe after commit creation and before the ref becomes durable.
        # This closes the ordinary mutation window between the final worktree
        # inventory and commit/ref publication; a mismatch leaves only orphaned
        # Git objects, never a falsely exact snapshot ref.
        final_inventory = _filesystem_inventory(repo, policy)
        final_status = _git(repo, ["status", "--porcelain=v2", "--branch", "--untracked-files=all"])
        final_head = _git(repo, ["rev-parse", "--verify", "HEAD"], check=False).strip() or None
        final_index = _real_index_digest(repo)
        if final_inventory != before_inventory or _status_hash(final_status) != _status_hash(status_before) or final_head != source_head or final_index != index_before:
            raise SnapshotConcurrentMutationError("worktree, HEAD, or real index changed before snapshot publication")
        controller.hit("snapshot.ref.before", {"snapshot_id": snapshot_id, "ref": ref_name})
        try:
            _git(repo, ["update-ref", ref_name, snapshot_commit, ""])
        except SnapshotError as error:
            raise SnapshotConflictError("snapshot ref update failed") from error
        controller.hit("snapshot.ref.after", {"snapshot_id": snapshot_id, "ref": ref_name})
        if _git(repo, ["rev-parse", "--verify", ref_name]).strip() != snapshot_commit:
            raise SnapshotIntegrityError("snapshot ref verification failed")
        changed = _changed_paths(repo, snapshot_commit)
        created_at = utc_now()
        snapshot_dir.mkdir(mode=0o700)
        value: dict[str, Any] = {
            "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
            "snapshotId": snapshot_id,
            "repositoryPath": str(repo),
            "commonDir": common,
            "objectFormat": object_format,
            "sourceHeadOid": source_head,
            "sourceTreeOid": source_tree,
            "snapshotCommitOid": snapshot_commit,
            "snapshotTreeOid": snapshot_tree,
            "refName": ref_name,
            "dirty": _dirty(status_before),
            "statusHash": _status_hash(status_before),
            "changedPaths": list(changed),
            "policy": policy.as_dict(),
            "createdAt": created_at,
            "recovered": False,
        }
        manifest_path = snapshot_dir / "manifest.json"
        digest = _write_manifest(manifest_path, value, failpoint=controller)
        return _record_from_manifest({**value, "manifestDigest": digest}, manifest_path=manifest_path)
    finally:
        try:
            temporary_index.unlink()
        except FileNotFoundError:
            pass


def load_snapshot(state_root: os.PathLike[str] | str, snapshot_id: str, repository: os.PathLike[str] | str) -> SnapshotRecord:
    _validate_id(snapshot_id)
    root = _secure_directory(Path(state_root).expanduser(), create=False)
    snapshots_root = _secure_directory(root / "snapshots", create=False)
    snapshot_dir = _secure_directory(snapshots_root / snapshot_id, create=False)
    manifest_path = snapshot_dir / "manifest.json"
    manifest = _read_manifest(manifest_path)
    if manifest.get("snapshotId") != snapshot_id:
        raise SnapshotIntegrityError("manifest snapshot ID mismatch")
    _verify_ref(Path(repository).expanduser().resolve(strict=True), manifest)
    return _record_from_manifest(manifest, manifest_path=manifest_path)


def reconcile_snapshot(
    repository: os.PathLike[str] | str,
    state_root: os.PathLike[str] | str,
    snapshot_id: str,
) -> SnapshotRecord:
    """Reconcile a ref/manifest crash boundary without deleting immutable data."""

    _validate_id(snapshot_id)
    root = _secure_directory(Path(state_root).expanduser())
    snapshot_dir = root / "snapshots" / snapshot_id
    manifest_path = snapshot_dir / "manifest.json"
    repo = Path(repository).expanduser().resolve(strict=True)
    ref_name = f"refs/pi/snapshots/{snapshot_id}"
    snapshots_root = _secure_directory(root / "snapshots")
    if snapshot_dir.is_symlink():
        raise SnapshotIntegrityError("snapshot recovery directory is symlinked")
    if manifest_path.exists() or manifest_path.is_symlink():
        return load_snapshot(root, snapshot_id, repo)
    commit_result = _git(repo, ["rev-parse", "--verify", ref_name], check=False).strip()
    if not commit_result:
        raise SnapshotIntegrityError("snapshot has neither a manifest nor an immutable ref")
    tree = _git(repo, ["rev-parse", "--verify", f"{ref_name}^{{tree}}"]).strip()
    parent_result = _git(repo, ["rev-parse", "--verify", f"{ref_name}^"], check=False).strip()
    parent_tree = _git(repo, ["rev-parse", "--verify", f"{parent_result}^{{tree}}"], check=False).strip() if parent_result else None
    common = _git(repo, ["rev-parse", "--git-common-dir"]).strip()
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = repo / common_path
    object_format = _git(repo, ["rev-parse", "--show-object-format"]).strip()
    if snapshot_dir.is_symlink() or (snapshot_dir.exists() and not snapshot_dir.is_dir()):
        raise SnapshotIntegrityError("snapshot recovery directory is unsafe")
    snapshot_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(snapshot_dir, 0o700)
    value: dict[str, Any] = {
        "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
        "snapshotId": snapshot_id,
        "repositoryPath": str(repo),
        "commonDir": str(common_path.resolve(strict=True)),
        "objectFormat": object_format,
        "sourceHeadOid": parent_result or None,
        "sourceTreeOid": parent_tree or "",
        "snapshotCommitOid": commit_result,
        "snapshotTreeOid": tree,
        "refName": ref_name,
        "dirty": True,
        "statusHash": "recovered",
        "changedPaths": list(_changed_paths(repo, commit_result)),
        "policy": {"recovered": True},
        "createdAt": utc_now(),
        "recovered": True,
    }
    digest = _write_manifest(manifest_path, value, failpoint=_NoOpFailpoint())
    return _record_from_manifest({**value, "manifestDigest": digest}, manifest_path=manifest_path)


# Friendly names for later controller/adapters.
create_snapshot = capture_snapshot

__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotConcurrentMutationError",
    "SnapshotConflictError",
    "SnapshotError",
    "SnapshotIntegrityError",
    "SnapshotPolicy",
    "SnapshotRecord",
    "capture_snapshot",
    "create_snapshot",
    "load_snapshot",
    "reconcile_snapshot",
]
