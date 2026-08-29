#!/usr/bin/env python3
"""Minimal stdio MCP server exposing the authenticated Pisec worker tools."""

from __future__ import annotations

import json
import hashlib
import os
import socket
import sys
from typing import Any, Callable

try:
    from .operation_catalogue_generated import SOCKET_OPERATIONS
except ImportError:
    from operation_catalogue_generated import SOCKET_OPERATIONS


def _object(properties: dict[str, Any] | None = None, *, required: tuple[str, ...] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": properties or {}}
    if required:
        schema["required"] = list(required)
    return schema


def _string(*, enum: tuple[str, ...] = (), minimum: int = 0, maximum: int = 0, pattern: str = "") -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    if enum:
        schema["enum"] = list(enum)
    if minimum:
        schema["minLength"] = minimum
    if maximum:
        schema["maxLength"] = maximum
    if pattern:
        schema["pattern"] = pattern
    return schema


def _array(items: dict[str, Any], *, minimum: int = 0, maximum: int = 0) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": items}
    if minimum:
        schema["minItems"] = minimum
    if maximum:
        schema["maxItems"] = maximum
    return schema


ANY_VALUE: dict[str, Any] = {}
EVIDENCE = _array(ANY_VALUE, maximum=64)
ISSUE_STATES = ("open", "acknowledged", "remediating", "verifying", "resolved")
RESEARCH_STATES = ("pending", "researching", "needs_context", "answered", "declined", "acknowledged")


COMPLETION_SCHEMA = _object(
    {
        "acceptance": _array(
            _object(
                {
                    "criterion": _string(minimum=1, maximum=4096),
                    "status": {"const": "passed"},
                    "evidence": _array(_string(minimum=1, maximum=4096), minimum=1),
                },
                required=("criterion", "status", "evidence"),
            ),
            minimum=1,
            maximum=32,
        ),
        "verification": _array(
            _object(
                {"command": _string(minimum=1, maximum=4096), "result": _string(minimum=1, maximum=8192)},
                required=("command", "result"),
            ),
            minimum=1,
            maximum=32,
        ),
        "source_commit": _string(pattern="^(?:[0-9a-f]{40}|[0-9a-f]{64})$"),
        "task_packet_sha256": _string(pattern="^[0-9a-f]{64}$"),
        "changed_surfaces": _array(_string(minimum=1, maximum=4096), maximum=32),
        "residual_risk": _string(maximum=4096),
    },
    required=("acceptance", "verification", "source_commit", "task_packet_sha256", "changed_surfaces", "residual_risk"),
)


TOOLS = {
    "pisec_show_task_packet": ("task.get", _object()),
    "pisec_checkpoint_workstream": (
        "workstream.checkpoint",
        _object(
            {
                "phase": _string(enum=("investigating", "implementing", "verifying")),
                "summary": _string(minimum=1, maximum=1024),
                "next_action": _string(minimum=1, maximum=1024),
                "evidence": EVIDENCE,
            },
            required=("phase", "summary", "next_action", "evidence"),
        ),
    ),
    "pisec_submit_completion": (
        "workstream.completion.submit",
        _object({"completion": COMPLETION_SCHEMA}, required=("completion",)),
    ),
    "pisec_request_help": (
        "help.request",
        _object(
            {
                "kind": _string(enum=("clarification", "blocker", "review", "access", "permission", "tooling", "lifecycle")),
                "summary": _string(minimum=1, maximum=1024),
                "details": _string(minimum=1, maximum=4096),
                "requested_action": _string(minimum=1, maximum=4096),
                "blocking": {"type": "boolean"},
                "evidence": EVIDENCE,
            },
            required=("kind", "summary", "details"),
        ),
    ),
    "pisec_list_coordination": ("coordination.list", _object({"include_resolved": {"type": "boolean"}})),
    "pisec_inspect_coordination": ("coordination.inspect", _object({"request_id": _string(minimum=1, maximum=128)}, required=("request_id",))),
    "pisec_report_issue": (
        "issue.report",
        _object(
            {
                "category": _string(enum=("permission", "access", "lifecycle", "tooling", "other")),
                "severity": _string(enum=("blocking", "degraded", "improvement")),
                "summary": _string(minimum=1, maximum=1024),
                "details": _string(minimum=1, maximum=4096),
                "requested_action": _string(minimum=1, maximum=4096),
                "evidence": EVIDENCE,
            },
            required=("category", "severity", "summary", "details", "requested_action", "evidence"),
        ),
    ),
    "pisec_list_issues": ("issue.list", _object({"state": _string(enum=ISSUE_STATES), "limit": {"type": "integer", "minimum": 1, "maximum": 1000}})),
    "pisec_inspect_issue": ("issue.inspect", _object({"issue_id": _string(minimum=1, maximum=128)}, required=("issue_id",))),
    "pisec_add_issue_context": ("issue.add_context", _object({"issue_id": _string(minimum=1, maximum=128), "context": ANY_VALUE}, required=("issue_id", "context"))),
    "pisec_verify_issue": (
        "issue.verify",
        _object({"issue_id": _string(minimum=1, maximum=128), "status": _string(enum=("fixed", "still_blocked")), "evidence": ANY_VALUE}, required=("issue_id", "status", "evidence")),
    ),
    "pisec_request_secretary_research": (
        "research.request",
        _object(
            {
                "summary": _string(minimum=1, maximum=1024),
                "question": _string(minimum=1, maximum=4096),
                "context": _string(maximum=4096),
                "attempted": _array(_string(minimum=1, maximum=4096), maximum=16),
                "candidate_sources": _array(_string(minimum=1, maximum=2048), maximum=16),
                "blocking": {"type": "boolean"},
            },
            required=("summary", "question"),
        ),
    ),
    "pisec_check_secretary_research": ("research.list", _object({"state": _string(enum=RESEARCH_STATES), "limit": {"type": "integer", "minimum": 1, "maximum": 100}})),
    "pisec_inspect_secretary_research": ("research.inspect", _object({"request_id": _string(minimum=1, maximum=128)}, required=("request_id",))),
    "pisec_add_secretary_research_context": (
        "research.add_context",
        _object(
            {
                "request_id": _string(minimum=1, maximum=128),
                "context": _string(minimum=1, maximum=4096),
                "attempted": _array(_string(minimum=1, maximum=4096), maximum=16),
                "candidate_sources": _array(_string(minimum=1, maximum=2048), maximum=16),
            },
            required=("request_id", "context"),
        ),
    ),
    "pisec_acknowledge_secretary_research": ("research.acknowledge", _object({"request_id": _string(minimum=1, maximum=128)}, required=("request_id",))),
    "pisec_list_attention": ("attention.list", _object({"limit": {"type": "integer", "minimum": 1, "maximum": 32}})),
    "pisec_inspect_attention": ("attention.inspect", _object({"attention_id": _string(minimum=1, maximum=128)}, required=("attention_id",))),
}


def _adapt_completion(params: dict[str, Any]) -> dict[str, Any]:
    completion = params["completion"]
    return {
        "completionPacket": {
            "acceptance": completion["acceptance"],
            "verification": completion["verification"],
            "sourceCommit": completion["source_commit"],
            "taskPacketSha256": completion["task_packet_sha256"],
            "changedSurfaces": completion["changed_surfaces"],
            "residualRisk": completion["residual_risk"],
        }
    }


def _adapt_help(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": params["kind"],
        "summary": params["summary"],
        "details": params["details"],
        "requestedAction": params.get("requested_action", "Provide guidance or remediation."),
        "blocking": params.get("blocking", params["kind"] == "blocker"),
        "evidence": params.get("evidence", []),
    }


def _adapt_research_request(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "request": {
            "kind": "research",
            "summary": params["summary"],
            "question": params["question"],
            "context": params.get("context", ""),
            "attempted": params.get("attempted", []),
            "candidateSources": params.get("candidate_sources", []),
            "blocking": params.get("blocking", True),
        }
    }


def _adapt_research_context(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "requestId": params["request_id"],
        "context": {
            "context": params["context"],
            "attempted": params.get("attempted", []),
            "candidateSources": params.get("candidate_sources", []),
        },
    }


def _rename(params: dict[str, Any], fields: dict[str, str]) -> dict[str, Any]:
    return {fields.get(key, key): value for key, value in params.items()}


PAYLOAD_ADAPTERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "attention.inspect": lambda value: _rename(value, {"attention_id": "attentionId"}),
    "coordination.list": lambda value: _rename(value, {"include_resolved": "includeResolved"}),
    "coordination.inspect": lambda value: _rename(value, {"request_id": "requestId"}),
    "help.request": _adapt_help,
    "issue.report": lambda value: _rename(value, {"requested_action": "requestedAction"}),
    "issue.inspect": lambda value: _rename(value, {"issue_id": "issueId"}),
    "issue.add_context": lambda value: _rename(value, {"issue_id": "issueId"}),
    "issue.verify": lambda value: _rename(value, {"issue_id": "issueId"}),
    "research.request": _adapt_research_request,
    "research.inspect": lambda value: _rename(value, {"request_id": "requestId"}),
    "research.add_context": _adapt_research_context,
    "research.acknowledge": lambda value: _rename(value, {"request_id": "requestId"}),
    "workstream.checkpoint": lambda value: _rename(value, {"next_action": "nextAction"}),
    "workstream.completion.submit": _adapt_completion,
}

TOOL_DESCRIPTIONS = {
    "pisec_show_task_packet": "Use when you need the immutable assigned outcome and boundaries; reads the authoritative task packet.",
    "pisec_checkpoint_workstream": "Use to record typed progress only. Use investigating, implementing, or verifying; submit final acceptance evidence with pisec_submit_completion.",
    "pisec_submit_completion": "Use once, after implementation and verification, to submit the immutable completion packet. The broker creates the matching ready_review checkpoint atomically.",
    "pisec_request_help": "Use when blocked, clarifying, reviewing, or needing access; creates the single typed upward-help source.",
    "pisec_list_attention": "Use at turn start and before ending a turn; lists current authorized attention references without acknowledging them.",
    "pisec_inspect_attention": "Use after attention.list to identify the typed source and its existing inspector; read-only.",
    "pisec_report_issue": "Use for access, tooling, lifecycle, or permission failures; records a canonical issue for the project Secretary.",
    "pisec_verify_issue": "Use when the Secretary asks you to verify a remediation; closes the reporter revision or reopens supervisor attention.",
}

IDEMPOTENT_OPERATIONS = frozenset({
    "help.request",
    "issue.add_context",
    "issue.report",
    "issue.verify",
    "research.add_context",
    "research.request",
    "workstream.checkpoint",
})

if any(operation not in SOCKET_OPERATIONS["runtime"] for operation, _schema in TOOLS.values()):
    raise RuntimeError("Codex Pisec MCP tools are absent from the generated operation catalogue")


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _adapter_idempotency_key(operation: str, params: dict[str, Any], native_tool_id: str) -> str:
    canonical = json.dumps(_canonical({"operation": operation, "nativeToolId": native_tool_id, "params": params}), separators=(",", ":"), ensure_ascii=True)
    return "adapter:codex:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _request(operation: str, params: dict[str, Any], native_tool_id: str = "") -> Any:
    socket_path = os.environ.get("PISEC_RUNTIME_SOCKET")
    token = os.environ.get("PISEC_RUNTIME_TOKEN")
    workstream = os.environ.get("PISEC_WORKSTREAM_ID")
    instance = os.environ.get("PISEC_RUNTIME_INSTANCE_ID")
    surface = os.environ.get("PISEC_SURFACE_ID")
    generation = os.environ.get("PISEC_RUNTIME_GENERATION")
    if not all(isinstance(value, str) and value for value in (socket_path, token, workstream, instance, surface, generation)):
        raise RuntimeError("Pisec runtime binding is incomplete")
    model_params = dict(params)
    model_params.pop("idempotencyKey", None)
    model_params.pop("idempotency_key", None)
    model_payload = PAYLOAD_ADAPTERS.get(operation, dict)(model_params)
    if operation in IDEMPOTENT_OPERATIONS:
        model_payload["idempotencyKey"] = _adapter_idempotency_key(operation, model_params, native_tool_id)
    payload = {"workstreamId": workstream, "runtimeInstanceId": instance, "surfaceId": surface, "token": token, "generation": generation, **model_payload}
    request_id = "req_" + hashlib.sha256(f"{workstream}:{instance}:{operation}:{native_tool_id}".encode("utf-8")).hexdigest()[:32]
    request = {"protocolVersion": 1, "requestId": request_id, "operation": operation, "payload": payload}
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
    if value.get("requestId") != request_id:
        raise RuntimeError("Pisec broker response identity does not match the request")
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
                result = {"content": [{"type": "text", "text": json.dumps(_request(operation, arguments, str(request_id or "")), sort_keys=True)}]}
            else:
                raise RuntimeError(f"unsupported MCP method: {method}")
            _reply(request_id, result=result)
        except Exception as error:
            _reply(request.get("id") if isinstance(request, dict) else None, error=error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
