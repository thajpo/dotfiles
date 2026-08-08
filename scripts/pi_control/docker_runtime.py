"""Real Docker identity checks for coding runs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping


class DockerRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class DockerImageIdentity:
    image_reference: str
    image_config_id: str
    registry_digest: str | None
    platform: str

    def as_dict(self) -> dict[str, Any]:
        return {"imageReference": self.image_reference, "imageConfigId": self.image_config_id, "registryDigest": self.registry_digest, "platform": self.platform}


def _docker() -> str:
    path = shutil.which("docker", path=os.defpath)
    if path is None:
        raise DockerRuntimeError("Docker is unavailable")
    return path


def _run(args: list[str], *, timeout: float = 60.0) -> str:
    result = subprocess.run([_docker(), *args], env={"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C"}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False, shell=False)
    if result.returncode != 0:
        raise DockerRuntimeError(result.stderr.strip()[:1024] or "Docker command failed")
    return result.stdout


def inspect_image(image_reference: str) -> DockerImageIdentity:
    if not isinstance(image_reference, str) or not image_reference or "\x00" in image_reference:
        raise DockerRuntimeError("image reference is invalid")
    values = json.loads(_run(["image", "inspect", "--format", "{{json .}}", image_reference]))
    if not isinstance(values, dict):
        raise DockerRuntimeError("Docker image inspection is malformed")
    config = str(values.get("Id") or "")
    if not config:
        raise DockerRuntimeError("Docker image configuration ID is missing")
    platform = f"{values.get('Os') or 'unknown'}/{values.get('Architecture') or 'unknown'}"
    repo_digests = values.get("RepoDigests") or []
    registry_digest = repo_digests[0] if repo_digests and isinstance(repo_digests[0], str) else None
    return DockerImageIdentity(image_reference, config, registry_digest, platform)


def inspect_container(container_id: str) -> dict[str, Any]:
    values = json.loads(_run(["container", "inspect", container_id]))
    if not isinstance(values, list) or not values or not isinstance(values[0], dict):
        raise DockerRuntimeError("Docker container inspection is malformed")
    item = values[0]
    config = item.get("Config") or {}
    host = item.get("HostConfig") or {}
    mounts = item.get("Mounts") or []
    return {"id": item.get("Id"), "name": str(item.get("Name") or "").removeprefix("/"), "imageConfigId": item.get("Image"), "platform": f"{config.get('Os') or 'unknown'}/{config.get('Architecture') or 'unknown'}", "running": bool((item.get("State") or {}).get("Running")), "user": config.get("User") or "", "networkMode": host.get("NetworkMode"), "mounts": mounts, "labels": config.get("Labels") or {}, "env": config.get("Env") or {}}


def create_coding_container(*, image_reference: str, working_copy_path: str | Path, container_name: str, runtime_spec: Mapping[str, Any], command: list[str]) -> dict[str, Any]:
    image = inspect_image(image_reference)
    source = Path(working_copy_path).resolve(strict=True)
    if not source.is_dir():
        raise DockerRuntimeError("working copy is not a directory")
    if not isinstance(command, list) or not command or any(not isinstance(item, str) or "\x00" in item for item in command):
        raise DockerRuntimeError("container command is invalid")
    expected_hash = "sha256:" + hashlib.sha256(json.dumps(dict(runtime_spec), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if runtime_spec.get("runtimeSpecHash") not in {None, expected_hash}:
        raise DockerRuntimeError("runtime specification hash mismatch")
    expected_config = runtime_spec.get("imageConfigId")
    if expected_config is not None and expected_config != image.image_config_id:
        raise DockerRuntimeError("runtime image configuration ID differs from the inspected image")
    expected_platform = runtime_spec.get("platform")
    if expected_platform is not None and expected_platform != image.platform:
        raise DockerRuntimeError("runtime platform differs from the inspected image")
    if runtime_spec.get("executionTarget", "container") != "container":
        raise DockerRuntimeError("coding container requires the container execution target")
    labels = runtime_spec.get("labels", {})
    if not isinstance(labels, Mapping) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()):
        raise DockerRuntimeError("container labels are invalid")
    uid = runtime_spec.get("uid", 1000)
    gid = runtime_spec.get("gid", 1000)
    if not isinstance(uid, int) or not isinstance(gid, int) or uid < 0 or gid < 0:
        raise DockerRuntimeError("container user identity is invalid")
    manifest_path = runtime_spec.get("manifestPath")
    if manifest_path is not None and (not isinstance(manifest_path, str) or not Path(manifest_path).is_file()):
        raise DockerRuntimeError("controller manifest path is unavailable")
    docker_args = ["create", "--name", container_name]
    for key, value in labels.items():
        docker_args.extend(["--label", f"{key}={value}"])
    for key, value in {
        "PI_RUN_ID": runtime_spec.get("runId"), "PI_MANIFEST_DIGEST": runtime_spec.get("manifestDigest"),
        "PI_PROJECT_ID": runtime_spec.get("projectId"), "PI_WORKING_COPY_ID": runtime_spec.get("workingCopyId"),
        "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
    }.items():
        if value is not None:
            docker_args.extend(["--env", f"{key}={value}"])
    docker_args.extend(["--network", "none", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", f"{uid}:{gid}", "--mount", f"type=bind,src={source},dst=/workspace,readonly=false,bind-propagation=rprivate", "-w", "/workspace", image_reference, *command])
    container_id = _run(docker_args).strip()
    observation = inspect_container(container_id)
    if observation["imageConfigId"] != image.image_config_id or observation["networkMode"] not in {"none", ""} or observation["name"] != container_name or observation["user"] != f"{uid}:{gid}":
        raise DockerRuntimeError("Docker container identity does not match launch record")
    writable = [item for item in observation["mounts"] if bool(item.get("RW"))]
    if len(writable) != 1 or Path(str(writable[0].get("Source", ""))).resolve() != source or str(writable[0].get("Destination", "")) != "/workspace":
        raise DockerRuntimeError("Docker container writable mounts are not scoped to working copy")
    if observation["labels"] != dict(labels):
        raise DockerRuntimeError("Docker container labels do not match the launch record")
    return {"image": image.as_dict(), "container": observation, "containerId": container_id, "runtimeSpecHash": expected_hash, "network": "none", "writableSource": str(source)}


__all__ = ["DockerImageIdentity", "DockerRuntimeError", "create_coding_container", "inspect_container", "inspect_image"]
