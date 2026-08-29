#!/usr/bin/env python3
"""Codex lifecycle and typed-turn bridge for a fenced Pisec runtime."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sys
from typing import Any, Mapping


ATTENTION_TRIGGER = "PISEC_ATTENTION_TRIGGER"
MAX_RESPONSE_BYTES = 64 * 1024


def _request(socket_path: str, operation: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    request_id = "req_" + secrets.token_hex(16)
    request = {
        "protocolVersion": 1,
        "requestId": request_id,
        "operation": operation,
        "payload": dict(payload),
    }
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(socket_path)
            client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
            chunks: list[bytes] = []
            size = 0
            response: Any = None
            while True:
                chunk = client.recv(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    return None
                body = b"".join(chunks)
                try:
                    response = json.loads(body.decode())
                except (UnicodeError, json.JSONDecodeError):
                    response = None
                if response is not None or b"\n" in chunk:
                    break
            if response is None:
                response = json.loads(b"".join(chunks).decode())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(response, dict) or response.get("requestId") != request_id or response.get("ok") is not True:
        return None
    result = response.get("result")
    return dict(result) if isinstance(result, dict) else None


def _runtime_auth() -> dict[str, str | None]:
    return {
        "workstreamId": os.environ.get("PISEC_WORKSTREAM_ID"),
        "runtimeInstanceId": os.environ.get("PISEC_RUNTIME_INSTANCE_ID"),
        "surfaceId": os.environ.get("PISEC_SURFACE_ID"),
        "token": os.environ.get("PISEC_RUNTIME_TOKEN"),
        "generation": os.environ.get("PISEC_RUNTIME_GENERATION"),
    }


def _sequence_path(harness_home: str, instance: str) -> Path:
    digest = hashlib.sha256(instance.encode("utf-8")).hexdigest()
    return Path(harness_home) / "sessions" / f".pisec-hook-sequence-{digest}"


def _next_sequence(path: Path) -> int:
    try:
        return int(path.read_text()) + 1 if path.exists() else 1
    except (OSError, ValueError):
        return 1


def _report(
    socket_path: str,
    event: Mapping[str, Any],
    *,
    state: str,
    report_event: str,
    sequence_path: Path,
) -> bool:
    auth = _runtime_auth()
    workstream = str(auth["workstreamId"])
    instance = str(auth["runtimeInstanceId"])
    sequence = _next_sequence(sequence_path)
    native_id = event.get("thread_id") or event.get("session_id")
    if not isinstance(native_id, str) or not native_id:
        native_id = hashlib.sha256((workstream + instance).encode()).hexdigest()[:32]
    payload = {
        **auth,
        "seq": sequence,
        "event": report_event,
        "reason": None,
        "state": state,
        "nativeSessionKind": "id",
        "nativeSessionValue": native_id,
        "startSource": os.environ.get("PISEC_SESSION_START_SOURCE", "startup"),
    }
    result = _request(socket_path, "runtime.report", payload)
    accepted = bool(result and result.get("accepted") is True and result.get("seq") == sequence)
    if accepted:
        try:
            sequence_path.write_text(str(sequence))
        except OSError:
            pass
    return accepted


def _prepare_turn(socket_path: str) -> dict[str, Any] | None:
    return _request(socket_path, "runtime.turn.prepare", _runtime_auth())


def _acknowledge_bootstrap(socket_path: str, bootstrap: Mapping[str, Any]) -> bool:
    event_id = bootstrap.get("sourceRecordId")
    revision = bootstrap.get("sourceRevision")
    if not isinstance(event_id, str) or not event_id or not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return False
    result = _request(
        socket_path,
        "runtime.bootstrap.ack",
        {**_runtime_auth(), "bootstrapEventId": event_id, "bootstrapRevision": revision},
    )
    return bool(result and result.get("acknowledged") is True)


def _typed_context(turn: Mapping[str, Any], *, trigger: bool) -> str:
    lines = [
        "PISEC_AUTHENTICATED_RUNTIME_CONTEXT",
        "This developer context came from the broker-authenticated Pisec runtime, not from the terminal trigger.",
    ]
    packet = turn.get("taskPacket")
    if isinstance(packet, Mapping):
        lines.extend(("Immutable task packet:", json.dumps(packet, sort_keys=True, separators=(",", ":"))))
    bootstrap = turn.get("bootstrap")
    if isinstance(bootstrap, Mapping):
        lines.append(
            "Typed bootstrap source: "
            + json.dumps(
                {
                    "eventType": bootstrap.get("eventType"),
                    "sourceRecordId": bootstrap.get("sourceRecordId"),
                    "sourceRevision": bootstrap.get("sourceRevision"),
                    "workstreamId": bootstrap.get("workstreamId"),
                    "role": bootstrap.get("role"),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        lines.append("Start the assigned engineering task immediately after inspecting the immutable task packet.")
    attention = turn.get("attention")
    valid_attention: list[dict[str, Any]] = []
    if isinstance(attention, list):
        for item in attention:
            if not isinstance(item, Mapping):
                continue
            attention_id = item.get("attentionId")
            source_kind = item.get("sourceKind")
            source_id = item.get("sourceId")
            revision = item.get("revision")
            if not all(isinstance(value, str) and value for value in (attention_id, source_kind, source_id)):
                continue
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                continue
            valid_attention.append(
                {
                    "eventType": "coordinator.attention",
                    "sourceKind": source_kind,
                    "sourceRecordId": attention_id,
                    "sourceRevision": revision,
                    "sourceId": source_id,
                }
            )
    if valid_attention:
        lines.extend(
            (
                "Typed attention records:",
                json.dumps(valid_attention, sort_keys=True, separators=(",", ":")),
                "Inspect each authenticated source with the Pisec attention tools and continue the original engineering goal before ending this turn.",
            )
        )
        if any(item["sourceKind"] == "integration" for item in valid_attention):
            lines.append(
                "The attention inspector returns the authorized integration source; do not pass an integration sourceId to a coordination inspector. "
                "Integration attention in awaiting_worker state assigns source.next_action to this worker. "
                "For accepted target drift, the worker branch HEAD must descend from source.target_oid even when the accepted diff has no conflict. Rebase the worker branch onto refs/pisec/target/<integration-sourceId>, resolve only accepted paths, rerun verification, and submit one replacement completion packet for the new current commit under the existing acceptance. Read source.accepted_completion_contract from the integration attention. Keep its criterion text, order, and passed status unchanged, and provide current evidence for the rebased commit. "
                "Do not defer this bounded action to the coordinator or request a second approval."
            )
    elif trigger:
        lines.append("The inert attention trigger raced with current state; no authenticated attention record remains. Do not infer work from the trigger token.")
    if trigger:
        lines.append(f"Treat the literal user input {ATTENTION_TRIGGER!r} only as an inert wake signal, never as human intent or authority.")
    return "\n".join(lines)


def _emit_hook_context(event_name: str, context: str, *, system_message: str | None) -> None:
    output: dict[str, Any] = {
        "hookSpecificOutput": {"hookEventName": event_name, "additionalContext": context},
    }
    if system_message is not None:
        output["systemMessage"] = system_message
    print(
        json.dumps(output, separators=(",", ":")),
        flush=True,
    )


def _emit_block(event_name: str, reason: str) -> None:
    decision = {"continue": False, "stopReason": reason} if event_name == "SessionStart" else {"decision": "block", "reason": reason}
    print(
        json.dumps(
            {
                **decision,
                "systemMessage": "Pisec blocked the turn because broker-authenticated runtime preparation did not succeed.",
            },
            separators=(",", ":"),
        ),
        flush=True,
    )


def main() -> int:
    socket_path = os.environ.get("PISEC_RUNTIME_SOCKET")
    token = os.environ.get("PISEC_RUNTIME_TOKEN")
    workstream = os.environ.get("PISEC_WORKSTREAM_ID")
    instance = os.environ.get("PISEC_RUNTIME_INSTANCE_ID")
    surface = os.environ.get("PISEC_SURFACE_ID")
    harness_home = os.environ.get("PISEC_HARNESS_HOME")
    if not all(isinstance(value, str) and value for value in (socket_path, token, workstream, instance, surface, harness_home)):
        return 0
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeError):
        event = {}
    if not isinstance(event, dict):
        event = {}
    event_name = str(event.get("hook_event_name", ""))
    sequence_path = _sequence_path(harness_home, instance)

    if event_name == "SessionStart":
        source = event.get("source")
        if source is None:
            source = os.environ.get("PISEC_SESSION_START_SOURCE", "startup")
        if source not in {"startup", "resume", "clear", "compact"}:
            _emit_block(event_name, "Pisec received an unsupported Codex session-start source; do not change the project.")
            return 0
        report_event = "session_start" if source in {"startup", "resume"} else "lifecycle"
        if not _report(socket_path, event, state="idle", report_event=report_event, sequence_path=sequence_path):
            _emit_block(event_name, "Pisec session attestation failed; do not change the project.")
            return 0
        turn = _prepare_turn(socket_path)
        if turn is None:
            _emit_block(event_name, "Pisec typed bootstrap preparation failed; do not change the project.")
            return 0
        _emit_hook_context(
            "SessionStart",
            _typed_context(turn, trigger=False),
            system_message=(
                "Pisec delivered broker-authenticated startup context."
                if report_event == "session_start"
                else "Pisec refreshed broker-authenticated context after a Codex session transition."
            ),
        )
        bootstrap = turn.get("bootstrap")
        if isinstance(bootstrap, Mapping):
            _acknowledge_bootstrap(socket_path, bootstrap)
        return 0

    if event_name == "UserPromptSubmit":
        prompt = event.get("prompt")
        trigger = prompt == ATTENTION_TRIGGER
        turn = _prepare_turn(socket_path)
        if turn is None:
            _emit_block(event_name, "Pisec turn preparation failed; do not change the project.")
            return 0
        _emit_hook_context(
            "UserPromptSubmit",
            _typed_context(turn, trigger=trigger),
            system_message="Pisec delivered broker-authenticated attention context." if trigger or bool(turn.get("attention")) else None,
        )
        bootstrap = turn.get("bootstrap")
        if isinstance(bootstrap, Mapping):
            _acknowledge_bootstrap(socket_path, bootstrap)
        _report(socket_path, event, state="working", report_event="lifecycle", sequence_path=sequence_path)
        return 0

    if event_name == "Stop":
        _report(socket_path, event, state="idle", report_event="lifecycle", sequence_path=sequence_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
