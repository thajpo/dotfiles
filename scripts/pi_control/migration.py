"""Read-only legacy inventory and disposable shadow import/reconciliation.

This module never writes a legacy source, Git ref, working tree, launcher, or
live runtime.  A shadow import may write only an explicitly supplied disposable
controller state root.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
from typing import Any, Iterable, Mapping, Sequence

from .events import append_event_in_transaction
from .git_adapter import GitObservationError, observe_repository
from .locks import secure_directory_fd
from .models import canonical_json, json_digest, new_id, utc_now
from .operations import create_operation, update_operation_in_transaction
from .store import ControllerStore

_MAX_FILES = 4096
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_RECORD_BYTES = 64 * 1024
_SKIP_DISCOVERY_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"})


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def inventory_canonical_json(value: Any, *, max_bytes: int = 64 * 1024 * 1024) -> str:
    """Serialize the typed inventory envelope without controller-request item limits.

    Controller requests and SQLite diagnostic fields remain bounded by the
    strict model serializer.  A complete host inventory is a larger immutable
    manifest with its own 64 MiB envelope bound and may legitimately contain
    more than 4096 records.
    """
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("inventory manifest contains a non-canonical value") from error
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError("inventory manifest exceeds its size bound")
    return text


def _bounded(value: Any, limit: int = 512) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return [_bounded(item, limit) for item in value[:64]]
    if isinstance(value, dict):
        return {str(key)[:128]: _bounded(item, limit) for key, item in list(value.items())[:64]}
    return value


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor or os.sep)
    for component in absolute.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ValueError(f"inventory path contains a symlink component: {current}")


def _regular_hash(path: Path) -> tuple[int, int, str]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError(f"inventory source is not a regular file: {path}")
    if info.st_size > _MAX_FILE_BYTES:
        raise ValueError(f"inventory source exceeds its size bound: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return info.st_size, stat.S_IMODE(info.st_mode), "sha256:" + digest.hexdigest()


def _source_type(path: Path) -> str:
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    if path.suffix.lower() == ".jsonl" or "session" in name or "sessions" in parts:
        return "session"
    if "route" in name or "routes" in parts:
        return "route"
    if "lease" in name or "leases" in parts:
        return "lease"
    if "registry" in name or "registr" in name:
        return "registry"
    if "policy" in name:
        return "policy"
    if "artifact" in name or "artifacts" in parts:
        return "artifact"
    if "build" in name or "installed" in parts:
        return "installed-build"
    return "unknown"


def _parse_file(path: Path, size: int) -> dict[str, Any]:
    result: dict[str, Any] = {"parser": "opaque", "sourceType": _source_type(path)}
    if size > _MAX_RECORD_BYTES:
        return result
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return result
    try:
        value = json.loads(text)
        result["parser"] = "json"
        if isinstance(value, dict):
            result["record_count"] = 1
            result["identities"] = _identities(value)
        elif isinstance(value, list):
            result["record_count"] = len(value)
            identities: list[dict[str, str]] = []
            for row in value:
                if isinstance(row, dict):
                    identities.extend(_identities(row))
            result["identities"] = identities[:256]
        else:
            result["record_count"] = 1
        return result
    except json.JSONDecodeError:
        records = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                result["parser"] = "text"
                return result
            if not isinstance(row, dict):
                result["parser"] = "text"
                return result
            identity = _identities(row)
            if identity:
                records.extend(identity)
            if line_number > 4096:
                break
        result["parser"] = "jsonl" if records or text.strip() == "" else "text"
        result["record_count"] = len(text.splitlines())
        result["identities"] = records[:256]
        return result


def _identities(value: Mapping[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    record_digest = _sha256_bytes(canonical_json(dict(value)).encode("utf-8"))
    for names in (
        ("projectId", "project_id"),
        ("sessionId", "session_id", "piSessionId", "pi_session_id"),
        ("workstreamId", "workstream_id"),
        ("workingCopyId", "working_copy_id"),
        ("routeId", "route_id"),
    ):
        found = next((value[name] for name in names if isinstance(value.get(name), str) and value[name]), None)
        if found is not None:
            result.append({"kind": names[0], "value": str(found)[:256], "recordDigest": record_digest})
    return result


def _normal_git(observation: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(observation)
    value.pop("observed_at", None)
    for worktree in value.get("worktrees", []):
        if isinstance(worktree, dict):
            worktree.pop("observed_at", None)
    return _bounded(value, 4096)


def _iter_sources(source: Path) -> Iterable[Path]:
    if source.is_file() or source.is_symlink():
        yield source
        return
    if not source.is_dir():
        raise FileNotFoundError(str(source))
    for directory, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIP_DISCOVERY_DIRS)
        filenames = sorted(filenames)
        directory_path = Path(directory)
        for name in filenames:
            yield directory_path / name
        for name in dirnames:
            candidate = directory_path / name
            if candidate.is_symlink():
                yield candidate


def _contradictions(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str], tuple[str, str]] = {}
    result: list[dict[str, Any]] = []
    for entry in entries:
        parser = entry.get("parser")
        if parser not in {"json", "jsonl"}:
            continue
        for identity in entry.get("identities", []):
            if not isinstance(identity, dict):
                continue
            key = (str(identity.get("kind", "")), str(identity.get("value", "")))
            source_hash = str(identity.get("recordDigest") or entry.get("sha256", ""))
            path = str(entry.get("path", ""))
            prior = seen.get(key)
            if prior is not None and prior[0] != source_hash:
                result.append({"kind": "duplicate-identity", "identity": {"kind": key[0], "value": key[1]}, "sources": [prior[1], path], "reason": "same durable identity has divergent source records"})
            else:
                seen[key] = (source_hash, path)
    git_refs: dict[str, tuple[str | None, str]] = {}
    worktree_paths: dict[str, tuple[str, str]] = {}
    for entry in entries:
        if entry.get("kind") != "git-repository" or not isinstance(entry.get("metadata"), dict):
            continue
        metadata = entry["metadata"]
        common = str(metadata.get("common_dir", ""))
        head = metadata.get("head_oid") if isinstance(metadata.get("head_oid"), str) else None
        path = str(entry.get("path", ""))
        prior = git_refs.get(common)
        if prior is not None and prior[0] != head:
            result.append({"kind": "divergent-ref", "commonDir": common, "sources": [prior[1], path], "reason": "observed Git heads disagree; no timestamp precedence was applied"})
        else:
            git_refs[common] = (head, path)
        for worktree in metadata.get("worktrees", []):
            if not isinstance(worktree, dict):
                continue
            worktree_path = str(worktree.get("path", ""))
            if not worktree_path:
                continue
            worktree_prior = worktree_paths.get(worktree_path)
            claim = str(worktree.get("branch_ref") or worktree.get("head_oid") or "")
            if worktree_prior is not None and worktree_prior[0] != claim:
                result.append({"kind": "duplicate-working-copy", "path": worktree_path, "sources": [worktree_prior[1], path], "reason": "working-copy claims disagree"})
            else:
                worktree_paths[worktree_path] = (claim, path)
    return result


@dataclass(frozen=True)
class InventoryReport:
    payload: dict[str, Any]
    digest: str
    inventory_id: str
    manifest_path: str | None = None
    source_paths: tuple[str, ...] = ()

    @property
    def contradictions(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.payload.get("contradictions", []))

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload, "inventoryId": self.inventory_id, "manifestDigest": self.digest, "manifestPath": self.manifest_path}

    def verify_sources(self) -> None:
        for entry in self.payload.get("entries", []):
            kind = entry.get("kind")
            path = Path(str(entry.get("path", "")))
            try:
                _reject_symlink_components(path if kind in {"file", "git-repository"} else path.parent)
            except ValueError as error:
                raise RuntimeError(f"inventory source path became unsafe: {path}") from error
            if kind == "file":
                if not path.exists() or path.is_symlink():
                    raise RuntimeError(f"inventory source changed or disappeared: {path}")
                _, mode, digest = _regular_hash(path)
                info = path.lstat()
                if digest != entry.get("sha256") or mode != entry.get("mode") or info.st_uid != entry.get("uid") or info.st_gid != entry.get("gid"):
                    raise RuntimeError(f"inventory source changed after observation: {path}")
            elif kind == "symlink":
                info = path.lstat() if path.is_symlink() else None
                if info is None or os.readlink(path) != entry.get("target") or stat.S_IMODE(info.st_mode) != entry.get("mode") or info.st_uid != entry.get("uid") or info.st_gid != entry.get("gid"):
                    raise RuntimeError(f"inventory symlink changed after observation: {path}")
            elif kind == "git-repository":
                current = _normal_git(observe_repository(path).as_dict())
                info = path.lstat()
                if current != entry.get("metadata") or info.st_uid != entry.get("uid") or info.st_gid != entry.get("gid") or stat.S_IMODE(info.st_mode) != entry.get("mode"):
                    raise RuntimeError(f"Git source changed after observation: {path}")


def _adapter_states(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    git_observed = any(entry.get("kind") == "git-repository" for entry in entries)
    return [
        {"sourceType": "filesystem", "state": "observed", "provenance": "bounded-explicit-roots"},
        {"sourceType": "git", "state": "observed" if git_observed else "unavailable", "provenance": "git-adapter" if git_observed else "no-explicit-repository-source"},
        {"sourceType": "docker", "state": "unavailable", "provenance": "non-live-inventory", "reason": "Docker runtime inspection is an explicit host acceptance adapter"},
        {"sourceType": "tmux", "state": "unavailable", "provenance": "non-live-inventory", "reason": "presentation inspection is not inferred from filesystem records"},
        {"sourceType": "herdr", "state": "unavailable", "provenance": "non-live-inventory", "reason": "presentation inspection is not inferred from filesystem records"},
        {"sourceType": "process-tree", "state": "unavailable", "provenance": "non-live-inventory", "reason": "process observation requires an explicit adapter invocation"},
        {"sourceType": "installed-build", "state": "unavailable", "provenance": "non-live-inventory", "reason": "installed artifact inspection requires an explicit manifest root"},
    ]


def _unmigrated_resources(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    source_types = sorted({str(entry.get("sourceType")) for entry in entries if entry.get("kind") == "file"})
    return [
        {"resourceType": resource, "state": "unmigrated", "sourceTypes": source_types, "reason": "requires a typed authority adapter; no resource row was synthesized"}
        for resource in ("conversations", "workstreams", "runs", "changes", "reviews", "leases", "artifacts", "routes")
    ]


def collect_inventory(source_paths: Iterable[os.PathLike[str] | str]) -> InventoryReport:
    paths = tuple(Path(item).expanduser().absolute() for item in source_paths)
    if not paths:
        raise ValueError("at least one explicit inventory source is required")
    entries: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for raw in paths:
        _reject_symlink_components(raw)
        if not raw.exists() and not raw.is_symlink():
            raise FileNotFoundError(str(raw))
        for path in _iter_sources(raw):
            resolved_key = str(path.absolute())
            if resolved_key in seen_paths:
                continue
            seen_paths.add(resolved_key)
            if len(entries) >= _MAX_FILES:
                raise ValueError("inventory file count exceeds its bound")
            if path.is_symlink():
                info = path.lstat()
                entries.append({"kind": "symlink", "path": str(path), "target": os.readlink(path)[:1024], "uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)})
                continue
            if not path.is_file():
                continue
            size, mode, digest = _regular_hash(path)
            parsed = _parse_file(path, size)
            info = path.lstat()
            entries.append({"kind": "file", "path": str(path), "sizeBytes": size, "uid": info.st_uid, "gid": info.st_gid, "mode": mode, "sha256": digest, **parsed})
        candidate = raw if raw.is_dir() else raw.parent
        if candidate.is_dir() and (candidate / ".git").exists():
            try:
                observed = _normal_git(observe_repository(candidate).as_dict())
                info = candidate.lstat()
                entries.append({"kind": "git-repository", "path": str(candidate), "uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode), "metadata": observed})
            except GitObservationError as error:
                entries.append({"kind": "git-repository", "path": str(candidate), "error": str(error)[:512], "detail": _bounded(error.detail)})
    entries.sort(key=lambda value: (str(value.get("kind")), str(value.get("path"))))
    contradictions = _contradictions(entries)
    payload = {
        "schemaVersion": 1,
        "provenance": "pi-control-inventory-v1",
        "entries": entries,
        "contradictions": contradictions,
        "adapterStates": _adapter_states(entries),
        "unmigratedResources": _unmigrated_resources(entries),
    }
    digest = _sha256_bytes(canonical_json(payload).encode("utf-8"))
    return InventoryReport(payload, digest, new_id("mig"), source_paths=tuple(str(item) for item in paths))


def _secure_directory(path: Path) -> None:
    try:
        directory_fd = secure_directory_fd(path, create=True)
    except RuntimeError as error:
        raise PermissionError(f"inventory directory is not user-owned and private: {path}") from error
    if directory_fd is None:  # pragma: no cover - create=True always returns or raises
        raise PermissionError(f"inventory directory is unavailable: {path}")
    os.close(directory_fd)


def write_inventory(report: InventoryReport, destination: os.PathLike[str] | str) -> InventoryReport:
    target = Path(destination).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError(str(target))
    _secure_directory(target.parent)
    if target.parent.is_symlink():
        raise PermissionError(f"inventory destination directory is a symlink: {target.parent}")
    body = canonical_json(report.payload).encode("utf-8") + b"\n"
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary, flags, 0o400)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return InventoryReport(report.payload, report.digest, report.inventory_id, str(target), report.source_paths)


def inventory(source_paths: Iterable[os.PathLike[str] | str], *, destination: os.PathLike[str] | str | None = None) -> InventoryReport:
    report = collect_inventory(source_paths)
    return write_inventory(report, destination) if destination is not None else report


def load_inventory(path: os.PathLike[str] | str) -> InventoryReport:
    source = Path(path).expanduser().absolute()
    if source.is_symlink() or not source.is_file():
        raise FileNotFoundError(str(source))
    body = source.read_bytes()
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise ValueError("inventory manifest schema is unsupported")
    digest = _sha256_bytes(canonical_json(payload).encode("utf-8"))
    return InventoryReport(payload, digest, "mig_" + digest.split(":", 1)[1][:32], str(source), tuple(sorted({str(item.get("path")) for item in payload.get("entries", []) if isinstance(item, dict) and isinstance(item.get("path"), str)})))


def _copy_manifest(report: InventoryReport, state_root: Path, migration_id: str) -> tuple[InventoryReport, str, int]:
    destination = state_root / "migrations" / migration_id / "source-inventory.json"
    if report.manifest_path:
        source = Path(report.manifest_path)
        if not source.is_file() or source.is_symlink():
            raise RuntimeError("inventory manifest is unavailable or unsafe")
        body = source.read_bytes()
        if _sha256_bytes(body.rstrip(b"\n")) != report.digest:
            raise RuntimeError("inventory manifest digest does not match report")
    else:
        body = canonical_json(report.payload).encode("utf-8") + b"\n"
    if _sha256_bytes(body.rstrip(b"\n")) != report.digest:
        raise RuntimeError("inventory payload digest does not match report")
    if destination.exists() or destination.is_symlink():
        info = destination.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o400:
            raise RuntimeError("existing shadow inventory manifest is unsafe")
        existing_body = destination.read_bytes()
        if _sha256_bytes(existing_body.rstrip(b"\n")) != report.digest or existing_body != body:
            raise RuntimeError("existing shadow inventory manifest disagrees with the source")
        return InventoryReport(report.payload, report.digest, report.inventory_id, str(destination), report.source_paths), report.digest, len(body)
    return write_inventory(report, destination), report.digest, len(body)


def _write_auxiliary_manifest(payload: Any, destination: Path) -> tuple[str, int]:
    _secure_directory(destination.parent)
    body = canonical_json(payload).encode("utf-8") + b"\n"
    if destination.exists() or destination.is_symlink():
        info = destination.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o400:
            raise RuntimeError("existing migration manifest is unsafe")
        existing_body = destination.read_bytes()
        if existing_body != body:
            raise RuntimeError("existing migration manifest disagrees with the source")
        return _sha256_bytes(body.rstrip(b"\n")), len(body)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        return _sha256_bytes(body.rstrip(b"\n")), len(body)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _project_records(report: InventoryReport) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for entry in report.payload.get("entries", []):
        if entry.get("kind") != "git-repository" or not isinstance(entry.get("metadata"), dict):
            continue
        metadata = entry["metadata"]
        common = str(metadata.get("common_dir", ""))
        top = str(metadata.get("top_level") or entry.get("path", ""))
        if not common or not top or metadata.get("is_bare"):
            continue
        digest = hashlib.sha256(common.encode("utf-8")).hexdigest()
        project_id = "prj_" + digest[:32]
        records.append({"project_id": project_id, "display_name": Path(top).name or "project", "common_dir": common, "primary_checkout": top, "metadata": metadata})
    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        unique.setdefault(record["project_id"], record)
    return list(unique.values())


def _manifest_is_intact(store: ControllerStore, migration_id: str, report: InventoryReport) -> bool:
    row = store.conn.execute("SELECT path,sha256,size_bytes FROM migration_manifests WHERE migration_id=? AND kind='source-inventory'", (migration_id,)).fetchone()
    if row is None:
        return False
    path = Path(str(row["path"]))
    try:
        root = store.state_root.absolute().resolve()
        resolved = path.absolute().resolve(strict=True)
        if root not in resolved.parents or resolved.parent.name != migration_id or resolved.parent.parent.name != "migrations":
            return False
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o400 or info.st_size != int(row["size_bytes"]):
            return False
        body = path.read_bytes()
        return _sha256_bytes(body.rstrip(b"\n")) == str(row["sha256"]) == report.digest
    except OSError:
        return False


def _shadow_marker(report_digest: str) -> dict[str, str]:
    return {
        "schemaVersion": "1",
        "mode": "shadow-import",
        "sourceManifestDigest": report_digest,
        "markerDigest": _sha256_bytes(("pi-control-shadow-v1:" + report_digest).encode("utf-8")),
    }


def _prepare_shadow_root(root: Path, report_digest: str) -> None:
    """Require an empty/new root or a controller-created shadow marker."""

    _secure_directory(root)
    marker = root / "shadow-import.marker"
    if marker.exists() or marker.is_symlink():
        info = marker.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("shadow import marker is unsafe")
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("shadow import marker is invalid") from error
        if value != _shadow_marker(report_digest):
            raise ValueError("shadow import root is bound to a different inventory")
        return
    children = list(root.iterdir())
    if children:
        raise ValueError("shadow import requires a new or controller-marked disposable state root")
    body = canonical_json(_shadow_marker(report_digest)).encode("utf-8") + b"\n"
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        raise


def shadow_import(report: InventoryReport, state_root: os.PathLike[str] | str | None, *, idempotency_key: str | None = None) -> dict[str, Any]:
    """Import an inventory into a new disposable ControllerStore only."""

    report.verify_sources()
    if state_root is None:
        raise ValueError("shadow import requires an explicit disposable state root")
    raw_root = Path(state_root).expanduser()
    if raw_root.is_symlink():
        raise ValueError("shadow import state root must not be a symlink")
    root = raw_root.resolve()
    default_root = (Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "pi-control").expanduser().resolve()
    if root == default_root:
        raise ValueError("shadow import refuses the ordinary live controller state root")
    _prepare_shadow_root(root, report.digest)
    migration_id = "mig_" + report.digest.split(":", 1)[1][:32]
    key = idempotency_key or f"shadow-import:{report.digest}"
    request = {"schemaVersion": 1, "migrationId": migration_id, "idempotencyKey": key, "mode": "shadow-import", "sourceManifestDigest": report.digest}
    build_id = "build_" + report.digest.split(":", 1)[1][:32]
    with ControllerStore(root) as store:
        existing = store.conn.execute("SELECT * FROM migration_runs WHERE migration_id=?", (migration_id,)).fetchone()
        if existing is not None:
            if existing["source_manifest_digest"] != report.digest:
                raise RuntimeError("migration identity is bound to a different source manifest")
            if existing["idempotency_key"] != key:
                raise RuntimeError("migration identity is bound to a different idempotency key")
            if not _manifest_is_intact(store, migration_id, report):
                attention = {"status": "blocked", "reason": "source-inventory manifest is missing, tampered, or has the wrong mode"}
                now = utc_now()
                with store.transaction():
                    store.conn.execute("UPDATE migration_runs SET state='needs_attention',step='manifest-tampered',result_json=?,updated_at=?,completed_at=?,resource_version=resource_version+1 WHERE migration_id=?", (canonical_json(attention), now, now, migration_id))
                    operation_row = store.conn.execute("SELECT state FROM operations WHERE operation_id=?", (existing["operation_id"],)).fetchone()
                    if operation_row is not None and operation_row["state"] not in {"succeeded", "failed", "needs_attention", "cancelled"}:
                        update_operation_in_transaction(store.conn, existing["operation_id"], state="needs_attention", step="manifest-tampered", result=attention)
                    append_event_in_transaction(store.conn, event_kind="migration.needs_attention", resource_type="migration", resource_id=migration_id, payload=attention, resource_version=int(existing["resource_version"]) + 1, operation_id=existing["operation_id"])
                return {"migrationId": migration_id, "state": "needs_attention", "sourceManifestDigest": report.digest, **attention}
            return {"migrationId": migration_id, "state": existing["state"], "sourceManifestDigest": report.digest, "idempotent": True, "projectCount": store.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]}
        operation = create_operation(store, idempotency_key=key, kind="migration", resource_type="migration", resource_id=migration_id, actor_type="controller", actor_id="shadow-import", request=request, operation_id="op_" + report.digest.split(":", 1)[1][:32])
        active_build = store.conn.execute("SELECT build_id FROM installed_builds WHERE status='active' ORDER BY activated_at DESC LIMIT 1").fetchone()
        if active_build is not None and active_build["build_id"] != build_id:
            raise ValueError("shadow import root already has a different active controller build")
        if store.conn.execute("SELECT 1 FROM installed_builds WHERE build_id=?", (build_id,)).fetchone() is None:
            store.register_build(build_id, source_tree_hash=report.digest, artifact_manifest_hash=report.digest, pi_version="shadow", package_lock_hash=report.digest, status="active", verification={"shadow": True, "sourceManifestDigest": report.digest})
        manifest_report, manifest_digest, manifest_size = _copy_manifest(report, root, migration_id)
        contradiction_path: Path | None = None
        contradiction_digest: str | None = None
        contradiction_size: int | None = None
        if report.contradictions:
            contradiction_path = root / "migrations" / migration_id / "contradictions.json"
            contradiction_digest, contradiction_size = _write_auxiliary_manifest(list(report.contradictions), contradiction_path)
        now = utc_now()
        with store.transaction():
            store.conn.execute("INSERT INTO migration_runs(migration_id,operation_id,idempotency_key,mode,controller_build_id,request_digest,source_manifest_digest,state,step,result_json,resource_version,created_at,updated_at,completed_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (migration_id, operation.operation_id, key, "shadow-import", build_id, json_digest(request), report.digest, "applying", "importing", None, 1, now, now, None, None, None))
            store.conn.execute("INSERT INTO migration_manifests(migration_id,kind,path,sha256,size_bytes,created_at) VALUES(?,?,?,?,?,?)", (migration_id, "source-inventory", str(manifest_report.manifest_path), manifest_digest, manifest_size, now))
            if contradiction_path is not None and contradiction_digest is not None and contradiction_size is not None:
                store.conn.execute("INSERT INTO migration_manifests(migration_id,kind,path,sha256,size_bytes,created_at) VALUES(?,?,?,?,?,?)", (migration_id, "contradictions", str(contradiction_path), contradiction_digest, contradiction_size, now))
            if report.contradictions:
                result = {"status": "blocked", "contradictions": list(report.contradictions), "adapterStates": report.payload.get("adapterStates", []), "unmigratedResources": report.payload.get("unmigratedResources", [])}
                state = "needs_attention"
            else:
                imported = 0
                for record in _project_records(report):
                    metadata = record["metadata"]
                    if store.conn.execute("SELECT 1 FROM projects WHERE project_id=?", (record["project_id"],)).fetchone() is not None:
                        continue
                    common_path = Path(record["common_dir"])
                    stat_result = common_path.stat()
                    policy_hash = _sha256_bytes(canonical_json({"defaultMode": "isolated"}).encode("utf-8"))
                    store.conn.execute("INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (record["project_id"], record["display_name"], record["common_dir"], stat_result.st_dev, stat_result.st_ino, record["primary_checkout"], metadata.get("object_format", "sha1"), "isolated", policy_hash, "active", "ready", 1, now, now, now, None, None))
                    wc_id = "wc_" + hashlib.sha256((record["project_id"] + record["primary_checkout"]).encode()).hexdigest()[:32]
                    branch = metadata.get("branch_ref")
                    observed_state = "dirty" if metadata.get("dirty") else "ready"
                    store.conn.execute("INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,git_dir,branch_ref,expected_head_oid,expected_tree_oid,effective_mode,desired_state,observed_state,writer_epoch,active_writer_run_id,resource_version,controller_owned,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (wc_id, record["project_id"], record["display_name"], "primary", "personal", record["primary_checkout"], metadata.get("git_dir"), branch, metadata.get("head_oid"), metadata.get("tree_oid"), "isolated", "present", observed_state, 0, None, 1, 0, now, now, now, None, None))
                    imported += 1
                result = {"status": "imported", "projectCount": imported, "contradictions": [], "adapterStates": report.payload.get("adapterStates", []), "unmigratedResources": report.payload.get("unmigratedResources", [])}
                state = "succeeded"
            # Re-observe before committing lifecycle success.  A source race
            # rolls back the shadow transaction instead of guessing completion.
            report.verify_sources()
            result_json = canonical_json(result)
            completed = utc_now()
            store.conn.execute("UPDATE migration_runs SET state=?,step=?,result_json=?,updated_at=?,completed_at=?,resource_version=resource_version+1 WHERE migration_id=? AND resource_version=1", (state, "blocked" if state == "needs_attention" else "complete", result_json, completed, completed, migration_id))
            store.conn.execute("UPDATE operations SET state=?,step=?,result_json=?,updated_at=?,completed_at=? WHERE operation_id=?", (state if state != "needs_attention" else "needs_attention", "blocked" if state == "needs_attention" else "complete", result_json, completed, completed, operation.operation_id))
            append_event_in_transaction(store.conn, event_kind="migration.shadow_imported" if state == "succeeded" else "migration.needs_attention", resource_type="migration", resource_id=migration_id, payload=result, resource_version=2, operation_id=operation.operation_id)
        return {"migrationId": migration_id, "state": state, "sourceManifestDigest": report.digest, **result}


def shadow_reconcile(store: ControllerStore, report: InventoryReport) -> dict[str, Any]:
    report.verify_sources()
    expected_records = {record["project_id"]: record for record in _project_records(report)}
    actual_rows = {str(row["project_id"]): row for row in store.conn.execute("SELECT * FROM projects")}
    expected = set(expected_records)
    actual = set(actual_rows)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    field_mismatches: list[dict[str, Any]] = []
    for project_id in sorted(expected & actual):
        expected_record = expected_records[project_id]
        actual_row = actual_rows[project_id]
        comparisons = {
            "display_name": expected_record["display_name"],
            "git_common_dir": expected_record["common_dir"],
            "primary_checkout": expected_record["primary_checkout"],
            "object_format": expected_record["metadata"].get("object_format", "sha1"),
        }
        for field, expected_value in comparisons.items():
            if actual_row[field] != expected_value:
                field_mismatches.append({"projectId": project_id, "field": field, "expected": expected_value, "actual": actual_row[field]})
        expected_wc = "wc_" + hashlib.sha256((project_id + expected_record["primary_checkout"]).encode()).hexdigest()[:32]
        wc = store.conn.execute("SELECT path,branch_ref,expected_head_oid,expected_tree_oid FROM working_copies WHERE working_copy_id=?", (expected_wc,)).fetchone()
        if wc is None:
            field_mismatches.append({"projectId": project_id, "resourceType": "working-copy", "state": "missing", "workingCopyId": expected_wc})
        else:
            wc_expected = {"path": expected_record["primary_checkout"], "branch_ref": expected_record["metadata"].get("branch_ref"), "expected_head_oid": expected_record["metadata"].get("head_oid"), "expected_tree_oid": expected_record["metadata"].get("tree_oid")}
            for field, expected_value in wc_expected.items():
                if wc[field] != expected_value:
                    field_mismatches.append({"projectId": project_id, "resourceType": "working-copy", "field": field, "expected": expected_value, "actual": wc[field]})
    contradictions = list(report.contradictions)
    return {
        "schemaVersion": 1,
        "sourceManifestDigest": report.digest,
        "state": "needs_attention" if missing or unexpected or contradictions or field_mismatches else "matched",
        "missingProjects": missing,
        "unexpectedProjects": unexpected,
        "fieldMismatches": field_mismatches,
        "contradictions": contradictions,
        "adapterStates": report.payload.get("adapterStates", []),
        "unmigratedResources": report.payload.get("unmigratedResources", []),
    }


@dataclass(frozen=True)
class InventoryV2Report:
    payload: dict[str, Any]
    digest: str
    inventory_id: str
    manifest_path: str | None = None

    @property
    def contradictions(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.payload.get("contradictions", []))

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload, "inventoryId": self.inventory_id, "manifestDigest": self.digest, "manifestPath": self.manifest_path}


def _adapter_registry() -> dict[str, Any]:
    from .migration_adapters import (
        observe_artifacts, observe_backups, observe_docker, observe_git,
        observe_herdr, observe_installed_build, observe_policy, observe_processes,
        observe_root_sessions, observe_routes_leases, observe_secretary, observe_tmux,
    )
    return {
        "git": observe_git, "root_sessions": observe_root_sessions,
        "secretary": observe_secretary, "routes_leases": observe_routes_leases,
        "artifacts": observe_artifacts, "processes": observe_processes,
        "docker": observe_docker, "tmux": observe_tmux, "herdr": observe_herdr,
        "installed_build": observe_installed_build, "policy": observe_policy,
        "backups": observe_backups,
    }


def collect_inventory_v2(sources: Mapping[str, os.PathLike[str] | str | None]) -> InventoryV2Report:
    """Collect the finite configured adapter graph without synthesizing authority."""
    if not isinstance(sources, Mapping):
        raise ValueError("inventory sources must be an explicit mapping")
    registry = _adapter_registry()
    unknown = set(sources) - set(registry)
    if unknown:
        raise ValueError("inventory source adapter is not configured")
    adapter_states: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    for adapter_kind in sorted(registry):
        source = sources.get(adapter_kind)
        if source is None:
            adapter_states.append({"adapterKind": adapter_kind, "state": "unavailable", "provenance": "explicit-adapter-registry", "reason": "no explicit source was supplied", "errorCode": "CP_ADAPTER_UNAVAILABLE"})
            continue
        result = registry[adapter_kind](source)
        value = result.as_dict()
        adapter_states.append({key: value[key] for key in value if key in {"adapterKind", "adapterSchemaVersion", "state", "provenance", "reason", "errorCode"}})
        records.extend(value.get("records", []))
        relationships.extend(value.get("relationships", []))
    if len(records) > 100_000:
        raise ValueError("inventory record count exceeds its bound")
    records.sort(key=lambda item: str(item.get("record_id", item.get("recordId", ""))))
    contradictions: list[dict[str, Any]] = []
    identities: dict[str, tuple[str, str]] = {}
    for record in records:
        identity = record.get("identity") if isinstance(record.get("identity"), Mapping) else {}
        key = canonical_json({"adapterKind": record.get("adapter_kind"), "resourceKind": record.get("resource_kind"), "identity": identity})
        prior = identities.get(key)
        current = (str(record.get("source_digest", "")), str(record.get("source_locator", "")))
        if prior is not None and prior[0] != current[0]:
            contradictions.append({"kind": "duplicate-identity", "identity": identity, "sources": [prior[1], current[1]], "reason": "same normalized identity has divergent source digests"})
        else:
            identities[key] = current
    for record in records:
        record["proposedDisposition"] = "observe" if record.get("resource_kind", "").endswith("observation") or "observation" in record.get("resource_kind", "") else "requires-decision"
    payload = {
        "schemaVersion": 2,
        "createdAt": None,
        "host": {"platform": os.name, "visibility": "bounded"},
        "sources": [{"adapterKind": kind, "source": str(sources[kind]) if sources.get(kind) is not None else None} for kind in sorted(registry)],
        "records": records,
        "relationships": sorted(relationships, key=canonical_json),
        "contradictions": contradictions,
        "adapterStates": adapter_states,
    }
    digest = _sha256_bytes(inventory_canonical_json(payload).encode("utf-8"))
    return InventoryV2Report(payload, digest, new_id("inv"))


def write_inventory_v2(report: InventoryV2Report, destination: os.PathLike[str] | str) -> InventoryV2Report:
    target = Path(destination).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError(str(target))
    _secure_directory(target.parent)
    body = inventory_canonical_json(report.payload).encode("utf-8") + b"\n"
    if len(body) > 64 * 1024 * 1024:
        raise ValueError("inventory manifest exceeds its size bound")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    except BaseException:
        try: target.unlink()
        except FileNotFoundError: pass
        raise
    return InventoryV2Report(report.payload, report.digest, report.inventory_id, str(target))


def load_inventory_v2(path: os.PathLike[str] | str) -> InventoryV2Report:
    source = Path(path).expanduser().absolute()
    if source.is_symlink() or not source.is_file(): raise FileNotFoundError(str(source))
    body = source.read_bytes()
    if len(body) > 64 * 1024 * 1024: raise ValueError("inventory manifest exceeds its size bound")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 2: raise ValueError("inventory v2 schema is unsupported")
    digest = _sha256_bytes(inventory_canonical_json(payload).encode("utf-8"))
    return InventoryV2Report(payload, digest, "inv_" + digest.split(":", 1)[1][:32], str(source))


def compare_shadow(store: ControllerStore, report: InventoryReport) -> dict[str, Any]:
    return shadow_reconcile(store, report)


__all__ = ["InventoryReport", "InventoryV2Report", "collect_inventory", "collect_inventory_v2", "compare_shadow", "inventory", "inventory_canonical_json", "load_inventory", "load_inventory_v2", "shadow_import", "shadow_reconcile", "write_inventory", "write_inventory_v2"]
