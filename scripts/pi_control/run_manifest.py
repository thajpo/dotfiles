"""Canonical cross-language run manifest for the greenfield controller."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Mapping

from .errors import UnsafeDatabaseError
from .models import canonical_json, utc_now, validate_id
from .process_adapter import process_start_identity
from .role_profiles import role_profile

MANIFEST_SCHEMA_VERSION = 2
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_BUILD_ID = re.compile(r"^build_[0-9a-f]{32}$")
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_TOP = frozenset({
    "schemaVersion", "runId", "operationId", "parentRunId", "conversation", "session",
    "project", "scope", "workingCopy", "installedBuild", "hostProcess", "toolRuntime",
    "channelBindingHash", "supervisorOwner", "createdAt", "expiresAt", "manifestDigest",
})
_CONVERSATION = frozenset({"conversationId", "role", "authorityProfile"})
_SESSION = frozenset({"piSessionId", "sessionPath"})
_PROJECT = frozenset({"projectId", "resourceVersion", "objectFormat", "trustMode", "policyHash"})
_SCOPE = frozenset({"source", "projectId", "projectResourceVersion", "workingCopyId", "workingCopyResourceVersion", "rootPath", "gitCommonDir", "branchRef", "headOid", "treeOid"})
_WORKING = frozenset({"workingCopyId", "projectId", "resourceVersion", "kind", "purpose", "effectiveMode", "hostPath", "gitDir", "writerEpoch"})
_BUILD = frozenset({"buildId", "buildManifestDigest", "resourceManifestDigest", "piVersion"})
_HOST = frozenset({"executable", "executableSha256", "argv", "toolProfile", "environmentKeys"})
_TOOL = frozenset({
    "specVersion", "specHash", "platform", "imageReference", "imageConfigId", "registryDigest", "command", "uid", "gid",
    "workdir", "mounts", "readOnlyRoot", "tmpfs", "networkMode", "capDrop", "securityOpt", "environment", "labels", "resources",
})
_OWNER = frozenset({"uid", "gid", "pid", "processStartIdentity"})


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ManifestFile:
    path: str
    digest: str
    size_bytes: int
    manifest: Mapping[str, Any]


def _exact(value: Any, keys: frozenset[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ManifestError(f"{name} fields do not match schema")
    return value


def _text(value: Any, name: str, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > limit:
        raise ManifestError(f"{name} is invalid")
    return value


def _digest(value: Any, name: str) -> str:
    result = _text(value, name, 71)
    if _SHA256.fullmatch(result) is None:
        raise ManifestError(f"{name} is not a canonical SHA-256 digest")
    return result


def _version(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ManifestError(f"{name} is invalid")
    return value


def _path(value: Any, name: str) -> str:
    result = _text(value, name)
    path = Path(result)
    if not path.is_absolute() or str(path) != os.path.normpath(result) or ".." in path.parts:
        raise ManifestError(f"{name} must be a normalized absolute path")
    return result


def _oid(value: Any, object_format: str, name: str) -> str | None:
    if value is None:
        return None
    result = _text(value, name, 64).lower()
    expected = 40 if object_format == "sha1" else 64
    if len(result) != expected or any(character not in "0123456789abcdef" for character in result):
        raise ManifestError(f"{name} is invalid for {object_format}")
    return result


def _timestamp(value: Any, name: str) -> datetime:
    result = _text(value, name, 64)
    if not result.endswith("Z"):
        raise ManifestError(f"{name} must be UTC")
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as error:
        raise ManifestError(f"{name} is invalid") from error
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ManifestError(f"{name} must be UTC")
    return parsed


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    content = dict(manifest)
    content.pop("manifestDigest", None)
    return "sha256:" + hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def executable_sha256(executable: os.PathLike[str] | str) -> str:
    """Hash one canonical regular non-symlink executable."""

    path = Path(executable).expanduser()
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)) or path.is_symlink():
        raise ManifestError("host executable must be an absolute non-symlink path")
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise ManifestError("host executable does not exist") from error
    if not stat.S_ISREG(info.st_mode):
        raise ManifestError("host executable must be a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def capability_hash(secret: str | bytes) -> str:
    """Hash legacy review tokens without admitting them to the P2 manifest."""
    raw = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    if len(raw) < 32:
        raise ManifestError("capability secret is too short")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    raw = _exact(manifest, _TOP, "manifest")
    if raw["schemaVersion"] != MANIFEST_SCHEMA_VERSION:
        raise ManifestError("unsupported manifest schema version")
    validate_id(raw["runId"], prefix="run")
    validate_id(raw["operationId"], prefix="op")
    if raw["parentRunId"] is not None:
        validate_id(raw["parentRunId"], prefix="run")

    conversation = _exact(raw["conversation"], _CONVERSATION, "conversation")
    validate_id(conversation["conversationId"], prefix="conv")
    profile = role_profile(conversation["role"])
    if conversation["authorityProfile"] != profile.authority_profile:
        raise ManifestError("conversation role and authority profile disagree")

    project = _exact(raw["project"], _PROJECT, "project")
    validate_id(project["projectId"], prefix="prj")
    _version(project["resourceVersion"], "project.resourceVersion")
    if project["objectFormat"] not in {"sha1", "sha256"} or project["trustMode"] not in {"trusted", "isolated"}:
        raise ManifestError("project identity is invalid")
    _digest(project["policyHash"], "project.policyHash")

    session = _exact(raw["session"], _SESSION, "session")
    if session["piSessionId"] != f"pi-{conversation['conversationId']}":
        raise ManifestError("Pi session identity is not controller-derived")
    session_path = _path(session["sessionPath"], "session.sessionPath")
    expected_suffix = f"/sessions/{project['projectId']}/{conversation['conversationId']}.jsonl"
    if not session_path.endswith(expected_suffix):
        raise ManifestError("session path is outside the controller project session root")

    scope = _exact(raw["scope"], _SCOPE, "scope")
    if scope["source"] != profile.scope_source or scope["projectId"] != project["projectId"] or scope["projectResourceVersion"] != project["resourceVersion"]:
        raise ManifestError("scope does not match role and project identity")
    validate_id(scope["workingCopyId"], prefix="wc")
    _version(scope["workingCopyResourceVersion"], "scope.workingCopyResourceVersion")
    _path(scope["rootPath"], "scope.rootPath")
    _path(scope["gitCommonDir"], "scope.gitCommonDir")
    if scope["branchRef"] is not None and (not isinstance(scope["branchRef"], str) or not scope["branchRef"].startswith("refs/")):
        raise ManifestError("scope.branchRef is invalid")
    _oid(scope["headOid"], project["objectFormat"], "scope.headOid")
    _oid(scope["treeOid"], project["objectFormat"], "scope.treeOid")

    working = raw["workingCopy"]
    if profile.authority_profile == "writer-container":
        working = _exact(working, _WORKING, "workingCopy")
        validate_id(working["workingCopyId"], prefix="wc")
        if working["workingCopyId"] != scope["workingCopyId"] or working["projectId"] != project["projectId"] or working["resourceVersion"] != scope["workingCopyResourceVersion"]:
            raise ManifestError("working copy and scope identity disagree")
        if working["purpose"] != profile.working_copy_purpose or working["effectiveMode"] not in {"trusted-live", "isolated"}:
            raise ManifestError("writer working-copy purpose or mode is invalid")
        if working["kind"] not in {"primary", "worktree", "isolated"}:
            raise ManifestError("writer working-copy kind is invalid")
        if _path(working["hostPath"], "workingCopy.hostPath") != scope["rootPath"]:
            raise ManifestError("working-copy host path differs from scope")
        _path(working["gitDir"], "workingCopy.gitDir")
        if not isinstance(working["writerEpoch"], int) or isinstance(working["writerEpoch"], bool) or working["writerEpoch"] < 1:
            raise ManifestError("writer epoch is invalid")
    elif working is not None:
        raise ManifestError("host-read-only manifests cannot contain writer working-copy authority")

    build = _exact(raw["installedBuild"], _BUILD, "installedBuild")
    if not isinstance(build["buildId"], str) or _BUILD_ID.fullmatch(build["buildId"]) is None:
        raise ManifestError("installed build ID is invalid")
    _digest(build["buildManifestDigest"], "installedBuild.buildManifestDigest")
    _digest(build["resourceManifestDigest"], "installedBuild.resourceManifestDigest")
    if build["piVersion"] in {"unknown", "0.0.0"}:
        raise ManifestError("installed Pi version is not exact")
    _text(build["piVersion"], "installedBuild.piVersion", 128)

    host = _exact(raw["hostProcess"], _HOST, "hostProcess")
    executable = _path(host["executable"], "hostProcess.executable")
    _digest(host["executableSha256"], "hostProcess.executableSha256")
    argv = host["argv"]
    if not isinstance(argv, list) or not argv or argv[0] != executable or any(not isinstance(item, str) or not item or "\x00" in item for item in argv):
        raise ManifestError("host process argv is invalid")
    if host["toolProfile"] != conversation["role"]:
        raise ManifestError("host tool profile differs from conversation role")
    environment_keys = host["environmentKeys"]
    if not isinstance(environment_keys, list) or environment_keys != sorted(set(environment_keys)) or any(not isinstance(key, str) or _ENV_KEY.fullmatch(key) is None for key in environment_keys):
        raise ManifestError("host environment-key allowlist is invalid")

    tool = raw["toolRuntime"]
    if profile.authority_profile == "host-read-only":
        if tool is not None:
            raise ManifestError("host read roles require toolRuntime null")
    else:
        tool = _exact(tool, _TOOL, "toolRuntime")
        if tool["specVersion"] != 2:
            raise ManifestError("tool runtime specification version is invalid")
        spec_hash = _digest(tool["specHash"], "toolRuntime.specHash")
        _text(tool["platform"], "toolRuntime.platform", 128)
        image = _text(tool["imageReference"], "toolRuntime.imageReference", 1024)
        config = _digest(tool["imageConfigId"], "toolRuntime.imageConfigId")
        registry = _digest(tool["registryDigest"], "toolRuntime.registryDigest")
        if _IMAGE.fullmatch(image) is None or not image.endswith("@" + registry) or config == registry:
            raise ManifestError("tool runtime image identity is incomplete")
        if not isinstance(tool["command"], list) or not tool["command"] or any(not isinstance(item, str) or not item or "\x00" in item for item in tool["command"]):
            raise ManifestError("tool runtime idle command is invalid")
        for key in ("uid", "gid"):
            if not isinstance(tool[key], int) or isinstance(tool[key], bool) or tool[key] < 0:
                raise ManifestError("tool runtime user identity is invalid")
        if tool["workdir"] != "/workspace" or tool["readOnlyRoot"] is not True or tool["networkMode"] != "none" or tool["capDrop"] != ["ALL"] or tool["securityOpt"] != ["no-new-privileges:true"]:
            raise ManifestError("tool runtime isolation boundary is incomplete")
        mounts = tool["mounts"]
        mount_keys = {"kind", "source", "target", "readOnly", "sourceDevice", "sourceInode"}
        if not isinstance(mounts, list) or len(mounts) != 3 or any(not isinstance(item, Mapping) or set(item) != mount_keys or not isinstance(item["sourceDevice"], int) or not isinstance(item["sourceInode"], int) for item in mounts):
            raise ManifestError("tool runtime mount identity is incomplete")
        if mounts[0].get("kind") != "working-copy" or mounts[0].get("source") != working["hostPath"] or mounts[0].get("target") != "/workspace" or mounts[0].get("readOnly") is not False or mounts[1].get("kind") != "git-mask" or mounts[1].get("target") != "/workspace/.git" or mounts[1].get("readOnly") is not True or mounts[2].get("kind") != "package-environment" or mounts[2].get("target") != "/environments" or mounts[2].get("readOnly") is not False:
            raise ManifestError("tool runtime mounts differ from the assigned working copy and Git mask")
        for field in ("tmpfs", "environment", "labels", "resources"):
            if not isinstance(tool[field], Mapping):
                raise ManifestError(f"tool runtime {field} is invalid")
        if tool["tmpfs"] != {"/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777"}:
            raise ManifestError("tool runtime tmpfs is invalid")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in tool["environment"].items()) or any(any(marker in key.upper() for marker in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "COOKIE", "CAPABILITY", "SSH_", "AWS_", "DOCKER", "PI_CONTROLLER")) for key in tool["environment"]):
            raise ManifestError("tool runtime environment allowlist is unsafe")
        expected_labels = {
            "pi.control.managed": "true", "pi.control.run-id": raw["runId"], "pi.control.project-id": project["projectId"],
            "pi.control.working-copy-id": working["workingCopyId"], "pi.control.writer-epoch": str(working["writerEpoch"]),
            "pi.control.controller-build-id": build["buildId"],
        }
        if tool["labels"] != expected_labels:
            raise ManifestError("tool runtime labels differ from controller identities")
        resources = tool["resources"]
        if set(resources) != {"memoryBytes", "nanoCpus", "pidsLimit"} or any(not isinstance(resources[key], int) or isinstance(resources[key], bool) or resources[key] < 1 for key in resources):
            raise ManifestError("tool runtime resource limits are invalid")
        hash_body = dict(tool)
        hash_body.pop("specHash")
        if spec_hash != "sha256:" + hashlib.sha256(canonical_json(hash_body).encode("utf-8")).hexdigest():
            raise ManifestError("tool runtime specification hash is invalid")

    _digest(raw["channelBindingHash"], "channelBindingHash")
    owner = _exact(raw["supervisorOwner"], _OWNER, "supervisorOwner")
    for key in ("uid", "gid", "pid"):
        if not isinstance(owner[key], int) or isinstance(owner[key], bool) or owner[key] < (1 if key == "pid" else 0):
            raise ManifestError(f"supervisorOwner.{key} is invalid")
    _text(owner["processStartIdentity"], "supervisorOwner.processStartIdentity", 256)
    created = _timestamp(raw["createdAt"], "createdAt")
    if raw["expiresAt"] is not None and _timestamp(raw["expiresAt"], "expiresAt") <= created:
        raise ManifestError("expiresAt must be later than createdAt")
    if raw["manifestDigest"] != manifest_digest(raw):
        raise ManifestError("manifestDigest does not match canonical content")
    return dict(raw)


def build_manifest(store: Any, run_id: str, *, host_process: Mapping[str, Any], tool_runtime: Mapping[str, Any] | None, owner: Mapping[str, Any] | None = None, expires_at: str | None = None) -> dict[str, Any]:
    run = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise ManifestError("run does not exist")
    conversation = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (run["conversation_id"],)).fetchone()
    project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (run["project_id"],)).fetchone()
    scope = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (run["working_copy_id"],)).fetchone()
    build = store.conn.execute("SELECT * FROM installed_builds WHERE build_id=?", (run["build_id"],)).fetchone()
    if conversation is None or project is None or scope is None or build is None:
        raise ManifestError("run bindings are incomplete")
    profile = role_profile(conversation["role"])
    working = None
    if profile.authority_profile == "writer-container":
        working = {
            "workingCopyId": scope["working_copy_id"], "projectId": project["project_id"], "resourceVersion": int(scope["resource_version"]),
            "kind": scope["kind"], "purpose": scope["purpose"], "effectiveMode": scope["effective_mode"], "hostPath": str(Path(scope["path"]).resolve(strict=True)),
            "gitDir": str(Path(scope["git_dir"] or project["git_common_dir"]).resolve(strict=True)), "writerEpoch": int(run["writer_epoch"]),
        }
    owner_value = dict(owner or {"uid": os.getuid(), "gid": os.getgid(), "pid": int(run["owner_pid"]), "processStartIdentity": run["owner_start_identity"] or process_start_identity(int(run["owner_pid"]))})
    value: dict[str, Any] = {
        "schemaVersion": MANIFEST_SCHEMA_VERSION, "runId": run_id, "operationId": run["operation_id"], "parentRunId": run["parent_run_id"],
        "conversation": {"conversationId": conversation["conversation_id"], "role": conversation["role"], "authorityProfile": profile.authority_profile},
        "session": {"piSessionId": conversation["pi_session_id"], "sessionPath": conversation["session_file"]},
        "project": {"projectId": project["project_id"], "resourceVersion": int(project["resource_version"]), "objectFormat": project["object_format"], "trustMode": project["trust_mode"], "policyHash": project["policy_hash"]},
        "scope": {"source": profile.scope_source, "projectId": project["project_id"], "projectResourceVersion": int(project["resource_version"]), "workingCopyId": scope["working_copy_id"], "workingCopyResourceVersion": int(scope["resource_version"]), "rootPath": str(Path(scope["path"]).resolve(strict=True)), "gitCommonDir": str(Path(project["git_common_dir"]).resolve(strict=True)), "branchRef": scope["branch_ref"], "headOid": scope["expected_head_oid"], "treeOid": scope["expected_tree_oid"]},
        "workingCopy": working,
        "installedBuild": {"buildId": build["build_id"], "buildManifestDigest": build["build_manifest_digest"], "resourceManifestDigest": build["resource_manifest_digest"], "piVersion": build["pi_version"]},
        "hostProcess": dict(host_process), "toolRuntime": dict(tool_runtime) if tool_runtime is not None else None,
        "channelBindingHash": run["channel_binding_hash"], "supervisorOwner": owner_value,
        "createdAt": run["created_at"] or utc_now(), "expiresAt": expires_at, "manifestDigest": "",
    }
    value["manifestDigest"] = manifest_digest(value)
    return validate_manifest(value)


def _secure_parent(parent: Path) -> None:
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise UnsafeDatabaseError("manifest parent is unsafe")
    if hasattr(os, "geteuid") and parent.stat().st_uid != os.geteuid():
        raise UnsafeDatabaseError("manifest parent is not user-owned")
    os.chmod(parent, 0o700)


def write_manifest(path: os.PathLike[str] | str, manifest: Mapping[str, Any]) -> ManifestFile:
    checked = validate_manifest(manifest)
    destination = Path(path).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ManifestError("manifest destination already exists")
    _secure_parent(destination.parent)
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
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    return ManifestFile(str(destination), "sha256:" + hashlib.sha256(data).hexdigest(), len(data), checked)


def read_manifest(path: os.PathLike[str] | str) -> ManifestFile:
    destination = Path(path)
    info = destination.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ManifestError("manifest file permissions or type are invalid")
    data = destination.read_bytes()
    try:
        checked = validate_manifest(json.loads(data.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError("manifest JSON is invalid") from error
    if canonical_json(checked).encode("utf-8") != data:
        raise ManifestError("manifest is not canonical JSON")
    return ManifestFile(str(destination.resolve(strict=True)), "sha256:" + hashlib.sha256(data).hexdigest(), len(data), checked)


def require_manifest_active(manifest: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    checked = validate_manifest(manifest)
    if checked["expiresAt"] is not None and _timestamp(checked["expiresAt"], "expiresAt") <= (now or datetime.now(timezone.utc)):
        raise ManifestError("run manifest has expired")
    return checked


__all__ = ["MANIFEST_SCHEMA_VERSION", "ManifestError", "ManifestFile", "build_manifest", "capability_hash", "executable_sha256", "manifest_digest", "read_manifest", "require_manifest_active", "validate_manifest", "write_manifest"]
