"""Build, verify, activate, and roll back immutable Pi generations."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from typing import Any

from .pi_store import PiStore
from .staged_build import BuildManifest, create_build_manifest, load_build_manifest, write_build_manifest


class InstallError(RuntimeError):
    pass


class InstallUnavailable(InstallError):
    """A bounded offline prerequisite is absent, rather than invalid."""


RESOURCE_CATALOG = "pi/pi-resources.v1.json"
RESOURCE_INVENTORY = "release-resources.json"
PI_PACKAGE = "@earendil-works/pi-coding-agent"
CANARY_LAUNCHERS = (
    "bin/pi-control",
    "bin/pi-authorize",
    "bin/pi-install",
    "bin/pi-system-run",
    "bin/pi-system-secretary",
    "bin/pi-system-investigator",
    "bin/pi-system-reviewer",
    "bin/pi-system-container-run",
    "bin/pi-system-workstream-run",
    "bin/pi-workstream",
    "bin/pi-integration",
    "bin/pi-activate",
    "bin/pisec",
    "bin/pi-personal",
    "bin/pi-start",
    "bin/pi-restart",
    "bin/pidev",
    "bin/pi-tmux",
    "bin/pi-tmux-switch",
    "bin/pi-help-custom",
)
EXCLUDED_LAUNCHERS = ("bin/pi",)
EXTENSION_ROLES = {
    "extension:controller-channel": ("pi/extensions/controller-channel/index.ts", ("secretary", "investigator", "reviewer", "personal", "workstream", "integration")),
    "extension:scoped-project-read": ("pi/extensions/scoped-project-read/index.ts", ("secretary", "investigator", "reviewer")),
    "extension:project-messages": ("pi/extensions/project-messages/index.ts", ("secretary", "investigator", "reviewer", "personal", "workstream", "integration")),
    "extension:project-commands": ("pi/extensions/project-commands/index.ts", ("personal", "workstream", "integration")),
    "extension:dependency-review": ("pi/extensions/dependency-review/index.ts", ("secretary", "investigator", "reviewer", "personal", "workstream", "integration")),
    "extension:secretary-work": ("pi/extensions/secretary-work/index.ts", ("secretary",)),
    "extension:change-flow": ("pi/extensions/change-flow/index.ts", ("secretary", "personal", "workstream", "integration")),
    "extension:observability": ("pi/extensions/observability/index.ts", ("secretary", "personal", "workstream", "integration")),
    "extension:harness-feedback": ("pi/extensions/harness-feedback/index.ts", ("secretary", "investigator", "reviewer", "personal", "workstream", "integration")),
}
FIRST_PARTY_PACKAGES = (
    ("package:pi-sandbox-control", "pi-sandbox-control", "0.3.0-control.1", "pi/packages/pi-sandbox-control"),
    ("package:pi-subagents", "pi-subagents", "0.35.1", "pi/packages/pi-subagents-control"),
)
FIRST_PARTY_RUNTIME_DEPENDENCIES = {
    "pi-sandbox-control": {},
    "pi-subagents": {},
}
_MAX_PACKAGE_MEMBERS = 16_384
_MAX_PACKAGE_BYTES = 512 * 1024 * 1024
ROLE_RESOURCES = {
    "secretary": ("package:pi-core", "package:pi-subagents", "extension:controller-channel", "extension:scoped-project-read", "extension:project-messages", "extension:dependency-review", "extension:secretary-work", "extension:change-flow", "extension:observability", "extension:harness-feedback"),
    "investigator": ("package:pi-core", "extension:controller-channel", "extension:scoped-project-read", "extension:project-messages", "extension:dependency-review", "extension:harness-feedback"),
    "reviewer": ("package:pi-core", "extension:controller-channel", "extension:scoped-project-read", "extension:project-messages", "extension:dependency-review", "extension:harness-feedback"),
    "personal": ("package:pi-core", "package:pi-subagents", "package:pi-sandbox-control", "extension:controller-channel", "extension:project-messages", "extension:project-commands", "extension:dependency-review", "extension:change-flow", "extension:observability", "extension:harness-feedback"),
    "workstream": ("package:pi-core", "package:pi-subagents", "package:pi-sandbox-control", "extension:controller-channel", "extension:project-messages", "extension:project-commands", "extension:dependency-review", "extension:change-flow", "extension:observability", "extension:harness-feedback"),
    "integration": ("package:pi-core", "package:pi-subagents", "package:pi-sandbox-control", "extension:controller-channel", "extension:project-messages", "extension:project-commands", "extension:dependency-review", "extension:change-flow", "extension:observability", "extension:harness-feedback"),
}
HOST_LAUNCH_PROFILES = {
    "secretary": {"supported": True, "resources": ("package:pi-core", "package:pi-subagents", "extension:controller-channel", "extension:scoped-project-read", "extension:project-messages", "extension:dependency-review", "extension:secretary-work", "extension:change-flow", "extension:observability", "extension:harness-feedback"), "tools": ("acknowledge_project_message", "analyze_integration", "approve_workstream", "check_package_review_gate", "git_read", "grep", "list_changes", "list_project_messages", "ls", "observe_change_queue", "observe_fleet", "observe_messages", "observe_tasks", "post_project_message", "propose_integration", "propose_review", "propose_workstream", "project_work_index", "read", "record_dependency_disposition", "reply_project_message", "request_review", "start_investigation", "subagent", "subagent_interrupt", "subagent_list", "subagent_resume", "subagent_start", "subagent_status", "subagent_steer", "subagent_stop", "subagent_wait", "harness_feedback")},
    "investigator": {"supported": True, "resources": ("package:pi-core", "extension:controller-channel", "extension:scoped-project-read", "extension:project-messages", "extension:dependency-review", "extension:harness-feedback"), "tools": ("acknowledge_project_message", "git_read", "grep", "list_project_messages", "ls", "post_project_message", "read", "record_package_security_review", "reply_project_message", "harness_feedback")},
    "reviewer": {"supported": True, "resources": ("package:pi-core", "extension:controller-channel", "extension:scoped-project-read", "extension:project-messages", "extension:dependency-review", "extension:harness-feedback"), "tools": ("acknowledge_project_message", "check_package_review_gate", "git_read", "grep", "list_project_messages", "ls", "post_project_message", "read", "reply_project_message", "harness_feedback")},
    "personal": {"supported": True, "resources": ("package:pi-core", "package:pi-subagents", "extension:controller-channel", "package:pi-sandbox-control", "extension:project-messages", "extension:project-commands", "extension:dependency-review", "extension:change-flow", "extension:observability", "extension:harness-feedback"), "tools": ("acknowledge_project_message", "bash", "edit", "inventory_dependency_changes", "list_project_messages", "observe_change_queue", "observe_fleet", "observe_messages", "observe_tasks", "package_operation_status", "post_project_message", "project_command_status", "read", "reply_project_message", "request_package_operation", "request_project_command", "subagent", "subagent_interrupt", "subagent_list", "subagent_resume", "subagent_start", "subagent_status", "subagent_steer", "subagent_stop", "subagent_wait", "submit_change", "worker_start", "write", "harness_feedback")},
    "workstream": {"supported": True, "resources": ("package:pi-core", "package:pi-subagents", "extension:controller-channel", "package:pi-sandbox-control", "extension:project-messages", "extension:project-commands", "extension:dependency-review", "extension:change-flow", "extension:observability", "extension:harness-feedback"), "tools": ("acknowledge_project_message", "bash", "edit", "inventory_dependency_changes", "list_project_messages", "observe_change_queue", "observe_fleet", "observe_messages", "observe_tasks", "package_operation_status", "post_project_message", "project_command_status", "read", "reply_project_message", "request_package_operation", "request_project_command", "subagent", "subagent_interrupt", "subagent_list", "subagent_resume", "subagent_start", "subagent_status", "subagent_steer", "subagent_stop", "subagent_wait", "submit_change", "worker_start", "write", "harness_feedback")},
    "integration": {"supported": False, "resources": ("package:pi-core", "extension:controller-channel"), "tools": ()},
}
RELEASE_FILES = (
    *CANARY_LAUNCHERS,
    "scripts/pi-system-run.py",
    "scripts/pi-system-container-run.py",
    "scripts/pi-system-workstream-run.py",
    "scripts/pi-surface.py",
    "scripts/pi_control/__init__.py",
    "scripts/pi_control/models.py",
    "scripts/pi_control/errors.py",
    "scripts/pi_control/events.py",
    "scripts/pi_control/operations.py",
    "scripts/pi_control/locks.py",
    "scripts/pi_control/git_adapter.py",
    "scripts/pi_control/project_policy.py",
    "scripts/pi_control/process_adapter.py",
    "scripts/pi_control/writer_lock.py",
    "scripts/pi_control/pi_schema.py",
    "scripts/pi_control/pi_store.py",
    "scripts/pi_control/pi_client.py",
    "scripts/pi_control/pi_protocol.py",
    "scripts/pi_control/pi_cli.py",
    "scripts/pi_control/pi_install.py",
    "scripts/pi_control/projects.py",
    "scripts/pi_control/conversations.py",
    "scripts/pi_control/role_profiles.py",
    "scripts/pi_control/installed_builds.py",
    "scripts/pi_control/messages.py",
    "scripts/pi_control/command_requests.py",
    "scripts/pi_control/authorization_cli.py",
    "scripts/pi_control/activation_cli.py",
    "scripts/pi_control/activation_approval.py",
    "scripts/pi_control/launch.py",
    "scripts/pi_control/controller_channel.py",
    "scripts/pi_control/host_supervisor.py",
    "scripts/pi_control/docker_runtime.py",
    "scripts/pi_control/pi_workstreams.py",
    "scripts/pi_control/pi_review.py",
    "scripts/pi_control/pi_reconcile.py",
    "scripts/pi_control/scoped_read.py",
    "scripts/pi_control/changes.py",
    "scripts/pi_control/reviews.py",
    "scripts/pi_control/integration.py",
    "scripts/pi_control/dependencies.py",
    "scripts/pi_control/package_diff.py",
    "scripts/pi_control/package_environment.py",
    "scripts/pi_control/investigators.py",
    "scripts/pi_control/subagents.py",
    "scripts/pi_control/presentation.py",
    "scripts/pi_control/tmux_scope.py",
    "scripts/pi_control/presentation_locator.py",
    "scripts/pi_control/reconcile.py",
    "scripts/pi_control/run_manifest.py",
    "scripts/pi_control/staged_build.py",
    "pi/PI_VERSION",
    RESOURCE_CATALOG,
    "pi/repository-policy.json",
    *(str(PurePosixPath(path).parent) for path, _roles in EXTENSION_ROLES.values()),
)


def _safe_root(value: os.PathLike[str] | str) -> Path:
    path = Path(value).expanduser().absolute()
    if path == Path(path.anchor) or path.is_symlink():
        raise InstallError("install root is too broad or symlinked")
    return path


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise InstallError(f"artifact path is not relative: {value!r}")
    if PurePosixPath(value).as_posix() != value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise InstallError(f"artifact path is not canonical: {value!r}")
    return value


def _canonical_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
    path.chmod(mode)


def _copy_entries(source: Path, destination: Path, manifest: BuildManifest) -> None:
    for entry in manifest.payload["files"]:
        relative = Path(entry["path"])
        source_path = source / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if entry["kind"] == "symlink":
            os.symlink(entry["target"], target)
        else:
            shutil.copy2(source_path, target, follow_symlinks=False)
            os.chmod(target, entry["mode"])


def _npm_error(error: subprocess.CalledProcessError) -> InstallError:
    stdout = error.stdout if isinstance(error.stdout, bytes) else str(error.stdout or "").encode()
    stderr = error.stderr if isinstance(error.stderr, bytes) else str(error.stderr or "").encode()
    detail = (stderr + b"\n" + stdout).decode("utf-8", errors="replace")
    if "ENOTCACHED" in detail and "EINTEGRITY" not in detail:
        return InstallUnavailable("the offline npm cache cannot materialize the pinned dependency tree")
    return InstallError(f"offline npm materialization failed integrity or resolution checks: {detail.strip()[-1024:]}")


def _run_npm(argv: list[str], *, cwd: Path, npm_cache: Path | None) -> subprocess.CompletedProcess[bytes]:
    env = {
        **os.environ,
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "npm_config_ignore_scripts": "true",
        "npm_config_offline": "true",
        "npm_config_update_notifier": "false",
    }
    if npm_cache is not None:
        env["npm_config_cache"] = str(npm_cache)
    try:
        return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, env=env)
    except subprocess.CalledProcessError as error:
        raise _npm_error(error) from error


def _tarball_name(name: str, version: str) -> str:
    return f"{name.removeprefix('@').replace('/', '-')}-{version}.tgz"


def _tarball_metadata(path: Path, *, name: str, version: str) -> dict[str, str]:
    package_json: bytes | None = None
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                pure = PurePosixPath(member.name)
                if pure.is_absolute() or ".." in pure.parts:
                    raise InstallError(f"package tarball contains an escaping path: {member.name}")
                if member.name == "package/package.json" and member.isfile():
                    stream = archive.extractfile(member)
                    package_json = stream.read() if stream is not None else None
    except (tarfile.TarError, OSError) as error:
        raise InstallError(f"package tarball is unreadable: {path.name}") from error
    if package_json is None:
        raise InstallError(f"package tarball lacks package/package.json: {path.name}")
    try:
        metadata = json.loads(package_json.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstallError(f"package tarball metadata is invalid: {path.name}") from error
    if metadata.get("name") != name or metadata.get("version") != version:
        raise InstallError(f"package tarball metadata does not match {name}@{version}")
    body = path.read_bytes()
    return {
        "integrity": "sha512-" + base64.b64encode(hashlib.sha512(body).digest()).decode("ascii"),
        "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
    }


def _tarball_file(path: Path, relative: str) -> bytes:
    target = f"package/{relative}"
    try:
        with tarfile.open(path, "r:gz") as archive:
            member = archive.getmember(target)
            if not member.isfile() or member.size > _MAX_PACKAGE_BYTES:
                raise InstallError(f"package tarball member is not a bounded regular file: {target}")
            stream = archive.extractfile(member)
            if stream is None:
                raise InstallError(f"package tarball member is unreadable: {target}")
            return stream.read()
    except (KeyError, tarfile.TarError, OSError) as error:
        raise InstallError(f"package tarball lacks a readable {target}") from error


def _extract_core_tarball(tarball: Path, build_root: Path) -> Path:
    package_root = build_root / "package"
    seen: set[str] = set()
    total_bytes = 0
    try:
        with tarfile.open(tarball, "r:gz") as archive:
            members = archive.getmembers()
            if len(members) > _MAX_PACKAGE_MEMBERS:
                raise InstallError("Pi core package has too many archive members")
            for member in members:
                pure = PurePosixPath(member.name)
                if pure.is_absolute() or not pure.parts or pure.parts[0] != "package" or ".." in pure.parts or "\x00" in member.name:
                    raise InstallError(f"Pi core package contains an escaping path: {member.name}")
                relative = pure.as_posix()
                if relative in seen:
                    raise InstallError(f"Pi core package contains a duplicate path: {relative}")
                seen.add(relative)
                target = build_root.joinpath(*pure.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target.chmod(member.mode & 0o777)
                    continue
                if not member.isfile():
                    raise InstallError(f"Pi core package contains a special file: {relative}")
                total_bytes += member.size
                if member.size > _MAX_PACKAGE_BYTES or total_bytes > _MAX_PACKAGE_BYTES:
                    raise InstallError("Pi core package exceeds its extraction size bound")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if target.exists() or target.is_symlink():
                    raise InstallError(f"Pi core package path collides during extraction: {relative}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise InstallError(f"Pi core package member is unreadable: {relative}")
                with target.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(member.mode & 0o777)
    except (tarfile.TarError, OSError) as error:
        raise InstallError("Pi core package extraction failed") from error
    if not package_root.is_dir() or package_root.is_symlink():
        raise InstallError("Pi core package root is missing after extraction")
    return package_root


def _pack_source(source: Path, packages: Path, *, name: str, version: str, npm_cache: Path | None) -> Path:
    output = _run_npm(["npm", "pack", "--offline", "--ignore-scripts", "--json", "--pack-destination", str(packages)], cwd=source, npm_cache=npm_cache)
    try:
        filename = json.loads(output.stdout.decode("utf-8"))[0]["filename"]
    except (UnicodeDecodeError, json.JSONDecodeError, IndexError, KeyError, TypeError) as error:
        raise InstallError(f"npm pack returned invalid metadata for {name}") from error
    packed = packages / filename
    target = packages / _tarball_name(name, version)
    if packed != target:
        if target.exists() or target.is_symlink():
            raise InstallError(f"duplicate package artifact: {target.name}")
        packed.rename(target)
    _tarball_metadata(target, name=name, version=version)
    return target


def _materialize_core(packages: Path, version: str, *, pi_core_tarball: Path | None, npm_cache: Path | None) -> Path:
    target = packages / _tarball_name(PI_PACKAGE, version)
    if pi_core_tarball is None:
        # The tarball may be cached even when npm's package metadata response is
        # not. Address the immutable registry tarball directly so offline builds
        # do not require a second, unrelated metadata cache entry.
        tarball_url = f"https://registry.npmjs.org/@earendil-works/pi-coding-agent/-/pi-coding-agent-{version}.tgz"
        output = _run_npm(["npm", "pack", tarball_url, "--offline", "--ignore-scripts", "--json", "--pack-destination", str(packages)], cwd=packages, npm_cache=npm_cache)
        try:
            packed = packages / json.loads(output.stdout.decode("utf-8"))[0]["filename"]
        except (UnicodeDecodeError, json.JSONDecodeError, IndexError, KeyError, TypeError) as error:
            raise InstallError("npm pack returned invalid metadata for pinned Pi core") from error
        if packed != target:
            packed.rename(target)
    else:
        source = pi_core_tarball.expanduser().resolve(strict=True)
        if source.is_symlink() or not source.is_file():
            raise InstallError("Pi core package input must be a regular tarball")
        shutil.copy2(source, target, follow_symlinks=False)
        target.chmod(0o600)
    _tarball_metadata(target, name=PI_PACKAGE, version=version)
    return target


def _install_core(tarball: Path, runtime: Path, version: str, *, npm_cache: Path | None) -> None:
    build_root = runtime.parent / ".core-build"
    if build_root.exists() or build_root.is_symlink():
        raise InstallError("temporary Pi core build root already exists")
    build_root.mkdir(mode=0o700)
    package_root = _extract_core_tarball(tarball, build_root)
    package_path = package_root / "package.json"
    shrinkwrap_path = package_root / "npm-shrinkwrap.json"
    if package_path.is_symlink() or shrinkwrap_path.is_symlink() or not package_path.is_file() or not shrinkwrap_path.is_file():
        raise InstallError("Pi core package lacks regular package and shrinkwrap metadata")
    original_package = package_path.read_bytes()
    original_shrinkwrap = shrinkwrap_path.read_bytes()
    original_mode = package_path.stat().st_mode & 0o777
    try:
        metadata = json.loads(original_package.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstallError("Pi core package metadata is invalid") from error
    if metadata.get("name") != PI_PACKAGE or metadata.get("version") != version:
        raise InstallError("Pi core package metadata differs from the exact pin")
    sanitized = dict(metadata)
    sanitized.pop("devDependencies", None)
    sanitized.pop("scripts", None)
    _canonical_json(package_path, sanitized, mode=original_mode)
    try:
        _run_npm([
            "npm", "ci", "--offline", "--ignore-scripts", "--omit=dev", "--legacy-peer-deps",
        ], cwd=package_root, npm_cache=npm_cache)
    finally:
        package_path.write_bytes(original_package)
        package_path.chmod(original_mode)
    if shrinkwrap_path.read_bytes() != original_shrinkwrap:
        raise InstallError("npm ci changed the exact Pi core shrinkwrap")
    destination = runtime / "node_modules" / "@earendil-works" / "pi-coding-agent"
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists() or destination.is_symlink():
        raise InstallError("Pi core destination already exists")
    package_root.rename(destination)
    shutil.rmtree(build_root)
    if build_root.exists() or build_root.is_symlink():
        raise InstallError("temporary Pi core build root was not removed")


def _expected_catalog(pi_version: str) -> dict[str, Any]:
    packages = [{
        "resourceId": "package:pi-core",
        "name": PI_PACKAGE,
        "version": pi_version,
        "source": "pi/PI_VERSION",
        "installedPath": f"runtime/node_modules/{PI_PACKAGE}",
    }]
    packages.extend({
        "resourceId": resource_id,
        "name": name,
        "version": version,
        "source": source,
        "installedPath": f"runtime/node_modules/{name}",
    } for resource_id, name, version, source in FIRST_PARTY_PACKAGES)
    return {
        "schemaVersion": 1,
        "launchers": list(CANARY_LAUNCHERS),
        "excludedLaunchers": list(EXCLUDED_LAUNCHERS),
        "extensions": [{"resourceId": resource_id, "path": path, "roles": list(roles)} for resource_id, (path, roles) in EXTENSION_ROLES.items()],
        "packages": packages,
        "roles": [{"role": role, "resources": list(resources)} for role, resources in ROLE_RESOURCES.items()],
        "hostLaunchProfiles": [{"role": role, "supported": value["supported"], "resources": list(value["resources"]), "tools": list(value["tools"])} for role, value in HOST_LAUNCH_PROFILES.items()],
    }


def _load_catalog(root: Path) -> tuple[str, dict[str, Any]]:
    version_path = root / "pi/PI_VERSION"
    catalog_path = root / RESOURCE_CATALOG
    if version_path.is_symlink() or not version_path.is_file() or catalog_path.is_symlink() or not catalog_path.is_file():
        raise InstallError("the pinned Pi version or resource catalog is missing")
    pi_version = version_path.read_text(encoding="utf-8").strip()
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise InstallError("the release resource catalog is invalid JSON") from error
    if catalog != _expected_catalog(pi_version):
        raise InstallError("the release resource catalog differs from the canonical role/resource set")
    return pi_version, catalog


def _package_records(root: Path, catalog: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for package in catalog["packages"]:
        name = package["name"]
        version = package["version"]
        tarball = f"packages/{_tarball_name(name, version)}"
        installed_path = package["installedPath"]
        for relative in (tarball, installed_path):
            _relative(relative)
        tarball_path = root / tarball
        installed = root / installed_path
        integrity = _tarball_metadata(tarball_path, name=name, version=version)
        metadata_path = installed / "package.json"
        if installed.is_symlink() or not installed.is_dir() or metadata_path.is_symlink() or not metadata_path.is_file():
            raise InstallError(f"installed package is missing or symlinked: {name}")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise InstallError(f"installed package metadata is invalid: {name}") from error
        if metadata.get("name") != name or metadata.get("version") != version:
            raise InstallError(f"installed package version is wrong: {name}")
        records.append({
            "resourceId": package["resourceId"],
            "name": name,
            "version": version,
            "source": package["source"],
            "tarball": tarball,
            "installedPath": installed_path,
            "integrity": integrity["integrity"],
            "tarballSha256": integrity["sha256"],
        })
    return records


def _verify_core_install(root: Path, record: dict[str, Any]) -> dict[str, str]:
    core = root / record["installedPath"]
    package_path = core / "package.json"
    shrinkwrap_path = core / "npm-shrinkwrap.json"
    tarball = root / record["tarball"]
    if package_path.is_symlink() or shrinkwrap_path.is_symlink() or not package_path.is_file() or not shrinkwrap_path.is_file():
        raise InstallError("installed Pi core lacks regular package or shrinkwrap metadata")
    original_package = _tarball_file(tarball, "package.json")
    original_shrinkwrap = _tarball_file(tarball, "npm-shrinkwrap.json")
    if package_path.read_bytes() != original_package or shrinkwrap_path.read_bytes() != original_shrinkwrap:
        raise InstallError("installed Pi core did not restore its original package metadata")
    try:
        package = json.loads(original_package.decode("utf-8"))
        shrinkwrap = json.loads(original_shrinkwrap.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstallError("installed Pi core package or shrinkwrap metadata is invalid") from error
    packages = shrinkwrap.get("packages")
    root_entry = packages.get("") if isinstance(packages, dict) else None
    if shrinkwrap.get("lockfileVersion") != 3 or not isinstance(root_entry, dict) or root_entry.get("name") != PI_PACKAGE or root_entry.get("version") != record["version"]:
        raise InstallError("installed Pi core shrinkwrap root identity is wrong")
    dependencies = package.get("dependencies", {})
    if not isinstance(dependencies, dict) or not all(isinstance(name, str) and name and isinstance(spec, str) and spec for name, spec in dependencies.items()) or root_entry.get("dependencies", {}) != dependencies:
        raise InstallError("installed Pi core shrinkwrap does not bind its production dependencies")
    exact_versions: dict[str, str] = {}
    for name in sorted(dependencies):
        lock_entry = packages.get(f"node_modules/{name}")
        installed_metadata = core / "node_modules" / name / "package.json"
        if not isinstance(lock_entry, dict) or not isinstance(lock_entry.get("version"), str) or not lock_entry["version"]:
            raise InstallError(f"installed Pi core shrinkwrap lacks an exact direct dependency: {name}")
        if installed_metadata.is_symlink() or not installed_metadata.is_file():
            raise InstallError(f"installed Pi core lacks a nested runtime dependency: {name}")
        try:
            installed = json.loads(installed_metadata.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise InstallError(f"installed Pi core dependency metadata is invalid: {name}") from error
        if installed.get("name") != name or installed.get("version") != lock_entry["version"]:
            raise InstallError(f"installed Pi core dependency version differs from shrinkwrap: {name}")
        exact_versions[name] = lock_entry["version"]
    return exact_versions


def _verify_package_lock(root: Path, records: list[dict[str, Any]]) -> None:
    lock_path = root / "runtime/package-lock.json"
    package_path = root / "runtime/package.json"
    if lock_path.is_symlink() or package_path.is_symlink() or not lock_path.is_file() or not package_path.is_file():
        raise InstallError("the installed runtime lacks regular npm metadata")
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise InstallError("the installed runtime npm metadata is invalid") from error
    outer_records = [record for record in records if record["name"] != PI_PACKAGE]
    expected_dependencies = {record["name"]: f"file:../{record['tarball']}" for record in outer_records}
    packages = lock.get("packages")
    if lock.get("lockfileVersion") != 3 or not isinstance(packages, dict) or package.get("dependencies") != expected_dependencies or packages.get("", {}).get("dependencies") != expected_dependencies:
        raise InstallError("the runtime package lock does not bind the exact artifact packages")
    if f"node_modules/{PI_PACKAGE}" in packages:
        raise InstallError("the outer runtime package lock must not resolve Pi core")
    for record in outer_records:
        key = f"node_modules/{record['name']}"
        entry = packages.get(key)
        if not isinstance(entry, dict) or entry.get("version") != record["version"] or entry.get("resolved") != f"file:../{record['tarball']}" or entry.get("integrity") != record["integrity"]:
            raise InstallError(f"the runtime package lock has wrong metadata or integrity for {record['name']}")
        expected_runtime = FIRST_PARTY_RUNTIME_DEPENDENCIES[record["name"]]
        if entry.get("dependencies", {}) != expected_runtime:
            raise InstallError(f"the runtime package lock has wrong production dependencies for {record['name']}")
        installed_package = root / record["installedPath"] / "package.json"
        try:
            installed_metadata = json.loads(installed_package.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise InstallError(f"installed first-party package metadata is invalid: {record['name']}") from error
        if installed_metadata.get("dependencies", {}) != expected_runtime:
            raise InstallError(f"installed first-party package has wrong production dependencies: {record['name']}")
        for dependency, version in expected_runtime.items():
            dependency_entry = packages.get(f"node_modules/{dependency}")
            dependency_metadata = root / "runtime/node_modules" / dependency / "package.json"
            if not isinstance(dependency_entry, dict) or dependency_entry.get("version") != version or dependency_metadata.is_symlink() or not dependency_metadata.is_file():
                raise InstallError(f"first-party runtime dependency is missing or wrong: {dependency}")
            try:
                installed_dependency = json.loads(dependency_metadata.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise InstallError(f"first-party runtime dependency metadata is invalid: {dependency}") from error
            if installed_dependency.get("name") != dependency or installed_dependency.get("version") != version:
                raise InstallError(f"first-party runtime dependency version is wrong: {dependency}")
    for key, entry in packages.items():
        if key and isinstance(entry, dict) and isinstance(entry.get("resolved"), str) and entry["resolved"].startswith(("http://", "https://")):
            integrity = entry.get("integrity")
            if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
                raise InstallError(f"the runtime package lock lacks exact integrity for {key}")


def _inventory(root: Path, catalog: dict[str, Any]) -> dict[str, Any]:
    records = _package_records(root, catalog)
    extension_entrypoints = {
        "package:pi-sandbox-control": "src/index.ts",
        "package:pi-subagents": "index.ts",
    }
    for record in records:
        relative_entry = extension_entrypoints.get(record["resourceId"])
        if relative_entry is None:
            continue
        extension = root / record["installedPath"] / relative_entry
        if extension.is_symlink() or not extension.is_file():
            raise InstallError(f"installed package extension entrypoint is missing or unsafe: {record['resourceId']}")
        record["extensionPath"] = f"{record['installedPath']}/{relative_entry}"
        record["extensionSha256"] = "sha256:" + hashlib.sha256(extension.read_bytes()).hexdigest()
    core_record = next(record for record in records if record["name"] == PI_PACKAGE)
    core_record["productionDependencies"] = _verify_core_install(root, core_record)
    _verify_package_lock(root, records)
    pi_cli = root / f"runtime/node_modules/{PI_PACKAGE}/dist/cli.js"
    if pi_cli.is_symlink() or not pi_cli.is_file() or not os.access(pi_cli, os.X_OK):
        raise InstallError("the exact pinned Pi executable is unavailable")
    node_value = shutil.which("node", path=os.defpath)
    if node_value is None:
        raise InstallUnavailable("Node.js is unavailable on the controlled system PATH")
    node = Path(node_value).resolve(strict=True)
    if node.is_symlink() or not node.is_file() or not os.access(node, os.X_OK):
        raise InstallError("the configured Node.js executable is unsafe")
    extensions = []
    for item in catalog["extensions"]:
        path = root / item["path"]
        if path.is_symlink() or not path.is_file():
            raise InstallError(f"staged extension is missing or unsafe: {item['resourceId']}")
        extensions.append({**item, "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()})
    return {
        "schemaVersion": 1,
        "catalog": RESOURCE_CATALOG,
        "runtimeRoot": "runtime",
        "piExecutable": f"runtime/node_modules/{PI_PACKAGE}/dist/cli.js",
        "piExecutableSha256": "sha256:" + hashlib.sha256(pi_cli.read_bytes()).hexdigest(),
        "nodeExecutable": str(node),
        "nodeExecutableSha256": "sha256:" + hashlib.sha256(node.read_bytes()).hexdigest(),
        "launchers": catalog["launchers"],
        "excludedLaunchers": catalog["excludedLaunchers"],
        "extensions": extensions,
        "packages": records,
        "roles": catalog["roles"],
        "hostLaunchProfiles": catalog["hostLaunchProfiles"],
    }


def _verify_symlinks(root: Path) -> None:
    canonical = root.resolve(strict=True)
    for path in sorted(root.rglob("*")):
        if not path.is_symlink():
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(canonical)
        except (OSError, ValueError) as error:
            raise InstallError(f"artifact symlink escapes its generation: {path.relative_to(root)}") from error


def _stage_from_source(
    source_root: os.PathLike[str] | str,
    staging_root: os.PathLike[str] | str,
    *,
    pi_core_tarball: os.PathLike[str] | str | None = None,
    npm_cache: os.PathLike[str] | str | None = None,
    repository_root: os.PathLike[str] | str | None = None,
    source_commit: str | None = None,
    source_tree_hash: str | None = None,
    source_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one complete generation; no caller may add production bytes."""

    source = _safe_root(source_root)
    stage_path = _safe_root(staging_root)
    if stage_path.exists():
        raise InstallError("staging root already exists; preserve it and choose a new stage")
    stage_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage_path.mkdir(mode=0o700)
    source_manifest = create_build_manifest(source, files=RELEASE_FILES, repository=repository_root or source, source_commit=source_commit, source_tree_hash=source_tree_hash, metadata=source_metadata)
    _copy_entries(source, stage_path, source_manifest)
    pi_version, catalog = _load_catalog(stage_path)
    packages = stage_path / "packages"
    runtime = stage_path / "runtime"
    packages.mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)
    cache_path = Path(npm_cache).expanduser().absolute() if npm_cache is not None else None
    core_input = Path(pi_core_tarball) if pi_core_tarball is not None else None
    core_tarball = _materialize_core(packages, pi_version, pi_core_tarball=core_input, npm_cache=cache_path)
    for _resource_id, name, version, relative in FIRST_PARTY_PACKAGES:
        package_source = source / relative
        try:
            metadata = json.loads((package_source / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise InstallError(f"first-party package metadata is unreadable: {name}") from error
        if metadata.get("name") != name or metadata.get("version") != version:
            raise InstallError(f"first-party package metadata does not match {name}@{version}")
        _pack_source(package_source, packages, name=name, version=version, npm_cache=cache_path)
    dependency_specs = {
        package["name"]: f"file:../packages/{_tarball_name(package['name'], package['version'])}"
        for package in catalog["packages"]
        if package["name"] != PI_PACKAGE
    }
    _canonical_json(runtime / "package.json", {"name": "pi-harness-runtime", "private": True, "version": "1.0.0", "dependencies": dependency_specs}, mode=0o600)
    _run_npm([
        "npm", "install", "--offline", "--ignore-scripts", "--no-audit", "--no-fund",
        "--package-lock", "--legacy-peer-deps", "--prefix", str(runtime),
    ], cwd=runtime, npm_cache=cache_path)
    _install_core(core_tarball, runtime, pi_version, npm_cache=cache_path)
    inventory = _inventory(stage_path, catalog)
    _canonical_json(stage_path / RESOURCE_INVENTORY, inventory)
    _verify_symlinks(stage_path)
    manifest = create_build_manifest(
        stage_path,
        repository=repository_root or source,
        source_commit=source_commit,
        source_tree_hash=source_tree_hash,
        metadata={"product": "pi-system", "artifactLayout": "pi-harness-v1", "freshState": True, **(source_metadata or {})},
        test_outcomes={},
        manifest_path=stage_path / "build-manifest.json",
    )
    written = write_build_manifest(manifest, stage_path / "build-manifest.json")
    written.verify_files(stage_path, exclude=["build-manifest.json"])
    return {
        "stageRoot": str(stage_path),
        "installedRoot": str(runtime),
        "controllerRoot": str(stage_path),
        "piExecutable": str(stage_path / inventory["piExecutable"]),
        "buildId": written.build_id,
        "manifestDigest": written.digest,
        "fileCount": len(written.payload["files"]),
        "packageCount": len(inventory["packages"]),
        "piVersion": pi_version,
        "freshState": True,
    }


def _git_output(repository: Path, argv: list[str]) -> str:
    try:
        result = subprocess.run(["git", "-C", str(repository), *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError("Git provenance command could not run") from error
    if result.returncode != 0:
        raise InstallError(f"Git provenance command failed: {result.stderr.strip()[-512:]}")
    return result.stdout.rstrip("\n")


def _release_path_allowed(relative: str) -> bool:
    normalized = PurePosixPath(relative).as_posix()
    for entry in RELEASE_FILES:
        if normalized == entry or normalized.startswith(entry.rstrip("/") + "/"):
            return True
    return False


def _changed_release_paths(repository: Path) -> list[str]:
    output = _git_output(repository, ["status", "--porcelain=v1", "--untracked-files=all", "--", *RELEASE_FILES])
    changed: list[str] = []
    for line in output.splitlines():
        if len(line) >= 4:
            changed.append(line[3:].split(" -> ", 1)[-1])
    return sorted(set(changed))


def _overlay_tree(source: Path, materialized: Path, overlays: list[str]) -> list[str]:
    copied: list[str] = []
    for raw in overlays:
        relative = PurePosixPath(raw).as_posix()
        if relative.startswith("../") or relative == "." or not _release_path_allowed(relative):
            raise InstallError(f"dirty overlay is outside the release file set: {raw}")
        origin = source / relative
        destination = materialized / relative
        if origin.is_symlink() or origin.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if origin.is_symlink():
                if destination.exists() or destination.is_symlink():
                    destination.unlink()
                destination.symlink_to(origin.readlink())
            else:
                shutil.copy2(origin, destination, follow_symlinks=False)
            copied.append(relative)
        elif origin.is_dir():
            for item in sorted(origin.rglob("*")):
                item_relative = item.relative_to(source).as_posix()
                if item.is_file() or item.is_symlink():
                    if not _release_path_allowed(item_relative):
                        continue
                    target = materialized / item_relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if item.is_symlink():
                        if target.exists() or target.is_symlink():
                            target.unlink()
                        target.symlink_to(item.readlink())
                    else:
                        shutil.copy2(item, target, follow_symlinks=False)
                    copied.append(item_relative)
        else:
            raise InstallError(f"dirty overlay does not exist: {raw}")
    return sorted(set(copied))


def _materialize_source(source: Path, source_ref: str, overlays: list[str]) -> tuple[Path, str, str, str, Path | None]:
    commit = _git_output(source, ["rev-parse", "--verify", f"{source_ref}^{{commit}}"])
    tree = _git_output(source, ["rev-parse", "--verify", f"{commit}^{{tree}}"])
    temporary = Path(tempfile.mkdtemp(prefix="pi-source-", dir=str(source.parent)))
    try:
        archive = subprocess.run(["git", "-C", str(source), "archive", "--format=tar", commit], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120)
        if archive.returncode != 0:
            raise InstallError(f"Git archive failed: {archive.stderr.decode(errors='replace')[-512:]}")
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as stream:
            for member in stream.getmembers():
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts:
                    raise InstallError("Git archive contains an unsafe path")
            stream.extractall(temporary, filter="data")
        copied = _overlay_tree(source, temporary, overlays)
        return temporary, commit, tree, "git-ref", temporary
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def stage(
    source_root: os.PathLike[str] | str,
    staging_root: os.PathLike[str] | str,
    *,
    pi_core_tarball: os.PathLike[str] | str | None = None,
    npm_cache: os.PathLike[str] | str | None = None,
    source_ref: str | None = None,
    overlays: list[str] | None = None,
    allow_dirty: bool = True,
) -> dict[str, Any]:
    source = _safe_root(source_root)
    overlay_paths = sorted(set(overlays or []))
    dirty = _changed_release_paths(source)
    if source_ref is None and dirty and not allow_dirty:
        raise InstallError("release source has uncommitted changes; pass --allow-dirty or use --source-ref")
    if source_ref is None and overlay_paths:
        raise InstallError("dirty overlays require an explicit --source-ref")
    materialized: Path | None = None
    try:
        if source_ref is None:
            return _stage_from_source(source, staging_root, pi_core_tarball=pi_core_tarball, npm_cache=npm_cache, repository_root=source, source_metadata={"sourceMode": "working-tree", "dirtyReleasePaths": dirty})
        materialized, commit, tree, _mode, _ = _materialize_source(source, source_ref, overlay_paths)
        copied = [path for path in overlay_paths if _release_path_allowed(path)]
        return _stage_from_source(materialized, staging_root, pi_core_tarball=pi_core_tarball, npm_cache=npm_cache, repository_root=source, source_commit=commit, source_tree_hash=tree, source_metadata={"sourceMode": "git-ref", "sourceRef": source_ref, "overlayFiles": copied})
    finally:
        if materialized is not None:
            shutil.rmtree(materialized, ignore_errors=True)


def _activation_artifacts(root: Path) -> list[str]:
    """Exclusions for an activated generation root.

    ``activate`` writes ``activation.json`` and a controller ``state/`` into
    the generation root after the byte-exact stage is renamed into place. An
    activated root is verified byte-exact for every manifest file; these two
    controller-owned artifacts are tolerated exactly.
    """
    exclude: list[str] = []
    if (root / "activation.json").is_file():
        exclude.append("activation.json")
    if (root / "state").is_dir():
        exclude.append("state")
    return exclude


def verify_stage(stage_root: os.PathLike[str] | str) -> dict[str, Any]:
    root = _safe_root(stage_root)
    manifest = load_build_manifest(root / "build-manifest.json")
    manifest.verify_files(root, exclude=["build-manifest.json", *_activation_artifacts(root)])
    _verify_symlinks(root)
    pi_version, catalog = _load_catalog(root)
    inventory_path = root / RESOURCE_INVENTORY
    if inventory_path.is_symlink() or not inventory_path.is_file():
        raise InstallError("the manifest-bound release resource inventory is missing")
    try:
        resources = json.loads(inventory_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise InstallError("the release resource inventory is invalid JSON") from error
    expected = _inventory(root, catalog)
    if resources != expected:
        raise InstallError("the release resource inventory is wrong")
    return {
        "stageRoot": str(root),
        "installedRoot": str(root / "runtime"),
        "controllerRoot": str(root),
        "piExecutable": str(root / expected["piExecutable"]),
        "buildId": manifest.build_id,
        "manifestDigest": manifest.digest,
        "fileCount": len(manifest.payload["files"]),
        "packageCount": len(expected["packages"]),
        "piVersion": pi_version,
        "verified": True,
    }


def _atomic_json(path: Path, value: dict[str, Any], *, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _protected_surfaces() -> dict[str, Any]:
    """Snapshot the existence of protected surfaces that cutover must not touch."""
    opencode_paths = [
        Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "opencode",
        Path("~/.opencode").expanduser(),
    ]
    tmux_sessions: list[str] = []
    if shutil.which("tmux") is not None:
        try:
            result = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=15)
            if result.returncode == 0:
                tmux_sessions = sorted(result.stdout.split())
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {
        "opencodeDirs": {str(path): path.is_dir() for path in opencode_paths},
        "tmuxSessions": tmux_sessions,
    }


def _assert_protected_surfaces(before: dict[str, Any]) -> None:
    after = _protected_surfaces()
    if after != before:
        raise InstallError("protected surfaces changed during activation: OpenCode directories or unrelated tmux sessions were touched")


def _bounded_smoke(data_root: Path, state_root: Path) -> None:
    controller = data_root / "bin" / "pi-control"
    if not controller.is_file():
        raise InstallError("activated generation is missing its controller entry point")
    try:
        result = subprocess.run([str(controller), "--state-root", str(state_root), "schema", "status"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError("activated generation smoke failed to start") from error
    if result.returncode != 0:
        raise InstallError(f"activated generation smoke failed: {result.stderr.strip()[-1024:] or result.stdout.strip()[-1024:]}")


def _register_generation_with_controller(data_root: Path, state_root: Path) -> None:
    controller = data_root / "bin" / "pi-control"
    try:
        result = subprocess.run([str(controller), "--state-root", str(state_root), "build", "register", "--staged-root", str(data_root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError("activated generation registration could not start") from error
    if result.returncode != 0:
        raise InstallError(f"activated generation registration failed: {result.stderr.strip()[-1024:] or result.stdout.strip()[-1024:]}")
    try:
        registered = json.loads(result.stdout)
        build_id = registered["build_id"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise InstallError("activated generation registration returned invalid JSON") from error
    try:
        activated = subprocess.run([str(controller), "--state-root", str(state_root), "build", "activate", "--build-id", str(build_id)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError("activated generation status update could not start") from error
    if activated.returncode != 0:
        raise InstallError(f"activated generation status update failed: {activated.stderr.strip()[-1024:] or activated.stdout.strip()[-1024:]}")


def _prepare_controller_state(state_root: Path) -> dict[str, Any]:
    if (state_root / "control.db").exists():
        with PiStore(state_root, read_only=True) as store:
            return {"stateRoot": str(state_root), "schema": store.schema_status().as_dict(), "fresh": False}
    return {"stateRoot": str(state_root), "fresh": True}


def activate(stage_root: os.PathLike[str] | str, data_root: os.PathLike[str] | str, *, state_root: os.PathLike[str] | str, reset_state: bool = False) -> dict[str, Any]:
    stage_path = _safe_root(stage_root)
    target = _safe_root(data_root)
    controller_state = _safe_root(state_root)
    if controller_state == target or controller_state.is_relative_to(target):
        raise InstallError("controller state root must not be inside the replaceable data root")
    verified = verify_stage(stage_path)
    _prepare_controller_state(controller_state) if not reset_state else None
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    controller_state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = target.parent / f"{target.name}.activation.lock"
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    state_lock_fd = None
    state_backup: Path | None = None
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise InstallError("another activation or rollback holds the launch lock") from error
        state_lock_path = controller_state.parent / f"{controller_state.name}.reset.lock"
        state_lock_fd = os.open(str(state_lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(state_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise InstallError("another state reset holds the launch lock") from error
        protected_before = _protected_surfaces()
        if reset_state and controller_state.exists():
            if controller_state.is_symlink() or not controller_state.is_dir():
                raise InstallError("existing controller state root is unsafe")
            state_backup = controller_state.parent / f"{controller_state.name}.reset-backup.{uuid.uuid4().hex}"
            os.rename(controller_state, state_backup)
        backup: Path | None = None
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_dir():
                raise InstallError("existing active Pi data root is unsafe")
            backup = target.parent / f"{target.name}.rollback.{uuid.uuid4().hex}"
            os.rename(target, backup)
        marker = {"schemaVersion": 1, "activeRoot": str(target), "buildId": verified["buildId"], "manifestDigest": verified["manifestDigest"], "rollbackRoot": str(backup) if backup else None, "activatedAt": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}
        _atomic_json(stage_path / "activation.json", marker)
        try:
            os.rename(stage_path, target)
            parent_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except BaseException:
            if backup is not None and not target.exists():
                os.rename(backup, target)
            raise
        try:
            _register_generation_with_controller(target, controller_state)
            _bounded_smoke(target, controller_state)
            _assert_protected_surfaces(protected_before)
            registered = {"build_id": verified["buildId"]}
        except BaseException as error:
            failed_root = target.parent / f"{target.name}.activation-failed.{uuid.uuid4().hex}"
            os.rename(target, failed_root)
            if backup is not None:
                os.rename(backup, target)
                if not reset_state:
                    try:
                        _register_generation_with_controller(target, controller_state)
                    except BaseException:
                        pass
            if state_backup is not None:
                if controller_state.exists():
                    shutil.rmtree(controller_state, ignore_errors=True)
                os.rename(state_backup, controller_state)
            raise InstallError(f"activated generation failed: {error}") from error
        if state_backup is not None:
            shutil.rmtree(state_backup, ignore_errors=True)
        return {**verified, "dataRoot": str(target), "stateRoot": str(controller_state), "rollbackRoot": str(backup) if backup else None, "activated": True, "registeredBuild": registered["build_id"], "stateReset": bool(reset_state)}
    finally:
        if state_backup is not None and state_backup.exists():
            if controller_state.exists():
                shutil.rmtree(controller_state, ignore_errors=True)
            os.rename(state_backup, controller_state)
        if state_lock_fd is not None:
            os.close(state_lock_fd)
        os.close(lock_fd)


def ensure_fresh_state(state_root: os.PathLike[str] | str) -> dict[str, Any]:
    root = _safe_root(state_root)
    if root.exists() and (root / "control.db").exists():
        raise InstallError("production Pi state already exists; refusing import or overwrite")
    with PiStore(root) as store:
        return {"stateRoot": str(root), "schema": store.schema_status().as_dict(), "fresh": True}


def rollback(data_root: os.PathLike[str] | str, *, state_root: os.PathLike[str] | str | None = None) -> dict[str, Any]:
    target = _safe_root(data_root)
    if not target.exists() or not target.is_dir() or target.is_symlink():
        raise InstallError("active Pi data root is unavailable")
    backups = sorted((item for item in target.parent.glob(f"{target.name}.rollback.*") if item.is_dir() and not item.is_symlink() and (item / "activation.json").is_file() and (item / "build-manifest.json").is_file() and (item / "bin" / "pi-control").is_file()), key=lambda item: item.stat().st_mtime_ns)
    lock_path = target.parent / f"{target.name}.activation.lock"
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise InstallError("another activation or rollback holds the launch lock") from error
        preserved = target.parent / f"{target.name}.preserved.{uuid.uuid4().hex}"
        os.rename(target, preserved)
        restored: Path | None = None
        try:
            if not backups:
                os.rename(preserved, target)
                return {"rolledBack": False, "restoredRoot": None, "preservedNewRoot": None, "statePreserved": True, "workPreserved": True, "registeredBuild": None}
            restored = backups[-1]
            os.rename(restored, target)
            directory_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            registration = None
            if restored is not None and state_root is not None:
                _register_generation_with_controller(target, _safe_root(state_root))
                registration = {"build_id": load_build_manifest(target / "build-manifest.json").build_id}
            return {"rolledBack": restored is not None, "restoredRoot": str(target) if restored else None, "preservedNewRoot": str(preserved), "statePreserved": True, "workPreserved": True, "registeredBuild": registration["build_id"] if registration else None}
        except BaseException:
            if target.exists() and restored is not None:
                failed = target.parent / f"{target.name}.rollback-failed.{uuid.uuid4().hex}"
                os.rename(target, failed)
                os.rename(preserved, target)
            elif not target.exists():
                os.rename(preserved, target)
            if state_root is not None and target.exists():
                try:
                    _register_generation_with_controller(target, _safe_root(state_root))
                except BaseException:
                    pass
            raise
    finally:
        os.close(lock_fd)


__all__ = ["InstallError", "InstallUnavailable", "RELEASE_FILES", "activate", "ensure_fresh_state", "rollback", "stage", "verify_stage"]
