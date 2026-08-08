"""Immutable child artifacts and terminal reconciliation.

Artifact payloads are controller-owned files outside project repositories.  The
SQLite index and child terminal row are lifecycle authority; the filesystem
manifest is an independently verifiable content record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
from typing import Any, Mapping, Sequence

from .errors import ConstraintError, ControlPlaneError, IdempotencyConflictError, InvalidRequestError, NotFoundError, ResourceStaleError
from .events import append_event_in_transaction
from .models import ChildSource, canonical_json, parse_canonical_json, utc_now, validate_child_source, validate_id

_ARTIFACT_ID = re.compile(r"^art_[0-9a-f]{32}$")
_CHILD_ID = re.compile(r"^child_[0-9a-f]{32}$")
_OID = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_RETENTION_CLASSES = frozenset({"run", "change", "recovery", "debug"})
_TERMINAL_CLASSES = frozenset({"success", "failed", "lost", "attention"})
_CHANGED_STATES = frozenset({"clean", "dirty", "unknown"})
_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_PROVENANCE_BYTES = 8 * 1024
_MAX_RESULT_BYTES = 16 * 1024


class ArtifactError(ControlPlaneError):
    """Base class for bounded artifact/terminal failures."""


class ArtifactConflictError(ArtifactError):
    """An immutable artifact identity is already bound to other content."""


class ArtifactIntegrityError(ArtifactError):
    """A manifest, payload, or database index does not verify."""


class TerminalStateError(ArtifactError):
    """A child terminal report violates the controller terminal contract."""


def _bounded_id(value: Any, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise InvalidRequestError(f"{name} is invalid")
    return value


def _bounded_mime(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value or any(ord(char) < 0x20 for char in value):
        raise InvalidRequestError("content type is invalid")
    return value


def _oid_or_none(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        raise InvalidRequestError(f"{name} is not a Git object ID")
    return value


def _sha256_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _manifest_digest(value: Mapping[str, Any]) -> str:
    body = dict(value)
    body.pop("manifestDigest", None)
    return _sha256_digest(canonical_json(body, max_bytes=_MAX_MANIFEST_BYTES).encode("utf-8"))


def _now_datetime(value: str | None = None) -> datetime:
    text = value or utc_now()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidRequestError("artifact timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise InvalidRequestError("artifact timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _safe_provenance(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidRequestError("artifact provenance must be an object")
    allowed = {"source", "producer", "createdAt", "sourceDigest", "toolVersion", "model", "provider", "evidenceDigest"}
    if any(key not in allowed or not isinstance(key, str) for key in value):
        raise InvalidRequestError("artifact provenance contains an unsupported field")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(item, str) or not item or len(item) > 512 or "\x00" in item:
            raise InvalidRequestError("artifact provenance text is invalid")
        if key.endswith("Digest") and not (_SHA256.fullmatch(item) or re.fullmatch(r"[0-9a-f]{64}", item)):
            raise InvalidRequestError("artifact provenance digest is invalid")
        result[key] = item
    canonical_json(result, max_bytes=_MAX_PROVENANCE_BYTES)
    return result


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor or os.curdir)
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        current = current / part
        try:
            if current.is_symlink():
                raise ArtifactIntegrityError("artifact path contains a symlink", detail={"path": str(path)})
        except OSError as error:
            raise ArtifactIntegrityError("artifact path cannot be inspected", detail={"path": str(path)}) from error


def _secure_directory(path: Path, *, create: bool = True) -> None:
    _reject_symlink_components(path)
    info = None
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            raise ArtifactIntegrityError("artifact directory is missing", detail={"path": str(path)})
        path.mkdir(mode=0o700)
        info = path.lstat()
    if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ArtifactIntegrityError("artifact path is not a directory", detail={"path": str(path)})
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise ArtifactIntegrityError("artifact directory is not owned by the controller")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(path, 0o700)
        info = path.lstat()
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ArtifactIntegrityError("artifact directory permissions are not private")


def _secure_file(path: Path, *, max_bytes: int) -> bytes:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise ArtifactIntegrityError("artifact file is missing", detail={"path": str(path)}) from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ArtifactIntegrityError("artifact file is not a regular file", detail={"path": str(path)})
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise ArtifactIntegrityError("artifact file is not owned by the controller")
    if stat.S_IMODE(info.st_mode) != 0o600:
        os.chmod(path, 0o600)
        info = path.lstat()
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > max_bytes:
        raise ArtifactIntegrityError("artifact file permissions or size are invalid")
    return path.read_bytes()


def _atomic_write(path: Path, content: bytes) -> None:
    if len(content) > _MAX_ARTIFACT_BYTES:
        raise InvalidRequestError("artifact exceeds the size bound")
    temporary = path.parent / ("." + path.name + "." + secrets.token_hex(8) + ".tmp")
    descriptor = -1
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        descriptor = -1
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError as error:
        raise ArtifactConflictError("artifact destination already exists") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    run_id: str
    project_id: str
    source: Mapping[str, Any]
    content_type: str
    size_bytes: int
    checksum: str
    sensitive: bool
    retention_class: str
    storage_path: str
    created_at: str
    expires_at: str | None
    manifest_path: str
    manifest_digest: str
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        _bounded_id(self.artifact_id, _ARTIFACT_ID, "artifact ID")
        validate_id(self.run_id, prefix="run")
        validate_id(self.project_id, prefix="prj")
        validate_child_source(self.source)
        _bounded_mime(self.content_type)
        if not isinstance(self.size_bytes, int) or self.size_bytes < 0 or self.size_bytes > _MAX_ARTIFACT_BYTES:
            raise InvalidRequestError("artifact size is invalid")
        if _SHA256.fullmatch(self.checksum) is None:
            raise InvalidRequestError("artifact checksum is invalid")
        if not isinstance(self.sensitive, bool) or self.retention_class not in _RETENTION_CLASSES:
            raise InvalidRequestError("artifact retention metadata is invalid")
        if not isinstance(self.storage_path, str) or self.storage_path != f"{self.artifact_id}/content.bin":
            raise InvalidRequestError("artifact storage path is not controller-derived")
        if not isinstance(self.manifest_path, str) or self.manifest_path != f"{self.artifact_id}/manifest.json":
            raise InvalidRequestError("artifact manifest path is not controller-derived")
        _now_datetime(self.created_at)
        if self.expires_at is not None:
            _now_datetime(self.expires_at)
        _safe_provenance(self.provenance)
        if _SHA256.fullmatch(self.manifest_digest) is None:
            raise InvalidRequestError("artifact manifest digest is invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "artifactId": self.artifact_id,
            "runId": self.run_id,
            "projectId": self.project_id,
            "source": dict(self.source),
            "contentType": self.content_type,
            "sizeBytes": self.size_bytes,
            "checksum": self.checksum,
            "sensitive": self.sensitive,
            "retentionClass": self.retention_class,
            "storagePath": self.storage_path,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "provenance": dict(self.provenance),
            "manifestPath": self.manifest_path,
            "manifestDigest": self.manifest_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactRecord":
        expected = {
            "schemaVersion", "artifactId", "runId", "projectId", "source", "contentType", "sizeBytes",
            "checksum", "sensitive", "retentionClass", "storagePath", "createdAt", "expiresAt",
            "provenance", "manifestPath", "manifestDigest",
        }
        if not isinstance(value, Mapping) or set(value) != expected or value.get("schemaVersion") != 1:
            raise ArtifactIntegrityError("artifact manifest schema is not exact")
        calculated = _manifest_digest(value)
        if value.get("manifestDigest") != calculated:
            raise ArtifactIntegrityError("artifact manifest digest does not match")
        return cls(
            artifact_id=value["artifactId"], run_id=value["runId"], project_id=value["projectId"],
            source=value["source"], content_type=value["contentType"], size_bytes=value["sizeBytes"],
            checksum=value["checksum"], sensitive=value["sensitive"], retention_class=value["retentionClass"],
            storage_path=value["storagePath"], created_at=value["createdAt"], expires_at=value["expiresAt"],
            manifest_path=value["manifestPath"], manifest_digest=value["manifestDigest"], provenance=value["provenance"],
        )


class ArtifactStore:
    """Secure external artifact storage with immutable manifest verification."""

    def __init__(self, state_root: os.PathLike[str] | str):
        self.state_root = Path(os.path.abspath(os.path.expanduser(os.fspath(state_root))))
        _secure_directory(self.state_root)
        self.root = self.state_root / "artifacts"
        _secure_directory(self.root)

    def _paths(self, artifact_id: str) -> tuple[Path, Path, Path]:
        _bounded_id(artifact_id, _ARTIFACT_ID, "artifact ID")
        directory = self.root / artifact_id
        return directory, directory / "content.bin", directory / "manifest.json"

    def put(
        self,
        *,
        run_id: str,
        project_id: str,
        producer_child_id: str,
        source: Mapping[str, Any] | ChildSource,
        content: bytes | bytearray | memoryview | str,
        content_type: str,
        sensitive: bool = False,
        retention_class: str = "run",
        provenance: Mapping[str, Any] | None = None,
        artifact_id: str | None = None,
    ) -> ArtifactRecord:
        validate_id(run_id, prefix="run")
        validate_id(project_id, prefix="prj")
        _bounded_id(producer_child_id, _CHILD_ID, "producer child ID")
        source_value = source.as_dict() if isinstance(source, ChildSource) else validate_child_source(source)
        content_type = _bounded_mime(content_type)
        if not isinstance(sensitive, bool) or retention_class not in _RETENTION_CLASSES:
            raise InvalidRequestError("artifact retention metadata is invalid")
        if isinstance(content, str):
            payload = content.encode("utf-8")
        elif isinstance(content, (bytes, bytearray, memoryview)):
            payload = bytes(content)
        else:
            raise InvalidRequestError("artifact content must be bytes or text")
        if len(payload) > _MAX_ARTIFACT_BYTES:
            raise InvalidRequestError("artifact exceeds the size bound")
        artifact_id = artifact_id or ("art_" + secrets.token_hex(16))
        directory, content_path, manifest_path = self._paths(artifact_id)
        created_at = utc_now()
        expires_at = None
        if retention_class == "run":
            expires_at = (datetime.fromisoformat(created_at.replace("Z", "+00:00")) + timedelta(days=30)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        supplied_provenance = dict(provenance or {})
        if "producer" in supplied_provenance and supplied_provenance["producer"] != producer_child_id:
            raise InvalidRequestError("artifact provenance producer does not match child")
        supplied_provenance["producer"] = producer_child_id
        provenance_value = _safe_provenance(supplied_provenance)
        body: dict[str, Any] = {
            "schemaVersion": 1,
            "artifactId": artifact_id,
            "runId": run_id,
            "projectId": project_id,
            "source": source_value,
            "contentType": content_type,
            "sizeBytes": len(payload),
            "checksum": _sha256_digest(payload),
            "sensitive": sensitive,
            "retentionClass": retention_class,
            "storagePath": f"{artifact_id}/content.bin",
            "createdAt": created_at,
            "expiresAt": expires_at,
            "provenance": provenance_value,
            "manifestPath": f"{artifact_id}/manifest.json",
        }
        body["manifestDigest"] = _manifest_digest(body)
        record = ArtifactRecord.from_dict(body)
        if directory.exists() or directory.is_symlink():
            _secure_directory(directory, create=False)
            existing = self.verify(artifact_id)
            if existing.as_dict() != record.as_dict() or _secure_file(content_path, max_bytes=_MAX_ARTIFACT_BYTES) != payload:
                raise ArtifactConflictError("artifact identity is already bound to different content")
            return existing
        directory.mkdir(mode=0o700)
        try:
            _atomic_write(content_path, payload)
            _atomic_write(manifest_path, canonical_json(record.as_dict(), max_bytes=_MAX_MANIFEST_BYTES).encode("utf-8"))
            return self.verify(artifact_id)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def verify(self, artifact_id: str) -> ArtifactRecord:
        directory, content_path, manifest_path = self._paths(artifact_id)
        _secure_directory(directory, create=False)
        manifest_bytes = _secure_file(manifest_path, max_bytes=_MAX_MANIFEST_BYTES)
        try:
            value = parse_canonical_json(manifest_bytes, max_bytes=_MAX_MANIFEST_BYTES)
            if manifest_bytes != canonical_json(value, max_bytes=_MAX_MANIFEST_BYTES).encode("utf-8"):
                raise ArtifactIntegrityError("artifact manifest is not canonical JSON")
        except ControlPlaneError as error:
            raise ArtifactIntegrityError("artifact manifest is not valid canonical JSON") from error
        record = ArtifactRecord.from_dict(value)
        if record.artifact_id != artifact_id:
            raise ArtifactIntegrityError("artifact ID does not match its path")
        payload = _secure_file(content_path, max_bytes=_MAX_ARTIFACT_BYTES)
        if len(payload) != record.size_bytes or _sha256_digest(payload) != record.checksum:
            raise ArtifactIntegrityError("artifact payload checksum or size does not match manifest")
        return record

    load = verify

    def eligible_cleanup(
        self,
        *,
        now: str | None = None,
        referenced_artifact_ids: Sequence[str] = (),
        live_artifact_ids: Sequence[str] = (),
        dry_run: bool = True,
        authorize: bool = False,
    ) -> list[dict[str, Any]]:
        if not dry_run and not authorize:
            raise InvalidRequestError("artifact cleanup requires explicit authorization")
        current = _now_datetime(now)
        referenced = set(referenced_artifact_ids)
        live = set(live_artifact_ids)
        results: list[dict[str, Any]] = []
        for entry in sorted(self.root.iterdir(), key=lambda item: item.name):
            if not entry.name.startswith("art_"):
                continue
            record = self.verify(entry.name)
            if record.retention_class != "run" or record.expires_at is None or _now_datetime(record.expires_at) > current:
                continue
            if record.artifact_id in referenced or record.artifact_id in live:
                continue
            candidate = {"artifactId": record.artifact_id, "manifestDigest": record.manifest_digest, "eligible": True}
            results.append(candidate)
            if not dry_run:
                directory, _, _ = self._paths(record.artifact_id)
                _secure_directory(directory, create=False)
                shutil.rmtree(directory)
        return results


def _register_artifact_in_transaction(connection: Any, record: ArtifactRecord) -> None:
    existing = connection.execute("SELECT * FROM artifact_manifests WHERE artifact_id=?", (record.artifact_id,)).fetchone()
    values = (
        record.artifact_id, record.run_id, record.project_id, record.manifest_path, record.manifest_digest,
        record.checksum, record.size_bytes, int(record.sensitive), record.retention_class,
        record.created_at, record.expires_at, 1,
    )
    if existing is not None:
        if tuple(existing) != values:
            raise ArtifactConflictError("artifact index identity is already bound to different metadata")
        return
    connection.execute(
        "INSERT INTO artifact_manifests(artifact_id,run_id,project_id,manifest_path,manifest_digest,checksum,size_bytes,sensitive,retention_class,created_at,expires_at,resource_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        values,
    )


def register_artifact(store: Any, record: ArtifactRecord) -> ArtifactRecord:
    with store.transaction():
        run = store.conn.execute("SELECT project_id FROM runs WHERE run_id=?", (record.run_id,)).fetchone()
        if run is None:
            raise NotFoundError("artifact producer run was not found", detail={"run_id": record.run_id})
        if run[0] != record.project_id:
            raise ConstraintError("artifact project does not match producer run")
        _register_artifact_in_transaction(store.conn, record)
    return record


@dataclass(frozen=True)
class TerminalRecord:
    child_run_id: str
    parent_run_id: str
    terminal_class: str
    changed_state: str
    submission_class: str
    submitted_change_id: str | None
    submitted_revision: int | None
    artifact_id: str | None
    result: Mapping[str, Any]
    provenance: Mapping[str, Any]
    terminal_digest: str
    observed_state: str
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "childRunId": self.child_run_id,
            "parentRunId": self.parent_run_id,
            "terminalClass": self.terminal_class,
            "changedState": self.changed_state,
            "submissionClass": self.submission_class,
            "submittedChangeId": self.submitted_change_id,
            "submittedRevision": self.submitted_revision,
            "artifactId": self.artifact_id,
            "result": dict(self.result),
            "provenance": dict(self.provenance),
            "terminalDigest": self.terminal_digest,
            "observedState": self.observed_state,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


def _terminal_from_rows(run: Mapping[str, Any], terminal: Mapping[str, Any]) -> TerminalRecord:
    return TerminalRecord(
        child_run_id=str(terminal["child_run_id"]), parent_run_id=str(terminal["parent_run_id"]),
        terminal_class=str(terminal["terminal_class"]), changed_state=str(terminal["changed_state"]),
        submission_class=str(terminal["submission_class"]), submitted_change_id=terminal["submitted_change_id"],
        submitted_revision=terminal["submitted_revision"], artifact_id=terminal["artifact_id"],
        result=parse_canonical_json(str(terminal["result_json"])),
        provenance=parse_canonical_json(str(terminal["provenance_json"])),
        terminal_digest=str(terminal["terminal_digest"]), observed_state=str(run["observed_state"]),
        created_at=str(terminal["created_at"]), updated_at=str(terminal["updated_at"]),
    )


def _changed_state(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"headOid", "treeOid", "dirty", "dirtyFingerprint"}:
        raise InvalidRequestError("terminal changed-state declaration is not exact")
    if not isinstance(value["dirty"], bool):
        raise InvalidRequestError("terminal dirty state must be boolean")
    head = _oid_or_none(value["headOid"], "changed HEAD")
    tree = _oid_or_none(value["treeOid"], "changed tree")
    fingerprint = value["dirtyFingerprint"]
    if fingerprint is not None and (not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None):
        raise InvalidRequestError("dirty fingerprint is invalid")
    if value["dirty"] and fingerprint is None:
        raise InvalidRequestError("dirty state requires a fingerprint")
    if not value["dirty"] and fingerprint is not None:
        raise InvalidRequestError("clean state cannot carry a dirty fingerprint")
    return {"headOid": head, "treeOid": tree, "dirty": value["dirty"], "dirtyFingerprint": fingerprint}


def _changed_label(changed: Mapping[str, Any]) -> str:
    if changed["dirty"]:
        return "dirty"
    if changed["headOid"] is None and changed["treeOid"] is None:
        return "unknown"
    return "clean"


def _terminal_result(summary: Any, changed: Mapping[str, Any], artifact_id: str | None) -> dict[str, Any]:
    if not isinstance(summary, str) or len(summary) > 4096 or "\x00" in summary:
        raise InvalidRequestError("terminal result summary is invalid")
    result = {"summary": summary, "changedState": dict(changed), "artifactId": artifact_id}
    canonical_json(result, max_bytes=_MAX_RESULT_BYTES)
    return result


def _terminal_row(store: Any, child_run_id: str) -> tuple[Any, Any]:
    run = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (child_run_id,)).fetchone()
    if run is None:
        raise NotFoundError("child run was not found", detail={"run_id": child_run_id})
    if run["parent_run_id"] is None or run["child_source_json"] is None:
        raise ConstraintError("terminal reconciliation requires a child run")
    terminal = store.conn.execute("SELECT * FROM child_terminal_records WHERE child_run_id=?", (child_run_id,)).fetchone()
    return run, terminal


def reconcile_child_terminal(
    store: Any,
    *,
    child_run_id: str,
    parent_run_id: str,
    terminal_class: str,
    changed_state: Mapping[str, Any],
    source: Mapping[str, Any] | ChildSource,
    child_id: str,
    result_summary: str = "",
    artifact_id: str | None = None,
    submitted_change_id: str | None = None,
    submitted_revision: int | None = None,
    provenance: Mapping[str, Any] | None = None,
    expected_resource_version: int | None = None,
    operation_id: str | None = None,
    artifact: ArtifactRecord | None = None,
) -> TerminalRecord:
    """Record one immutable child terminal result and its lifecycle projection."""

    validate_id(child_run_id, prefix="run")
    validate_id(parent_run_id, prefix="run")
    _bounded_id(child_id, _CHILD_ID, "child ID")
    if terminal_class not in _TERMINAL_CLASSES:
        raise InvalidRequestError("terminal class is invalid")
    source_value = source.as_dict() if isinstance(source, ChildSource) else validate_child_source(source)
    changed = _changed_state(changed_state)
    provenance_value = _safe_provenance(provenance)
    if artifact_id is not None:
        _bounded_id(artifact_id, _ARTIFACT_ID, "artifact ID")
    if artifact is not None:
        if artifact_id != artifact.artifact_id:
            raise ConstraintError("terminal artifact ID does not match supplied artifact")
        if dict(artifact.source) != source_value or artifact.provenance.get("producer") != child_id:
            raise ConstraintError("artifact source or child provenance does not match terminal request")
    if submitted_change_id is None:
        if submitted_revision is not None:
            raise InvalidRequestError("submitted revision requires a submitted change")
    else:
        validate_id(submitted_change_id, prefix="chg")
        if not isinstance(submitted_revision, int) or submitted_revision < 1:
            raise InvalidRequestError("submitted revision is invalid")
    submission_class = "submitted-change" if submitted_change_id is not None else "none"
    result = _terminal_result(result_summary, changed, artifact_id)
    terminal_identity = {
        "childRunId": child_run_id, "parentRunId": parent_run_id, "terminalClass": terminal_class,
        "changedState": _changed_label(changed), "submissionClass": submission_class,
        "submittedChangeId": submitted_change_id, "submittedRevision": submitted_revision,
        "artifactId": artifact_id, "source": source_value, "result": result, "provenance": provenance_value,
    }
    terminal_digest = _sha256_digest(canonical_json(terminal_identity).encode("utf-8"))
    now = utc_now()
    with store.transaction():
        run, existing = _terminal_row(store, child_run_id)
        if run["parent_run_id"] != parent_run_id:
            raise ConstraintError("child parent run does not match terminal request")
        stored_source = validate_child_source(parse_canonical_json(str(run["child_source_json"])))
        if stored_source != source_value:
            raise ConstraintError("terminal source does not match durable child source")
        if stored_source["authority"] != run["authority"]:
            raise ConstraintError("durable child authority is inconsistent")
        if existing is not None:
            if existing["terminal_digest"] != terminal_digest:
                raise IdempotencyConflictError(child_run_id, existing_digest=str(existing["terminal_digest"]), request_digest=terminal_digest)
            return _terminal_from_rows(run, existing)
        if run["observed_state"] in {"stopped", "failed", "lost", "needs_attention"}:
            raise TerminalStateError("child run is already terminal without a terminal record")
        changed_label = _changed_label(changed)
        if run["authority"] == "read-only" and changed["dirty"]:
            raise TerminalStateError("read-only child cannot report dirty state")
        if run["authority"] == "read-only" and submitted_change_id is not None:
            raise TerminalStateError("read-only child cannot submit a change")
        if terminal_class == "success" and changed_label != "clean":
            raise TerminalStateError("child success requires a clean observed state")
        if terminal_class == "lost" and (changed_label != "unknown" or submitted_change_id is not None):
            raise TerminalStateError("lost child requires unknown/unsubmitted state")
        if terminal_class == "failed" and submitted_change_id is not None:
            raise TerminalStateError("failed child cannot carry a submitted change")
        if submitted_change_id is not None:
            if run["authority"] != "writer":
                raise TerminalStateError("only writer children can submit a change")
            revision = store.conn.execute(
                "SELECT c.project_id FROM change_revisions r JOIN changes c ON c.change_id=r.change_id WHERE r.change_id=? AND r.revision=?",
                (submitted_change_id, submitted_revision),
            ).fetchone()
            if revision is None or revision[0] != run["project_id"]:
                raise ConstraintError("submitted change revision is not a project revision")
        if artifact is not None:
            _register_artifact_in_transaction(store.conn, artifact)
        artifact_record = None
        if artifact_id is not None:
            artifact_record = store.conn.execute("SELECT * FROM artifact_manifests WHERE artifact_id=?", (artifact_id,)).fetchone()
            if artifact_record is None:
                raise NotFoundError("artifact manifest was not indexed", detail={"artifact_id": artifact_id})
            if artifact_record["run_id"] != child_run_id or artifact_record["project_id"] != run["project_id"]:
                raise ConstraintError("artifact provenance does not match child run")
        if expected_resource_version is not None and (not isinstance(expected_resource_version, int) or expected_resource_version < 1):
            raise InvalidRequestError("expected run resource version is invalid")
        expected = int(run["resource_version"]) if expected_resource_version is None else expected_resource_version
        if int(run["resource_version"]) != expected:
            raise ResourceStaleError(child_run_id, expected, int(run["resource_version"]))
        observed_state = {"success": "stopped", "failed": "failed", "lost": "lost", "attention": "needs_attention"}[terminal_class]
        terminal_row = {
            "child_run_id": child_run_id, "parent_run_id": parent_run_id, "terminal_class": terminal_class,
            "changed_state": changed_label, "submission_class": submission_class,
            "submitted_change_id": submitted_change_id, "submitted_revision": submitted_revision,
            "artifact_id": artifact_id, "result_json": canonical_json(result),
            "provenance_json": canonical_json(provenance_value), "terminal_digest": terminal_digest,
            "created_at": now, "updated_at": now,
        }
        store.conn.execute(
            "INSERT INTO child_terminal_records(child_run_id,parent_run_id,terminal_class,changed_state,submission_class,submitted_change_id,submitted_revision,artifact_id,result_json,provenance_json,terminal_digest,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(terminal_row.values()),
        )
        error_code = None if terminal_class == "success" else "CP_CHILD_" + terminal_class.upper()
        cursor = store.conn.execute(
            "UPDATE runs SET desired_state='stopped',observed_state=?,ended_at=COALESCE(ended_at,?),updated_at=?,error_code=?,error_detail=?,resource_version=resource_version+1 WHERE run_id=? AND resource_version=?",
            (observed_state, now, now, error_code, canonical_json({"terminalDigest": terminal_digest}), child_run_id, expected),
        )
        if cursor.rowcount != 1:
            actual = store.conn.execute("SELECT resource_version FROM runs WHERE run_id=?", (child_run_id,)).fetchone()
            raise ResourceStaleError(child_run_id, expected, int(actual[0]) if actual else None)
        append_event_in_transaction(
            store.conn,
            event_kind="child.terminal",
            resource_type="run",
            resource_id=child_run_id,
            resource_version=expected + 1,
            operation_id=operation_id,
            payload={
                "runId": child_run_id, "parentRunId": parent_run_id, "terminalDigest": terminal_digest,
                "observedState": observed_state, "artifactId": artifact_id,
                "submittedChangeId": submitted_change_id, "submittedRevision": submitted_revision,
            },
        )
        return _terminal_from_rows({**dict(run), "observed_state": observed_state}, terminal_row)


def index_and_reconcile_child_terminal(
    store: Any,
    artifact_store: ArtifactStore,
    *,
    artifact: ArtifactRecord | None = None,
    **kwargs: Any,
) -> TerminalRecord:
    """Index a verified artifact and atomically record its child terminal state."""

    if artifact is not None:
        artifact_store.verify(artifact.artifact_id)
    return reconcile_child_terminal(store, artifact=artifact, **kwargs)


__all__ = [
    "ArtifactConflictError", "ArtifactError", "ArtifactIntegrityError", "ArtifactRecord", "ArtifactStore",
    "TerminalRecord", "TerminalStateError", "index_and_reconcile_child_terminal", "reconcile_child_terminal",
    "register_artifact",
]
