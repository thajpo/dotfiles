"""Canonical source/build manifest generation for disposable staging.

The helper is deliberately file-producing only: it never swaps a live
symlink, starts/stops a process, builds/pushes an image, or changes Git.

A build manifest is a canonical envelope.  ``manifestDigest`` is calculated
from the canonical payload only; the identity fields are therefore never part
of their own digest.  The same validation code is used when manifests are
loaded by an installer and when a staged tree is checked.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Iterable, Mapping

from .git_adapter import GitObservationError, observe_repository

_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
# The staged generation is dominated by the npm tree; the current dependency
# set produces roughly 9.5k file/symlink entries. The bound is a runaway
# guard, not a tight budget: keep it well above the reviewed baseline while
# still failing closed on pathological trees.
_MAX_FILES = 16_384
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_SYMLINK_BYTES = 1024
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MANIFEST_IDS = frozenset({"manifestDigest", "buildId"})
_PAYLOAD_KEYS = frozenset(
    {
        "schemaVersion",
        "provenance",
        "sourceRoot",
        "files",
        "repository",
        "sourceTreeHash",
        "sourceCommit",
        "packageLockSha256",
        "metadata",
        "testOutcomes",
        "sourceDigest",
    }
)


def _canonical(value: Any) -> str:
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(text.encode("utf-8")) > _MAX_MANIFEST_BYTES:
        raise ValueError("staged build manifest exceeds its size bound")
    return text


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_components(path: Path, *, allow_final_symlink: bool = False) -> None:
    """Reject an input path whose parent components are symlinks.

    A manifest path is interpreted relative to one root.  Following an
    intermediate symlink would make that path mean something different from
    its serialized spelling, so it is never allowed.
    """

    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor or os.sep)
    for component in absolute.parts[1:]:
        current /= component
        if current.is_symlink() and not (allow_final_symlink and current == absolute):
            raise ValueError(f"manifest path contains a symlink component: {current}")


def _relative_path(root: Path, path: Path) -> str:
    root_abs = Path(os.path.abspath(root))
    path_abs = Path(os.path.abspath(path))
    if not _is_within(path_abs, root_abs):
        raise ValueError(f"manifest path escapes source root: {path}")
    relative = path_abs.relative_to(root_abs).as_posix()
    _validate_relative_path(relative)
    return relative


def _validate_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("manifest entry path must be a non-empty string")
    if "\\" in value:
        raise ValueError(f"manifest entry path uses an unsupported separator: {value!r}")
    if value.startswith("/") or value.endswith("/"):
        raise ValueError(f"manifest entry path must be a relative file path: {value!r}")
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError(f"manifest entry path contains traversal: {value!r}")
    normalized = PurePosixPath(value).as_posix()
    if normalized != value:
        raise ValueError(f"manifest entry path is not canonical: {value!r}")
    return value


def _safe_exclusions(root: Path, exclusions: Iterable[os.PathLike[str] | str] | None) -> set[str]:
    result: set[str] = set()
    for item in exclusions or ():
        candidate = Path(item).expanduser()
        if candidate.is_absolute():
            relative = _relative_path(root, candidate)
        else:
            relative = candidate.as_posix()
            _validate_relative_path(relative)
        result.add(relative)
    return result


def _symlink_target(path: Path) -> str:
    target = os.readlink(path)
    if "\x00" in target or len(target.encode("utf-8", errors="surrogateescape")) > _MAX_SYMLINK_BYTES:
        raise ValueError(f"manifest symlink target is invalid or exceeds its bound: {path}")
    return target


def _file_entry(path: Path, root: Path) -> dict[str, Any]:
    _safe_components(path, allow_final_symlink=True)
    if not _is_within(Path(os.path.abspath(path)), Path(os.path.abspath(root))):
        raise ValueError(f"manifest path escapes source root: {path}")
    info = path.lstat()
    relative = _relative_path(root, path)
    if stat.S_ISLNK(info.st_mode):
        # The target is recorded verbatim rather than resolved. The staged
        # generation intentionally contains links to immutable SDK packages in
        # the separately activated Pi core; exact verification compares the
        # target text and never follows it as source content.
        return {
            "path": relative,
            "kind": "symlink",
            "target": _symlink_target(path),
            "mode": stat.S_IMODE(info.st_mode),
        }
    if stat.S_ISREG(info.st_mode):
        if info.st_size > _MAX_FILE_BYTES:
            raise ValueError(f"manifest file exceeds bound: {path}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return {
            "path": relative,
            "kind": "file",
            "sizeBytes": info.st_size,
            "mode": stat.S_IMODE(info.st_mode),
            "sha256": "sha256:" + digest.hexdigest(),
        }
    raise ValueError(f"special files are not allowed in a build manifest: {path}")


def _walk_selected(root: Path, candidate: Path, excluded: set[str]) -> list[dict[str, Any]]:
    """Enumerate a selected path without following symlinks."""

    if candidate.is_symlink() or not candidate.is_dir():
        relative = _relative_path(root, candidate)
        if relative in excluded:
            return []
        return [_file_entry(candidate, root)]

    result: list[dict[str, Any]] = []
    stack = [candidate]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name, reverse=True)
        except OSError as exc:
            raise ValueError(f"cannot enumerate manifest directory: {directory}") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = _relative_path(root, path)
            if relative in excluded:
                continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                result.append(_file_entry(path, root))
            elif stat.S_ISDIR(info.st_mode):
                stack.append(path)
            elif stat.S_ISREG(info.st_mode):
                result.append(_file_entry(path, root))
            else:
                raise ValueError(f"special files are not allowed in a build manifest: {path}")
            if len(result) > _MAX_FILES:
                raise ValueError("build manifest file count exceeds its bound")
    return result


def _files(
    root: Path,
    paths: Iterable[os.PathLike[str] | str] | None,
    *,
    exclusions: Iterable[os.PathLike[str] | str] | None = None,
) -> list[dict[str, Any]]:
    selected = [Path(item).expanduser() for item in paths] if paths is not None else [root]
    excluded = _safe_exclusions(root, exclusions)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in selected:
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = Path(os.path.abspath(candidate))
        _safe_components(candidate, allow_final_symlink=True)
        if not candidate.exists() and not candidate.is_symlink():
            raise FileNotFoundError(str(candidate))
        if candidate != root:
            _relative_path(root, candidate)
        for entry in _walk_selected(root, candidate, excluded):
            key = entry["path"]
            if key in seen:
                continue
            seen.add(key)
            result.append(entry)
            if len(result) > _MAX_FILES:
                raise ValueError("build manifest file count exceeds its bound")
    return sorted(result, key=lambda value: value["path"])


def _tree_entries(root: Path, exclusions: set[str]) -> list[dict[str, Any]]:
    """Enumerate every file/symlink below ``root`` for exact verification."""

    if not root.is_dir() or root.is_symlink():
        raise ValueError("manifest verification root must be a regular non-symlink directory")
    return _files(root, None, exclusions=exclusions)


def _repository_metadata(repository: Path) -> dict[str, Any] | None:
    try:
        observed = observe_repository(repository).as_dict()
    except (GitObservationError, OSError):
        return None
    observed.pop("observed_at", None)
    # Preserve dirty-state evidence without embedding the full patch/status.
    observed.pop("status", None)
    for worktree in observed.get("worktrees", []):
        if isinstance(worktree, dict):
            worktree.pop("status", None)
    return observed


def _validate_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("manifest file entries must be objects")
    if set(entry) != {"path", "kind", "mode", "sha256", "sizeBytes"} and set(entry) != {"path", "kind", "mode", "target"}:
        raise ValueError("manifest file entry has an invalid schema")
    path = _validate_relative_path(entry.get("path"))
    kind = entry.get("kind")
    mode = entry.get("mode")
    if not isinstance(mode, int) or isinstance(mode, bool) or mode < 0 or mode > 0o7777:
        raise ValueError(f"manifest mode is invalid for {path}")
    if kind == "file":
        if set(entry) != {"path", "kind", "mode", "sha256", "sizeBytes"}:
            raise ValueError(f"file entry schema is invalid for {path}")
        digest = entry.get("sha256")
        size = entry.get("sizeBytes")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ValueError(f"file digest is invalid for {path}")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > _MAX_FILE_BYTES:
            raise ValueError(f"file size is invalid for {path}")
    elif kind == "symlink":
        if set(entry) != {"path", "kind", "mode", "target"}:
            raise ValueError(f"symlink entry schema is invalid for {path}")
        target = entry.get("target")
        if not isinstance(target, str) or "\x00" in target or len(target.encode("utf-8", errors="surrogateescape")) > _MAX_SYMLINK_BYTES:
            raise ValueError(f"symlink target is invalid for {path}")
    else:
        raise ValueError(f"manifest entry kind is not allowed for {path}: {kind!r}")
    return dict(entry)


def _validate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("build manifest payload must be an object")
    if set(payload) != _PAYLOAD_KEYS:
        missing = sorted(_PAYLOAD_KEYS - set(payload))
        extra = sorted(set(payload) - _PAYLOAD_KEYS)
        raise ValueError(f"build manifest payload schema mismatch (missing={missing}, extra={extra})")
    if payload.get("schemaVersion") != 1:
        raise ValueError("unsupported build manifest schema version")
    if payload.get("provenance") != "pi-control-staged-build-v1":
        raise ValueError("unexpected build manifest provenance")
    if payload.get("sourceRoot") != ".":
        raise ValueError("build manifest sourceRoot must be '.'")
    files = payload.get("files")
    if not isinstance(files, list) or len(files) > _MAX_FILES:
        raise ValueError("build manifest files must be a bounded list")
    normalized: list[dict[str, Any]] = []
    paths: set[str] = set()
    for raw_entry in files:
        entry = _validate_entry(raw_entry)
        if entry["path"] in paths:
            raise ValueError(f"duplicate build manifest path: {entry['path']}")
        paths.add(entry["path"])
        normalized.append(entry)
    if normalized != sorted(normalized, key=lambda value: value["path"]):
        raise ValueError("build manifest file entries are not in canonical order")
    if not isinstance(payload.get("metadata"), dict) or not isinstance(payload.get("testOutcomes"), dict):
        raise ValueError("build manifest metadata and test outcomes must be objects")
    source_digest = payload.get("sourceDigest")
    if not isinstance(source_digest, str) or _SHA256_RE.fullmatch(source_digest) is None:
        raise ValueError("build manifest sourceDigest is invalid")
    for field in ("sourceTreeHash", "sourceCommit"):
        value = payload.get(field)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"build manifest {field} is invalid")
    package_lock = payload.get("packageLockSha256")
    if package_lock is not None and (not isinstance(package_lock, str) or _SHA256_RE.fullmatch(package_lock) is None):
        raise ValueError("build manifest packageLockSha256 is invalid")
    repository = payload.get("repository")
    if repository is not None and not isinstance(repository, dict):
        raise ValueError("build manifest repository metadata must be an object or null")
    return dict(payload)


def _payload_digest(payload: Mapping[str, Any]) -> str:
    return _digest(_canonical(dict(payload)).encode("utf-8"))


@dataclass(frozen=True)
class BuildManifest:
    payload: dict[str, Any]
    digest: str
    path: str | None = None

    @property
    def build_id(self) -> str:
        return "build_" + self.digest.split(":", 1)[1][:32]

    def as_dict(self) -> dict[str, Any]:
        """Return the serialized canonical envelope without a path field."""

        return {**self.payload, "manifestDigest": self.digest, "buildId": self.build_id}

    def recompute_digest(self) -> str:
        return _payload_digest(_validate_payload(self.payload))

    def validate(self) -> None:
        payload = _validate_payload(self.payload)
        recomputed = _payload_digest(payload)
        if recomputed != self.digest:
            raise ValueError("build manifest digest does not match its payload")
        if self.build_id != "build_" + recomputed.split(":", 1)[1][:32]:
            raise ValueError("build manifest buildId does not match its digest")

    def verify_files(
        self,
        root: os.PathLike[str] | str,
        *,
        exclude: Iterable[os.PathLike[str] | str] | None = None,
        exclude_paths: Iterable[os.PathLike[str] | str] | None = None,
    ) -> None:
        """Require an exact file/symlink set below ``root``.

        ``exclude``/``exclude_paths`` are explicit relative paths (or paths
        inside ``root``), intended for the envelope file itself when checking
        a root that contains the serialized manifest.
        """

        if exclude is not None and exclude_paths is not None:
            raise ValueError("use only one of exclude and exclude_paths")
        base = Path(root).expanduser().absolute()
        payload = _validate_payload(self.payload)
        exclusions = _safe_exclusions(base, exclude if exclude is not None else exclude_paths)
        expected = {
            entry["path"]: entry
            for entry in payload["files"]
            if entry["path"] not in exclusions
        }
        actual_entries = _tree_entries(base, exclusions)
        actual = {entry["path"]: entry for entry in actual_entries}
        if set(actual) != set(expected):
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            raise RuntimeError(f"staged tree does not exactly match build manifest (missing={missing}, extra={extra})")
        for relative, expected_entry in expected.items():
            if actual[relative] != expected_entry:
                raise RuntimeError(f"staged file does not match build manifest: {base / relative}")


def load_build_manifest(path: os.PathLike[str] | str) -> BuildManifest:
    """Load and validate a serialized build-manifest envelope."""

    target = Path(path).expanduser().absolute()
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"build manifest is not a regular file: {target}")
    raw = target.read_bytes()
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ValueError("build manifest exceeds its size bound")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("build manifest is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("build manifest envelope must be an object")
    if "manifestDigest" not in value or "buildId" not in value:
        raise ValueError("build manifest envelope is missing manifestDigest or buildId")
    digest = value.get("manifestDigest")
    build_id = value.get("buildId")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise ValueError("build manifest manifestDigest is invalid")
    if not isinstance(build_id, str) or build_id != "build_" + digest.split(":", 1)[1][:32]:
        raise ValueError("build manifest buildId is invalid")
    payload = {key: item for key, item in value.items() if key not in _MANIFEST_IDS}
    if set(value) - (_PAYLOAD_KEYS | _MANIFEST_IDS):
        raise ValueError("build manifest envelope contains unknown fields")
    normalized = _validate_payload(payload)
    recomputed = _payload_digest(normalized)
    if recomputed != digest:
        raise ValueError("build manifest digest does not match its payload")
    return BuildManifest(normalized, digest, str(target))


# Explicit aliases make the loader easy to discover for callers that use the
# usual parser naming while keeping one implementation and one validation path.
read_build_manifest = load_build_manifest
parse_build_manifest = load_build_manifest


def create_build_manifest(
    source_root: os.PathLike[str] | str,
    *,
    files: Iterable[os.PathLike[str] | str] | None = None,
    metadata: Mapping[str, Any] | None = None,
    repository: os.PathLike[str] | str | None = None,
    test_outcomes: Mapping[str, Any] | None = None,
    manifest_path: os.PathLike[str] | str | None = None,
    exclude_paths: Iterable[os.PathLike[str] | str] | None = None,
    require_repository_metadata: bool = False,
) -> BuildManifest:
    root = Path(source_root).expanduser().absolute()
    if not root.is_dir() or root.is_symlink():
        raise ValueError("build source root must be a regular non-symlink directory")
    exclusions = list(exclude_paths or ())
    if manifest_path is not None:
        exclusions.append(manifest_path)
    file_entries = _files(root, files, exclusions=exclusions)
    repo = Path(repository).expanduser().absolute() if repository is not None else root
    repository_metadata = _repository_metadata(repo)
    if require_repository_metadata and repository_metadata is None:
        raise ValueError(f"repository metadata is unavailable: {repo}")
    payload: dict[str, Any] = {
        "schemaVersion": 1,
        "provenance": "pi-control-staged-build-v1",
        # The path is intentionally not part of identity: disposable staging
        # directories are regenerated for every build.
        "sourceRoot": ".",
        "files": file_entries,
        "repository": repository_metadata,
        "sourceTreeHash": (repository_metadata or {}).get("tree_oid"),
        "sourceCommit": (repository_metadata or {}).get("head_oid"),
        "packageLockSha256": next(
            (item["sha256"] for item in file_entries if item["path"].endswith("package-lock.json")),
            None,
        ),
        "metadata": dict(metadata or {}),
        "testOutcomes": dict(test_outcomes or {}),
    }
    # Canonical metadata is intentionally content/hash based; timestamps and
    # host process IDs do not become build identity.
    payload["sourceDigest"] = _digest(
        _canonical(
            {
                "files": file_entries,
                "repository": payload["repository"],
                "metadata": payload["metadata"],
                "testOutcomes": payload["testOutcomes"],
            }
        ).encode("utf-8")
    )
    normalized = _validate_payload(payload)
    digest = _payload_digest(normalized)
    return BuildManifest(normalized, digest)


def write_build_manifest(manifest: BuildManifest, destination: os.PathLike[str] | str) -> BuildManifest:
    target = Path(destination).expanduser().absolute()
    manifest.validate()
    _safe_components(target.parent)
    if target.exists() or target.is_symlink():
        raise FileExistsError(str(target))
    parent_info = target.parent.lstat()
    if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode) or parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o022:
        raise PermissionError("build manifest parent must be a user-owned non-group-writable directory")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(str(target.parent), directory_flags)
    temporary_name = f".{target.name}.{os.getpid()}.tmp"
    temporary_created = False
    try:
        current = os.fstat(directory_fd)
        if (current.st_dev, current.st_ino) != (parent_info.st_dev, parent_info.st_ino):
            raise PermissionError("build manifest parent changed during validation")
        body = _canonical(manifest.as_dict()).encode("utf-8") + b"\n"
        fd = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o400, dir_fd=directory_fd)
        temporary_created = True
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            # link+unlink publishes without replacing a concurrent destination.
            os.link(temporary_name, target.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
            os.unlink(temporary_name, dir_fd=directory_fd)
            temporary_created = False
            os.fsync(directory_fd)
        except BaseException:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            temporary_created = False
            raise
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)
    return BuildManifest(manifest.payload, manifest.digest, str(target))


def stage_build(
    source_root: os.PathLike[str] | str,
    staging_root: os.PathLike[str] | str,
    *,
    files: Iterable[os.PathLike[str] | str] | None = None,
    metadata: Mapping[str, Any] | None = None,
    repository: os.PathLike[str] | str | None = None,
    test_outcomes: Mapping[str, Any] | None = None,
) -> BuildManifest:
    root = Path(source_root).expanduser().absolute()
    stage = Path(staging_root).expanduser().absolute()
    if stage.exists() and stage.is_symlink():
        raise ValueError("staging root cannot be a symlink")
    stage.mkdir(parents=True, exist_ok=False)
    os.chmod(stage, 0o700)
    manifest = create_build_manifest(
        root,
        files=files,
        metadata=metadata,
        repository=repository,
        test_outcomes=test_outcomes,
    )
    return write_build_manifest(manifest, stage / "build-manifest.json")


__all__ = [
    "BuildManifest",
    "create_build_manifest",
    "load_build_manifest",
    "parse_build_manifest",
    "read_build_manifest",
    "stage_build",
    "write_build_manifest",
]
