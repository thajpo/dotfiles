"""Sole Docker lifecycle and tool-execution owner for writer runs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any, Mapping

from .models import canonical_json, utc_now


PINNED_ACCEPTANCE_IMAGE = "python:3.11-slim@sha256:a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0"
MANAGED_LABEL = "pi.control.managed"
IDLE_COMMAND = ["python3", "-c", "import signal,time,sys;signal.signal(signal.SIGTERM,lambda *_:sys.exit(0));time.sleep(2147483647)"]
WORKDIR = "/workspace"
MAX_TOOL_INPUT = 48 * 1024
MAX_TOOL_OUTPUT = 48 * 1024
DEFAULT_TIMEOUT = 30


class DockerRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class DockerImageIdentity:
    image_reference: str
    image_config_id: str
    registry_digest: str
    platform: str
    environment: Mapping[str, str]

    def as_dict(self) -> dict[str, str]:
        return {
            "imageReference": self.image_reference,
            "imageConfigId": self.image_config_id,
            "registryDigest": self.registry_digest,
            "platform": self.platform,
        }


def _docker() -> str:
    path = shutil.which("docker", path=os.defpath)
    if path is None:
        raise DockerRuntimeError("Docker is unavailable")
    return path


def _environment() -> dict[str, str]:
    return {"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C"}


def _invoke(args: list[str], *, timeout: float = 60.0, check: bool = True, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            [_docker(), *args], env=_environment(), stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False, shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise DockerRuntimeError("Docker command timed out") from error
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[:1024]
        raise DockerRuntimeError(detail or "Docker command failed")
    return result


def _run(args: list[str], *, timeout: float = 60.0) -> str:
    return _invoke(args, timeout=timeout).stdout.decode("utf-8", errors="replace")


def inspect_image(image_reference: str) -> DockerImageIdentity:
    if not isinstance(image_reference, str) or not image_reference or "\x00" in image_reference or "@sha256:" not in image_reference:
        raise DockerRuntimeError("an explicit digest-pinned image reference is required")
    value = json.loads(_run(["image", "inspect", "--format", "{{json .}}", image_reference]))
    if not isinstance(value, dict) or not isinstance(value.get("Id"), str) or not value["Id"].startswith("sha256:"):
        raise DockerRuntimeError("Docker image inspection is malformed")
    registry_digest = "sha256:" + image_reference.rsplit("@sha256:", 1)[1]
    repo_digests = value.get("RepoDigests") or []
    if not any(isinstance(item, str) and item.endswith("@" + registry_digest) for item in repo_digests):
        raise DockerRuntimeError("the exact local image does not expose the requested registry digest")
    platform = f"{value.get('Os') or 'unknown'}/{value.get('Architecture') or 'unknown'}"
    if not platform.startswith("linux/"):
        raise DockerRuntimeError("writer tool images must be Linux images")
    environment: dict[str, str] = {}
    for entry in (value.get("Config") or {}).get("Env") or []:
        if not isinstance(entry, str) or "=" not in entry:
            raise DockerRuntimeError("Docker image environment is malformed")
        key, item_value = entry.split("=", 1)
        if any(marker in key.upper() for marker in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "COOKIE", "CAPABILITY", "SSH_", "AWS_", "DOCKER")):
            raise DockerRuntimeError("Docker image environment contains a forbidden authority field")
        environment[key] = item_value
    return DockerImageIdentity(image_reference, value["Id"], registry_digest, platform, environment)


def inspect_package_image(image_reference: str, *, expected_image_config_id: str, expected_platform: str, allow_local_config_id_only: bool) -> dict[str, Any]:
    """Inspect a package image, with one explicit test-only config-ID mode."""

    if not allow_local_config_id_only:
        image = inspect_image(image_reference)
        if image.image_config_id != expected_image_config_id or image.platform != expected_platform:
            raise DockerRuntimeError("package image identity or platform changed")
        return {**image.as_dict(), "testOnlyConfigIdentity": False}
    if image_reference != expected_image_config_id or not image_reference.startswith("sha256:"):
        raise DockerRuntimeError("test-only package image must use its exact local config ID as the reference")
    value = json.loads(_run(["image", "inspect", "--format", "{{json .}}", image_reference]))
    if not isinstance(value, dict) or value.get("Id") != expected_image_config_id:
        raise DockerRuntimeError("test-only local package image config is unavailable")
    platform = f"{value.get('Os') or 'unknown'}/{value.get('Architecture') or 'unknown'}"
    if platform != expected_platform or not platform.startswith("linux/"):
        raise DockerRuntimeError("test-only package image platform changed")
    for entry in (value.get("Config") or {}).get("Env") or []:
        key = entry.split("=", 1)[0] if isinstance(entry, str) else ""
        if any(marker in key.upper() for marker in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "COOKIE", "CAPABILITY", "SSH_", "AWS_", "DOCKER")):
            raise DockerRuntimeError("test-only package image environment contains a forbidden authority field")
    return {"imageReference": image_reference, "imageConfigId": expected_image_config_id, "registryDigest": None, "platform": platform, "testOnlyConfigIdentity": True}


def _valid_container_id(value: str) -> str:
    if not isinstance(value, str) or len(value) < 12 or any(character not in "0123456789abcdef" for character in value):
        raise DockerRuntimeError("container identity is invalid")
    return value


def inspect_container(container_id: str) -> dict[str, Any]:
    item = json.loads(_run(["container", "inspect", _valid_container_id(container_id)]))
    if not isinstance(item, list) or len(item) != 1 or not isinstance(item[0], dict):
        raise DockerRuntimeError("Docker container inspection is malformed")
    value = item[0]
    config = value.get("Config") or {}
    host = value.get("HostConfig") or {}
    state = value.get("State") or {}
    environment: dict[str, str] = {}
    for entry in config.get("Env") or []:
        if isinstance(entry, str) and "=" in entry:
            key, item_value = entry.split("=", 1)
            environment[key] = item_value
    return {
        "id": value.get("Id"), "name": str(value.get("Name") or "").removeprefix("/"), "imageConfigId": value.get("Image"),
        "running": bool(state.get("Running")), "status": state.get("Status"), "pid": int(state.get("Pid") or 0),
        "user": config.get("User") or "", "command": config.get("Cmd") or [], "workdir": config.get("WorkingDir") or "",
        "networkMode": host.get("NetworkMode"), "readOnlyRoot": bool(host.get("ReadonlyRootfs")),
        "capDrop": host.get("CapDrop") or [], "capAdd": host.get("CapAdd") or [], "securityOpt": host.get("SecurityOpt") or [],
        "privileged": bool(host.get("Privileged")), "pidMode": host.get("PidMode") or "", "ipcMode": host.get("IpcMode") or "",
        "devices": host.get("Devices") or [], "deviceRequests": host.get("DeviceRequests") or [], "portBindings": host.get("PortBindings") or {},
        "tmpfs": host.get("Tmpfs") or {}, "memoryBytes": int(host.get("Memory") or 0), "nanoCpus": int(host.get("NanoCpus") or 0),
        "pidsLimit": int(host.get("PidsLimit") or 0), "mounts": value.get("Mounts") or [], "labels": config.get("Labels") or {},
        "environment": environment, "execIds": value.get("ExecIDs") or [],
    }


def _canonical_owned_path(value: str | Path, *, directory: bool) -> Path:
    candidate = Path(value).expanduser().absolute()
    current = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        current /= component
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise DockerRuntimeError("mount source paths must not contain symlinks")
        if info.st_uid not in {0, os.geteuid()}:
            raise DockerRuntimeError("mount source path ownership is unsafe")
        if info.st_uid != os.geteuid() and info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise DockerRuntimeError("mount source parent is writable by another principal")
    info = candidate.lstat()
    if directory and not stat.S_ISDIR(info.st_mode):
        raise DockerRuntimeError("working-copy mount source is not a directory")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise DockerRuntimeError("Git mask mount source is not a regular file")
    return candidate


def _assert_no_nested_git(source: Path) -> None:
    for root, directories, files in os.walk(source, topdown=True, followlinks=False):
        relative = Path(root).relative_to(source)
        if relative == Path("."):
            directories[:] = [name for name in directories if name != ".git"]
            files = [name for name in files if name != ".git"]
        if ".git" in directories or ".git" in files:
            raise DockerRuntimeError("nested Git metadata or submodules are not allowed in a writer mount")
        for name in list(directories):
            candidate = Path(root) / name
            if candidate.is_symlink():
                directories.remove(name)
        if relative != Path(".") and relative.name == ".git":
            raise DockerRuntimeError("nested Git metadata is not allowed")


def _inode(path: Path) -> tuple[int, int]:
    info = path.lstat()
    return int(info.st_dev), int(info.st_ino)


def _spec_hash(value: Mapping[str, Any]) -> str:
    body = dict(value)
    body.pop("specHash", None)
    return "sha256:" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def container_name(run_id: str) -> str:
    return "pi-tool-" + run_id.removeprefix("run_")


def prepare_tool_runtime(
    *, state_root: str | Path, run_id: str, image_reference: str, project: Mapping[str, Any],
    working_copy: Mapping[str, Any], build_id: str, writer_epoch: int,
) -> dict[str, Any]:
    image = inspect_image(image_reference)
    source = _canonical_owned_path(str(working_copy["path"]), directory=True)
    if source == Path(source.anchor):
        raise DockerRuntimeError("working-copy mount source is too broad")
    _assert_no_nested_git(source)
    root_git = source / ".git"
    git_info = root_git.lstat()
    if stat.S_ISLNK(git_info.st_mode):
        raise DockerRuntimeError("working-copy Git metadata must not be a symlink")
    expected_git = Path(str(working_copy["git_dir"] or project["git_common_dir"])).resolve(strict=True)
    if stat.S_ISREG(git_info.st_mode):
        if git_info.st_size > 4096:
            raise DockerRuntimeError("working-copy root Git metadata is unavailable or unsafe")
        try:
            marker = root_git.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as error:
            raise DockerRuntimeError("working-copy Git marker is unreadable") from error
        if not marker.startswith("gitdir: ") or Path(marker.removeprefix("gitdir: ")).expanduser().resolve(strict=True) != expected_git:
            raise DockerRuntimeError("working-copy Git marker differs from controller state")
    elif stat.S_ISDIR(git_info.st_mode):
        # Directory-form root metadata is the registered primary checkout. It
        # is hidden from the container by the same controller-owned empty
        # regular read-only file mount at /workspace/.git. Only the registered
        # primary may be exposed this way, and only when its metadata is
        # exactly the controller-recorded Git common directory.
        if str(working_copy["kind"]) != "primary":
            raise DockerRuntimeError("directory-form Git metadata is allowed only for the registered primary checkout")
        git_device, git_inode = _inode(root_git)
        expected_device, expected_inode = _inode(expected_git)
        if (git_device, git_inode) != (expected_device, expected_inode):
            raise DockerRuntimeError("primary checkout Git metadata differs from controller state")
    else:
        raise DockerRuntimeError("working-copy root Git metadata is unavailable or unsafe")

    runtime_root = Path(state_root).expanduser().absolute() / "runtime" / run_id
    runtime_root.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(runtime_root, 0o700)
    mask = runtime_root / "git-mask"
    if stat.S_ISDIR(git_info.st_mode):
        # Directory-form primary checkout: the mask must be a directory so the
        # bind mount covers the container's /workspace/.git directory.
        os.mkdir(mask, 0o500)
        mask = _canonical_owned_path(mask, directory=True)
    else:
        fd = os.open(mask, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o400)
        os.close(fd)
        mask = _canonical_owned_path(mask, directory=False)
    source_device, source_inode = _inode(source)
    mask_device, mask_inode = _inode(mask)
    environment_root = Path(state_root).expanduser().absolute() / "environments" / str(working_copy["working_copy_id"])
    environment_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(environment_root, 0o700)
    environment_root = _canonical_owned_path(environment_root, directory=True)
    try:
        environment_root.relative_to(Path(state_root).expanduser().absolute() / "environments")
    except ValueError as error:
        raise DockerRuntimeError("package environment root escapes controller state") from error
    environment_device, environment_inode = _inode(environment_root)
    labels = {
        MANAGED_LABEL: "true", "pi.control.run-id": run_id, "pi.control.project-id": str(project["project_id"]),
        "pi.control.working-copy-id": str(working_copy["working_copy_id"]), "pi.control.writer-epoch": str(writer_epoch),
        "pi.control.controller-build-id": build_id,
    }
    value: dict[str, Any] = {
        "specVersion": 2, "specHash": "", **image.as_dict(), "command": list(IDLE_COMMAND),
        "uid": os.getuid(), "gid": os.getgid(), "workdir": WORKDIR,
        "mounts": [
            {"kind": "working-copy", "source": str(source), "target": WORKDIR, "readOnly": False, "sourceDevice": source_device, "sourceInode": source_inode},
            {"kind": "git-mask", "source": str(mask), "target": f"{WORKDIR}/.git", "readOnly": True, "sourceDevice": mask_device, "sourceInode": mask_inode},
            {"kind": "package-environment", "source": str(environment_root), "target": "/environments", "readOnly": False, "sourceDevice": environment_device, "sourceInode": environment_inode},
        ],
        "readOnlyRoot": True, "tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777"},
        "networkMode": "none", "capDrop": ["ALL"], "securityOpt": ["no-new-privileges:true"],
        "environment": {**image.environment, "HOME": "/tmp", "LANG": "C.UTF-8", "PATH": "/usr/local/bin:/usr/bin:/bin", "TMPDIR": "/tmp", "PI_PACKAGE_ENV_ROOT": "/environments"},
        "labels": labels, "resources": {"memoryBytes": 536_870_912, "nanoCpus": 1_000_000_000, "pidsLimit": 64},
    }
    value["specHash"] = _spec_hash(value)
    return value


def validate_tool_runtime(tool: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "specVersion", "specHash", "imageReference", "imageConfigId", "registryDigest", "platform", "command", "uid", "gid", "workdir",
        "mounts", "readOnlyRoot", "tmpfs", "networkMode", "capDrop", "securityOpt", "environment", "labels", "resources",
    }
    value = dict(tool)
    if set(value) != required or value.get("specVersion") != 2 or value.get("specHash") != _spec_hash(value):
        raise DockerRuntimeError("tool runtime specification is incomplete or has the wrong hash")
    if value.get("command") != IDLE_COMMAND or value.get("workdir") != WORKDIR or value.get("readOnlyRoot") is not True:
        raise DockerRuntimeError("tool runtime command or filesystem boundary is invalid")
    if value.get("networkMode") != "none" or value.get("capDrop") != ["ALL"] or value.get("securityOpt") != ["no-new-privileges:true"]:
        raise DockerRuntimeError("tool runtime isolation boundary is invalid")
    if not isinstance(value.get("mounts"), list) or len(value["mounts"]) != 3:
        raise DockerRuntimeError("tool runtime must have exactly the working-copy, Git-mask, and private package-environment mounts")
    if value["mounts"][0].get("target") != WORKDIR or value["mounts"][0].get("readOnly") is not False or value["mounts"][1].get("target") != f"{WORKDIR}/.git" or value["mounts"][1].get("readOnly") is not True or value["mounts"][2].get("kind") != "package-environment" or value["mounts"][2].get("target") != "/environments" or value["mounts"][2].get("readOnly") is not False:
        raise DockerRuntimeError("tool runtime mount boundary is invalid")
    image = inspect_image(str(value["imageReference"]))
    if image.as_dict() != {key: value[key] for key in ("imageReference", "imageConfigId", "registryDigest", "platform")}:
        raise DockerRuntimeError("tool runtime image differs from current exact local inspection")
    return value


def _docker_create_args(tool: Mapping[str, Any], name: str) -> list[str]:
    args = ["container", "create", "--name", name]
    for key, value in sorted(tool["labels"].items()):
        args.extend(["--label", f"{key}={value}"])
    for key, value in sorted(tool["environment"].items()):
        args.extend(["--env", f"{key}={value}"])
    for target, options in sorted(tool["tmpfs"].items()):
        args.extend(["--tmpfs", f"{target}:{options}"])
    for mount in tool["mounts"]:
        options = f"type=bind,src={mount['source']},dst={mount['target']},bind-propagation=rprivate"
        if mount["readOnly"]:
            options += ",readonly"
        args.extend(["--mount", options])
    resources = tool["resources"]
    args.extend([
        "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--user", f"{tool['uid']}:{tool['gid']}", "--workdir", WORKDIR, "--memory", str(resources["memoryBytes"]),
        "--cpus", str(resources["nanoCpus"] / 1_000_000_000), "--pids-limit", str(resources["pidsLimit"]),
        str(tool["imageReference"]), *tool["command"],
    ])
    return args


def _attest_mounts(tool: Mapping[str, Any], observation: Mapping[str, Any]) -> None:
    expected = {item["target"]: item for item in tool["mounts"]}
    actual_binds = {str(item.get("Destination")): item for item in observation["mounts"] if item.get("Type") == "bind"}
    if set(actual_binds) != set(expected):
        raise DockerRuntimeError("container bind mounts differ from the complete runtime specification")
    for target, mount in expected.items():
        if mount["kind"] == "git-mask":
            info = Path(str(mount["source"])).expanduser().absolute().lstat()
            is_directory = stat.S_ISDIR(info.st_mode)
            if not is_directory and not stat.S_ISREG(info.st_mode):
                raise DockerRuntimeError("Git mask mount source is neither a regular file nor a directory")
        else:
            is_directory = mount["kind"] in {"working-copy", "package-environment"}
        source = _canonical_owned_path(str(mount["source"]), directory=is_directory)
        if _inode(source) != (int(mount["sourceDevice"]), int(mount["sourceInode"])):
            raise DockerRuntimeError("container mount source inode changed")
        actual = actual_binds[target]
        if Path(str(actual.get("Source"))).absolute() != source or bool(actual.get("RW")) == bool(mount["readOnly"]) or actual.get("Propagation") != "rprivate":
            raise DockerRuntimeError("container mount attestation failed")


def attest_container(tool: Mapping[str, Any], observation: Mapping[str, Any], *, name: str, running: bool) -> dict[str, Any]:
    value = validate_tool_runtime(tool)
    expected = {
        "name": name, "imageConfigId": value["imageConfigId"], "running": running, "user": f"{value['uid']}:{value['gid']}",
        "command": value["command"], "workdir": WORKDIR, "networkMode": "none", "readOnlyRoot": True,
        "capDrop": ["ALL"], "capAdd": [], "securityOpt": ["no-new-privileges:true"], "privileged": False,
        "pidMode": "", "ipcMode": "private", "devices": [], "deviceRequests": [], "portBindings": {}, "tmpfs": value["tmpfs"],
        "memoryBytes": value["resources"]["memoryBytes"], "nanoCpus": value["resources"]["nanoCpus"], "pidsLimit": value["resources"]["pidsLimit"],
        "labels": value["labels"], "environment": value["environment"],
    }
    for key, expected_value in expected.items():
        if observation.get(key) != expected_value:
            raise DockerRuntimeError(f"container attestation mismatch: {key}")
    _attest_mounts(value, observation)
    return dict(observation)


def create_start_container(store: Any, *, run_id: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
    tool = validate_tool_runtime(manifest["toolRuntime"])
    name = container_name(run_id)
    intent = {"schemaVersion": 1, "state": "create-intent", "name": name, "specHash": tool["specHash"], "toolRuntime": tool, "observedAt": utc_now()}
    with store.transaction():
        cursor = store.conn.execute("UPDATE runs SET container_observation_json=?,updated_at=? WHERE run_id=? AND observed_state='ready' AND container_id IS NULL", (canonical_json(intent), utc_now(), run_id))
        if cursor.rowcount != 1:
            raise DockerRuntimeError("container intent could not be recorded")
    container_id: str | None = None
    try:
        container_id = _valid_container_id(_run(_docker_create_args(tool, name)).strip())
        created = inspect_container(container_id)
        with store.transaction():
            cursor = store.conn.execute("UPDATE runs SET container_id=?,container_observation_json=?,updated_at=? WHERE run_id=? AND container_id IS NULL", (container_id, canonical_json({"schemaVersion": 1, "state": "created", "name": name, "observation": created, "observedAt": utc_now()}), utc_now(), run_id))
            if cursor.rowcount != 1:
                raise DockerRuntimeError("created container identity could not be persisted")
        attest_container(tool, created, name=name, running=False)
        _run(["container", "start", container_id])
        started = attest_container(tool, inspect_container(container_id), name=name, running=True)
        mask_check = execute_argv(container_id, ["python3", "-c", "import os,stat; s=os.lstat('/workspace/.git'); ok = (stat.S_ISREG(s.st_mode) and s.st_size==0) or (stat.S_ISDIR(s.st_mode) and not os.listdir('/workspace/.git')); raise SystemExit(0 if ok else 9)"], timeout=5, output_limit=1024)
        if mask_check["exitCode"] != 0:
            raise DockerRuntimeError("container Git metadata mask is not empty")
        exact = {"schemaVersion": 1, "state": "running", "name": name, "observation": started, "observedAt": utc_now()}
        with store.transaction():
            store.conn.execute("UPDATE runs SET container_observation_json=?,updated_at=? WHERE run_id=? AND container_id=?", (canonical_json(exact), utc_now(), run_id, container_id))
        return {"containerId": container_id, "name": name, "observation": started}
    except BaseException:
        cleanup = cleanup_container(container_id=container_id, container_name=name, expected_labels=tool["labels"])
        with store.transaction():
            state = "cleanup-proved" if cleanup["absent"] else "cleanup-unknown"
            store.conn.execute("UPDATE runs SET container_observation_json=?,updated_at=? WHERE run_id=?", (canonical_json({"schemaVersion": 1, "state": state, "name": name, "cleanup": cleanup, "observedAt": utc_now()}), utc_now(), run_id))
        raise


def cleanup_container(*, container_id: str | None, container_name: str, expected_labels: Mapping[str, str]) -> dict[str, Any]:
    identifier = container_id or container_name
    inspected: dict[str, Any] | None = None
    errors: list[str] = []
    result = _invoke(["container", "inspect", identifier], check=False)
    if result.returncode == 0:
        try:
            raw = json.loads(result.stdout)
            actual_id = _valid_container_id(str(raw[0]["Id"]))
            inspected = inspect_container(actual_id)
            if container_id is not None and actual_id != container_id:
                raise DockerRuntimeError("cleanup container ID differs from durable intent")
            if inspected["name"] != container_name or inspected["labels"] != dict(expected_labels):
                raise DockerRuntimeError("cleanup refused a container with different identity labels")
            if inspected["running"]:
                _run(["container", "stop", "--time", "5", actual_id], timeout=15)
            _run(["container", "rm", actual_id])
        except Exception as error:
            errors.append(str(error)[:1024])
    elif container_id is not None:
        named = _invoke(["container", "inspect", container_name], check=False)
        if named.returncode == 0:
            errors.append("durable container ID is absent but its durable name is occupied")
    by_id = _invoke(["container", "inspect", identifier], check=False).returncode != 0
    by_name = _invoke(["container", "inspect", container_name], check=False).returncode != 0
    return {"containerId": container_id, "containerName": container_name, "wasObserved": inspected is not None, "absent": by_id and by_name and not errors, "errors": errors}


def cleanup_run_container(store: Any, *, run_id: str) -> dict[str, Any]:
    run = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None or not run["manifest_path"]:
        raise DockerRuntimeError("run container binding is unavailable")
    from .run_manifest import read_manifest
    manifest = read_manifest(run["manifest_path"]).manifest
    try:
        prior_observation = json.loads(run["container_observation_json"]) if run["container_observation_json"] else None
    except json.JSONDecodeError:
        prior_observation = None
    tool = validate_tool_runtime(manifest["toolRuntime"])
    with store.transaction():
        store.conn.execute("UPDATE runs SET observed_state=CASE WHEN observed_state IN ('ready','running') THEN 'stopping' ELSE observed_state END,updated_at=? WHERE run_id=?", (utc_now(), run_id))
    cleanup = cleanup_container(container_id=run["container_id"], container_name=container_name(run_id), expected_labels=tool["labels"])
    state = "absent" if cleanup["absent"] else "cleanup-unknown"
    with store.transaction():
        store.conn.execute("UPDATE runs SET container_observation_json=?,updated_at=? WHERE run_id=?", (canonical_json({"schemaVersion": 1, "state": state, "cleanup": cleanup, "prior": prior_observation, "observedAt": utc_now()}), utc_now(), run_id))
    return cleanup


def _active_exec_ids(container_id: str) -> set[str]:
    return {str(value) for value in inspect_container(container_id)["execIds"] if isinstance(value, str)}


def _kill_exact_exec(container_id: str, previous: set[str]) -> bool:
    candidates = _active_exec_ids(container_id) - previous
    if len(candidates) != 1:
        return False
    value = json.loads(_run(["inspect", "--type", "exec", next(iter(candidates))]))
    pid = int(value.get("Pid") or 0) if isinstance(value, dict) else 0
    if pid <= 0:
        return False
    return _invoke(["exec", container_id, "/bin/kill", "-KILL", str(pid)], check=False).returncode == 0


def execute_argv(container_id: str, argv: list[str], *, input_bytes: bytes | None = None, timeout: int = DEFAULT_TIMEOUT, output_limit: int = MAX_TOOL_OUTPUT, cancellation: threading.Event | None = None) -> dict[str, Any]:
    _valid_container_id(container_id)
    if not isinstance(argv, list) or not argv or len(argv) > 128 or any(not isinstance(item, str) or not item or "\x00" in item or len(item) > 4096 for item in argv):
        raise DockerRuntimeError("tool argv is invalid")
    if input_bytes is not None and len(input_bytes) > MAX_TOOL_INPUT:
        raise DockerRuntimeError("tool input exceeds its bound")
    if not isinstance(timeout, int) or timeout < 1 or timeout > 120:
        raise DockerRuntimeError("tool timeout is invalid")
    before = _active_exec_ids(container_id)
    process = subprocess.Popen(
        [_docker(), "exec", "--workdir", WORKDIR, "--interactive" if input_bytes is not None else "--detach-keys=", container_id, *argv],
        env=_environment(), stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
    )
    deadline = time.monotonic() + timeout
    sent_input = False
    while True:
        if cancellation is not None and cancellation.is_set():
            _kill_exact_exec(container_id, before)
            process.kill()
            process.communicate()
            raise DockerRuntimeError("tool request was cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_exact_exec(container_id, before)
            process.kill()
            process.communicate()
            raise DockerRuntimeError("tool request timed out")
        try:
            stdout, stderr = process.communicate(input=input_bytes if not sent_input else None, timeout=min(0.1, remaining))
            break
        except subprocess.TimeoutExpired:
            sent_input = True
            continue
    truncated = len(stdout) > output_limit or len(stderr) > output_limit
    return {
        "exitCode": int(process.returncode), "stdout": stdout[:output_limit].decode("utf-8", errors="replace"),
        "stderr": stderr[:output_limit].decode("utf-8", errors="replace"), "truncated": truncated,
    }


FILE_HELPER = r'''import json,os,secrets,stat,sys
MAX=49152
req=json.loads(sys.stdin.buffer.read(MAX+1))
rel=req["path"]
parts=rel.split("/")
if not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in parts): raise ValueError("path must be canonical relative")
fd=os.open("/workspace",os.O_RDONLY|os.O_DIRECTORY)
try:
  for part in parts[:-1]:
    nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
  name=parts[-1]
  def read():
    f=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
    try:
      s=os.fstat(f)
      if not stat.S_ISREG(s.st_mode) or s.st_size>MAX: raise ValueError("file is not a bounded regular file")
      return os.read(f,MAX+1)
    finally: os.close(f)
  def write(data):
    if len(data)>MAX: raise ValueError("content exceeds bound")
    tmp=".pi-write-"+secrets.token_hex(12)
    f=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
    try: os.write(f,data);os.fsync(f)
    finally: os.close(f)
    os.replace(tmp,name,src_dir_fd=fd,dst_dir_fd=fd);os.fsync(fd)
  op=req["operation"]
  if op=="read":
    data=read().decode("utf-8"); lines=data.splitlines(); start=req.get("offset",1); limit=req.get("limit",2000); out={"path":rel,"lines":lines[start-1:start-1+limit],"totalLines":len(lines)}
  elif op=="write":
    data=req["content"].encode("utf-8");write(data);out={"path":rel,"bytesWritten":len(data)}
  elif op=="edit":
    data=read().decode("utf-8");old=req["oldText"]
    if data.count(old)!=1: raise ValueError("oldText must occur exactly once")
    changed=data.replace(old,req["newText"],1).encode("utf-8");write(changed);out={"path":rel,"bytesWritten":len(changed)}
  else: raise ValueError("unsupported file operation")
  print(json.dumps(out,sort_keys=True,separators=(",",":")))
finally: os.close(fd)
'''


def execute_file_tool(container_id: str, tool: str, arguments: Mapping[str, Any], *, cancellation: threading.Event | None = None) -> dict[str, Any]:
    allowed = {"read": {"path", "offset", "limit"}, "write": {"path", "content"}, "edit": {"path", "oldText", "newText"}}
    if tool not in allowed or set(arguments) - allowed[tool]:
        raise DockerRuntimeError("file tool fields are invalid")
    path = arguments.get("path")
    if not isinstance(path, str) or not path or len(path) > 4096 or path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise DockerRuntimeError("tool path must be canonical relative")
    payload = {"operation": tool, **dict(arguments)}
    body = canonical_json(payload).encode("utf-8")
    result = execute_argv(container_id, ["python3", "-c", FILE_HELPER], input_bytes=body, timeout=DEFAULT_TIMEOUT, cancellation=cancellation)
    if result["exitCode"] != 0:
        raise DockerRuntimeError(result["stderr"].strip()[:1024] or "container file helper failed")
    try:
        return json.loads(result["stdout"])
    except json.JSONDecodeError as error:
        raise DockerRuntimeError("container file helper returned invalid JSON") from error


def execute_shell_tool(container_id: str, arguments: Mapping[str, Any], *, cancellation: threading.Event | None = None) -> dict[str, Any]:
    if set(arguments) - {"command", "argv", "timeout"}:
        raise DockerRuntimeError("shell tool fields are invalid")
    command = arguments.get("command")
    argv = arguments.get("argv")
    if (command is None) == (argv is None):
        raise DockerRuntimeError("shell requires exactly one of command or argv")
    if command is not None:
        if not isinstance(command, str) or not command or "\x00" in command or len(command.encode("utf-8")) > 16 * 1024:
            raise DockerRuntimeError("shell text is invalid")
        exact = ["/bin/sh", "-lc", command]
    else:
        exact = list(argv) if isinstance(argv, list) else []
    return execute_argv(container_id, exact, timeout=int(arguments.get("timeout", DEFAULT_TIMEOUT)), cancellation=cancellation)


def run_one_shot_network(
    *, request_id: str, image_reference: str, expected_image_config_id: str, expected_platform: str,
    working_copy: str | Path, argv: list[str], timeout_ms: int, mount_read_only: bool,
) -> dict[str, Any]:
    """Run one exact approved argv in a separately attested bridge container."""

    if not isinstance(request_id, str) or not request_id.startswith(("cmd_", "pkreq_")) or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in request_id):
        raise DockerRuntimeError("one-shot request identity is invalid")
    if not isinstance(argv, list) or not argv or len(argv) > 128 or any(not isinstance(item, str) or not item or "\x00" in item or len(item) > 4096 for item in argv):
        raise DockerRuntimeError("one-shot argv is invalid")
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not 1 <= timeout_ms <= 120_000:
        raise DockerRuntimeError("one-shot timeout is invalid")
    image = inspect_image(image_reference)
    if image.image_config_id != expected_image_config_id or image.platform != expected_platform:
        raise DockerRuntimeError("one-shot local image identity or platform changed")
    root = _canonical_owned_path(working_copy, directory=True)
    name = "pi-network-" + request_id.split("_", 1)[1]
    labels = {MANAGED_LABEL: "true", "pi.control.request-id": request_id, "pi.control.kind": "one-shot-network"}
    container_id: str | None = None
    timed_out = False
    exit_code: int | None = None
    stdout = b""
    stderr = b""
    with tempfile.TemporaryDirectory(prefix="pi-network-mask-", dir="/tmp") as temporary:
        mask = Path(temporary) / "git-mask"
        mask.write_bytes(b"")
        os.chmod(mask, 0o400)
        args = ["container", "create", "--name", name]
        for key, value in sorted(labels.items()):
            args.extend(["--label", f"{key}={value}"])
        args.extend([
            "--network", "bridge", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", f"{os.getuid()}:{os.getgid()}",
            "--mount", f"type=bind,src={root},dst={WORKDIR},{'readonly,' if mount_read_only else ''}bind-propagation=rprivate",
            "--mount", f"type=bind,src={mask},dst={WORKDIR}/.git,readonly,bind-propagation=rprivate",
            "--workdir", WORKDIR, "--memory", "268435456", "--cpus", "0.5", "--pids-limit", "32",
            image_reference, *argv,
        ])
        try:
            container_id = _valid_container_id(_run(args).strip())
            observation = inspect_container(container_id)
            expected = {
                "name": name, "imageConfigId": image.image_config_id, "running": False, "user": f"{os.getuid()}:{os.getgid()}",
                "command": argv, "workdir": WORKDIR, "networkMode": "bridge", "readOnlyRoot": True,
                "capDrop": ["ALL"], "capAdd": [], "securityOpt": ["no-new-privileges:true"], "privileged": False,
                "pidMode": "", "ipcMode": "private", "devices": [], "deviceRequests": [], "portBindings": {},
                "tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777"}, "memoryBytes": 268_435_456,
                "nanoCpus": 500_000_000, "pidsLimit": 32, "labels": labels,
            }
            for key, expected_value in expected.items():
                if observation.get(key) != expected_value:
                    raise DockerRuntimeError(f"one-shot container attestation mismatch: {key}")
            mounts = {str(item.get("Destination")): item for item in observation["mounts"] if item.get("Type") == "bind"}
            if set(mounts) != {WORKDIR, f"{WORKDIR}/.git"} or bool(mounts[WORKDIR].get("RW")) == mount_read_only or bool(mounts[f"{WORKDIR}/.git"].get("RW")):
                raise DockerRuntimeError("one-shot container mounts differ from the approved boundary")
            process = subprocess.Popen([_docker(), "container", "start", "--attach", container_id], env=_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
            try:
                stdout, stderr = process.communicate(timeout=max(0.001, timeout_ms / 1000))
                exit_code = int(process.returncode)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                stdout, stderr = process.communicate()
        finally:
            if container_id is not None:
                _invoke(["container", "rm", "--force", container_id], check=False, timeout=15)
            absent_by_id = container_id is None or _invoke(["container", "inspect", container_id], check=False).returncode != 0
            absent_by_name = _invoke(["container", "inspect", name], check=False).returncode != 0
            if not absent_by_id or not absent_by_name:
                raise DockerRuntimeError("one-shot network container removal could not be proved")
    output_limit = 64 * 1024
    return {
        "executionPlace": "container-network", "exitCode": exit_code, "timedOut": timed_out,
        "stdout": stdout[:output_limit].decode("utf-8", errors="replace"), "stderr": stderr[:output_limit].decode("utf-8", errors="replace"),
        "stdoutTruncated": len(stdout) > output_limit, "stderrTruncated": len(stderr) > output_limit,
        "networkMode": "bridge", "networkContacted": False, "image": image.as_dict(),
        "mountMode": "read-only" if mount_read_only else "read-write", "containerId": container_id,
        "cleanup": {"absentById": True, "absentByName": True, "name": name},
    }


def run_one_shot_package(
    *, request_id: str, image_reference: str, expected_image_config_id: str, expected_platform: str,
    allow_local_config_id_only: bool, working_copy: str | Path, cache_root: str | Path,
    environment_root: str | Path, argv: list[str], timeout_ms: int,
) -> dict[str, Any]:
    """Materialize one lock in a separate bounded package container."""

    if not isinstance(request_id, str) or not request_id.startswith("pkreq_"):
        raise DockerRuntimeError("package request identity is invalid")
    if not isinstance(argv, list) or not argv or len(argv) > 128 or any(not isinstance(item, str) or not item or "\x00" in item or len(item) > 8192 for item in argv):
        raise DockerRuntimeError("package operation argv is invalid")
    if not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= 120_000:
        raise DockerRuntimeError("package operation timeout is invalid")
    image = inspect_package_image(image_reference, expected_image_config_id=expected_image_config_id, expected_platform=expected_platform, allow_local_config_id_only=allow_local_config_id_only)
    workspace = _canonical_owned_path(working_copy, directory=True)
    cache = _canonical_owned_path(cache_root, directory=True)
    environment = _canonical_owned_path(environment_root, directory=True)
    name = "pi-package-" + request_id.removeprefix("pkreq_")
    labels = {MANAGED_LABEL: "true", "pi.control.request-id": request_id, "pi.control.kind": "one-shot-package"}
    container_id: str | None = None
    stdout = b""
    stderr = b""
    exit_code: int | None = None
    timed_out = False
    with tempfile.TemporaryDirectory(prefix="pi-package-mask-", dir="/tmp") as temporary:
        mask = Path(temporary) / "git-mask"
        mask.write_bytes(b"")
        os.chmod(mask, 0o400)
        args = ["container", "create", "--name", name]
        for key, value in sorted(labels.items()):
            args.extend(["--label", f"{key}={value}"])
        package_environment = {
            "HOME": "/tmp", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TMPDIR": "/tmp",
            "npm_config_audit": "false", "npm_config_fund": "false", "npm_config_ignore_scripts": "true",
            "npm_config_offline": "true", "npm_config_update_notifier": "false", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1", "PIP_ROOT_USER_ACTION": "ignore",
        }
        for key, value in sorted(package_environment.items()):
            args.extend(["--env", f"{key}={value}"])
        args.extend([
            "--network", "bridge", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", f"{os.getuid()}:{os.getgid()}",
            "--mount", f"type=bind,src={workspace},dst={WORKDIR},readonly,bind-propagation=rprivate",
            "--mount", f"type=bind,src={mask},dst={WORKDIR}/.git,readonly,bind-propagation=rprivate",
            "--mount", f"type=bind,src={cache},dst=/cache,readonly,bind-propagation=rprivate",
            "--mount", f"type=bind,src={environment},dst=/environment,bind-propagation=rprivate",
            "--workdir", WORKDIR, "--memory", "536870912", "--cpus", "1.0", "--pids-limit", "64",
            "--entrypoint", "", image_reference, *argv,
        ])
        try:
            container_id = _valid_container_id(_run(args).strip())
            observation = inspect_container(container_id)
            expected = {
                "name": name, "imageConfigId": expected_image_config_id, "running": False, "user": f"{os.getuid()}:{os.getgid()}",
                "command": argv, "workdir": WORKDIR, "networkMode": "bridge", "readOnlyRoot": True,
                "capDrop": ["ALL"], "capAdd": [], "securityOpt": ["no-new-privileges:true"], "privileged": False,
                "pidMode": "", "ipcMode": "private", "devices": [], "deviceRequests": [], "portBindings": {},
                "tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777"}, "memoryBytes": 536_870_912,
                "nanoCpus": 1_000_000_000, "pidsLimit": 64,
            }
            for key, expected_value in expected.items():
                if observation.get(key) != expected_value:
                    raise DockerRuntimeError(f"package container attestation mismatch: {key}")
            if any(observation["labels"].get(key) != value for key, value in labels.items()) or any(key.startswith("pi.control.") and key not in labels for key in observation["labels"]):
                raise DockerRuntimeError("package container controller labels differ from the approved identity")
            if any(marker in key.upper() for key in observation["environment"] for marker in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "COOKIE", "CAPABILITY", "SSH_", "AWS_", "DOCKER")):
                raise DockerRuntimeError("package container environment exposes a credential or authority field")
            mounts = {str(item.get("Destination")): item for item in observation["mounts"] if item.get("Type") == "bind"}
            if set(mounts) != {WORKDIR, f"{WORKDIR}/.git", "/cache", "/environment"}:
                raise DockerRuntimeError("package container mounts differ from the complete approved set")
            if any(bool(mounts[target].get("RW")) for target in (WORKDIR, f"{WORKDIR}/.git", "/cache")) or not bool(mounts["/environment"].get("RW")):
                raise DockerRuntimeError("package container mount modes differ from the approved boundary")
            process = subprocess.Popen([_docker(), "container", "start", "--attach", container_id], env=_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
            try:
                stdout, stderr = process.communicate(timeout=max(0.001, timeout_ms / 1000))
                exit_code = int(process.returncode)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                stdout, stderr = process.communicate()
        finally:
            if container_id is not None:
                _invoke(["container", "rm", "--force", container_id], check=False, timeout=15)
            absent_by_id = container_id is None or _invoke(["container", "inspect", container_id], check=False).returncode != 0
            absent_by_name = _invoke(["container", "inspect", name], check=False).returncode != 0
            if not absent_by_id or not absent_by_name:
                raise DockerRuntimeError("package container removal could not be proved")
    limit = 64 * 1024
    return {
        "executionPlace": "package-network-container", "exitCode": exit_code, "timedOut": timed_out,
        "stdout": stdout[:limit].decode("utf-8", errors="replace"), "stderr": stderr[:limit].decode("utf-8", errors="replace"),
        "stdoutTruncated": len(stdout) > limit, "stderrTruncated": len(stderr) > limit,
        "networkMode": "bridge", "networkContacted": False, "remoteProviderContacted": False,
        "image": image, "mounts": {"workingCopy": "read-only", "cache": "read-only", "environment": "read-write", "git": "masked"},
        "containerId": container_id, "cleanup": {"absentById": True, "absentByName": True, "name": name},
    }


__all__ = [
    "DockerImageIdentity", "DockerRuntimeError", "MANAGED_LABEL", "PINNED_ACCEPTANCE_IMAGE", "attest_container",
    "cleanup_container", "cleanup_run_container", "container_name", "create_start_container", "execute_file_tool", "execute_shell_tool",
    "inspect_container", "inspect_image", "inspect_package_image", "prepare_tool_runtime", "run_one_shot_network", "run_one_shot_package", "validate_tool_runtime",
]
