"""Strict newline-delimited Pisec Unix socket protocol."""

from __future__ import annotations

import socket
import re
from pathlib import Path
from typing import Any, Mapping

from .models import InvalidRequestError, PisecError, canonical_json, new_id, parse_json_strict, validate_id

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024
REQUEST_FIELDS = frozenset({"protocolVersion", "requestId", "operation", "payload"})
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class BrokerResponseError(PisecError):
    """Expected error returned by a Pisec broker."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message, detail={"code": code})
        self.code = code


def validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != REQUEST_FIELDS:
        raise InvalidRequestError("request fields do not match protocol version 1")
    if value["protocolVersion"] != PROTOCOL_VERSION:
        raise InvalidRequestError("unsupported protocol version")
    validate_id(value["requestId"], prefix="req")
    if not isinstance(value["operation"], str) or not value["operation"] or len(value["operation"]) > 128:
        raise InvalidRequestError("operation is invalid")
    if not isinstance(value["payload"], dict):
        raise InvalidRequestError("payload must be an object")
    return value


def decode_request(raw: bytes) -> dict[str, Any]:
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise InvalidRequestError("request must contain exactly one newline-terminated JSON value")
    if len(raw) > MAX_MESSAGE_BYTES:
        raise InvalidRequestError("request is too large")
    return validate_request(parse_json_strict(raw[:-1]))


def _wire(document: Mapping[str, Any]) -> bytes:
    encoded = (canonical_json(document) + "\n").encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise InvalidRequestError("response is too large")
    return encoded


def success_response(request_id: str, result: Any) -> bytes:
    try:
        return _wire({"protocolVersion": PROTOCOL_VERSION, "requestId": request_id, "ok": True, "result": result})
    except BaseException as error:
        return error_response(request_id, error if isinstance(error, PisecError) else InvalidRequestError("response is too large"))


def error_response(request_id: str | None, error: BaseException) -> bytes:
    if isinstance(error, PisecError):
        code = error.code
        message = error.message
    else:
        code, message = "internal_error", "internal broker error"
    safe_id = request_id if isinstance(request_id, str) and request_id.startswith("req_") else "req_00000000000000000000000000000000"
    return _wire({"protocolVersion": PROTOCOL_VERSION, "requestId": safe_id, "ok": False, "error": {"code": code, "message": str(message)[:512]}})


def request(socket_path: Path | str, operation: str, payload: Mapping[str, Any], *, timeout: float = 30.0) -> Any:
    request_id = new_id("req")
    wire = (canonical_json({"protocolVersion": PROTOCOL_VERSION, "requestId": request_id, "operation": operation, "payload": dict(payload)}) + "\n").encode("utf-8")
    if len(wire) > MAX_MESSAGE_BYTES:
        raise InvalidRequestError("request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall(wire)
        client.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = client.recv(8192)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_MESSAGE_BYTES:
                raise InvalidRequestError("response is too large")
            chunks.append(chunk)
    response = parse_json_strict(b"".join(chunks).rstrip(b"\n"))
    if not isinstance(response, dict) or response.get("protocolVersion") != 1 or response.get("requestId") != request_id or not isinstance(response.get("ok"), bool):
        raise InvalidRequestError("broker returned an invalid response")
    if response["ok"]:
        return response.get("result")
    error = response.get("error")
    if not isinstance(error, dict):
        raise InvalidRequestError("broker returned an invalid error")
    code = error.get("code")
    if not isinstance(code, str) or _ERROR_CODE_RE.fullmatch(code) is None:
        raise InvalidRequestError("broker returned an invalid error code")
    message = error.get("message", "broker request failed")
    if not isinstance(message, str):
        raise InvalidRequestError("broker returned an invalid error message")
    raise BrokerResponseError(message, code=code)
