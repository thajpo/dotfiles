"""Bounded, read-only inventory of legacy lifecycle records for Phase 3."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterable, Mapping, Sequence

from .models import utc_now

_MAX_FILES = 2048
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_JSON_BYTES = 256 * 1024


@dataclass(frozen=True)
class LegacyRecord:
    path: str
    source_kind: str
    file_type: str
    owner_uid: int | None
    mode: int
    size_bytes: int
    sha256: str | None
    parser: str
    parsed: Any
    error: str | None = None
    observed_at: str = field(default_factory=utc_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "source_kind": self.source_kind, "file_type": self.file_type,
            "owner_uid": self.owner_uid, "mode": self.mode, "size_bytes": self.size_bytes,
            "sha256": self.sha256, "parser": self.parser, "parsed": self.parsed, "error": self.error,
            "observed_at": self.observed_at,
        }


def _hash_file(path: Path, size: int) -> str:
    digest = hashlib.sha256()
    remaining = min(size, _MAX_FILE_BYTES)
    with path.open("rb") as handle:
        while remaining:
            block = handle.read(min(1024 * 1024, remaining))
            if not block:
                break
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def inventory_file(path: os.PathLike[str] | str, *, source_kind: str = "legacy") -> LegacyRecord:
    candidate = Path(path).expanduser()
    try:
        info = candidate.lstat()
    except OSError as error:
        return LegacyRecord(str(candidate), source_kind, "missing", None, 0, 0, None, "unavailable", None, type(error).__name__)
    mode = stat.S_IMODE(info.st_mode)
    owner = info.st_uid if hasattr(info, "st_uid") else None
    if stat.S_ISLNK(info.st_mode):
        return LegacyRecord(str(candidate), source_kind, "symlink", owner, mode, 0, None, "not-followed", None, "symlink is not followed")
    if stat.S_ISDIR(info.st_mode):
        return LegacyRecord(str(candidate), source_kind, "directory", owner, mode, 0, None, "directory", None)
    if not stat.S_ISREG(info.st_mode):
        return LegacyRecord(str(candidate), source_kind, "special", owner, mode, int(info.st_size), None, "not-parsed", None, "special file is not read")
    size = int(info.st_size)
    if size > _MAX_FILE_BYTES:
        return LegacyRecord(str(candidate), source_kind, "file", owner, mode, size, None, "oversize", None, "file exceeds inventory bound")
    digest = _hash_file(candidate, size)
    parsed: Any = None
    parser = "opaque"
    error = None
    if candidate.suffix.lower() in {".json", ".jsonl"} and size <= _MAX_JSON_BYTES:
        try:
            text = candidate.read_text(encoding="utf-8")
            if candidate.suffix.lower() == ".json":
                parsed = json.loads(text)
                parser = "json"
            else:
                rows = []
                for line in text.splitlines()[:2048]:
                    if line.strip():
                        rows.append(json.loads(line))
                parsed = {"line_count": len(rows), "records": rows}
                parser = "jsonl"
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as parse_error:
            parser = "invalid"
            error = type(parse_error).__name__
    return LegacyRecord(str(candidate), source_kind, "file", owner, mode, size, digest, parser, parsed, error)


def _iter_files(root: Path) -> Iterable[Path]:
    if not root.exists() or root.is_symlink():
        return ()
    if root.is_file():
        return (root,)
    found: list[Path] = []
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()]
        for name in files:
            found.append(Path(current) / name)
            if len(found) >= _MAX_FILES:
                return found
    return found


def _contradictions(records: Sequence[LegacyRecord]) -> list[dict[str, Any]]:
    by_worktree: dict[str, list[LegacyRecord]] = {}
    for record in records:
        parsed = record.parsed
        if not isinstance(parsed, Mapping):
            continue
        value = parsed.get("workingCopy") or parsed.get("worktree") or parsed.get("path")
        if isinstance(value, str):
            by_worktree.setdefault(value, []).append(record)
    result = []
    for path, matches in by_worktree.items():
        digests = {item.sha256 for item in matches}
        if len(matches) > 1 and len(digests) > 1:
            result.append({"kind": "duplicate-working-copy-record", "path": path, "sources": [item.path for item in matches]})
    return result


def inventory_legacy(paths: Sequence[os.PathLike[str] | str] | None = None) -> dict[str, Any]:
    """Inventory explicit roots only; the default is a bounded empty plan.

    A caller must name roots in Phase 3.  This prevents a cheap diagnostic from
    silently scanning arbitrary host state; migration later supplies explicit
    host-maintenance roots.
    """

    records: list[LegacyRecord] = []
    for raw in paths or ():
        root = Path(raw).expanduser()
        for path in _iter_files(root):
            records.append(inventory_file(path, source_kind=str(root)))
            if len(records) >= _MAX_FILES:
                break
    return {
        "schemaVersion": 1,
        "records": [record.as_dict() for record in records],
        "contradictions": _contradictions(records),
        "recordCount": len(records),
        "provenance": "legacy-read-only-inventory-v1",
    }


legacy_inventory = inventory_legacy

__all__ = ["LegacyRecord", "inventory_file", "inventory_legacy", "legacy_inventory"]
