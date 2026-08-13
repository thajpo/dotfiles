"""Strict versioned external JSON protocol and camelCase adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .errors import ControlPlaneError, ErrorCode

PROTOCOL_VERSION = 2


class ProtocolError(ControlPlaneError):
    pass


@dataclass(frozen=True)
class RequestSpec:
    fields: Mapping[str, str]
    required: frozenset[str]
    conversions: Mapping[str, Callable[[Any], Any]]


def _set(value: Any) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ProtocolError("states must be a string array", code=ErrorCode.PROTOCOL_REQUEST)
    return set(value)


def _spec(fields: Mapping[str, str], required: set[str] | None = None, conversions: Mapping[str, Callable[[Any], Any]] | None = None) -> RequestSpec:
    required_fields = frozenset(required if required is not None else fields)
    if any(not fields.get(field) for field in required_fields):
        raise ValueError("required protocol fields must have explicit Python adapters")
    return RequestSpec(dict(fields), required_fields, dict(conversions or {}))


SPECS: Mapping[str, RequestSpec] = {
    "negotiate": _spec({}, set()),
    "build.register": _spec({"stagedRoot": "staged_root"}),
    "project.register": _spec({"repository": "repository", "displayName": "display_name"}, {"repository"}),
    "project.status": _spec({"projectId": "project_id"}),
    "project.work-index": _spec({"projectId": "project_id"}),
    "project.reconcile": _spec({"projectId": "project_id"}),
    "conversation.create": _spec({"projectId": "project_id", "role": "role", "displayName": "display_name", "workingCopyId": "working_copy_id", "idempotencyKey": "idempotency_key"}, {"projectId", "role", "displayName", "idempotencyKey"}),
    "conversation.focus": _spec({"projectId": "project_id", "conversationId": "conversation_id"}),
    "conversation.archive": _spec({"projectId": "project_id", "conversationId": "conversation_id", "expectedResourceVersion": "expected_resource_version"}, {"projectId", "conversationId"}),
    "conversation.recover": _spec({"conversationId": "conversation_id", "actorId": "actor_id"}, {"conversationId", "actorId"}),
    "workstream.create": _spec({"projectId": "project_id", "title": "title", "brief": "brief", "displayName": "display_name", "idempotencyKey": "idempotency_key"}, {"projectId", "title", "idempotencyKey"}),
    "message.post": _spec({"projectId": "project_id", "conversationId": "conversation_id", "runId": "run_id", "kind": "kind", "payload": "payload", "idempotencyKey": "idempotency_key", "workstreamId": "workstream_id", "writerGeneration": "writer_generation", "requestId": "request_id", "replyToMessageId": "reply_to_message_id"}, {"projectId", "conversationId", "runId", "kind", "payload", "idempotencyKey"}),
    "message.list": _spec({"projectId": "project_id", "conversationId": "conversation_id", "states": "states", "limit": "limit"}, {"projectId"}, {"states": _set}),
    "message.deliver": _spec({"projectId": "project_id", "messageId": "message_id"}),
    "message.acknowledge": _spec({"projectId": "project_id", "messageId": "message_id", "resolve": "resolve"}, {"projectId", "messageId"}),
    "message.reply": _spec({"projectId": "project_id", "targetMessageId": "target_message_id", "conversationId": "conversation_id", "runId": "run_id", "payload": "payload", "idempotencyKey": "idempotency_key", "writerGeneration": "writer_generation"}, {"projectId", "targetMessageId", "conversationId", "runId", "payload", "idempotencyKey"}),
    "command.request": _spec({"projectId": "project_id", "conversationId": "conversation_id", "runId": "run_id", "writerGeneration": "writer_generation", "operation": "operation", "purpose": "purpose"}),
    "command.status": _spec({"projectId": "project_id", "commandRequestId": "command_request_id"}),
    "run.prepare": _spec({"conversationId": "conversation_id", "buildId": "build_id", "hostProcess": "host_process", "toolRuntime": "tool_runtime", "parentRunId": "parent_run_id", "idempotencyKey": "idempotency_key"}, {"conversationId", "buildId", "hostProcess", "idempotencyKey"}),
    "run.attest": _spec({"runId": "run_id", "manifestDigest": "manifest_digest", "observed": "observed"}, {"runId", "manifestDigest"}),
    "run.stop": _spec({"runId": "run_id", "reason": "reason"}, {"runId"}),
    "run.reconcile": _spec({"runId": "run_id"}),
    "run.recover": _spec({"runId": "run_id", "actorId": "actor_id"}),
    "dependency.inventory": _spec({"projectId": "project_id", "changeId": "change_id", "revision": "revision", "workerReason": "worker_reason"}, {"projectId", "changeId", "revision"}),
    "dependency.disposition": _spec({"dependencyChangeId": "dependency_change_id", "disposition": "disposition"}),
    "package-review.record": _spec({"dependencyChangeId": "dependency_change_id", "evidence": "evidence", "riskLevel": "risk_level", "recommendation": "recommendation", "investigatorRunId": "investigator_run_id"}),
    "package-review.gate": _spec({"changeId": "change_id", "revision": "revision"}),
    "package.request": _spec({"projectId": "project_id", "conversationId": "conversation_id", "runId": "run_id", "writerGeneration": "writer_generation", "changeId": "change_id", "revision": "revision", "ecosystem": "ecosystem", "action": "action", "packageName": "package_name", "exactVersion": "exact_version"}, {"projectId", "conversationId", "runId", "writerGeneration", "changeId", "revision", "ecosystem", "action"}),
    "package.status": _spec({"projectId": "project_id", "packageRequestId": "package_request_id"}),
    "change.submit": _spec({"projectId": "project_id", "project_id": "project_id", "workingCopyId": "working_copy_id", "working_copy_id": "working_copy_id", "targetRef": "target_ref", "target_ref": "target_ref", "title": "title", "summary": "summary", "captureMode": "capture_mode", "capture_mode": "capture_mode", "selectedPaths": "selected_paths", "selected_paths": "selected_paths", "excludedPaths": "excluded_paths", "excluded_paths": "excluded_paths", "expectedStatusHash": "expected_status_hash", "expected_status_hash": "expected_status_hash", "idempotencyKey": "idempotency_key", "idempotency_key": "idempotency_key", "createdByConversationId": "created_by_conversation_id", "created_by_conversation_id": "created_by_conversation_id", "actorType": "actor_type", "actor_type": "actor_type", "actorId": "actor_id", "actor_id": "actor_id", "authorizationId": "authorization_id", "authorization_id": "authorization_id"}, {"projectId", "workingCopyId", "targetRef", "title", "summary", "idempotencyKey"}),
    "change.revise": _spec({"changeId": "change_id", "change_id": "change_id", "title": "title", "summary": "summary", "captureMode": "capture_mode", "capture_mode": "capture_mode", "selectedPaths": "selected_paths", "selected_paths": "selected_paths", "excludedPaths": "excluded_paths", "excluded_paths": "excluded_paths", "idempotencyKey": "idempotency_key", "idempotency_key": "idempotency_key", "actorType": "actor_type", "actor_type": "actor_type", "actorId": "actor_id", "actor_id": "actor_id", "createdByConversationId": "created_by_conversation_id", "created_by_conversation_id": "created_by_conversation_id"}, {"changeId", "idempotencyKey"}),
    "change.list": _spec({"projectId": "project_id"}),
    "change.show": _spec({"changeId": "change_id"}),
    "review.request": _spec({"changeId": "change_id", "change_id": "change_id", "revision": "revision", "reviewerConversationId": "reviewer_conversation_id", "reviewer_conversation_id": "reviewer_conversation_id", "reviewerRunId": "reviewer_run_id", "reviewer_run_id": "reviewer_run_id", "reviewerActorId": "reviewer_actor_id", "reviewer_actor_id": "reviewer_actor_id", "reviewerCapabilitySecret": "reviewer_capability_secret", "reviewer_capability_secret": "reviewer_capability_secret", "evidence": "evidence", "reviewId": "review_id", "review_id": "review_id", "dependencyReviewDigest": "dependency_review_digest", "dependency_review_digest": "dependency_review_digest"}, {"changeId", "revision"}),
    "review.create-assignment": _spec({"changeId": "change_id", "change_id": "change_id", "revision": "revision"}, {"changeId", "revision"}),
    "review.submit": _spec({"reviewId": "review_id", "review_id": "review_id", "verdict": "verdict", "summary": "summary", "findings": "findings", "evidence": "evidence", "reviewerRunId": "reviewer_run_id", "reviewer_run_id": "reviewer_run_id", "reviewerActorId": "reviewer_actor_id", "reviewer_actor_id": "reviewer_actor_id", "reviewerCapabilitySecret": "reviewer_capability_secret", "reviewer_capability_secret": "reviewer_capability_secret"}, {"reviewId", "verdict"}),
    "integration.analyze": _spec({"projectId": "project_id", "project_id": "project_id", "changeId": "change_id", "change_id": "change_id", "revision": "revision", "targetWorkingCopyId": "target_working_copy_id", "target_working_copy_id": "target_working_copy_id", "targetRef": "target_ref", "target_ref": "target_ref", "integrationId": "integration_id", "integration_id": "integration_id"}, {"projectId", "changeId", "revision", "targetWorkingCopyId", "targetRef"}),
    "integration.authorize": _spec({"integrationId": "integration_id", "integration_id": "integration_id", "actorId": "actor_id", "actor_id": "actor_id", "requestContextId": "request_context_id", "request_context_id": "request_context_id", "expiresAt": "expires_at", "expires_at": "expires_at", "reviewId": "review_id", "review_id": "review_id"}, {"integrationId", "actorId", "requestContextId", "expiresAt"}),
    "integration.integrate": _spec({"integrationId": "integration_id", "integration_id": "integration_id", "authorizationId": "authorization_id", "authorization_id": "authorization_id", "expectedResourceVersion": "expected_resource_version", "expected_resource_version": "expected_resource_version"}, {"integrationId", "authorizationId"}),
    "investigation.start": _spec({"projectId": "project_id", "project_id": "project_id", "purpose": "purpose", "workingCopyId": "working_copy_id", "working_copy_id": "working_copy_id"}, {"projectId", "purpose"}),
    "presentation.ensure": _spec({"projectId": "project_id", "conversationId": "conversation_id", "title": "title"}, {"projectId", "conversationId"}),
}


def adapt_request(operation: str, request: Mapping[str, Any]) -> dict[str, Any]:
    spec = SPECS.get(operation)
    if spec is None:
        raise ProtocolError("unsupported protocol operation", code=ErrorCode.PROTOCOL_OPERATION, detail={"operation": operation})
    if not isinstance(request, Mapping):
        raise ProtocolError("protocol request must be an object", code=ErrorCode.PROTOCOL_REQUEST)
    keys = set(request)
    unknown = keys - set(spec.fields)
    if unknown:
        raise ProtocolError("protocol request fields do not match operation schema", code=ErrorCode.PROTOCOL_REQUEST, detail={"operation": operation, "unknown": ",".join(sorted(unknown))})
    result: dict[str, Any] = {}
    provided_internal: set[str] = set()
    for external, value in request.items():
        internal = spec.fields[external]
        provided_internal.add(internal)
        converter = spec.conversions.get(external)
        if converter is not None:
            value = converter(value)
        result[internal] = value
    required_internal = {spec.fields[field] for field in spec.required}
    missing = required_internal - provided_internal
    if missing:
        raise ProtocolError("protocol request fields do not match operation schema", code=ErrorCode.PROTOCOL_REQUEST, detail={"operation": operation, "missing": ",".join(sorted(missing))})
    return result


def protocol_request(client: Any, envelope: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(envelope, Mapping) or set(envelope) != {"protocolVersion", "operation", "request"}:
        raise ProtocolError("protocol envelope fields are invalid", code=ErrorCode.PROTOCOL_ENVELOPE)
    if envelope["protocolVersion"] != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version", code=ErrorCode.PROTOCOL_VERSION)
    operation = envelope["operation"]
    if not isinstance(operation, str):
        raise ProtocolError("protocol operation must be text", code=ErrorCode.PROTOCOL_OPERATION)
    request = envelope["request"]
    if not isinstance(request, Mapping):
        raise ProtocolError("protocol request must be an object", code=ErrorCode.PROTOCOL_REQUEST)
    result = client.dispatch(operation, request)
    return {"protocolVersion": PROTOCOL_VERSION, "operation": operation, "result": result}


__all__ = ["PROTOCOL_VERSION", "ProtocolError", "SPECS", "adapt_request", "protocol_request"]
