#!/usr/bin/env python3
"""Loopback compatibility gateway for Codex and the OMP auth gateway.

Codex sends valid Responses API items that the OMP auth gateway does not yet
accept.  This proxy keeps the authenticated OMP gateway as the authority and
only translates the two incompatible wire shapes.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from typing import Any, Mapping


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class RequestRewriteError(ValueError):
    """The request cannot be translated without changing its meaning."""


def _flat_tool_name(namespace: str, name: str) -> str:
    return f"{namespace}__{name}"


def rewrite_responses_request(body: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, tuple[str, str]]]:
    """Return an OMP-compatible copy and its reversible namespace map."""

    rewritten = deepcopy(dict(body))
    namespace_map: dict[str, tuple[str, str]] = {}

    request_input = rewritten.get("input")
    if isinstance(request_input, list):
        for item in request_input:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "reasoning" and item.get("content") is None:
                item.pop("content", None)
            if item.get("type") == "function_call":
                namespace = item.get("namespace")
                name = item.get("name")
                if isinstance(namespace, str) and namespace and isinstance(name, str) and name:
                    item["name"] = _flat_tool_name(namespace, name)
                    item.pop("namespace", None)

    tools = rewritten.get("tools")
    if not isinstance(tools, list):
        return rewritten, namespace_map

    flattened: list[Any] = []
    callable_names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "namespace":
            if isinstance(tool, dict) and tool.get("type") == "function" and isinstance(tool.get("name"), str):
                name = str(tool["name"])
                if name in callable_names:
                    raise RequestRewriteError(f"duplicate function tool name: {name}")
                callable_names.add(name)
            flattened.append(tool)
            continue

        namespace = tool.get("name")
        nested_tools = tool.get("tools")
        if not isinstance(namespace, str) or not namespace or not isinstance(nested_tools, list):
            raise RequestRewriteError("namespace tool has an invalid name or tool list")
        for nested in nested_tools:
            if not isinstance(nested, dict) or nested.get("type") != "function" or not isinstance(nested.get("name"), str) or not nested["name"]:
                raise RequestRewriteError(f"namespace {namespace} contains a non-function tool")
            original_name = str(nested["name"])
            flat_name = _flat_tool_name(namespace, original_name)
            if flat_name in callable_names or flat_name in namespace_map:
                raise RequestRewriteError(f"flattened namespace tool name collides: {flat_name}")
            callable_names.add(flat_name)
            namespace_map[flat_name] = (namespace, original_name)
            flat_tool = deepcopy(nested)
            flat_tool["name"] = flat_name
            flattened.append(flat_tool)

    rewritten["tools"] = flattened
    return rewritten, namespace_map


def restore_responses_namespaces(value: Any, namespace_map: Mapping[str, tuple[str, str]]) -> int:
    """Restore Codex namespace fields in JSON response items in place."""

    restored = 0
    if isinstance(value, dict):
        name = value.get("name")
        if value.get("type") == "function_call" and isinstance(name, str) and name in namespace_map:
            namespace, original_name = namespace_map[name]
            value["name"] = original_name
            value["namespace"] = namespace
            restored += 1
        for child in value.values():
            restored += restore_responses_namespaces(child, namespace_map)
    elif isinstance(value, list):
        for child in value:
            restored += restore_responses_namespaces(child, namespace_map)
    return restored


def rewrite_sse_line(line: bytes, namespace_map: Mapping[str, tuple[str, str]]) -> bytes:
    """Rewrite one SSE data line while preserving its original line ending."""

    stripped = line.rstrip(b"\r\n")
    ending = line[len(stripped) :]
    if not stripped.startswith(b"data:"):
        return line
    payload = stripped[5:].lstrip()
    if not payload or payload == b"[DONE]":
        return line
    try:
        event = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return line
    restore_responses_namespaces(event, namespace_map)
    return b"data: " + json.dumps(event, separators=(",", ":")).encode() + ending


class ResponsesGatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], upstream: tuple[str, int]):
        self.upstream = upstream
        super().__init__(server_address, ResponsesGatewayHandler)


class ResponsesGatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PisecResponsesGateway/1"

    def log_message(self, format: str, *args: object) -> None:
        print(f"pisec responses gateway: {format % args}", file=sys.stderr, flush=True)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._proxy()

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._proxy()

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._proxy()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._proxy()

    def _json_error(self, status: int, message: str) -> None:
        payload = json.dumps({"error": {"message": message, "type": "invalid_request_error"}}, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.send_header("connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        self.close_connection = True

    def _read_request_body(self) -> bytes:
        transfer_encoding = self.headers.get("transfer-encoding")
        if transfer_encoding:
            raise RequestRewriteError("chunked request bodies are not supported")
        raw_length = self.headers.get("content-length")
        if raw_length is None:
            return b""
        try:
            length = int(raw_length)
        except ValueError as error:
            raise RequestRewriteError("request content-length is invalid") from error
        if length < 0 or length > 64 * 1024 * 1024:
            raise RequestRewriteError("request body size is invalid")
        return self.rfile.read(length)

    def _proxy(self) -> None:
        try:
            request_body = self._read_request_body()
            namespace_map: dict[str, tuple[str, str]] = {}
            if self.command == "POST" and self.path.split("?", 1)[0].rstrip("/") == "/v1/responses":
                try:
                    document = json.loads(request_body)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise RequestRewriteError("Responses request body is not valid JSON") from error
                if not isinstance(document, dict):
                    raise RequestRewriteError("Responses request body must be an object")
                document, namespace_map = rewrite_responses_request(document)
                request_body = json.dumps(document, separators=(",", ":")).encode()
        except RequestRewriteError as error:
            self._json_error(400, str(error))
            return

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_BY_HOP_HEADERS | {"host", "content-length", "accept-encoding"}
        }
        headers["accept-encoding"] = "identity"
        if request_body or self.command in {"POST", "PUT"}:
            headers["content-length"] = str(len(request_body))

        server = self.server
        assert isinstance(server, ResponsesGatewayServer)
        upstream = http.client.HTTPConnection(*server.upstream, timeout=600)
        try:
            upstream.request(self.command, self.path, body=request_body or None, headers=headers)
            response = upstream.getresponse()
            content_type = response.getheader("content-type", "")
            if "text/event-stream" in content_type.lower():
                self._stream_sse(response, namespace_map)
            else:
                self._copy_buffered(response, namespace_map)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except (OSError, http.client.HTTPException) as error:
            self.log_error("upstream request failed: %s", error)
            if not self.wfile.closed:
                try:
                    self._json_error(502, "authenticated inference gateway is unavailable")
                except OSError:
                    self.close_connection = True
        finally:
            upstream.close()

    def _send_upstream_headers(self, response: http.client.HTTPResponse, *, content_length: int | None) -> None:
        self.send_response(response.status, response.reason)
        for key, value in response.getheaders():
            if key.lower() not in _HOP_BY_HOP_HEADERS | {"content-length"}:
                self.send_header(key, value)
        if content_length is not None:
            self.send_header("content-length", str(content_length))
        else:
            self.send_header("connection", "close")
            self.close_connection = True
        self.end_headers()

    def _copy_buffered(self, response: http.client.HTTPResponse, namespace_map: Mapping[str, tuple[str, str]]) -> None:
        payload = response.read()
        content_type = response.getheader("content-type", "").lower()
        if payload and "application/json" in content_type and namespace_map:
            try:
                document = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            else:
                restore_responses_namespaces(document, namespace_map)
                payload = json.dumps(document, separators=(",", ":")).encode()
        self._send_upstream_headers(response, content_length=len(payload))
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _stream_sse(self, response: http.client.HTTPResponse, namespace_map: Mapping[str, tuple[str, str]]) -> None:
        self._send_upstream_headers(response, content_length=None)
        if self.command == "HEAD":
            return
        while True:
            line = response.readline()
            if not line:
                break
            self.wfile.write(rewrite_sse_line(line, namespace_map))
            self.wfile.flush()


def _endpoint(value: str) -> tuple[str, int]:
    host, separator, raw_port = value.rpartition(":")
    if separator != ":" or host != "127.0.0.1":
        raise argparse.ArgumentTypeError("endpoint must use 127.0.0.1 and an explicit port")
    try:
        port = int(raw_port)
    except ValueError as error:
        raise argparse.ArgumentTypeError("endpoint port is invalid") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("endpoint port is invalid")
    return host, port


def serve(bind: tuple[str, int], upstream: tuple[str, int], backend: list[str]) -> int:
    if bind == upstream:
        raise SystemExit("public and upstream gateway endpoints must differ")
    if not backend:
        raise SystemExit("backend command is required")
    executable = Path(backend[0])
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise SystemExit("backend executable is unavailable or unsafe")

    server = ResponsesGatewayServer(bind, upstream)
    child = subprocess.Popen(backend, close_fds=True)
    serving = threading.Thread(target=server.serve_forever, name="pisec-responses-gateway", daemon=True)
    serving.start()
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        server.shutdown()
        if child.poll() is None:
            child.terminate()

    prior_handlers = {name: signal.signal(name, stop) for name in (signal.SIGINT, signal.SIGTERM)}
    try:
        return_code = child.wait()
    finally:
        server.shutdown()
        server.server_close()
        serving.join(timeout=5)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for name, prior in prior_handlers.items():
            signal.signal(name, prior)
    return 0 if stopping else return_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", required=True, type=_endpoint)
    parser.add_argument("--upstream", required=True, type=_endpoint)
    parser.add_argument("backend", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    backend = list(args.backend)
    if backend[:1] == ["--"]:
        backend = backend[1:]
    return serve(args.bind, args.upstream, backend)


if __name__ == "__main__":
    raise SystemExit(main())
