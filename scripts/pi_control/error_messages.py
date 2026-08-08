"""Stable consequence-oriented projections for controller errors."""

from __future__ import annotations

from typing import Any, Mapping

from .errors import ControlPlaneError, ErrorCode, ErrorPayload, error_from_exception

_MESSAGES = {
    ErrorCode.RESOURCE_STALE: "This operation used an older state version; refresh before retrying.",
    ErrorCode.WRITER_STALE: "Another writer owns this working copy or its epoch is stale; no write was started.",
    ErrorCode.PROJECT_DRIFT: "The project no longer matches its controller-bound Git identity; stopped before mutation.",
    ErrorCode.WORKING_COPY_DRIFT: "The selected working copy differs from its controller assignment; stopped before mutation.",
    ErrorCode.RUN_ATTESTATION_FAILED: "The runtime would open a different project or revision; stopped before tools ran.",
    ErrorCode.GIT_REF_MOVED: "The target Git ref moved during the operation; no guessed integration was applied.",
    ErrorCode.INTEGRATION_CONFLICT: "The candidate and target require explicit conflict resolution; source and target were preserved.",
    ErrorCode.OPERATION_AMBIGUOUS: "The operation has an uncertain external result; state was retained for review.",
    ErrorCode.IDEMPOTENCY_CONFLICT: "This request identity is already bound to different content.",
    ErrorCode.PERMISSION_INVALID: "The requested action is outside the current authority boundary.",
    ErrorCode.INVALID_REQUEST: "The request is malformed or missing an explicit controller binding.",
    ErrorCode.NOT_FOUND: "The exact controller resource was not found; no fallback selection was made.",
    ErrorCode.DB_BUSY: "The controller is busy; no partial lifecycle mutation was assumed.",
    ErrorCode.MIGRATION_UNRESOLVED: "Migration evidence is unresolved; no controller authority was synthesized.",
    ErrorCode.ACTIVATION_MISMATCH: "Activation bindings do not match the exact reviewed build, migration, or project state.",
    ErrorCode.WORKSTREAM_CONFLICT: "The workstream resources are not an exact controller-owned relationship.",
    ErrorCode.PRESENTATION_UNKNOWN: "Presentation state is unknown; no process was migrated or removed.",
    ErrorCode.ADAPTER_UNAVAILABLE: "A required observation adapter is unavailable; the applicable gate is stopped."
}


def consequence_message(error: BaseException | ErrorPayload | Mapping[str, Any]) -> str:
    """Return bounded user-facing text without echoing raw diagnostics/content."""

    if isinstance(error, ControlPlaneError):
        code = error.code
    elif isinstance(error, ErrorPayload):
        code = error.code
    elif isinstance(error, Mapping):
        code = error.get("code") if isinstance(error.get("code"), str) else ErrorCode.INVALID_REQUEST
    else:
        code = error_from_exception(error).code
    return _MESSAGES.get(code, "The controller could not complete this operation; inspect the bounded error details.")


_NEXT_ACTIONS = {
    ErrorCode.RESOURCE_STALE: ["Refresh controller state", "Review the current resource version", "Retry only with the refreshed intent"],
    ErrorCode.LOCK_BUSY: ["Wait for the other operation", "Focus the exact resource", "Retry after the lock is released"],
    ErrorCode.GIT_REF_MOVED: ["Inspect the preserved change and target ref", "Re-analyze the exact revision", "Authorize again only after review"],
    ErrorCode.INTEGRATION_CONFLICT: ["Inspect preserved candidate and integration evidence", "Resolve conflicts explicitly", "Run a fresh analysis"],
    ErrorCode.OPERATION_AMBIGUOUS: ["Do not blindly retry", "Inspect the durable operation record", "Resolve or reconcile the recorded side effect"],
    ErrorCode.ACTIVATION_MISMATCH: ["Inspect the exact activation bindings", "Resolve the build or migration evidence", "Retry only with a new explicit operation"],
    ErrorCode.MIGRATION_UNRESOLVED: ["Inspect the unresolved mapping", "Preserve the source evidence", "Create an explicit resolution before retrying"],
}


def _outcome_field(detail: Mapping[str, str], *names: str, fallback: str) -> str:
    for name in names:
        value = detail.get(name)
        if value:
            return value[:512]
    return fallback


def projected_error(error: BaseException) -> dict[str, Any]:
    """Project a failure without claiming side effects that were not proven."""

    translated = error_from_exception(error)
    message = consequence_message(translated)
    detail = dict(translated.detail)
    action = _outcome_field(detail, "attempted_action", "action", fallback="controller operation")
    changed = _outcome_field(detail, "changed", "side_effect", fallback="unknown — no side-effect proof was supplied")
    preserved = _outcome_field(detail, "preserved", "preservation", fallback="unknown — inspect the durable operation and technical evidence")
    observed = _outcome_field(detail, "observed_risk", "risk", fallback=message)
    next_actions = _NEXT_ACTIONS.get(translated.code, ["Inspect the bounded technical details", "Refresh or reconcile the exact resource before retrying"])
    return {
        "code": translated.code,
        "message": message,
        "attemptedAction": action,
        "observedRisk": observed,
        "changed": changed,
        "preserved": preserved,
        "nextActions": next_actions,
        "technicalDetails": {"code": translated.code, "detail": detail},
        # Keep the original bounded diagnostic field for existing machine clients.
        "detail": detail,
    }


__all__ = ["consequence_message", "projected_error"]
