"""Bounded, read-only migration adapter primitives."""

from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterable, Mapping

MAX_FILES = 4096
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024
MAX_JSONL_HEADERS = 8
MAX_RECORDS = 100_000
_SECRET = re.compile(r"(?:secret|token|password|credential|capability|authorization|cookie|private.?key|prompt|command|environment|headers?)", re.I)


class AdapterError(Exception):
    """Typed bounded observation failure."""


@dataclass(frozen=True)
class AdapterRecord:
    record_id: str
    adapter_kind: str
    adapter_schema_version: int
    source_kind: str
    source_locator: str
    source_digest: str
    resource_kind: str
    identity: Mapping[str, Any]
    normalized: Mapping[str, Any]
    observation_state: str
    relationships: tuple[str, ...] = ()
    omission: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"relationships": list(self.relationships)}


@dataclass(frozen=True)
class AdapterResult:
    adapter_kind: str
    adapter_schema_version: int
    state: str
    provenance: str
    records: tuple[AdapterRecord, ...] = ()
    relationships: tuple[Mapping[str, Any], ...] = ()
    reason: str | None = None
    error_code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapterKind": self.adapter_kind,
            "adapterSchemaVersion": self.adapter_schema_version,
            "state": self.state,
            "provenance": self.provenance,
            "records": [record.as_dict() for record in self.records],
            "relationships": list(self.relationships),
            **({"reason": self.reason} if self.reason else {}),
            **({"errorCode": self.error_code} if self.error_code else {}),
        }


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): redact(item) for key, item in value.items() if not _SECRET.search(str(key))}
    if isinstance(value, list):
        return [redact(item) for item in value[:256]]
    if isinstance(value, tuple):
        return [redact(item) for item in value[:256]]
    if isinstance(value, str):
        return value[:4096]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:4096]


def _safe_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor or os.sep)
    for part in absolute.parts[1:]:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise AdapterError("source path contains a symlink component")


def safe_read(path: Path) -> tuple[bytes, os.stat_result]:
    _safe_components(path)
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise AdapterError("source is not a regular file")
    if before.st_size > MAX_FILE_BYTES:
        raise AdapterError("source exceeds the adapter size bound")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        data = b""
        while len(data) <= MAX_FILE_BYTES:
            block = os.read(fd, min(1024 * 1024, MAX_FILE_BYTES + 1 - len(data)))
            if not block:
                break
            data += block
        if len(data) > MAX_FILE_BYTES:
            raise AdapterError("source exceeds the adapter size bound")
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise AdapterError("source changed during observation")
        return data, after
    finally:
        os.close(fd)


def _parse_payload(path: Path, data: bytes, *, session: bool = False) -> tuple[Any, str]:
    if len(data) > MAX_RECORD_BYTES:
        return {"sizeBytes": len(data)}, "opaque"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"sizeBytes": len(data)}, "opaque"
    try:
        return redact(json.loads(text)), "json"
    except json.JSONDecodeError:
        rows: list[Any] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            if len(rows) >= MAX_JSONL_HEADERS:
                break
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                return {"sizeBytes": len(data)}, "opaque"
            if session and isinstance(row, Mapping):
                # A session header/identity is evidence; message bodies are
                # explicitly omitted from migration records.
                row = {key: value for key, value in row.items() if str(key).lower() not in {"message", "messages", "body", "content", "toolresult"}}
            rows.append(redact(row))
        return rows, "jsonl" if rows else "opaque"


def _source_kind(path: Path, role: str) -> str:
    name = path.name.lower()
    if role == "routes_leases":
        if "lease" in name: return "lease"
        if "route" in name: return "route"
    if role == "artifacts" or "artifact" in name: return "artifact"
    if path.suffix.lower() == ".jsonl" or "session" in name: return "session"
    if "registry" in name or "secretary" in name: return "registry"
    return role


def _resource_kind(path: Path, role: str) -> str:
    name = path.name.lower()
    if role == "routes_leases":
        if "lease" in name: return "lease-observation"
        if "route" in name: return "route-observation"
    if role == "artifacts" or "artifact" in name: return "artifact-observation"
    if path.suffix.lower() == ".jsonl" or "session" in name: return "conversation-observation"
    return "legacy-observation"


def observe_tree(root: os.PathLike[str] | str, *, adapter_kind: str, role: str) -> AdapterResult:
    root_path = Path(root).expanduser()
    try:
        _safe_components(root_path)
        info = root_path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise AdapterError("adapter root is a symlink")
        if not root_path.is_dir():
            raise AdapterError("adapter root is not a directory")
    except FileNotFoundError:
        return AdapterResult(adapter_kind, 1, "unavailable", "explicit-root", reason="source root is unavailable", error_code="CP_ADAPTER_UNAVAILABLE")
    except (OSError, AdapterError) as error:
        return AdapterResult(adapter_kind, 1, "error", "explicit-root", reason=str(error)[:512], error_code="CP_ADAPTER_UNAVAILABLE")
    paths: list[Path] = []
    try:
        for directory, dirnames, filenames in os.walk(root_path, topdown=True, followlinks=False):
            dirnames[:] = sorted(dirnames)
            for name in sorted(filenames):
                candidate = Path(directory) / name
                if candidate.is_symlink():
                    raise AdapterError("symlink source is not accepted")
                paths.append(candidate)
                if len(paths) > MAX_FILES:
                    raise AdapterError("adapter file count exceeds its bound")
    except (OSError, AdapterError) as error:
        return AdapterResult(adapter_kind, 1, "error", "explicit-root", reason=str(error)[:512], error_code="CP_INVALID_REQUEST")
    if not paths:
        return AdapterResult(adapter_kind, 1, "empty", "explicit-root", reason="explicit source root contains no regular files")
    records: list[AdapterRecord] = []
    try:
        for path in paths:
            data, info = safe_read(path)
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            payload, parser = _parse_payload(path, data, session=role == "root_sessions")
            source_kind = _source_kind(path, role)
            resource_kind = _resource_kind(path, role)
            identity = {"sourceKind": source_kind, "path": str(path.relative_to(root_path)), "digest": digest}
            if isinstance(payload, Mapping):
                for key in ("projectId", "project_id", "sessionId", "session_id", "workstreamId", "workstream_id", "routeId", "route_id", "workingCopyId", "working_copy_id"):
                    if isinstance(payload.get(key), str) and payload[key]: identity[key] = payload[key][:256]
            stable = _canonical({"adapterKind": adapter_kind, "sourceKind": source_kind, "identity": identity, "normalized": payload, "parser": parser})
            record_id = "rec_" + hashlib.sha256(stable.encode()).hexdigest()[:32]
            records.append(AdapterRecord(record_id, adapter_kind, 1, source_kind, str(path), digest, resource_kind, identity, payload if isinstance(payload, Mapping) else {"records": payload}, "observed", (), "message bodies omitted" if role == "root_sessions" else ""))
    except (OSError, UnicodeError, AdapterError) as error:
        return AdapterResult(adapter_kind, 1, "error", "explicit-root", reason=str(error)[:512], error_code="CP_INVALID_REQUEST")
    records.sort(key=lambda record: record.record_id)
    return AdapterResult(adapter_kind, 1, "observed", "explicit-root", tuple(records))


__all__ = ["AdapterError", "AdapterRecord", "AdapterResult", "MAX_FILES", "MAX_FILE_BYTES", "observe_tree", "redact", "safe_read"]
