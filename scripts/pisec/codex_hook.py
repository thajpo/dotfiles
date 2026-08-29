#!/usr/bin/env python3
"""Codex lifecycle hook bridge for a fenced Pisec worker."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import sys


def main() -> int:
    socket_path = os.environ.get("PISEC_RUNTIME_SOCKET")
    token = os.environ.get("PISEC_RUNTIME_TOKEN")
    workstream = os.environ.get("PISEC_WORKSTREAM_ID")
    instance = os.environ.get("PISEC_RUNTIME_INSTANCE_ID")
    surface = os.environ.get("PISEC_SURFACE_ID")
    harness_home = os.environ.get("PISEC_HARNESS_HOME")
    if not all(isinstance(value, str) and value for value in (socket_path, token, workstream, instance, surface)):
        return 0
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeError):
        event = {}
    event_name = str(event.get("hook_event_name", "")) if isinstance(event, dict) else ""
    state = "working" if event_name == "SessionStart" else "idle"
    native_id = event.get("thread_id") or event.get("session_id") if isinstance(event, dict) else None
    if not isinstance(native_id, str) or not native_id:
        native_id = hashlib.sha256((workstream + instance).encode()).hexdigest()[:32]
    sequence_path = (
        Path(harness_home) / "sessions" / f".pisec-hook-sequence-{hashlib.sha256(instance.encode('utf-8')).hexdigest()}"
        if isinstance(harness_home, str) and harness_home
        else None
    )
    sequence = 1
    if sequence_path is not None:
        try:
            sequence = int(sequence_path.read_text()) + 1 if sequence_path.exists() else 1
        except (OSError, ValueError):
            sequence = 1
    payload = {
        "workstreamId": workstream,
        "runtimeInstanceId": instance,
        "seq": sequence,
        "event": "session_start" if event_name == "SessionStart" else "lifecycle",
        "reason": None,
        "state": state,
        "nativeSessionKind": "id",
        "nativeSessionValue": native_id,
        "startSource": os.environ.get("PISEC_SESSION_START_SOURCE", "startup"),
        "surfaceId": surface,
        "token": token,
        "generation": os.environ.get("PISEC_RUNTIME_GENERATION"),
    }
    request_id = "req_" + hashlib.sha256(f"{workstream}:{instance}:{sequence}".encode("utf-8")).hexdigest()[:32]
    request = {"protocolVersion": 1, "requestId": request_id, "operation": "runtime.report", "payload": payload}
    accepted = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(socket_path)
            client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
            response = json.loads(client.recv(65536).decode())
            result = response.get("result") if isinstance(response, dict) else None
            accepted = bool(
                response.get("requestId") == request_id
                and response.get("ok") is True
                and isinstance(result, dict)
                and result.get("accepted") is True
                and result.get("seq") == sequence
            )
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    if accepted and sequence_path is not None:
        try:
            sequence_path.write_text(str(sequence))
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
