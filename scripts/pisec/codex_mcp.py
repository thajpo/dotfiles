#!/usr/bin/env python3
"""Minimal stdio MCP server exposing the authenticated Pisec worker tools."""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any

try:
    from .operation_catalogue_generated import SOCKET_OPERATIONS
except ImportError:
    from operation_catalogue_generated import SOCKET_OPERATIONS


TOOLS = {
    "pisec_show_task_packet": ("task.get", {}),
    "pisec_checkpoint_workstream": ("workstream.checkpoint", {"type": "object"}),
    "pisec_request_help": ("help.request", {"type": "object"}),
    "pisec_list_coordination": ("coordination.list", {"type": "object"}),
    "pisec_inspect_coordination": ("coordination.inspect", {"type": "object"}),
    "pisec_request_secretary_research": ("research.request", {"type": "object"}),
    "pisec_check_secretary_research": ("research.list", {"type": "object"}),
    "pisec_inspect_secretary_research": ("research.inspect", {"type": "object"}),
    "pisec_add_secretary_research_context": ("research.add_context", {"type": "object"}),
    "pisec_acknowledge_secretary_research": ("research.acknowledge", {"type": "object"}),
    "pisec_list_attention": ("attention.list", {"type": "object"}),
    "pisec_inspect_attention": ("attention.inspect", {"type": "object"}),
    "pisec_report_issue": ("issue.report", {"type": "object"}),
    "pisec_list_issues": ("issue.list", {"type": "object"}),
    "pisec_inspect_issue": ("issue.inspect", {"type": "object"}),
    "pisec_add_issue_context": ("issue.add_context", {"type": "object"}),
    "pisec_verify_issue": ("issue.verify", {"type": "object"}),
}

TOOL_DESCRIPTIONS = {
    "pisec_show_task_packet": "Use when you need the immutable assigned outcome and boundaries; reads the authoritative task packet.",
    "pisec_checkpoint_workstream": "Use to record typed progress or the final ready_review evidence; creates the durable checkpoint transition.",
    "pisec_request_help": "Use when blocked, clarifying, reviewing, or needing access; creates the single typed upward-help source.",
    "pisec_list_attention": "Use at turn start and before ending a turn; lists current authorized attention references without acknowledging them.",
    "pisec_inspect_attention": "Use after attention.list to identify the typed source and its existing inspector; read-only.",
    "pisec_report_issue": "Use for access, tooling, lifecycle, or permission failures; records a canonical issue for the project Secretary.",
    "pisec_verify_issue": "Use when the Secretary asks you to verify a remediation; closes the reporter revision or reopens supervisor attention.",
}

if any(operation not in SOCKET_OPERATIONS["runtime"] for operation, _schema in TOOLS.values()):
    raise RuntimeError("Codex Pisec MCP tools are absent from the generated operation catalogue")


def _request(operation: str, params: dict[str, Any]) -> Any:
    socket_path = os.environ.get("PISEC_RUNTIME_SOCKET")
    token = os.environ.get("PISEC_RUNTIME_TOKEN")
    workstream = os.environ.get("PISEC_WORKSTREAM_ID")
    instance = os.environ.get("PISEC_RUNTIME_INSTANCE_ID")
    surface = os.environ.get("PISEC_SURFACE_ID")
    if not all(isinstance(value, str) and value for value in (socket_path, token, workstream, instance, surface)):
        raise RuntimeError("Pisec runtime binding is incomplete")
    payload = {"workstreamId": workstream, "runtimeInstanceId": instance, "surfaceId": surface, "token": token, **params}
    request = {"protocolVersion": 1, "requestId": "codex-mcp", "operation": operation, "payload": payload}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(30)
        client.connect(socket_path)
        client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
        response = b""
        while not response.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
            if len(response) > 128 * 1024:
                raise RuntimeError("Pisec response is too large")
    value = json.loads(response.decode())
    if not value.get("ok"):
        error = value.get("error")
        raise RuntimeError(str(error.get("message", "Pisec request failed") if isinstance(error, dict) else "Pisec request failed"))
    return value.get("result")


def _reply(request_id: Any, result: Any = None, error: Any = None) -> None:
    payload = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = {"code": -32000, "message": str(error)}
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            method = request.get("method")
            request_id = request.get("id")
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "pisec", "version": "1"}}
            elif method == "notifications/initialized":
                continue
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "tools/list":
                result = {"tools": [{"name": name, "description": TOOL_DESCRIPTIONS.get(name, f"Use to perform the typed {operation} transition; inspect the result and follow its next action."), "inputSchema": schema} for name, (operation, schema) in TOOLS.items()]}
            elif method == "tools/call":
                params = request.get("params", {})
                name = params.get("name")
                if name not in TOOLS:
                    raise RuntimeError("unknown Pisec tool")
                arguments = params.get("arguments") or {}
                operation, _schema = TOOLS[name]
                result = {"content": [{"type": "text", "text": json.dumps(_request(operation, arguments), sort_keys=True)}]}
            else:
                raise RuntimeError(f"unsupported MCP method: {method}")
            _reply(request_id, result=result)
        except Exception as error:
            _reply(request.get("id") if isinstance(request, dict) else None, error=error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
