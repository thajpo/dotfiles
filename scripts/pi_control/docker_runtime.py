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
    return {"id": item.get("Id"), "name": item.get("Name"), "imageConfigId": item.get("Image"), "platform": f"{config.get('Os') or 'unknown'}/{config.get('Architecture') or 'unknown'}", "running": bool((item.get("State") or {}).get("Running")), "user": config.get("User") or "", "networkMode": host.get("NetworkMode"), "mounts": mounts, "labels": config.get("Labels") or {}}


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
    docker_args = ["create", "--name", container_name, "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "1000:1000", "-v", f"{source}:/workspace:rw", "-w", "/workspace", image_reference, *command]
    container_id = _run(docker_args).strip()
    observation = inspect_container(container_id)
    if observation["imageConfigId"] != image.image_config_id or observation["networkMode"] not in {"none", ""}:
        raise DockerRuntimeError("Docker container identity does not match launch record")
    if not observation["mounts"] or any(Path(str(item.get("Source", ""))).resolve() != source for item in observation["mounts"] if item.get("RW")):
        raise DockerRuntimeError("Docker container writable mounts are not scoped to working copy")
    return {"image": image.as_dict(), "container": observation, "containerId": container_id, "runtimeSpecHash": expected_hash}


__all__ = ["DockerImageIdentity", "DockerRuntimeError", "create_coding_container", "inspect_container", "inspect_image"]
