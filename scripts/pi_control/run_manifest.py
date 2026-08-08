"""Strict immutable run-manifest construction and secure file persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from typing import Any, Mapping

from .errors import InvalidRequestError, UnsafeDatabaseError
from .models import canonical_json, json_digest, new_id, validate_id, utc_now
from .process_adapter import process_start_identity

MANIFEST_SCHEMA_VERSION = 1
_MANIFEST_KEYS = frozenset({
    "schemaVersion", "runId", "operationId", "taskId", "conversationId", "piSessionId",
    "parentRunId", "project", "workingCopy", "authority", "runtime", "owner",
    "capabilityHash", "attestationNonce", "createdAt", "expiresAt", "manifestDigest",
})
_PROJECT_KEYS = frozenset({"projectId", "resourceVersion", "objectFormat", "trustMode", "policyHash"})
_WORKING_COPY_KEYS = frozenset({"workingCopyId", "resourceVersion", "kind", "purpose", "effectiveMode", "hostPath", "gitCommonDir", "gitDir", "branchRef", "headOid", "treeOid", "dirtyFingerprint", "writerEpoch"})
_RUNTIME_KEYS = frozenset({"runtimeSpecVersion", "runtimeSpecHash", "executionTarget", "platform", "imageDigest", "controllerBuildId", "piVersion"})
_OWNER_KEYS = frozenset({"uid", "gid", "pid", "processStartIdentity"})
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ManifestError(ValueError):
    pass


def _utc_timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ManifestError(f"{name} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ManifestError(f"{name} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ManifestError(f"{name} must be UTC")
    return parsed


class _NoOpFailpoint:
    def hit(self, name: str, context: Mapping[str, str]) -> None:
        return None


@dataclass(frozen=True)
class ManifestFile:
    path: str
    digest: str
    size_bytes: int
    manifest: Mapping[str, Any]


def capability_hash(secret: str | bytes) -> str:
    raw = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    if len(raw) < 32:
        raise ManifestError("capability secret is too short")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _require_keys(value: Mapping[str, Any], allowed: frozenset[str], *, name: str) -> None:
    if set(value) != set(allowed):
        raise ManifestError(f"{name} fields do not match manifest schema")


def _oid(value: Any, object_format: str, *, name: str, allow_none: bool = True) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or any(character not in "0123456789abcdef" for character in value.lower()):
        raise ManifestError(f"{name} is not a hexadecimal object ID")
    expected = 40 if object_format == "sha1" else 64 if object_format == "sha256" else 0
    if len(value) != expected:
        raise ManifestError(f"{name} has the wrong length for {object_format}")
    return value.lower()


def _path(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise ManifestError(f"{name} must be an absolute path")
    candidate = Path(value)
    if candidate.is_symlink() or not candidate.exists():
        raise ManifestError(f"{name} is not an existing non-symlink path")
    return str(candidate.resolve(strict=True))


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        raise ManifestError("manifest must be an object")
    _require_keys(manifest, _MANIFEST_KEYS, name="manifest")
    if manifest["schemaVersion"] != MANIFEST_SCHEMA_VERSION:
        raise ManifestError("unsupported manifest schema version")
    for key, prefix in (("runId", "run"), ("operationId", "op"), ("conversationId", "conv")):
        validate_id(manifest[key], prefix=prefix)
    if manifest["parentRunId"] is not None:
        validate_id(manifest["parentRunId"], prefix="run")
    if manifest["taskId"] is not None and (not isinstance(manifest["taskId"], str) or len(manifest["taskId"]) > 256):
        raise ManifestError("taskId is invalid")
    if not isinstance(manifest["piSessionId"], str) or not manifest["piSessionId"]:
        raise ManifestError("piSessionId is invalid")
    project = manifest["project"]
    _require_keys(project, _PROJECT_KEYS, name="project")
    validate_id(project["projectId"], prefix="prj")
    if not isinstance(project["resourceVersion"], int) or project["resourceVersion"] < 1:
        raise ManifestError("project resourceVersion is invalid")
    if project["objectFormat"] not in {"sha1", "sha256"}:
        raise ManifestError("project objectFormat is invalid")
    if project["trustMode"] not in {"trusted", "isolated"}:
        raise ManifestError("project trustMode is invalid")
    if not isinstance(project["policyHash"], str) or _SHA256_RE.fullmatch(project["policyHash"]) is None:
        raise ManifestError("project policyHash is invalid")
    working = manifest["workingCopy"]
    if manifest["authority"] == "writer" and working is None:
        raise ManifestError("writer manifests require a working copy")
    if manifest["authority"] in {"secretary", "host-maintenance"} and working is not None:
        raise ManifestError("this authority cannot bind a working copy")
    if working is not None:
        _require_keys(working, _WORKING_COPY_KEYS, name="workingCopy")
        validate_id(working["workingCopyId"], prefix="wc")
        if not isinstance(working["resourceVersion"], int) or working["resourceVersion"] < 1:
            raise ManifestError("working-copy resourceVersion is invalid")
        if working["kind"] not in {"primary", "worktree", "isolated", "review"}:
            raise ManifestError("working-copy kind is invalid")
        if working["purpose"] not in {"personal", "workstream", "integration", "review", "recovery", "other"}:
            raise ManifestError("working-copy purpose is invalid")
        if working["effectiveMode"] not in {"trusted-live", "isolated", "read-only"}:
            raise ManifestError("working-copy effectiveMode is invalid")
        if manifest["authority"] == "writer" and working["effectiveMode"] == "read-only":
            raise ManifestError("writer manifests cannot use a read-only working copy")
        for key in ("hostPath", "gitCommonDir", "gitDir"):
            _path(working[key], name=key)
        _oid(working["headOid"], project["objectFormat"], name="headOid")
        _oid(working["treeOid"], project["objectFormat"], name="treeOid")
        if working["branchRef"] is not None and (not isinstance(working["branchRef"], str) or not working["branchRef"].startswith("refs/")):
            raise ManifestError("branchRef is invalid")
        if working["writerEpoch"] is not None and (not isinstance(working["writerEpoch"], int) or working["writerEpoch"] < 0):
            raise ManifestError("writerEpoch is invalid")
        if manifest["authority"] == "writer" and (not isinstance(working["writerEpoch"], int) or working["writerEpoch"] < 1):
            raise ManifestError("writer manifests require a positive writer epoch")
    runtime = manifest["runtime"]
    _require_keys(runtime, _RUNTIME_KEYS, name="runtime")
    if runtime["runtimeSpecVersion"] != 1 or not isinstance(runtime["runtimeSpecHash"], str) or _SHA256_RE.fullmatch(runtime["runtimeSpecHash"]) is None:
        raise ManifestError("runtime specification is invalid")
    for key in ("executionTarget", "platform", "controllerBuildId", "piVersion"):
        if not isinstance(runtime[key], str) or not runtime[key] or len(runtime[key]) > 512:
            raise ManifestError(f"runtime {key} is invalid")
    if not isinstance(runtime["imageDigest"], str) or _SHA256_RE.fullmatch(runtime["imageDigest"]) is None:
        raise ManifestError("runtime imageDigest is invalid")
    owner = manifest["owner"]
    _require_keys(owner, _OWNER_KEYS, name="owner")
    for key in ("uid", "gid", "pid"):
        if not isinstance(owner[key], int) or owner[key] < 0:
            raise ManifestError(f"owner {key} is invalid")
    if not isinstance(owner["processStartIdentity"], str) or not owner["processStartIdentity"]:
        raise ManifestError("owner processStartIdentity is invalid")
    if not isinstance(manifest["capabilityHash"], str) or _SHA256_RE.fullmatch(manifest["capabilityHash"]) is None:
        raise ManifestError("capabilityHash is invalid")
    if not isinstance(manifest["attestationNonce"], str) or len(manifest["attestationNonce"]) < 20:
        raise ManifestError("attestationNonce is invalid")
    created_at = _utc_timestamp(manifest["createdAt"], "createdAt")
    if manifest["expiresAt"] is not None:
        expires_at = _utc_timestamp(manifest["expiresAt"], "expiresAt")
        if expires_at <= created_at:
            raise ManifestError("expiresAt must be later than createdAt")
    if not isinstance(manifest["authority"], str) or manifest["authority"] not in {"read-only", "writer", "secretary", "host-maintenance"}:
        raise ManifestError("authority is invalid")
    expected_digest = manifest_digest(manifest)
    if manifest["manifestDigest"] != expected_digest:
        raise ManifestError("manifestDigest does not match canonical content")
    return dict(manifest)


def require_manifest_active(manifest: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    checked = validate_manifest(manifest)
    if checked["expiresAt"] is None:
        return checked
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ManifestError("manifest activity time must be timezone-aware")
    if _utc_timestamp(checked["expiresAt"], "expiresAt") <= current.astimezone(timezone.utc):
        raise ManifestError("run manifest has expired")
    return checked


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    content = dict(manifest)
    content.pop("manifestDigest", None)
    return "sha256:" + hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def build_manifest(
    store: Any,
    run_id: str,
    *,
    operation_id: str,
    capability_secret: str | bytes,
    runtime: Mapping[str, Any] | None = None,
    task_id: str | None = None,
    owner: Mapping[str, Any] | None = None,
    attestation_nonce: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    run = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise ManifestError("run does not exist")
    conversation = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (run["conversation_id"],)).fetchone()
    project_id = run["project_id"]
    project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone() if project_id else None
    working = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (run["working_copy_id"],)).fetchone() if run["working_copy_id"] else None
    if conversation is None or project is None:
        raise ManifestError("run bindings are incomplete")
    runtime_input = dict(runtime or {})
    runtime_value = {
        "runtimeSpecVersion": 1,
        "runtimeSpecHash": run["runtime_spec_hash"],
        "executionTarget": runtime_input.pop("executionTarget", "linux-container"),
        "platform": runtime_input.pop("platform", "linux/amd64"),
        "imageDigest": runtime_input.pop("imageDigest", "sha256:" + "0" * 64),
        "controllerBuildId": runtime_input.pop("controllerBuildId", run["build_id"]),
        "piVersion": runtime_input.pop("piVersion", "unknown"),
    }
    if runtime_input:
        raise ManifestError("runtime contains fields outside Phase 4 manifest schema")
    if owner is None:
        owner = {"uid": os.getuid() if hasattr(os, "getuid") else 0, "gid": os.getgid() if hasattr(os, "getgid") else 0, "pid": os.getpid(), "processStartIdentity": process_start_identity(os.getpid())}
    owner_value = dict(owner)
    if working is None:
        working_value = None
    else:
        working_value = {
            "workingCopyId": working["working_copy_id"], "resourceVersion": int(working["resource_version"]),
            "kind": working["kind"], "purpose": working["purpose"], "effectiveMode": working["effective_mode"],
            "hostPath": str(Path(working["path"]).resolve(strict=True)), "gitCommonDir": str(Path(project["git_common_dir"]).resolve(strict=True)),
            "gitDir": str(Path(working["git_dir"] or project["git_common_dir"]).resolve(strict=True)), "branchRef": working["branch_ref"],
            "headOid": working["expected_head_oid"], "treeOid": working["expected_tree_oid"],
            "dirtyFingerprint": run["dirty_fingerprint"], "writerEpoch": run["writer_epoch"],
        }
    manifest: dict[str, Any] = {
        "schemaVersion": 1, "runId": run_id, "operationId": operation_id, "taskId": task_id,
        "conversationId": conversation["conversation_id"], "piSessionId": conversation["pi_session_id"],
        "parentRunId": run["parent_run_id"],
        "project": {"projectId": project["project_id"], "resourceVersion": int(project["resource_version"]), "objectFormat": project["object_format"], "trustMode": project["trust_mode"], "policyHash": project["policy_hash"]},
        "workingCopy": working_value, "authority": run["authority"], "runtime": runtime_value,
        "owner": owner_value, "capabilityHash": capability_hash(capability_secret),
        "attestationNonce": attestation_nonce or secrets.token_urlsafe(24), "createdAt": utc_now(), "expiresAt": expires_at,
        "manifestDigest": "",
    }
    if manifest["capabilityHash"] != run["capability_hash"]:
        raise ManifestError("capability does not match the run claim")
    manifest["manifestDigest"] = manifest_digest(manifest)
    return validate_manifest(manifest)


def _secure_manifest_parent(parent: Path) -> None:
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise UnsafeDatabaseError("manifest parent is unsafe")
    if hasattr(os, "geteuid") and parent.stat().st_uid != os.geteuid():
        raise UnsafeDatabaseError("manifest parent is not user-owned")
    if stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise UnsafeDatabaseError("manifest parent is too permissive")
    os.chmod(parent, 0o700)


def write_manifest(path: os.PathLike[str] | str, manifest: Mapping[str, Any], *, failpoint: Any | None = None) -> ManifestFile:
    checked = validate_manifest(manifest)
    destination = Path(path).expanduser()
    if destination.exists() or destination.is_symlink():
        raise ManifestError("manifest destination already exists")
    _secure_manifest_parent(destination.parent)
    controller = failpoint or _NoOpFailpoint()
    controller.hit("manifest.write.before", {"run_id": checked["runId"]})
    data = canonical_json(checked).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".manifest-", dir=str(destination.parent))
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        os.chmod(destination, 0o600)
        controller.hit("manifest.write.after", {"run_id": checked["runId"]})
    except BaseException:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise
    return ManifestFile(str(destination), hashlib.sha256(data).hexdigest(), len(data), checked)


def read_manifest(path: os.PathLike[str] | str) -> ManifestFile:
    destination = Path(path)
    info = destination.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ManifestError("manifest file permissions or type are invalid")
    data = destination.read_bytes()
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError("manifest JSON is invalid") from error
    checked = validate_manifest(value)
    if canonical_json(checked).encode("utf-8") != data:
        raise ManifestError("manifest is not canonical JSON")
    return ManifestFile(str(destination.resolve(strict=True)), hashlib.sha256(data).hexdigest(), len(data), checked)


__all__ = ["MANIFEST_SCHEMA_VERSION", "ManifestError", "ManifestFile", "build_manifest", "capability_hash", "manifest_digest", "read_manifest", "require_manifest_active", "validate_manifest", "write_manifest"]
