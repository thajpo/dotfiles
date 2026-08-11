"""Bounded canonical framing for the inherited host Pi controller channel."""

from __future__ import annotations

import json

import select
import socket
from typing import Any, Mapping

from .models import canonical_json


PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024
# Channel frames carry live tool payloads (file contents, command output) that
# legitimately exceed the 4096-char controller-record text bound. The per-text
# limit for channel JSON is the frame bound itself; the encoded total is still
# capped by MAX_FRAME_BYTES in send_frame.
_CHANNEL_MAX_TEXT = MAX_FRAME_BYTES


class ControllerChannelError(RuntimeError):
    pass


def send_frame(channel: socket.socket, value: Mapping[str, Any]) -> None:
    body = canonical_json(dict(value), max_text=_CHANNEL_MAX_TEXT).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise ControllerChannelError("controller channel frame exceeds its bound")
    try:
        channel.sendall(body + b"\n")
    except (ConnectionResetError, BrokenPipeError) as error:
        raise ControllerChannelError("controller channel closed before the frame was sent") from error


def receive_frame(channel: socket.socket, *, timeout: float) -> dict[str, Any]:
    chunks = bytearray()
    while True:
        readable, _, _ = select.select([channel], [], [], timeout)
        if not readable:
            raise ControllerChannelError("controller channel handshake timed out")
        try:
            block = channel.recv(min(4096, MAX_FRAME_BYTES + 2 - len(chunks)))
        except (ConnectionResetError, BrokenPipeError) as error:
            raise ControllerChannelError("controller channel closed before a complete frame") from error
        if not block:
            raise ControllerChannelError("controller channel closed before a complete frame")
        chunks.extend(block)
        newline = chunks.find(b"\n")
        if newline >= 0:
            if newline != len(chunks) - 1:
                raise ControllerChannelError("controller channel sent trailing frame bytes")
            body = bytes(chunks[:newline])
            break
        if len(chunks) > MAX_FRAME_BYTES:
            raise ControllerChannelError("controller channel frame exceeds its bound")
    return _decode_frame(body)


class ChannelReader:
    """Buffered reader for the host Pi controller channel.

    The child can pipeline several frames into one send; receive_frame treats
    any extra bytes as corruption. This reader keeps the leftover bytes across
    calls so each pipelined frame is delivered exactly once and in order.
    """

    def __init__(self, channel: socket.socket) -> None:
        self._channel = channel
        self._buffer = bytearray()

    def receive(self, *, timeout: float) -> dict[str, Any]:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                body = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                if len(self._buffer) > MAX_FRAME_BYTES:
                    raise ControllerChannelError("controller channel buffered frame exceeds its bound")
                return _decode_frame(body)
            if len(self._buffer) > MAX_FRAME_BYTES:
                raise ControllerChannelError("controller channel frame exceeds its bound")
            readable, _, _ = select.select([self._channel], [], [], timeout)
            if not readable:
                raise ControllerChannelError("controller channel handshake timed out")
            try:
                block = self._channel.recv(min(4096, MAX_FRAME_BYTES + 2 - len(self._buffer)))
            except (ConnectionResetError, BrokenPipeError) as error:
                raise ControllerChannelError("controller channel closed before a complete frame") from error
            if not block:
                raise ControllerChannelError("controller channel closed before a complete frame")
            self._buffer.extend(block)


def _decode_frame(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControllerChannelError("controller channel frame is not UTF-8 JSON") from error
    if not isinstance(value, dict) or canonical_json(value, max_text=_CHANNEL_MAX_TEXT).encode("utf-8") != body:
        raise ControllerChannelError("controller channel frame is not canonical JSON")
    return value


def validate_handshake(handshake: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "protocolVersion", "type", "runId", "manifestDigest", "childPid",
        "childStartIdentity", "role", "sessionId", "sessionPath",
        "activeTools", "toolSources", "loadedResources",
    }
    if not isinstance(handshake, Mapping) or set(handshake) != keys:
        raise ControllerChannelError("startup handshake fields do not match the protocol")
    if handshake["protocolVersion"] != PROTOCOL_VERSION or handshake["type"] != "startup":
        raise ControllerChannelError("startup handshake protocol is invalid")
    for key in ("runId", "manifestDigest", "childPid", "childStartIdentity", "role", "sessionId", "sessionPath"):
        if handshake[key] != expected[key]:
            raise ControllerChannelError(f"startup handshake mismatch: {key}")
    tools = handshake["activeTools"]
    if tools != expected["activeTools"]:
        raise ControllerChannelError("startup handshake active tools differ from the role profile")
    if handshake["toolSources"] != expected["toolSources"]:
        raise ControllerChannelError("startup handshake tool sources differ from staged resources")
    if handshake["loadedResources"] != expected["loadedResources"]:
        raise ControllerChannelError("startup handshake resource identity differs from staged resources")
    return dict(handshake)


__all__ = [
    "ChannelReader", "ControllerChannelError", "MAX_FRAME_BYTES", "PROTOCOL_VERSION",
    "receive_frame", "send_frame", "validate_handshake",
]
