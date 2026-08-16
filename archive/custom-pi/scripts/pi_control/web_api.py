"""Loopback-only HTTP gateway for the Pi Web control plane."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import stat
import sys
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import ControlPlaneError, ErrorCode, NotFoundError
from .controller_channel import ChannelReader, ControllerChannelError, send_frame
from .events import get_events
from .models import new_id, validate_id
from .pi_store import PiStore
from .run_manifest import ManifestError, read_manifest
from .web_projection import build_bootstrap, conversation_timeline, project_bootstrap

API_PREFIX = "/api/v1"
DEFAULT_WEB_ROOT = Path(__file__).resolve().parents[2] / "pi" / "web"
_PROJECT_ID = r"[a-z][a-z0-9]{0,15}_[0-9a-f]{32}"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class PiWebRequestHandler(BaseHTTPRequestHandler):
    server_version = "PiWeb/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Do not include query strings, cookies, or request bodies in logs.
        sys.stderr.write("pi-web: " + (format % args).split("?")[0] + "\n")

    @property
    def gateway(self) -> "PiWebServer":
        return self.server  # type: ignore[return-value]

    def _security_headers(self, content_type: str) -> dict[str, str]:
        return {
            "Content-Type": content_type,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        }

    def _send(self, status: int, value: Any, *, content_type: str = "application/json; charset=utf-8") -> None:
        body = value if isinstance(value, bytes) else _json_bytes(value)
        headers = self._security_headers(content_type)
        self.send_response(status)
        for key, header_value in headers.items():
            self.send_header(key, header_value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str, *, code: str = "CP_INVALID_REQUEST") -> None:
        self._send(status, {"error": {"code": code, "message": message[:1024], "detail": {}}})

    def _authorized(self) -> bool:
        try:
            address = ipaddress.ip_address(self.client_address[0])
            return address.is_loopback
        except ValueError:
            return False

    def _store(self) -> PiStore:
        return PiStore(self.gateway.state_root, read_only=True)

    def do_OPTIONS(self) -> None:
        if not self._authorized():
            self._error(HTTPStatus.FORBIDDEN, "web gateway is loopback-only", code="CP_PERMISSION_INVALID")
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        if not self._authorized():
            self._error(HTTPStatus.FORBIDDEN, "web gateway is loopback-only", code="CP_PERMISSION_INVALID")
            return
        origin = self.headers.get("Origin")
        if not origin or not self._same_origin():
            self._error(HTTPStatus.FORBIDDEN, "cross-origin web mutation is not allowed", code="CP_PERMISSION_INVALID")
            return
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        try:
            self._bridge_command(path)
        except ControlPlaneError as error:
            status = HTTPStatus.NOT_FOUND if isinstance(error, NotFoundError) else HTTPStatus.BAD_REQUEST
            self._send(status, {"error": error.as_dict()})
        except (KeyError, ValueError) as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
        except (OSError, ControllerChannelError):
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "live Pi bridge is unavailable", code="CP_RUNTIME_UNAVAILABLE")

    def _dispatch(self) -> None:
        if not self._authorized():
            self._error(HTTPStatus.FORBIDDEN, "web gateway is loopback-only", code="CP_PERMISSION_INVALID")
            return
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if "\x00" in path:
            self._error(HTTPStatus.BAD_REQUEST, "invalid URL path")
            return
        try:
            if path.startswith(API_PREFIX + "/") or path == API_PREFIX:
                if self.headers.get("Origin") and not self._same_origin():
                    self._error(HTTPStatus.FORBIDDEN, "cross-origin web read is not allowed", code="CP_PERMISSION_INVALID")
                    return
                self._api(path, parse_qs(parsed.query, keep_blank_values=False))
            else:
                self._asset(path)
        except ControlPlaneError as error:
            status = HTTPStatus.NOT_FOUND if isinstance(error, NotFoundError) else HTTPStatus.BAD_REQUEST
            self._send(status, {"error": error.as_dict()})
        except (KeyError, ValueError) as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
        except (OSError, ControllerChannelError):
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "controller state is temporarily unavailable", code="CP_RUNTIME_UNAVAILABLE")

    def _api(self, path: str, query: dict[str, list[str]]) -> None:
        if path == API_PREFIX or path == API_PREFIX + "/bootstrap":
            with self._store() as store:
                result = {"controller": store.controller_identity(), "data": build_bootstrap(store, include_timeline=False)}
            self._send(HTTPStatus.OK, result)
            return
        if path == API_PREFIX + "/health":
            with self._store() as store:
                result = {"ok": True, "controller": store.controller_identity(), "schema": store.schema_status().as_dict()}
            self._send(HTTPStatus.OK, result)
            return
        if path == API_PREFIX + "/projects":
            with self._store() as store:
                self._send(HTTPStatus.OK, {"projects": build_bootstrap(store, include_timeline=False)["projects"]})
            return
        project_match = re.fullmatch(API_PREFIX + r"/projects/(" + _PROJECT_ID + r")", path)
        if project_match:
            with self._store() as store:
                self._send(HTTPStatus.OK, project_bootstrap(store, project_match.group(1)))
            return
        conversation_match = re.fullmatch(API_PREFIX + r"/projects/(" + _PROJECT_ID + r")/conversations/(conv_[0-9a-f]{32})/timeline", path)
        if conversation_match:
            after = query.get("after", [None])[0]
            limit = int(query.get("limit", ["512"])[0])
            with self._store() as store:
                self._send(HTTPStatus.OK, conversation_timeline(store, conversation_match.group(1), conversation_match.group(2), after=after, limit=limit))
            return
        bridge_state = re.fullmatch(API_PREFIX + r"/projects/(" + _PROJECT_ID + r")/conversations/(conv_[0-9a-f]{32})/bridge/state", path)
        if bridge_state:
            with self._store() as store:
                self._bridge_state(store, bridge_state.group(1), bridge_state.group(2))
            return
        bridge_stream = re.fullmatch(API_PREFIX + r"/projects/(" + _PROJECT_ID + r")/conversations/(conv_[0-9a-f]{32})/bridge/stream", path)
        if bridge_stream:
            with self._store() as store:
                self._bridge_stream(store, bridge_stream.group(1), bridge_stream.group(2), after_event_id=query.get("afterEventId", [None])[0])
            return
        if path == API_PREFIX + "/events":
            after = int(query.get("after", ["0"])[0])
            limit = min(256, int(query.get("limit", ["64"])[0]))
            with self._store() as store:
                events = [event.as_dict() for event in get_events(store, after=after, limit=limit)]
            self._send(HTTPStatus.OK, {"events": events, "after": after})
            return
        self._error(HTTPStatus.NOT_FOUND, "web API route was not found", code="CP_NOT_FOUND")

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = self.gateway.server_port
        return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}

    def _request_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("web mutation requires a bounded Content-Length")
        length = int(raw_length)
        if length < 1 or length > 64 * 1024:
            raise ValueError("web mutation body exceeds its bound")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("web mutation body must be an object")
        return value

    def _live_run(self, store: PiStore, project_id: str, conversation_id: str) -> tuple[Any, dict[str, Any]]:
        validate_id(project_id, prefix="prj")
        validate_id(conversation_id, prefix="conv")
        row = store.conn.execute(
            "SELECT r.*,c.pi_session_id FROM runs r JOIN conversations c ON c.conversation_id=r.conversation_id "
            "WHERE r.project_id=? AND r.conversation_id=? AND r.desired_state='running' "
            "AND r.observed_state IN ('ready','running','needs_attention') ORDER BY r.updated_at DESC LIMIT 1",
            (project_id, conversation_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("conversation has no live Pi run")
        descriptor_root = (store.state_root / "web-bridges").absolute()
        descriptor_path = descriptor_root / f"{row['run_id']}.json"
        try:
            info = descriptor_path.lstat()
        except FileNotFoundError as error:
            raise NotFoundError("conversation live bridge has not started") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 64 * 1024:
            raise ValueError("conversation live bridge descriptor is unsafe")
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        if not isinstance(descriptor, dict):
            raise ValueError("conversation live bridge descriptor is invalid")
        identity = store.controller_identity()
        if row["child_pid"] is None or not row["child_start_identity"] or not row["manifest_path"]:
            raise ControlPlaneError("conversation live bridge identity is incomplete", code=ErrorCode.BRIDGE_STALE)
        manifest_path = (store.state_root / "runs" / str(row["run_id"]) / "manifest.json").absolute()
        if Path(str(row["manifest_path"])).absolute() != manifest_path:
            raise ControlPlaneError("conversation live bridge manifest binding is stale", code=ErrorCode.BRIDGE_STALE)
        try:
            manifest = read_manifest(manifest_path).manifest
        except (OSError, ManifestError) as error:
            raise ControlPlaneError("conversation live bridge manifest is unavailable", code=ErrorCode.BRIDGE_STALE) from error
        expected = {
            "runId": row["run_id"], "conversationId": conversation_id, "projectId": project_id,
            "sessionId": row["pi_session_id"], "controllerBuildId": identity["buildId"],
            "runBuildId": row["build_id"], "manifestDigest": manifest["manifestDigest"],
            "childPid": int(row["child_pid"]), "childStartIdentity": row["child_start_identity"],
            "restartEpoch": identity["restartEpoch"],
        }
        if any(descriptor.get(key) != value for key, value in expected.items()) or not isinstance(descriptor.get("capability"), str) or not descriptor["capability"]:
            raise ControlPlaneError("conversation live bridge identity is stale", code=ErrorCode.BRIDGE_STALE)
        socket_path = descriptor.get("socketPath") or str(descriptor_root / f"{row['run_id']}.sock")
        if socket_path != str(descriptor_root / f"{row['run_id']}.sock"):
            raise ValueError("conversation live bridge socket is outside its run root")
        socket_info = Path(socket_path).lstat()
        if stat.S_ISLNK(socket_info.st_mode) or not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != os.geteuid() or stat.S_IMODE(socket_info.st_mode) & 0o077:
            raise ValueError("conversation live bridge socket is unsafe")
        return row, descriptor

    @staticmethod
    def _connect_bridge(row: Any, descriptor: dict[str, Any]) -> tuple[socket.socket, ChannelReader]:
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        channel.settimeout(3.0)
        reader = ChannelReader(channel)
        channel.connect(str(descriptor.get("socketPath") or ""))
        send_frame(channel, {"protocolVersion": 2, "type": "connect", "runId": descriptor["runId"], "conversationId": descriptor["conversationId"], "projectId": descriptor["projectId"], "sessionId": descriptor["sessionId"], "controllerBuildId": descriptor["controllerBuildId"], "runBuildId": descriptor["runBuildId"], "manifestDigest": descriptor["manifestDigest"], "childPid": descriptor["childPid"], "childStartIdentity": descriptor["childStartIdentity"], "restartEpoch": descriptor["restartEpoch"], "capability": descriptor["capability"]})
        reply = reader.receive(timeout=3.0)
        expected = {"protocolVersion": 2, "type": "connected", "runId": row["run_id"], "conversationId": row["conversation_id"], "projectId": descriptor["projectId"], "sessionId": row["pi_session_id"], "controllerBuildId": descriptor["controllerBuildId"], "runBuildId": row["build_id"], "manifestDigest": descriptor["manifestDigest"], "childPid": int(row["child_pid"]), "childStartIdentity": row["child_start_identity"], "restartEpoch": descriptor["restartEpoch"]}
        if any(reply.get(key) != value for key, value in expected.items()):
            channel.close()
            raise ControllerChannelError("live bridge handshake identity is invalid")
        channel.settimeout(None)
        return channel, reader

    def _bridge_command(self, path: str) -> None:
        match = re.fullmatch(API_PREFIX + r"/projects/(" + _PROJECT_ID + r")/conversations/(conv_[0-9a-f]{32})/bridge", path)
        if not match:
            self._error(HTTPStatus.NOT_FOUND, "web API route was not found", code="CP_NOT_FOUND")
            return
        body = self._request_body()
        if set(body) - {"operation", "text", "deliverAs", "idempotencyKey", "inputId", "model", "thinkingLevel"} or body.get("operation") not in {"prompt", "removeQueued", "stop", "compact", "setModel", "setThinking"}:
            raise ValueError("web bridge operation is invalid")
        idempotency_key = body.get("idempotencyKey")
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 128 or "\x00" in idempotency_key:
            raise ValueError("web bridge idempotency key is invalid")
        operation = body["operation"]
        if operation == "prompt":
            if set(body) - {"operation", "text", "deliverAs", "idempotencyKey"}:
                raise ValueError("prompt operation has unexpected fields")
            if not isinstance(body.get("text"), str) or not body["text"].strip() or len(body["text"].encode("utf-8")) > 16 * 1024:
                raise ValueError("prompt is empty or exceeds its bound")
            if body.get("deliverAs") not in {None, "steer", "followUp"}:
                raise ValueError("prompt delivery mode is invalid")
        elif operation == "removeQueued":
            if set(body) - {"operation", "idempotencyKey", "inputId"}:
                raise ValueError("remove queued operation has unexpected fields")
            if not isinstance(body.get("inputId"), str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", body["inputId"]):
                raise ValueError("queued input ID is invalid")
        elif operation in {"stop", "compact"}:
            if set(body) - {"operation", "idempotencyKey"}:
                raise ValueError("bridge operation has unexpected fields")
        elif operation == "setModel":
            model = body.get("model")
            if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}/[A-Za-z0-9._:-]{1,128}", model):
                raise ValueError("model selection is invalid")
            if set(body) - {"operation", "idempotencyKey", "model"}:
                raise ValueError("model operation has unexpected fields")
        elif operation == "setThinking":
            if body.get("thinkingLevel") not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}:
                raise ValueError("thinking level is invalid")
            if set(body) - {"operation", "idempotencyKey", "thinkingLevel"}:
                raise ValueError("thinking operation has unexpected fields")
        with self._store() as store:
            row, descriptor = self._live_run(store, match.group(1), match.group(2))
        channel, reader = self._connect_bridge(row, descriptor)
        request_id = new_id("web")
        request = {"protocolVersion": 2, "type": "command", "requestId": request_id, "idempotencyKey": idempotency_key, "operation": operation}
        if operation == "prompt":
            request["text"] = body["text"].strip()
            if body.get("deliverAs") is not None:
                request["deliverAs"] = body["deliverAs"]
        elif operation == "removeQueued":
            request["inputId"] = body["inputId"]
        elif operation == "setModel":
            request["model"] = body["model"]
        elif operation == "setThinking":
            request["thinkingLevel"] = body["thinkingLevel"]
        try:
            send_frame(channel, request)
            response = reader.receive(timeout=5.0)
        finally:
            channel.close()
        if response.get("requestId") != request_id or response.get("type") not in {"accepted", "pending", "uncertain", "rejected"}:
            raise ControllerChannelError("live bridge response identity is invalid")
        if response["type"] == "rejected":
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            self._send(HTTPStatus.CONFLICT, {"error": {"code": error.get("code", ErrorCode.CONVERSATION_CONFLICT), "message": error.get("message", "live conversation rejected the request"), "detail": error.get("detail", {})}})
        else:
            self._send(HTTPStatus.ACCEPTED if response["type"] in {"pending", "uncertain"} else HTTPStatus.OK, response)

    def _bridge_state(self, store: PiStore, project_id: str, conversation_id: str) -> None:
        row, descriptor = self._live_run(store, project_id, conversation_id)
        channel, reader = self._connect_bridge(row, descriptor)
        request_id = new_id("web")
        try:
            send_frame(channel, {"protocolVersion": 2, "type": "command", "requestId": request_id, "operation": "state"})
            response = reader.receive(timeout=3.0)
        finally:
            channel.close()
        if response.get("requestId") != request_id or response.get("type") != "state" or not isinstance(response.get("state"), dict):
            raise ControllerChannelError("live bridge state response identity is invalid")
        self._send(HTTPStatus.OK, response["state"])

    def _bridge_stream(self, store: PiStore, project_id: str, conversation_id: str, *, after_event_id: str | None = None) -> None:
        self.close_connection = True
        try:
            row, descriptor = self._live_run(store, project_id, conversation_id)
        except ControlPlaneError as error:
            if error.code != ErrorCode.BRIDGE_STALE:
                raise
            self.send_response(HTTPStatus.OK)
            for key, value in self._security_headers("text/event-stream; charset=utf-8").items():
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                return
            payload = json.dumps({"type": "bridge_stale", "message": error.message}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()
            return
        channel, reader = self._connect_bridge(row, descriptor)
        subscribe = {"protocolVersion": 2, "type": "subscribe", "requestId": new_id("web")}
        last_event_id = self.headers.get("Last-Event-ID") or after_event_id
        if last_event_id:
            subscribe["afterEventId"] = last_event_id[:256]
        send_frame(channel, subscribe)
        subscription = reader.receive(timeout=3.0)
        if subscription.get("type") != "subscribed":
            channel.close()
            raise ControllerChannelError("live bridge subscription was rejected")
        self.send_response(HTTPStatus.OK)
        for key, value in self._security_headers("text/event-stream; charset=utf-8").items():
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            channel.close()
            return
        deadline = time.monotonic() + 25.0
        try:
            while time.monotonic() < deadline:
                try:
                    event = reader.receive(timeout=1.0)
                except ControllerChannelError as error:
                    if "timed out" in str(error):
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue
                    break
                if event.get("type") != "event":
                    continue
                payload = json.dumps(event.get("event", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                event_id = event.get("eventId")
                if isinstance(event_id, str) and event_id:
                    self.wfile.write(f"id: {event_id}\n".encode("utf-8"))
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(b'data: {"type":"stream_end"}\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            channel.close()

    def _asset(self, path: str) -> None:
        relative = path.lstrip("/") or "index.html"
        root = self.gateway.web_root.resolve(strict=True)
        candidate = root / relative
        try:
            if stat.S_ISLNK(candidate.lstat().st_mode):
                self._error(HTTPStatus.NOT_FOUND, "asset was not found", code="CP_NOT_FOUND")
                return
        except FileNotFoundError:
            self._error(HTTPStatus.NOT_FOUND, "asset was not found", code="CP_NOT_FOUND")
            return
        asset = candidate.resolve(strict=False)
        if root not in asset.parents and asset != root:
            self._error(HTTPStatus.NOT_FOUND, "asset was not found", code="CP_NOT_FOUND")
            return
        if not asset.is_file() or asset.is_symlink():
            self._error(HTTPStatus.NOT_FOUND, "asset was not found", code="CP_NOT_FOUND")
            return
        content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
        self._send(HTTPStatus.OK, asset.read_bytes(), content_type=content_type)


class PiWebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], *, state_root: Path, web_root: Path):
        host = ipaddress.ip_address(address[0])
        if not host.is_loopback:
            raise ValueError("Pi Web must bind to a loopback address")
        self.state_root = state_root
        self.web_root = web_root
        super().__init__(address, PiWebRequestHandler)


def serve(*, state_root: Path | None = None, web_root: Path | None = None, host: str = "127.0.0.1", port: int = 8787) -> None:
    root = (web_root or DEFAULT_WEB_ROOT).resolve(strict=True)
    state = (state_root or Path(os.environ.get("PI_SYSTEM_STATE_ROOT", "~/.local/state/pi-system")).expanduser()).resolve()
    with PiStore(state, read_only=True):
        pass
    with PiWebServer((host, port), state_root=state, web_root=root) as server:
        print(f"Pi Web listening on http://{host}:{port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="pi-web-gateway")
    parser.add_argument("--state-root", default=os.environ.get("PI_SYSTEM_STATE_ROOT"))
    parser.add_argument("--web-root", default=str(DEFAULT_WEB_ROOT))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PI_WEB_PORT", "8787")))
    args = parser.parse_args(argv)
    serve(state_root=Path(args.state_root).expanduser() if args.state_root else None, web_root=Path(args.web_root).expanduser(), host=args.host, port=args.port)
    return 0


__all__ = ["PiWebServer", "main", "serve"]
