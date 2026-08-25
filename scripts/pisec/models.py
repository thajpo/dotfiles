"""Bounded values, identifiers, canonical JSON, and Pisec errors."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import secrets
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

MAX_JSON_BYTES = 64 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_ITEMS = 4096
MAX_TEXT = 4096
ID_PREFIXES = frozenset({"prj", "ws", "op", "az", "dec", "evt", "req", "tp", "wrq", "rpk", "cp", "cmp", "cr", "cop", "sir", "iss", "iup", "rem", "agr", "dep", "mrg", "acc", "int", "rel"})
ID_RE = re.compile(r"^(?P<prefix>[a-z][a-z0-9]{0,15})_(?P<random>[0-9a-f]{32})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SCP_REMOTE_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s:]+$")


class PisecError(Exception):
    """Expected broker error with a stable public code."""

    code = "internal_error"

    def __init__(self, message: str, *, detail: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.detail = dict(detail or {})


class InvalidRequestError(PisecError):
    code = "invalid_request"


class NotFoundError(PisecError):
    code = "not_found"


class ConflictError(PisecError):
    code = "conflict"


class IdempotencyConflictError(ConflictError):
    code = "idempotency_conflict"


class ScopeMismatchError(ConflictError):
    code = "scope_mismatch"


class AuthorizationError(PisecError):
    code = "authorization_denied"


class NeedsAttentionError(PisecError):
    code = "needs_attention"


class UnsafeStateError(PisecError):
    code = "unsafe_state"


class SchemaError(PisecError):
    code = "schema_mismatch"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    if prefix not in ID_PREFIXES:
        raise ValueError(f"unknown id prefix: {prefix}")
    return f"{prefix}_{secrets.token_hex(16)}"


def validate_id(value: Any, *, prefix: str | None = None) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError("identifier must be a string")
    match = ID_RE.fullmatch(value)
    if match is None or match.group("prefix") not in ID_PREFIXES:
        raise InvalidRequestError("identifier is invalid")
    if prefix is not None and match.group("prefix") != prefix:
        raise InvalidRequestError(f"expected {prefix} identifier")
    return value


def bounded_text(value: Any, *, name: str = "value", limit: int = MAX_TEXT, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise InvalidRequestError(f"{name} must be text")
    if not allow_empty and not value.strip():
        raise InvalidRequestError(f"{name} must not be empty")
    if len(value) > limit:
        raise InvalidRequestError(f"{name} exceeds {limit} characters")
    return value


def validate_sha256(value: Any, name: str = "digest") -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise InvalidRequestError(f"{name} is not a lowercase hexadecimal SHA-256 digest")
    return value


def validate_git_oid(value: Any, name: str = "commit") -> str:
    if not isinstance(value, str) or GIT_OID_RE.fullmatch(value) is None:
        raise InvalidRequestError(f"{name} is not a full hexadecimal object id")
    return value


def validate_remote_url(value: Any) -> str:
    """Validate a canonical credential-free HTTPS or SSH Git remote."""
    if not isinstance(value, str) or not value or value.startswith("-") or any(ord(char) < 0x21 or ord(char) == 0x7f for char in value):
        raise InvalidRequestError("remote URL is invalid")
    if SCP_REMOTE_RE.fullmatch(value):
        user, host = value.split("@", 1)
        if user != "git":
            raise InvalidRequestError("remote URL must use the git SSH user")
        host = host.split(":", 1)[0]
        if host.startswith(".") or host.endswith(".") or ".." in host:
            raise InvalidRequestError("remote URL is invalid")
        return value
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise InvalidRequestError("remote URL is invalid") from error
    if parsed.scheme not in {"https", "ssh"} or not hostname or parsed.username is None and parsed.password is not None:
        raise InvalidRequestError("remote URL must be credential-free HTTPS or SSH")
    if parsed.password is not None or parsed.query or parsed.fragment or not parsed.path or "\\" in parsed.path:
        raise InvalidRequestError("remote URL is invalid")
    if parsed.scheme == "https" and parsed.username is not None:
        raise InvalidRequestError("remote URL must not contain HTTPS userinfo")
    if parsed.scheme == "ssh" and parsed.username != "git":
        raise InvalidRequestError("remote URL must use the git SSH user")
    if parsed.scheme == "ssh" and any(char in (parsed.username or "") for char in "\\/:@"):
        raise InvalidRequestError("remote URL is invalid")
    if port is not None and not 1 <= port <= 65535:
        raise InvalidRequestError("remote URL port is invalid")
    if hostname.lower() != hostname or parsed.scheme != value.split(":", 1)[0]:
        raise InvalidRequestError("remote URL is not canonical")
    canonical_netloc = parsed.netloc
    canonical = urlunsplit((parsed.scheme, canonical_netloc, parsed.path, "", ""))
    if canonical != value:
        raise InvalidRequestError("remote URL is not canonical")
    return value


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise InvalidRequestError(f"unsupported JSON value type: {type(value).__name__}")


def _check_json(value: Any, *, depth: int = 0, max_text: int = MAX_TEXT, counter: list[int] | None = None) -> None:
    if depth > MAX_JSON_DEPTH:
        raise InvalidRequestError("JSON nesting is too deep")
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_JSON_ITEMS:
        raise InvalidRequestError("JSON contains too many values")
    if isinstance(value, str):
        if "\x00" in value or len(value) > max_text:
            raise InvalidRequestError("JSON string is invalid or too long")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidRequestError("non-finite JSON numbers are forbidden")
    elif value is None or isinstance(value, (bool, int)):
        return
    elif isinstance(value, list):
        for item in value:
            _check_json(item, depth=depth + 1, max_text=max_text, counter=counter)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or "\x00" in key or len(key) > max_text:
                raise InvalidRequestError("JSON object key is invalid")
            _check_json(item, depth=depth + 1, max_text=max_text, counter=counter)
    else:
        raise InvalidRequestError(f"unsupported JSON value type: {type(value).__name__}")


def canonical_json(value: Any, *, max_bytes: int = MAX_JSON_BYTES, max_text: int = MAX_TEXT) -> str:
    plain = _plain(value)
    _check_json(plain, max_text=max_text)
    text = json.dumps(plain, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    if len(text.encode("utf-8")) > max_bytes:
        raise InvalidRequestError("JSON value is too large")
    return text


def parse_json_strict(value: str | bytes, *, max_bytes: int = MAX_JSON_BYTES) -> Any:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if len(raw) > max_bytes:
        raise InvalidRequestError("JSON value is too large")

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise InvalidRequestError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    try:
        parsed = json.loads(raw, object_pairs_hook=object_pairs, parse_constant=lambda token: (_ for _ in ()).throw(InvalidRequestError(f"non-finite JSON number: {token}")))
    except UnicodeDecodeError as error:
        raise InvalidRequestError("request is not UTF-8") from error
    except json.JSONDecodeError as error:
        raise InvalidRequestError("malformed JSON") from error
    _check_json(parsed)
    return parsed


def json_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    kind: str
    project_id: str | None
    workstream_id: str | None
    idempotency_key: str
    request_json: str
    request_sha256: str
    state: str
    step: str
    result_json: str | None
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "OperationRecord":
        return cls(**{field: row[field] for field in cls.__dataclass_fields__})
