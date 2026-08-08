"""Pure runtime-specification and fake attestation adapter for Phase 5A.

This module deliberately has no Docker, subprocess, filesystem, or SQLite side
 effects.  It turns an already-validated controller manifest into a strict,
canonical runtime specification and compares an in-memory observation with that
specification.  The real sandbox adapter is a later Phase 5B surface.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import re
from typing import Any, Mapping

from .models import canonical_json, new_id, utc_now, validate_id
from .run_manifest import ManifestError, require_manifest_active, validate_manifest

RUNTIME_SPEC_VERSION = 1
ATTESTATION_VERSION = 1
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?(?::[0-9]+)?(?:/[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)*@sha256:[0-9a-f]{64}$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")

_SPEC_KEYS = frozenset({
    "schemaVersion", "runtimeSpecVersion", "runId", "manifestDigest", "authority",
    "project", "workingCopy", "executionTarget", "platform", "image", "identity",
    "mounts", "network", "security", "filesystem", "git", "controlPlane",
    "skills", "environment", "workingDirectory", "helper",
})
_PROJECT_KEYS = frozenset({"projectId", "objectFormat", "trustMode", "policyHash"})
_WORKING_COPY_KEYS = frozenset({
    "projectId", "workingCopyId", "sourcePath", "containerPath", "readOnly",
    "branchRef", "headOid", "treeOid", "gitCommonDir", "gitDir",
})
_IMAGE_KEYS = frozenset({"repository", "digest", "imageId"})
_IDENTITY_KEYS = frozenset({"uid", "gid", "supplementaryGroups"})
_MOUNT_KEYS = frozenset({"source", "target", "mode", "propagation", "recursiveReadOnly"})
_NETWORK_KEYS = frozenset({"mode", "approvedLoopbackPublications"})
_PUBLICATION_KEYS = frozenset({"hostPort", "containerPort", "protocol"})
_SECURITY_KEYS = frozenset({"capabilityDrops", "securityOptions"})
_FILESYSTEM_KEYS = frozenset({"readOnlyRootfs", "privateTmpfs", "cacheVolumes"})
_TMPFS_KEYS = frozenset({"path", "uid", "gid", "mode"})
_CACHE_KEYS = frozenset({"name", "scopeKey", "uid", "gid"})
_GIT_KEYS = frozenset({"commonDir", "gitDir", "identityFile", "identityReadOnly"})
_CONTROL_PLANE_KEYS = frozenset({"mounts"})
_SKILLS_KEYS = frozenset({"manifests", "readOnlyPaths"})
_ENVIRONMENT_KEYS = frozenset({"allowlist", "values", "hashes"})
_HELPER_KEYS = frozenset({"path", "buildId"})

_ATTESTATION_KEYS = frozenset({
    "schemaVersion", "runId", "manifestDigest", "attestationNonce", "observedAt", "authority",
    "project", "container", "identity", "workingCopy", "mounts", "network", "security",
    "filesystem", "git", "controlPlane", "skills", "environment", "workingDirectory",
    "helper", "checks", "attestationDigest",
})
_CONTAINER_KEYS = frozenset({"id", "name", "imageId", "imageRepository", "imageDigest", "platform", "running"})
_ATTEST_IDENTITY_KEYS = _IDENTITY_KEYS
_ATTEST_WORKING_COPY_KEYS = frozenset({
    "projectId", "workingCopyId", "branchRef", "headOid", "treeOid", "gitCommonDir",
    "gitDir", "writable",
})
_CHECK_KEYS = frozenset({"name", "passed", "detail"})


class RuntimeSpecError(ValueError):
    """The runtime specification or attestation is unsafe or malformed."""


@dataclass(frozen=True)
class FakeRuntimeHandle:
    run_id: str
    runtime_id: str
    state: str
    spec: Mapping[str, Any]
    manifest: Mapping[str, Any]


def _keys(value: Mapping[str, Any], allowed: frozenset[str], name: str) -> None:
    if not isinstance(value, Mapping) or set(value) != set(allowed):
        raise RuntimeSpecError(f"{name} fields do not match the schema")


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeSpecError(f"{name} must be an object")
    return value


def _text(value: Any, name: str, *, allow_empty: bool = False, limit: int = 4096) -> str:
    if not isinstance(value, str) or (not allow_empty and not value) or len(value) > limit or "\x00" in value:
        raise RuntimeSpecError(f"{name} is invalid")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeSpecError(f"{name} must be boolean")
    return value


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RuntimeSpecError(f"{name} is invalid")
    return value


def _hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise RuntimeSpecError(f"{name} is not a sha256 digest")
    return value


def _path(value: Any, name: str) -> str:
    text = _text(value, name)
    parts = text.split("/")
    if not text.startswith("/") or (text != "/" and any(part in {"", ".", ".."} for part in parts[1:])):
        raise RuntimeSpecError(f"{name} must be a normalized absolute path")
    return text


def _oid(value: Any, object_format: str, name: str, *, allow_none: bool = True) -> str | None:
    if value is None and allow_none:
        return None
    expected = 40 if object_format == "sha1" else 64
    if not isinstance(value, str) or len(value) != expected or _HEX_RE.fullmatch(value.lower()) is None:
        raise RuntimeSpecError(f"{name} is invalid for {object_format}")
    return value.lower()


def _image(value: Mapping[str, Any], name: str = "image") -> None:
    _keys(value, _IMAGE_KEYS, name)
    original_repository = _text(value["repository"], f"{name}.repository", limit=512)
    repository = original_repository.lower()
    if repository != original_repository:
        raise RuntimeSpecError(f"{name}.repository must be lowercase")
    digest = _hash(value["digest"], f"{name}.digest")
    image_id = _hash(value["imageId"], f"{name}.imageId")
    if _IMAGE_RE.fullmatch(repository + "@" + digest) is None:
        raise RuntimeSpecError("image must use repository@sha256:digest syntax")


def _string_list(value: Any, name: str, *, limit: int = 256) -> list[str]:
    if not isinstance(value, list):
        raise RuntimeSpecError(f"{name} must be a list")
    result = [_text(item, name, limit=limit) for item in value]
    if len(set(result)) != len(result):
        raise RuntimeSpecError(f"{name} contains duplicates")
    return result


def _validate_project(value: Any) -> None:
    project = _mapping(value, "project")
    _keys(project, _PROJECT_KEYS, "project")
    validate_id(project["projectId"], prefix="prj")
    if project["objectFormat"] not in {"sha1", "sha256"}:
        raise RuntimeSpecError("project.objectFormat is invalid")
    if project["trustMode"] not in {"trusted", "isolated"}:
        raise RuntimeSpecError("project.trustMode is invalid")
    _hash(project["policyHash"], "project.policyHash")


def _validate_working_copy(value: Any, project: Mapping[str, Any], authority: str) -> None:
    if value is None:
        if authority == "writer":
            raise RuntimeSpecError("writer runtime spec requires a working copy")
        return
    working = _mapping(value, "workingCopy")
    _keys(working, _WORKING_COPY_KEYS, "workingCopy")
    validate_id(working["projectId"], prefix="prj")
    validate_id(working["workingCopyId"], prefix="wc")
    if working["projectId"] != project["projectId"]:
        raise RuntimeSpecError("working copy project does not match project")
    _path(working["sourcePath"], "workingCopy.sourcePath")
    _path(working["containerPath"], "workingCopy.containerPath")
    _bool(working["readOnly"], "workingCopy.readOnly")
    if authority == "writer" and working["readOnly"]:
        raise RuntimeSpecError("writer runtime spec cannot be read-only")
    branch = working["branchRef"]
    if branch is not None and (not isinstance(branch, str) or not branch.startswith("refs/") or "\x00" in branch):
        raise RuntimeSpecError("workingCopy.branchRef is invalid")
    _oid(working["headOid"], project["objectFormat"], "workingCopy.headOid")
    _oid(working["treeOid"], project["objectFormat"], "workingCopy.treeOid")
    _path(working["gitCommonDir"], "workingCopy.gitCommonDir")
    _path(working["gitDir"], "workingCopy.gitDir")


def _validate_mounts(value: Any, name: str) -> None:
    if not isinstance(value, list):
        raise RuntimeSpecError(f"{name} must be a list")
    seen_targets: set[str] = set()
    for index, raw in enumerate(value):
        mount = _mapping(raw, f"{name}[{index}]")
        _keys(mount, _MOUNT_KEYS, f"{name}[{index}]")
        source = _path(mount["source"], f"{name}[{index}].source")
        target = _path(mount["target"], f"{name}[{index}].target")
        if source == target or target in seen_targets:
            raise RuntimeSpecError("mount targets must be distinct")
        seen_targets.add(target)
        if mount["mode"] not in {"ro", "rw"}:
            raise RuntimeSpecError("mount mode is invalid")
        if mount["propagation"] not in {"private", "rprivate", "slave", "rslave", "shared", "rshared"}:
            raise RuntimeSpecError("mount propagation is invalid")
        _bool(mount["recursiveReadOnly"], f"{name}[{index}].recursiveReadOnly")
        # Phase 5B Docker creation requests ordinary read-only mounts; recursive
        # read-only is a separate runtime capability and is not claimed here.


def _validate_identity(value: Any) -> None:
    identity = _mapping(value, "identity")
    _keys(identity, _IDENTITY_KEYS, "identity")
    _integer(identity["uid"], "identity.uid")
    _integer(identity["gid"], "identity.gid")
    groups = identity["supplementaryGroups"]
    if not isinstance(groups, list):
        raise RuntimeSpecError("identity.supplementaryGroups must be a list")
    for group in groups:
        _integer(group, "identity.supplementaryGroups[]")
    if groups != sorted(set(groups)):
        raise RuntimeSpecError("identity.supplementaryGroups must be sorted and unique")


def _validate_spec_children(spec: Mapping[str, Any]) -> None:
    network = _mapping(spec["network"], "network")
    _keys(network, _NETWORK_KEYS, "network")
    if network["mode"] not in {"none", "loopback", "bridge"}:
        raise RuntimeSpecError("network mode is not an approved least-privilege mode")
    publications = network["approvedLoopbackPublications"]
    if not isinstance(publications, list):
        raise RuntimeSpecError("network publications must be a list")
    for index, raw in enumerate(publications):
        publication = _mapping(raw, f"network.approvedLoopbackPublications[{index}]")
        _keys(publication, _PUBLICATION_KEYS, f"network.approvedLoopbackPublications[{index}]")
        host_port = _integer(publication["hostPort"], "network.hostPort", minimum=1)
        container_port = _integer(publication["containerPort"], "network.containerPort", minimum=1)
        if host_port > 65535 or container_port > 65535 or publication["protocol"] not in {"tcp", "udp"}:
            raise RuntimeSpecError("network publication is invalid")
        if network["mode"] != "loopback":
            raise RuntimeSpecError("publications require loopback network mode")

    security = _mapping(spec["security"], "security")
    _keys(security, _SECURITY_KEYS, "security")
    _string_list(security["capabilityDrops"], "security.capabilityDrops")
    _string_list(security["securityOptions"], "security.securityOptions")

    filesystem = _mapping(spec["filesystem"], "filesystem")
    _keys(filesystem, _FILESYSTEM_KEYS, "filesystem")
    _bool(filesystem["readOnlyRootfs"], "filesystem.readOnlyRootfs")
    if not isinstance(filesystem["privateTmpfs"], list) or not isinstance(filesystem["cacheVolumes"], list):
        raise RuntimeSpecError("filesystem private resources must be lists")
    for index, raw in enumerate(filesystem["privateTmpfs"]):
        tmpfs = _mapping(raw, f"filesystem.privateTmpfs[{index}]")
        _keys(tmpfs, _TMPFS_KEYS, f"filesystem.privateTmpfs[{index}]")
        _path(tmpfs["path"], "filesystem.privateTmpfs.path")
        _integer(tmpfs["uid"], "filesystem.privateTmpfs.uid")
        _integer(tmpfs["gid"], "filesystem.privateTmpfs.gid")
        _text(tmpfs["mode"], "filesystem.privateTmpfs.mode", limit=4)
    for index, raw in enumerate(filesystem["cacheVolumes"]):
        cache = _mapping(raw, f"filesystem.cacheVolumes[{index}]")
        _keys(cache, _CACHE_KEYS, f"filesystem.cacheVolumes[{index}]")
        _text(cache["name"], "filesystem.cacheVolumes.name", limit=256)
        _text(cache["scopeKey"], "filesystem.cacheVolumes.scopeKey", limit=512)
        _integer(cache["uid"], "filesystem.cacheVolumes.uid")
        _integer(cache["gid"], "filesystem.cacheVolumes.gid")

    git = _mapping(spec["git"], "git")
    _keys(git, _GIT_KEYS, "git")
    _path(git["commonDir"], "git.commonDir")
    _path(git["gitDir"], "git.gitDir")
    _path(git["identityFile"], "git.identityFile")
    _bool(git["identityReadOnly"], "git.identityReadOnly")

    control_plane = _mapping(spec["controlPlane"], "controlPlane")
    _keys(control_plane, _CONTROL_PLANE_KEYS, "controlPlane")
    _validate_mounts(control_plane["mounts"], "controlPlane.mounts")

    skills = _mapping(spec["skills"], "skills")
    _keys(skills, _SKILLS_KEYS, "skills")
    _string_list(skills["manifests"], "skills.manifests", limit=4096)
    if not isinstance(skills["readOnlyPaths"], list):
        raise RuntimeSpecError("skills.readOnlyPaths must be a list")
    for path in skills["readOnlyPaths"]:
        _path(path, "skills.readOnlyPaths[]")

    environment = _mapping(spec["environment"], "environment")
    _keys(environment, _ENVIRONMENT_KEYS, "environment")
    allowlist = _string_list(environment["allowlist"], "environment.allowlist", limit=256)
    values = _mapping(environment["values"], "environment.values")
    hashes = _mapping(environment["hashes"], "environment.hashes")
    if set(values) - set(allowlist) or set(hashes) - set(allowlist):
        raise RuntimeSpecError("environment values/hash keys exceed allowlist")
    for key, value in values.items():
        _text(key, "environment key", limit=256)
        _text(value, f"environment.values[{key}]", allow_empty=True, limit=8192)
    for key, value in hashes.items():
        _text(key, "environment key", limit=256)
        _hash(value, f"environment.hashes[{key}]")

    _path(spec["workingDirectory"], "workingDirectory")
    helper = _mapping(spec["helper"], "helper")
    _keys(helper, _HELPER_KEYS, "helper")
    _path(helper["path"], "helper.path")
    _text(helper["buildId"], "helper.buildId", limit=512)


def validate_runtime_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, Mapping):
        raise RuntimeSpecError("runtime spec must be an object")
    _keys(spec, _SPEC_KEYS, "runtime spec")
    if spec["schemaVersion"] != RUNTIME_SPEC_VERSION or spec["runtimeSpecVersion"] != RUNTIME_SPEC_VERSION:
        raise RuntimeSpecError("unsupported runtime spec version")
    validate_id(spec["runId"], prefix="run")
    _hash(spec["manifestDigest"], "manifestDigest")
    if spec["authority"] not in {"read-only", "writer", "secretary", "host-maintenance"}:
        raise RuntimeSpecError("authority is invalid")
    _validate_project(spec["project"])
    _validate_working_copy(spec["workingCopy"], spec["project"], spec["authority"])
    _text(spec["executionTarget"], "executionTarget", limit=256)
    _text(spec["platform"], "platform", limit=256)
    _image(_mapping(spec["image"], "image"))
    _validate_identity(spec["identity"])
    _validate_mounts(spec["mounts"], "mounts")
    _validate_spec_children(spec)
    return copy.deepcopy(dict(spec))


def _validate_spec_manifest_binding(spec: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    try:
        checked_manifest = require_manifest_active(manifest)
    except ManifestError as error:
        raise RuntimeSpecError(str(error)) from error
    expected_project = checked_manifest["project"]
    actual_project = spec["project"]
    for key in ("projectId", "objectFormat", "trustMode", "policyHash"):
        if actual_project[key] != expected_project[key]:
            raise RuntimeSpecError(f"runtime project differs from manifest at {key}")
    if spec["runId"] != checked_manifest["runId"] or spec["manifestDigest"] != checked_manifest["manifestDigest"] or spec["authority"] != checked_manifest["authority"]:
        raise RuntimeSpecError("runtime identity differs from manifest")
    runtime = checked_manifest["runtime"]
    if spec["executionTarget"] != runtime["executionTarget"] or spec["platform"] != runtime["platform"]:
        raise RuntimeSpecError("runtime target or platform differs from manifest")
    if spec["image"]["digest"] != runtime["imageDigest"]:
        raise RuntimeSpecError("runtime image digest differs from manifest")
    if spec["helper"]["buildId"] != runtime["controllerBuildId"]:
        raise RuntimeSpecError("runtime helper build differs from manifest")
    working = checked_manifest["workingCopy"]
    actual_working = spec["workingCopy"]
    if working is None:
        if actual_working is not None:
            raise RuntimeSpecError("runtime has an unexpected working copy")
    else:
        if actual_working is None:
            raise RuntimeSpecError("runtime is missing the manifest working copy")
        expected_read_only = checked_manifest["authority"] in {"read-only", "secretary", "host-maintenance"} or working["effectiveMode"] == "read-only"
        expected = {
            "projectId": expected_project["projectId"], "workingCopyId": working["workingCopyId"],
            "sourcePath": working["hostPath"], "readOnly": expected_read_only,
            "branchRef": working["branchRef"], "headOid": working["headOid"], "treeOid": working["treeOid"],
            "gitCommonDir": working["gitCommonDir"], "gitDir": working["gitDir"],
        }
        for key, value in expected.items():
            if actual_working[key] != value:
                raise RuntimeSpecError(f"runtime working copy differs from manifest at {key}")
        if spec["git"]["commonDir"] != working["gitCommonDir"] or spec["git"]["gitDir"] != working["gitDir"]:
            raise RuntimeSpecError("runtime Git paths differ from manifest")


def runtime_spec_digest(spec: Mapping[str, Any]) -> str:
    checked = validate_runtime_spec(spec)
    return "sha256:" + hashlib.sha256(canonical_json(checked).encode("utf-8")).hexdigest()


def runtime_spec_hash(spec: Mapping[str, Any]) -> str:
    return runtime_spec_digest(spec)


def _default_image(image: str, image_id: str | None = None) -> dict[str, str]:
    if "@" not in image:
        raise RuntimeSpecError("image must include an immutable repository digest")
    repository, digest = image.rsplit("@", 1)
    if repository != repository.lower():
        raise RuntimeSpecError("image repository must be lowercase")
    return {"repository": repository, "digest": digest, "imageId": image_id or digest}


def build_runtime_spec(
    manifest: Mapping[str, Any],
    *,
    image: str,
    image_id: str | None = None,
    uid: int | None = None,
    gid: int | None = None,
    supplementary_groups: list[int] | tuple[int, ...] = (),
    container_path: str = "/workspace",
    mounts: list[Mapping[str, Any]] | None = None,
    network_mode: str = "none",
    approved_loopback_publications: list[Mapping[str, Any]] | None = None,
    capability_drops: list[str] | None = None,
    security_options: list[str] | None = None,
    private_tmpfs: list[Mapping[str, Any]] | None = None,
    cache_volumes: list[Mapping[str, Any]] | None = None,
    control_plane_mounts: list[Mapping[str, Any]] | None = None,
    skill_manifests: list[str] | None = None,
    skill_read_only_paths: list[str] | None = None,
    environment_allowlist: list[str] | None = None,
    environment_values: Mapping[str, str] | None = None,
    environment_hashes: Mapping[str, str] | None = None,
    working_directory: str = "/workspace",
    helper_path: str = "/usr/local/bin/pi-runtime-helper",
) -> dict[str, Any]:
    """Build a deterministic spec from a validated immutable run manifest."""

    checked_manifest = validate_manifest(manifest)
    project = checked_manifest["project"]
    working = checked_manifest["workingCopy"]
    authority = checked_manifest["authority"]
    owner = checked_manifest["owner"]
    actual_uid = owner["uid"] if uid is None else uid
    actual_gid = owner["gid"] if gid is None else gid
    read_only = authority in {"read-only", "secretary", "host-maintenance"} or (working is not None and working["effectiveMode"] == "read-only")
    working_value = None
    default_mounts: list[dict[str, Any]] = []
    if working is not None:
        working_value = {
            "projectId": project["projectId"], "workingCopyId": working["workingCopyId"],
            "sourcePath": working["hostPath"], "containerPath": container_path, "readOnly": read_only,
            "branchRef": working["branchRef"], "headOid": working["headOid"], "treeOid": working["treeOid"],
            "gitCommonDir": working["gitCommonDir"], "gitDir": working["gitDir"],
        }
        default_mounts = [{
            "source": working["hostPath"], "target": container_path, "mode": "ro" if read_only else "rw",
            "propagation": "rprivate", "recursiveReadOnly": False,
        }]
    spec = {
        "schemaVersion": 1, "runtimeSpecVersion": 1, "runId": checked_manifest["runId"],
        "manifestDigest": checked_manifest["manifestDigest"], "authority": authority,
        "project": {key: project[key] for key in _PROJECT_KEYS}, "workingCopy": working_value,
        "executionTarget": checked_manifest["runtime"]["executionTarget"],
        "platform": checked_manifest["runtime"]["platform"], "image": _default_image(image, image_id),
        "identity": {"uid": actual_uid, "gid": actual_gid, "supplementaryGroups": sorted(set(supplementary_groups))},
        "mounts": copy.deepcopy(mounts if mounts is not None else default_mounts),
        "network": {"mode": network_mode, "approvedLoopbackPublications": copy.deepcopy(approved_loopback_publications or [])},
        "security": {"capabilityDrops": list(capability_drops or []), "securityOptions": list(security_options or [])},
        "filesystem": {"readOnlyRootfs": True, "privateTmpfs": copy.deepcopy(private_tmpfs or []), "cacheVolumes": copy.deepcopy(cache_volumes or [])},
        "git": {"commonDir": working["gitCommonDir"] if working else project.get("gitCommonDir", "/git"), "gitDir": working["gitDir"] if working else project.get("gitDir", "/git"), "identityFile": "/run/pi/gitconfig", "identityReadOnly": True},
        "controlPlane": {"mounts": copy.deepcopy(control_plane_mounts or [])},
        "skills": {"manifests": list(skill_manifests or []), "readOnlyPaths": list(skill_read_only_paths or [])},
        "environment": {"allowlist": list(environment_allowlist or []), "values": dict(environment_values or {}), "hashes": dict(environment_hashes or {})},
        "workingDirectory": working_directory,
        "helper": {"path": helper_path, "buildId": checked_manifest["runtime"]["controllerBuildId"]},
    }
    checked_spec = validate_runtime_spec(spec)
    _validate_spec_manifest_binding(checked_spec, checked_manifest)
    return checked_spec


def _attestation_without_digest(attestation: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(attestation))
    value.pop("attestationDigest", None)
    return value


def attestation_digest(attestation: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(_attestation_without_digest(attestation)).encode("utf-8")).hexdigest()


def _validate_attestation_shape(attestation: Mapping[str, Any]) -> None:
    _keys(attestation, _ATTESTATION_KEYS, "attestation")
    if attestation["schemaVersion"] != ATTESTATION_VERSION:
        raise RuntimeSpecError("unsupported attestation version")
    validate_id(attestation["runId"], prefix="run")
    _hash(attestation["manifestDigest"], "attestation.manifestDigest")
    _text(attestation["attestationNonce"], "attestationNonce", limit=512)
    if not _text(attestation["observedAt"], "observedAt", limit=128).endswith("Z"):
        raise RuntimeSpecError("observedAt must be UTC")
    if attestation["authority"] not in {"read-only", "writer", "secretary", "host-maintenance"}:
        raise RuntimeSpecError("attestation authority is invalid")
    _validate_project(attestation["project"])
    container = _mapping(attestation["container"], "container")
    _keys(container, _CONTAINER_KEYS, "container")
    for key in ("id", "name", "imageId", "imageRepository", "platform"):
        _text(container[key], f"container.{key}", limit=512)
    _hash(container["imageDigest"], "container.imageDigest")
    if _IMAGE_RE.fullmatch(container["imageRepository"] + "@" + container["imageDigest"]) is None:
        raise RuntimeSpecError("attested image repository/digest is invalid")
    _bool(container["running"], "container.running")
    if not container["running"]:
        raise RuntimeSpecError("runtime is not running")
    identity = _mapping(attestation["identity"], "identity")
    _keys(identity, _ATTEST_IDENTITY_KEYS, "attestation.identity")
    _validate_identity(identity)
    working = attestation["workingCopy"]
    if working is not None:
        working = _mapping(working, "attestation.workingCopy")
        _keys(working, _ATTEST_WORKING_COPY_KEYS, "attestation.workingCopy")
        validate_id(working["projectId"], prefix="prj")
        validate_id(working["workingCopyId"], prefix="wc")
        if working["branchRef"] is not None and (not isinstance(working["branchRef"], str) or not working["branchRef"].startswith("refs/")):
            raise RuntimeSpecError("attestation branchRef is invalid")
        _text(working["gitCommonDir"], "attestation.gitCommonDir")
        _text(working["gitDir"], "attestation.gitDir")
        _oid(working["headOid"], "sha1" if len(working["headOid"] or "") == 40 else "sha256", "attestation.headOid")
        _oid(working["treeOid"], "sha1" if len(working["treeOid"] or "") == 40 else "sha256", "attestation.treeOid")
        _bool(working["writable"], "attestation.workingCopy.writable")
    _validate_mounts(attestation["mounts"], "attestation.mounts")
    _validate_spec_children({key: attestation[key] for key in ("network", "security", "filesystem", "git", "controlPlane", "skills", "environment", "workingDirectory", "helper")})
    if not isinstance(attestation["checks"], list) or not attestation["checks"] or len(attestation["checks"]) > 32:
        raise RuntimeSpecError("attestation checks are required and bounded")
    for index, raw in enumerate(attestation["checks"]):
        check = _mapping(raw, f"checks[{index}]")
        _keys(check, _CHECK_KEYS, f"checks[{index}]")
        _text(check["name"], f"checks[{index}].name", limit=128)
        _bool(check["passed"], f"checks[{index}].passed")
        _text(check["detail"], f"checks[{index}].detail", allow_empty=True, limit=1024)
        if not check["passed"]:
            raise RuntimeSpecError(f"attestation check failed: {check['name']}")
    _hash(attestation["attestationDigest"], "attestationDigest")


def validate_attestation(attestation: Mapping[str, Any], spec: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    checked_spec = validate_runtime_spec(spec)
    checked_manifest = validate_manifest(manifest)
    _validate_spec_manifest_binding(checked_spec, checked_manifest)
    _validate_attestation_shape(attestation)
    if attestation["runId"] != checked_spec["runId"] or attestation["manifestDigest"] != checked_spec["manifestDigest"]:
        raise RuntimeSpecError("attestation run or manifest does not match runtime spec")
    if checked_manifest["runId"] != attestation["runId"] or checked_manifest["manifestDigest"] != attestation["manifestDigest"] or checked_manifest["attestationNonce"] != attestation["attestationNonce"]:
        raise RuntimeSpecError("attestation nonce or manifest does not match the current run")
    if attestation["authority"] != checked_spec["authority"] or attestation["project"] != checked_spec["project"]:
        raise RuntimeSpecError("attestation authority or project identity differs")
    if (attestation["container"]["imageRepository"] != checked_spec["image"]["repository"] or attestation["container"]["imageId"] != checked_spec["image"]["imageId"] or attestation["container"]["imageDigest"] != checked_spec["image"]["digest"] or attestation["container"]["platform"] != checked_spec["platform"]):
        raise RuntimeSpecError("attestation image or platform does not match runtime spec")
    if attestation["identity"] != checked_spec["identity"]:
        raise RuntimeSpecError("attestation identity does not match runtime spec")
    expected_working = checked_spec["workingCopy"]
    actual_working = attestation["workingCopy"]
    if expected_working is None:
        if actual_working is not None:
            raise RuntimeSpecError("unexpected working-copy attestation")
    else:
        if actual_working is None:
            raise RuntimeSpecError("working-copy attestation is missing")
        for key in ("projectId", "workingCopyId", "branchRef", "headOid", "treeOid", "gitCommonDir", "gitDir"):
            if actual_working[key] != expected_working[key]:
                raise RuntimeSpecError(f"working-copy attestation differs at {key}")
        if actual_working["writable"] != (not expected_working["readOnly"]):
            raise RuntimeSpecError("working-copy writability differs from runtime spec")
    if attestation["mounts"] != checked_spec["mounts"]:
        raise RuntimeSpecError("attested mounts differ from runtime spec")
    for key in ("network", "security", "filesystem", "git", "controlPlane", "skills", "environment", "workingDirectory", "helper"):
        if attestation[key] != checked_spec[key]:
            raise RuntimeSpecError(f"attestation differs at {key}")
    if attestation["attestationDigest"] != attestation_digest(attestation):
        raise RuntimeSpecError("attestation digest does not match canonical content")
    return copy.deepcopy(dict(attestation))


def build_fake_attestation(spec: Mapping[str, Any], manifest: Mapping[str, Any], *, runtime_id: str = "fake-runtime", runtime_name: str = "fake-runtime") -> dict[str, Any]:
    checked_spec = validate_runtime_spec(spec)
    checked_manifest = validate_manifest(manifest)
    if checked_spec["runId"] != checked_manifest["runId"] or checked_spec["manifestDigest"] != checked_manifest["manifestDigest"]:
        raise RuntimeSpecError("fake attestation inputs do not match")
    _validate_spec_manifest_binding(checked_spec, checked_manifest)
    working = checked_spec["workingCopy"]
    actual_working = None if working is None else {
        "projectId": working["projectId"], "workingCopyId": working["workingCopyId"], "branchRef": working["branchRef"],
        "headOid": working["headOid"], "treeOid": working["treeOid"], "gitCommonDir": working["gitCommonDir"],
        "gitDir": working["gitDir"], "writable": not working["readOnly"],
    }
    attestation = {
        "schemaVersion": 1, "runId": checked_spec["runId"], "manifestDigest": checked_spec["manifestDigest"],
        "attestationNonce": checked_manifest["attestationNonce"], "observedAt": utc_now(), "authority": checked_spec["authority"],
        "project": copy.deepcopy(checked_spec["project"]),
        "container": {"id": _text(runtime_id, "runtime_id", limit=512), "name": _text(runtime_name, "runtime_name", limit=512), "imageId": checked_spec["image"]["imageId"], "imageRepository": checked_spec["image"]["repository"], "imageDigest": checked_spec["image"]["digest"], "platform": checked_spec["platform"], "running": True},
        "identity": copy.deepcopy(checked_spec["identity"]),
        "workingCopy": actual_working, "mounts": copy.deepcopy(checked_spec["mounts"]),
        "network": copy.deepcopy(checked_spec["network"]), "security": copy.deepcopy(checked_spec["security"]),
        "filesystem": copy.deepcopy(checked_spec["filesystem"]), "git": copy.deepcopy(checked_spec["git"]),
        "controlPlane": copy.deepcopy(checked_spec["controlPlane"]), "skills": copy.deepcopy(checked_spec["skills"]),
        "environment": copy.deepcopy(checked_spec["environment"]), "workingDirectory": checked_spec["workingDirectory"],
        "helper": copy.deepcopy(checked_spec["helper"]),
        "checks": [{"name": name, "passed": True, "detail": "fake observation"} for name in ("manifest", "image", "identity", "working-copy", "mounts", "runtime-policy")],
        "attestationDigest": "",
    }
    attestation["attestationDigest"] = attestation_digest(attestation)
    return validate_attestation(attestation, checked_spec, checked_manifest)


fake_attest = build_fake_attestation


class FakeRuntimeAdapter:
    """In-memory runtime lifecycle; no host/container side effects."""

    def __init__(self) -> None:
        self._handles: dict[str, FakeRuntimeHandle] = {}

    def prepare(self, spec: Mapping[str, Any], manifest: Mapping[str, Any]) -> FakeRuntimeHandle:
        checked_spec = validate_runtime_spec(spec)
        checked_manifest = validate_manifest(manifest)
        if checked_spec["runId"] != checked_manifest["runId"] or checked_spec["manifestDigest"] != checked_manifest["manifestDigest"]:
            raise RuntimeSpecError("runtime preparation inputs do not match")
        _validate_spec_manifest_binding(checked_spec, checked_manifest)
        run_id = checked_spec["runId"]
        if run_id in self._handles:
            raise RuntimeSpecError("cross-run runtime reuse is not allowed")
        handle = FakeRuntimeHandle(run_id, "fake-runtime-" + run_id, "created", checked_spec, checked_manifest)
        self._handles[run_id] = handle
        return handle

    create = prepare

    def attest(self, run_id: str) -> dict[str, Any]:
        handle = self._handles.get(run_id)
        if handle is None or handle.state == "stopped":
            raise RuntimeSpecError("runtime is not available for attestation")
        result = build_fake_attestation(handle.spec, handle.manifest, runtime_id=handle.runtime_id, runtime_name=handle.runtime_id)
        self._handles[run_id] = FakeRuntimeHandle(handle.run_id, handle.runtime_id, "ready", handle.spec, handle.manifest)
        return result

    def stop(self, run_id: str) -> FakeRuntimeHandle:
        handle = self._handles.get(run_id)
        if handle is None:
            raise RuntimeSpecError("runtime was not prepared")
        stopped = FakeRuntimeHandle(handle.run_id, handle.runtime_id, "stopped", handle.spec, handle.manifest)
        self._handles[run_id] = stopped
        return stopped

    def state(self, run_id: str) -> str:
        handle = self._handles.get(run_id)
        if handle is None:
            raise RuntimeSpecError("runtime was not prepared")
        return handle.state


__all__ = [
    "ATTESTATION_VERSION", "FakeRuntimeAdapter", "FakeRuntimeHandle", "RUNTIME_SPEC_VERSION",
    "RuntimeSpecError", "attestation_digest", "build_fake_attestation", "build_runtime_spec",
    "fake_attest", "runtime_spec_digest", "runtime_spec_hash", "validate_attestation", "validate_runtime_spec",
]
