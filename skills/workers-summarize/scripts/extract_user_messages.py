#!/usr/bin/env python3
"""Extract genuine user-text input events from a local Codex rollout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract user.text messages from the current local Codex session as JSON."
        )
    )
    parser.add_argument(
        "--session-id",
        help="Codex thread/session ID; defaults to CODEX_THREAD_ID or CODEX_SESSION_ID.",
    )
    parser.add_argument(
        "--session-file",
        type=Path,
        help="Explicit rollout JSONL file; overrides automatic session lookup.",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Exit successfully even when malformed or ambiguous input was found.",
    )
    parser.add_argument(
        "--include-metadata",
        action="store_true",
        help="Include local session paths, IDs, timestamps, and rollout line numbers.",
    )
    parser.add_argument(
        "--snapshot",
        help="Expected snapshot hash from the first page; fail if recovered input changed.",
    )
    parser.add_argument(
        "--start",
        type=int,
        default=1,
        help="One-based source-message ordinal at which to start (default: 1).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum source messages to return (default: 5).",
    )
    parser.add_argument(
        "--text-offset",
        type=int,
        default=0,
        help="Character offset for a single-message continuation; requires --limit 1.",
    )
    parser.add_argument(
        "--max-text-chars",
        type=int,
        default=6000,
        help="Maximum characters returned per message (default: 6000).",
    )
    return parser.parse_args()


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def resolve_session_file(args: argparse.Namespace) -> tuple[Path, str | None]:
    if args.session_file:
        path = args.session_file.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"session file does not exist: {path}")
        return path, args.session_id

    session_id = (
        args.session_id
        or os.environ.get("CODEX_THREAD_ID")
        or os.environ.get("CODEX_SESSION_ID")
    )
    if not session_id:
        raise ValueError(
            "no session ID; pass --session-id or set CODEX_THREAD_ID/CODEX_SESSION_ID"
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", session_id):
        raise ValueError("session ID contains unsupported characters")

    sessions = codex_home() / "sessions"
    matches = sorted(sessions.rglob(f"rollout-*-{session_id}.jsonl"))
    if not matches:
        raise FileNotFoundError(
            f"no rollout ending in session ID {session_id!r} under {sessions}"
        )
    if len(matches) > 1:
        raise RuntimeError(
            "multiple exact rollout files matched; pass --session-file explicitly"
        )
    return matches[0], session_id


def text_parts(payload: dict[str, Any]) -> tuple[list[str], int]:
    parts: list[str] = []
    malformed_parts = 0
    for item in payload.get("content") or []:
        if not isinstance(item, dict):
            malformed_parts += 1
            continue
        if item.get("type") != "input_text":
            continue
        value = item.get("text")
        if isinstance(value, str) and value:
            parts.append(value)
        else:
            malformed_parts += 1
    return parts, malformed_parts


def extract(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    messages: list[dict[str, Any]] = []
    message_indexes: dict[str, int] = {}
    unresolved_empty_ids: set[str] = set()
    malformed_lines = 0
    invalid_records = 0
    invalid_user_events = 0
    malformed_content_parts = 0
    events_without_message_id = 0
    revisions_applied = 0

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed_lines += 1
                continue

            if not isinstance(record, dict):
                invalid_records += 1
                continue
            payload = record.get("payload")
            if record.get("type") != "response_item":
                continue
            if not isinstance(payload, dict):
                invalid_records += 1
                continue
            if payload.get("type") != "message" or payload.get("role") != "user":
                continue

            metadata = payload.get("internal_chat_message_metadata_passthrough")
            if not isinstance(metadata, dict):
                invalid_user_events += 1
                continue
            kinds = metadata.get("content_item_kinds")
            if not isinstance(kinds, list):
                invalid_user_events += 1
                continue
            if "user.text" not in kinds:
                continue

            message_id = payload.get("id")
            if not isinstance(payload.get("content"), list):
                invalid_user_events += 1
                continue
            parts, malformed_parts = text_parts(payload)
            malformed_content_parts += malformed_parts
            text = "\n".join(parts)
            if not text:
                if isinstance(message_id, str) and message_id not in message_indexes:
                    unresolved_empty_ids.add(message_id)
                elif not isinstance(message_id, str):
                    invalid_user_events += 1
                continue
            if not isinstance(message_id, str):
                events_without_message_id += 1
            entry = {
                "source_ref": "",
                "text": text,
                "timestamp": record.get("timestamp"),
                "message_id": message_id,
                "turn_id": metadata.get("turn_id"),
                "rollout_line": line_number,
            }
            if isinstance(message_id, str) and message_id in message_indexes:
                index = message_indexes[message_id]
                entry["source_ref"] = messages[index]["source_ref"]
                messages[index] = entry
                revisions_applied += 1
            else:
                entry["source_ref"] = f"M{len(messages) + 1}"
                messages.append(entry)
                if isinstance(message_id, str):
                    message_indexes[message_id] = len(messages) - 1
            if isinstance(message_id, str):
                unresolved_empty_ids.discard(message_id)

    diagnostics = {
        "malformed_json_lines": malformed_lines,
        "invalid_records": invalid_records,
        "invalid_user_events": invalid_user_events,
        "malformed_content_parts": malformed_content_parts,
        "events_without_message_id": events_without_message_id,
        "unresolved_empty_user_events": len(unresolved_empty_ids),
        "revisions_applied": revisions_applied,
    }
    return messages, diagnostics


def snapshot_id(messages: list[dict[str, Any]], diagnostics: dict[str, int]) -> str:
    snapshot = {
        "messages": [
            {
                "message_id": message["message_id"],
                "turn_id": message["turn_id"],
                "text": message["text"],
            }
            for message in messages
        ],
        "diagnostics": diagnostics,
    }
    encoded = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main() -> int:
    args = parse_args()
    if args.start < 1 or args.limit < 1 or args.text_offset < 0 or args.max_text_chars < 1:
        print("workers-summarize: pagination values are out of range", file=sys.stderr)
        return 1
    if args.text_offset and args.limit != 1:
        print("workers-summarize: --text-offset requires --limit 1", file=sys.stderr)
        return 1
    if args.snapshot and not re.fullmatch(r"[0-9a-f]{64}", args.snapshot):
        print("workers-summarize: --snapshot must be a lowercase SHA-256 hash", file=sys.stderr)
        return 1
    try:
        path, session_id = resolve_session_file(args)
        messages, diagnostics = extract(path)
    except (FileNotFoundError, RuntimeError, ValueError, OSError) as error:
        print(f"workers-summarize: {error}", file=sys.stderr)
        return 1

    current_snapshot = snapshot_id(messages, diagnostics)
    partial_reasons = [
        name for name, count in diagnostics.items()
        if name != "revisions_applied" and count
    ]
    if not messages:
        partial_reasons.append("no_user_messages_recovered")
    if args.snapshot and args.snapshot != current_snapshot:
        partial_reasons.append("snapshot_changed")

    start_index = min(args.start - 1, len(messages))
    page_messages = messages[start_index:start_index + args.limit]
    rendered_messages = []
    for message in page_messages:
        text_start = args.text_offset if len(page_messages) == 1 else 0
        text_end = text_start + args.max_text_chars
        text = message["text"]
        next_text_offset = text_end if text_end < len(text) else None
        rendered = {
            "source_ref": message["source_ref"],
            "text": text[text_start:text_end],
            "text_chars_total": len(text),
            "text_offset": text_start,
            "next_text_offset": next_text_offset,
            "text_has_more": next_text_offset is not None,
        }
        if args.include_metadata:
            rendered.update(
                {
                    "timestamp": message["timestamp"],
                    "message_id": message["message_id"],
                    "turn_id": message["turn_id"],
                    "rollout_line": message["rollout_line"],
                }
            )
        rendered_messages.append(rendered)

    result = {
        "complete": not partial_reasons,
        "snapshot_id": current_snapshot,
        "snapshot_match": not args.snapshot or args.snapshot == current_snapshot,
        "message_count": len(messages),
        "partial_reasons": partial_reasons,
        "diagnostics": diagnostics,
        "page": {
            "start": args.start,
            "limit": args.limit,
            "returned_count": len(rendered_messages),
            "next_start": (
                start_index + len(page_messages) + 1
                if start_index + len(page_messages) < len(messages)
                else None
            ),
            "has_more_messages": start_index + len(page_messages) < len(messages),
        },
        "messages": rendered_messages,
    }
    if args.include_metadata:
        result["session"] = {
            "session_id": session_id,
            "session_file": str(path),
        }
    result["output_end"] = "workers-summarize-extract-v1"
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    if partial_reasons and not args.allow_partial:
        print(
            "workers-summarize: transcript extraction was incomplete; "
            "retry or use --allow-partial and disclose every partial reason",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
