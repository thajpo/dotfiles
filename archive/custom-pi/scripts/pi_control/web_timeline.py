"""Bounded, user-facing projections of Pi session records."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from .models import validate_id

_MAX_BYTES = 2 * 1024 * 1024
_MAX_RECORDS = 512
_MAX_LINE_BYTES = 64 * 1024
_MAX_PAGE_BYTES = 2 * 1024 * 1024
_TOOL_NAMES = frozenset({"bash", "edit", "grep", "read", "write", "git", "find", "ls", "search"})


def _text(value: Any, *, limit: int = 16 * 1024) -> str:
    if isinstance(value, str):
        return value.replace("\x00", "")[:limit]
    if isinstance(value, Mapping):
        for key in ("text", "content", "value", "message"):
            if key in value:
                result = _text(value[key], limit=limit)
                if result:
                    return result
    if isinstance(value, list):
        parts = [_text(item, limit=limit) for item in value]
        return "".join(part for part in parts if part)[:limit]
    return ""


def _entry_timestamp(record: Mapping[str, Any]) -> str:
    for value in (record.get("timestamp"), record.get("createdAt"), record.get("created_at")):
        if isinstance(value, str) and value:
            return value
    return ""


def _tool_summary(message: Mapping[str, Any]) -> tuple[str, str | None]:
    name = message.get("toolName") or message.get("name")
    failed = bool(message.get("isError") or message.get("error"))
    if failed:
        return "Tool failed", None
    if isinstance(name, str) and name in _TOOL_NAMES:
        return f"Used {name}", None
    return "Tool completed", None


def _message_projection(record: Mapping[str, Any]) -> dict[str, Any] | None:
    message = record.get("message") if isinstance(record.get("message"), Mapping) else record
    if not isinstance(message, Mapping):
        return None
    role = message.get("role")
    timestamp = _entry_timestamp(record) or _entry_timestamp(message)
    entry_id = record.get("id")
    if role == "user":
        projected = {"kind": "user", "text": _text(message), "time": timestamp}
    elif role == "assistant":
        projected = {"kind": "assistant", "markdown": _text(message), "time": timestamp}
    elif role in {"toolResult", "tool", "tool_result"}:
        summary, detail = _tool_summary(message)
        projected = {"kind": "tool", "summary": summary, "detail": detail, "bounded": True, "time": timestamp}
    else:
        return None
    if isinstance(entry_id, str) and entry_id:
        projected["entryId"] = entry_id
    return projected


def _safe_session_path(state_root: Path, project_id: str, conversation_id: str, session_file: str) -> Path:
    validate_id(project_id, prefix="prj")
    validate_id(conversation_id, prefix="conv")
    expected = (state_root / "sessions" / project_id / f"{conversation_id}.jsonl").absolute()
    supplied = Path(session_file).expanduser()
    if not supplied.is_absolute() or supplied.absolute() != expected:
        raise ValueError("conversation session path is outside the controller session root")
    return expected


def _read_bounded_line(stream: BinaryIO) -> bytes | None:
    line = stream.readline(_MAX_LINE_BYTES + 1)
    if not line:
        return b""
    if len(line) <= _MAX_LINE_BYTES:
        return line
    while line and not line.endswith(b"\n"):
        line = stream.readline(_MAX_LINE_BYTES + 1)
    return None


def read_session_timeline_page(
    state_root: Path,
    *,
    project_id: str,
    conversation_id: str,
    session_file: str,
    after: str | None = None,
    limit: int = _MAX_RECORDS,
) -> dict[str, Any]:
    """Read one bounded page from an allowlisted controller-owned session."""

    if after is not None and (not isinstance(after, str) or not after or len(after) > 256 or "\x00" in after):
        raise ValueError("timeline cursor is invalid")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > _MAX_RECORDS:
        raise ValueError("timeline limit is invalid")

    path = _safe_session_path(state_root, project_id, conversation_id, session_file)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"timeline": [], "cursorFound": after is None, "nextCursor": None}
    if path.is_symlink() or not path.is_file():
        raise ValueError("conversation session file is missing or unsafe")
    entries: deque[dict[str, Any]] = deque(maxlen=_MAX_RECORDS)
    with path.open("rb") as stream:
        if info.st_size > _MAX_BYTES:
            stream.seek(info.st_size - _MAX_BYTES)
            _read_bounded_line(stream)
        while True:
            line = _read_bounded_line(stream)
            if line == b"":
                break
            if line is None:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, Mapping):
                continue
            projected = _message_projection(record)
            if projected is not None and (_text(projected.get("text")) or _text(projected.get("markdown")) or projected["kind"] == "tool"):
                entries.append(projected)
    all_entries = list(entries)
    cursor_found = after is None
    if after is not None:
        cursor_index = next((index for index, item in enumerate(all_entries) if item.get("entryId") == after), None)
        if cursor_index is None:
            selected = all_entries[:limit]
        else:
            cursor_found = True
            selected = all_entries[cursor_index + 1:cursor_index + 1 + limit]
    else:
        selected = all_entries[-limit:]
    while selected and len(json.dumps(selected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > _MAX_PAGE_BYTES:
        selected.pop(0)
    next_cursor = selected[-1].get("entryId") if selected and isinstance(selected[-1].get("entryId"), str) else None
    return {"timeline": selected, "cursorFound": cursor_found, "nextCursor": next_cursor}


def read_session_timeline(state_root: Path, *, project_id: str, conversation_id: str, session_file: str) -> list[dict[str, Any]]:
    """Read the newest bounded view of one controller-owned session."""

    return read_session_timeline_page(
        state_root,
        project_id=project_id,
        conversation_id=conversation_id,
        session_file=session_file,
    )["timeline"]


__all__ = ["read_session_timeline", "read_session_timeline_page"]
