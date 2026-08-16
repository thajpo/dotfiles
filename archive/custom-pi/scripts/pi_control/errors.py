"""Stable, bounded errors for the local Pi control plane.

Errors in this module deliberately carry machine-readable codes and only a
small amount of redacted diagnostic context.  They are safe to render from the
JSON CLI and do not contain exception reprs, credentials, or unbounded input.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


class ErrorCode:
    SCHEMA_NEWER = "CP_SCHEMA_NEWER"
    SQLITE_UNSUPPORTED = "CP_SQLITE_UNSUPPORTED"
    DB_UNSAFE = "CP_DB_UNSAFE"
    RESOURCE_STALE = "CP_RESOURCE_STALE"
    IDEMPOTENCY_CONFLICT = "CP_IDEMPOTENCY_CONFLICT"
    LOCK_BUSY = "CP_LOCK_BUSY"
    WRITER_STALE = "CP_WRITER_STALE"
    WRITER_UNKNOWN = "CP_WRITER_UNKNOWN"
    PROJECT_DRIFT = "CP_PROJECT_DRIFT"
    WORKING_COPY_DRIFT = "CP_WORKING_COPY_DRIFT"
    WORKING_COPY_MISSING = "CP_WORKING_COPY_MISSING"
    CONVERSATION_CONFLICT = "CP_CONVERSATION_CONFLICT"
    RUN_ATTESTATION_FAILED = "CP_RUN_ATTESTATION_FAILED"
    RUNTIME_UNAVAILABLE = "CP_RUNTIME_UNAVAILABLE"
    GIT_REF_MOVED = "CP_GIT_REF_MOVED"
    CHANGE_AMBIGUOUS = "CP_CHANGE_AMBIGUOUS"
    INTEGRATION_CONFLICT = "CP_INTEGRATION_CONFLICT"
    OPERATION_AMBIGUOUS = "CP_OPERATION_AMBIGUOUS"
    BUILD_MISMATCH = "CP_BUILD_MISMATCH"
    PERMISSION_INVALID = "CP_PERMISSION_INVALID"
    INVALID_REQUEST = "CP_INVALID_REQUEST"
    NOT_FOUND = "CP_NOT_FOUND"
    DB_CORRUPT = "CP_DB_CORRUPT"
    SCHEMA_RESET_REQUIRED = "CP_SCHEMA_RESET_REQUIRED"
    DB_BUSY = "CP_LOCK_BUSY"
    MIGRATION_FAILED = "CP_MIGRATION_FAILED"
    FOREIGN_KEY = "CP_FOREIGN_KEY"
    CONSTRAINT = "CP_CONSTRAINT"
    UNSUPPORTED = "CP_UNSUPPORTED"
    MIGRATION_UNRESOLVED = "CP_MIGRATION_UNRESOLVED"
    ACTIVATION_MISMATCH = "CP_ACTIVATION_MISMATCH"
    WORKSTREAM_CONFLICT = "CP_WORKSTREAM_CONFLICT"
    PRESENTATION_UNKNOWN = "CP_PRESENTATION_UNKNOWN"
    ADAPTER_UNAVAILABLE = "CP_ADAPTER_UNAVAILABLE"
    BRIDGE_STALE = "CP_BRIDGE_STALE"
    INPUT_CONFLICT = "CP_INPUT_CONFLICT"
    DELIVERY_UNCERTAIN = "CP_DELIVERY_UNCERTAIN"
    INPUT_REJECTED = "CP_INPUT_REJECTED"
    MODEL_UNAVAILABLE = "CP_MODEL_UNAVAILABLE"
    QUEUE_ITEM_NOT_FOUND = "CP_QUEUE_ITEM_NOT_FOUND"
    PROTOCOL_ENVELOPE = "CP_PROTOCOL_ENVELOPE"
    PROTOCOL_VERSION = "CP_PROTOCOL_VERSION"
    PROTOCOL_OPERATION = "CP_PROTOCOL_OPERATION"
    PROTOCOL_REQUEST = "CP_PROTOCOL_REQUEST"


_SECRET_KEY = re.compile(
    r"(?:pass(?:word)?|secret|token|credential|authorization|cookie|private.?key|capability|prompt|header)",
    re.IGNORECASE,
)
_MAX_DETAIL_ITEMS = 16
_MAX_DETAIL_KEY = 80
_MAX_DETAIL_VALUE = 512
_MAX_DETAIL_TOTAL = 4096


def _safe_text(value: Any, limit: int = _MAX_DETAIL_VALUE) -> str:
    if isinstance(value, bytes):
        text = value.hex()
    else:
        text = str(value)
    text = text.replace("\x00", "\\0")
    return text[:limit]


def bounded_detail(detail: Mapping[str, Any] | None = None, **extra: Any) -> dict[str, str]:
    """Copy and redact a bounded diagnostic mapping.

    Diagnostics are not a storage channel.  Keys are normalized, sorted, and
    secret-shaped values are replaced before truncation.
    """

    source: dict[str, Any] = {}
    if detail is not None:
        if not isinstance(detail, Mapping):
            raise TypeError("error detail must be a mapping")
        source.update(detail)
    source.update(extra)
    result: dict[str, str] = {}
    total = 0
    for raw_key in sorted(source, key=lambda item: str(item)):
        if len(result) >= _MAX_DETAIL_ITEMS or total >= _MAX_DETAIL_TOTAL:
            break
        key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(raw_key))[:_MAX_DETAIL_KEY] or "_"
        value = "[redacted]" if _SECRET_KEY.search(key) else _safe_text(source[raw_key])
        remaining = _MAX_DETAIL_TOTAL - total - len(key)
        if remaining <= 0:
            break
        value = value[: min(_MAX_DETAIL_VALUE, remaining)]
        result[key] = value
        total += len(key) + len(value)
    return result


@dataclass(frozen=True)
class ErrorPayload:
    code: str
    message: str
    detail: Mapping[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": dict(self.detail)}


class ControlPlaneError(Exception):
    """Base class for all expected controller failures."""

    code = ErrorCode.INVALID_REQUEST

    def __init__(
        self,
        message: str,
        *,
        detail: Mapping[str, Any] | None = None,
        code: str | None = None,
    ) -> None:
        self.code = code or self.code
        self.message = _safe_text(message, 1024)
        self.detail = bounded_detail(detail)
        super().__init__(self.message)

    @property
    def payload(self) -> ErrorPayload:
        return ErrorPayload(self.code, self.message, self.detail)

    def as_dict(self) -> dict[str, Any]:
        return self.payload.as_dict()


class SQLiteUnsupportedError(ControlPlaneError):
    code = ErrorCode.SQLITE_UNSUPPORTED


class UnsafeDatabaseError(ControlPlaneError):
    code = ErrorCode.DB_UNSAFE


class SchemaNewerError(ControlPlaneError):
    code = ErrorCode.SCHEMA_NEWER


class ResourceStaleError(ControlPlaneError):
    code = ErrorCode.RESOURCE_STALE

    def __init__(self, resource_id: str, expected: int, actual: int | None = None) -> None:
        detail: dict[str, Any] = {"resource_id": resource_id, "expected_version": expected}
        if actual is not None:
            detail["actual_version"] = actual
        super().__init__("resource version is stale", detail=detail)
        self.resource_id = resource_id
        self.expected_version = expected
        self.actual_version = actual


class IdempotencyConflictError(ControlPlaneError):
    code = ErrorCode.IDEMPOTENCY_CONFLICT

    def __init__(self, idempotency_key: str, *, existing_digest: str | None = None, request_digest: str | None = None) -> None:
        super().__init__(
            "idempotency key is already bound to a different request",
            detail={
                "idempotency_key": idempotency_key,
                "existing_digest": existing_digest or "",
                "request_digest": request_digest or "",
            },
        )
        self.idempotency_key = idempotency_key
        self.existing_digest = existing_digest
        self.request_digest = request_digest


class LockBusyError(ControlPlaneError):
    code = ErrorCode.LOCK_BUSY


class WriterStaleError(ControlPlaneError):
    code = ErrorCode.WRITER_STALE


class WriterUnknownError(ControlPlaneError):
    code = ErrorCode.WRITER_UNKNOWN


class BuildMismatchError(ControlPlaneError):
    code = ErrorCode.BUILD_MISMATCH


class PermissionInvalidError(ControlPlaneError):
    code = ErrorCode.PERMISSION_INVALID


class NotFoundError(ControlPlaneError):
    code = ErrorCode.NOT_FOUND


class DatabaseCorruptError(ControlPlaneError):
    code = ErrorCode.DB_CORRUPT


class SchemaResetRequiredError(DatabaseCorruptError):
    code = ErrorCode.SCHEMA_RESET_REQUIRED


class MigrationError(ControlPlaneError):
    code = ErrorCode.MIGRATION_FAILED


class ConstraintError(ControlPlaneError):
    code = ErrorCode.CONSTRAINT


class InvalidRequestError(ControlPlaneError):
    code = ErrorCode.INVALID_REQUEST


class MigrationUnresolvedError(ControlPlaneError):
    code = ErrorCode.MIGRATION_UNRESOLVED


class ActivationMismatchError(ControlPlaneError):
    code = ErrorCode.ACTIVATION_MISMATCH


class WorkstreamConflictError(ControlPlaneError):
    code = ErrorCode.WORKSTREAM_CONFLICT


class PresentationUnknownError(ControlPlaneError):
    code = ErrorCode.PRESENTATION_UNKNOWN


def error_from_exception(error: BaseException) -> ControlPlaneError:
    """Translate a narrow set of SQLite errors without leaking SQL/input."""

    if isinstance(error, ControlPlaneError):
        return error
    # Kept here rather than importing sqlite3 at module import in callers that
    # only need error types.
    import sqlite3

    if isinstance(error, sqlite3.OperationalError):
        text = str(error).lower()
        if "busy" in text or "locked" in text:
            return LockBusyError("database is busy")
        if "no such table" in text or "malformed" in text or "not a database" in text:
            return DatabaseCorruptError("database is unreadable")
    if isinstance(error, sqlite3.IntegrityError):
        text = str(error).lower()
        if "foreign key" in text:
            return ConstraintError("foreign-key constraint failed", code=ErrorCode.FOREIGN_KEY)
        return ConstraintError("database constraint failed")
    return ControlPlaneError("control-plane operation failed")


__all__ = [
    "ControlPlaneError",
    "ConstraintError",
    "DatabaseCorruptError",
    "ErrorCode",
    "ErrorPayload",
    "IdempotencyConflictError",
    "InvalidRequestError",
    "MigrationUnresolvedError",
    "ActivationMismatchError",
    "WorkstreamConflictError",
    "PresentationUnknownError",
    "LockBusyError",
    "MigrationError",
    "NotFoundError",
    "PermissionInvalidError",
    "ResourceStaleError",
    "SQLiteUnsupportedError",
    "SchemaNewerError",
    "UnsafeDatabaseError",
    "WriterStaleError",
    "WriterUnknownError",
    "bounded_detail",
    "error_from_exception",
]
