"""Small immutable value objects and safe serialization helpers.

The database stores canonical JSON text, never Python object serialization.  The
helpers here are intentionally strict so a caller cannot smuggle arbitrary
objects, non-finite numbers, or unbounded diagnostics into controller state.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import secrets
from typing import Any, Mapping, Sequence, TypeVar

from .errors import InvalidRequestError


UTC = timezone.utc
_MAX_JSON_BYTES = 64 * 1024
_MAX_JSON_DEPTH = 32
_MAX_JSON_ITEMS = 4096
_MAX_TEXT = 4096

# Controller IDs have a type prefix and exactly 128 bits represented as lower
# case hex.  The prefix is part of the identifier's type, not a path/name.
ID_RE = re.compile(r"^(?P<prefix>[a-z][a-z0-9]{0,15})_(?P<random>[0-9a-f]{32})$")
PI_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")
ID_PREFIXES = frozenset(
    {
        "prj",
        "wc",
        "conv",
        "ws",
        "pa",
        "mig",
        "inv",
        "art",
        "run",
        "chg",
        "rev",
        "review",
        "int",
        "auth",
        "op",
        "evt",
        "consumer",
        "attention",
        "migration",
    }
)


def utc_now() -> str:
    """Return a UTC RFC3339 timestamp with stable microsecond precision."""

    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    """Generate a controller ID with at least 128 bits of CSPRNG entropy."""

    if not isinstance(prefix, str) or re.fullmatch(r"[a-z][a-z0-9]{0,15}", prefix) is None:
        raise ValueError("invalid identifier prefix")
    return f"{prefix}_{secrets.token_hex(16)}"


def validate_id(value: str, *, prefix: str | None = None) -> str:
    if not isinstance(value, str):
        raise ValueError("identifier must be text")
    match = ID_RE.fullmatch(value)
    if match is None or match.group("prefix") not in ID_PREFIXES:
        raise ValueError("invalid controller identifier")
    if prefix is not None and match.group("prefix") != prefix:
        raise ValueError("identifier has the wrong type prefix")
    return value


def validate_pi_session_id(value: str) -> str:
    if not isinstance(value, str) or PI_SESSION_ID_RE.fullmatch(value) is None:
        raise ValueError("invalid Pi session identifier")
    return value


def _json_depth(value: Any, depth: int = 0) -> int:
    if depth > _MAX_JSON_DEPTH:
        raise InvalidRequestError("JSON nesting is too deep")
    if isinstance(value, Mapping):
        if len(value) > _MAX_JSON_ITEMS:
            raise InvalidRequestError("JSON object has too many members")
        maximum = depth
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidRequestError("JSON object keys must be strings")
            maximum = max(maximum, _json_depth(item, depth + 1))
        return maximum
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_JSON_ITEMS:
            raise InvalidRequestError("JSON array has too many members")
        maximum = depth
        for item in value:
            maximum = max(maximum, _json_depth(item, depth + 1))
        return maximum
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        if isinstance(value, str) and len(value) > _MAX_TEXT:
            raise InvalidRequestError("JSON string is too long")
        return depth
    if value is None or isinstance(value, bool):
        return depth
    if isinstance(value, int):
        # SQLite JSON values are bounded by the JSON text limit below.  A
        # separate bit bound avoids giant decimal conversion work.
        if value.bit_length() > 4096:
            raise InvalidRequestError("JSON integer is too large")
        return depth
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidRequestError("JSON numbers must be finite")
        return depth
    raise InvalidRequestError(f"unsupported JSON value type: {type(value).__name__}")


def _plain_json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain_json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _plain_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise InvalidRequestError(f"unsupported JSON value type: {type(value).__name__}")


def canonical_json(value: Any, *, max_bytes: int = _MAX_JSON_BYTES) -> str:
    """Serialize a JSON-compatible value canonically and with strict bounds."""

    plain = _plain_json_value(value)
    _json_depth(plain)
    try:
        text = json.dumps(
            plain,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise InvalidRequestError("value is not canonical JSON") from error
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        raise InvalidRequestError("canonical JSON exceeds its size bound")
    return text


def canonical_json_bytes(value: Any, *, max_bytes: int = _MAX_JSON_BYTES) -> bytes:
    return canonical_json(value, max_bytes=max_bytes).encode("utf-8")


def parse_canonical_json(text: str | bytes, *, max_bytes: int = _MAX_JSON_BYTES) -> Any:
    if isinstance(text, bytes):
        if len(text) > max_bytes:
            raise InvalidRequestError("JSON exceeds its size bound")
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as error:
            raise InvalidRequestError("JSON must be UTF-8") from error
    if not isinstance(text, str) or len(text.encode("utf-8")) > max_bytes:
        raise InvalidRequestError("JSON exceeds its size bound")
    try:
        value = json.loads(text, parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidRequestError("invalid JSON") from error
    # Re-serializing also rejects values accepted by a permissive parser and
    # enforces the same nesting/member bounds.
    canonical_json(value, max_bytes=max_bytes)
    return value


def json_digest(value: Any, *, max_bytes: int = _MAX_JSON_BYTES) -> str:
    return hashlib.sha256(canonical_json_bytes(value, max_bytes=max_bytes)).hexdigest()


def bounded_text(value: Any, *, name: str = "value", limit: int = _MAX_TEXT, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be text")
    if not allow_empty and not value:
        raise InvalidRequestError(f"{name} must not be empty")
    if "\x00" in value:
        raise InvalidRequestError(f"{name} contains a NUL")
    if len(value) > limit:
        raise InvalidRequestError(f"{name} exceeds its size bound")
    return value


_CHILD_SOURCE_KEYS = frozenset({
    "snapshotId", "snapshotRef", "snapshotCommitOid", "snapshotTreeOid",
    "sourceHeadOid", "sourceTreeOid", "authority",
})
_CHILD_SNAPSHOT_ID_RE = re.compile(r"^snap_[0-9a-f]{32}$")


def _child_oid(value: Any, *, name: str, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or len(value) not in {40, 64} or re.fullmatch(r"[0-9a-f]+", value) is None:
        raise InvalidRequestError(f"{name} is not a valid Git object ID")
    return value


def validate_child_source(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CHILD_SOURCE_KEYS:
        raise InvalidRequestError("child source fields do not match the frozen schema")
    snapshot_id = value["snapshotId"]
    if not isinstance(snapshot_id, str) or _CHILD_SNAPSHOT_ID_RE.fullmatch(snapshot_id) is None:
        raise InvalidRequestError("child source snapshot ID is invalid")
    expected_ref = f"refs/pi/snapshots/{snapshot_id}"
    if value["snapshotRef"] != expected_ref:
        raise InvalidRequestError("child source snapshot ref is not bound to its ID")
    if not isinstance(value["snapshotRef"], str) or len(value["snapshotRef"]) > 256:
        raise InvalidRequestError("child source snapshot ref is invalid")
    _child_oid(value["snapshotCommitOid"], name="snapshotCommitOid")
    _child_oid(value["snapshotTreeOid"], name="snapshotTreeOid")
    _child_oid(value["sourceHeadOid"], name="sourceHeadOid", allow_none=True)
    _child_oid(value["sourceTreeOid"], name="sourceTreeOid", allow_none=True)
    if value["authority"] not in {"read-only", "writer"}:
        raise InvalidRequestError("child source authority is invalid")
    result = dict(value)
    canonical_json(result, max_bytes=16 * 1024)
    return result


@dataclass(frozen=True)
class ChildSource:
    snapshot_id: str
    snapshot_ref: str
    snapshot_commit_oid: str
    snapshot_tree_oid: str
    source_head_oid: str | None
    source_tree_oid: str | None
    authority: str

    def __post_init__(self) -> None:
        validate_child_source({
            "snapshotId": self.snapshot_id,
            "snapshotRef": self.snapshot_ref,
            "snapshotCommitOid": self.snapshot_commit_oid,
            "snapshotTreeOid": self.snapshot_tree_oid,
            "sourceHeadOid": self.source_head_oid,
            "sourceTreeOid": self.source_tree_oid,
            "authority": self.authority,
        })

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ChildSource":
        checked = validate_child_source(value)
        return cls(
            snapshot_id=checked["snapshotId"], snapshot_ref=checked["snapshotRef"],
            snapshot_commit_oid=checked["snapshotCommitOid"], snapshot_tree_oid=checked["snapshotTreeOid"],
            source_head_oid=checked["sourceHeadOid"], source_tree_oid=checked["sourceTreeOid"],
            authority=checked["authority"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshotId": self.snapshot_id, "snapshotRef": self.snapshot_ref,
            "snapshotCommitOid": self.snapshot_commit_oid, "snapshotTreeOid": self.snapshot_tree_oid,
            "sourceHeadOid": self.source_head_oid, "sourceTreeOid": self.source_tree_oid,
            "authority": self.authority,
        }


@dataclass(frozen=True)
class SchemaStatus:
    schema_version: int
    user_version: int
    controller_build_id: str
    migration_versions: tuple[int, ...]
    sqlite_version: str
    journal_mode: str
    synchronous: int
    foreign_keys: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "user_version": self.user_version,
            "controller_build_id": self.controller_build_id,
            "migration_versions": list(self.migration_versions),
            "sqlite_version": self.sqlite_version,
            "journal_mode": self.journal_mode,
            "synchronous": self.synchronous,
            "foreign_keys": self.foreign_keys,
        }


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    idempotency_key: str
    kind: str
    resource_type: str
    resource_id: str
    actor_type: str
    actor_id: str | None
    authorization_id: str | None
    request_digest: str
    expected_resource_version: int | None
    writer_epoch: int | None
    state: str
    step: str
    request_json: str
    result_json: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    error_code: str | None
    error_detail: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "OperationRecord":
        return cls(**{field.name: row[field.name] for field in fields(cls)})

    def as_dict(self) -> dict[str, Any]:
        result = {field.name: getattr(self, field.name) for field in fields(self)}
        for key in ("request_json", "result_json"):
            if result[key] is not None:
                result[key] = parse_canonical_json(result[key])
        return result


@dataclass(frozen=True)
class EventRecord:
    sequence: int
    event_id: str
    event_kind: str
    resource_type: str
    resource_id: str
    resource_version: int | None
    operation_id: str | None
    payload_json: str
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "EventRecord":
        return cls(**{field.name: row[field.name] for field in fields(cls)})

    def as_dict(self) -> dict[str, Any]:
        result = {field.name: getattr(self, field.name) for field in fields(self)}
        result["payload_json"] = parse_canonical_json(self.payload_json)
        return result


@dataclass(frozen=True)
class ConsumerCursor:
    consumer_id: str
    last_sequence: int
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "consumer_id": self.consumer_id,
            "last_sequence": self.last_sequence,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class CASResult:
    resource_id: str
    previous_version: int
    resource_version: int


T = TypeVar("T")


def row_to_dict(row: Any) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    if isinstance(row, Mapping):
        return dict(row)
    raise TypeError("expected a sqlite row or mapping")


__all__ = [
    "CASResult",
    "ChildSource",
    "ConsumerCursor",
    "EventRecord",
    "ID_PREFIXES",
    "ID_RE",
    "OperationRecord",
    "PI_SESSION_ID_RE",
    "SchemaStatus",
    "bounded_text",
    "canonical_json",
    "canonical_json_bytes",
    "json_digest",
    "new_id",
    "parse_canonical_json",
    "row_to_dict",
    "utc_now",
    "validate_child_source",
    "validate_id",
    "validate_pi_session_id",
]
