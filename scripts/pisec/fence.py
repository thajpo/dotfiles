"""Typed rendering of concrete Fence policies."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .fsutil import _atomic_write, _secure_tree
from .models import InvalidRequestError, NeedsAttentionError, canonical_json, validate_id
from .platform import is_macos, runtime_root

DOMAIN_RE = re.compile(r"^(?:\*\.)?[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
PROFILES = frozenset({"secretary-project", "first-mate", "worker-default"})


def _read_pyvenv_home(venv: Path) -> Path | None:
    cfg = venv / "pyvenv.cfg"
    if not cfg.is_file():
        return None
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError as error:
        raise NeedsAttentionError("python env venv config is unreadable") from error
    if len(text) > 4096:
        raise NeedsAttentionError("python env venv config is too large")
    for line in text.splitlines():
        match = _PYVENV_HOME_RE.match(line)
        if match is not None:
            value = match.group(1)
            if not value or len(value) > 4096 or "\x00" in value:
                raise NeedsAttentionError("python env venv config home is invalid")
            return Path(value)
    raise NeedsAttentionError("python env venv config lacks a home interpreter")


def _validate_python_env_path(python_env: str) -> Path:
    if not isinstance(python_env, str) or not python_env or len(python_env) > 4096 or "\x00" in python_env:
        raise InvalidRequestError("approved python env is invalid")
    env_path = Path(python_env)
    if not env_path.is_absolute():
        raise InvalidRequestError("approved python env must be absolute")
    resolved_env = env_path.resolve(strict=False)
    if resolved_env != env_path:
        raise NeedsAttentionError("approved python env is a symlink or resolves elsewhere")
    return resolved_env


def resolve_python_env_paths(python_env: str) -> list[str]:
    resolved_env = _validate_python_env_path(python_env)
    home = _read_pyvenv_home(resolved_env)
    if home is None:
        return [str(resolved_env)]
    if not home.is_absolute() or len(str(home)) > 4096:
        raise NeedsAttentionError("python env venv config home must be an absolute path")
    interpreter_root = home.parent if home.name == "bin" else home
    resolved_home = interpreter_root.resolve(strict=False)
    if resolved_home != interpreter_root:
        raise NeedsAttentionError("python env interpreter home is a symlink or resolves elsewhere")
    if not resolved_home.is_dir():
        raise NeedsAttentionError("python env interpreter home does not exist")
    return [str(resolved_env), str(resolved_home)]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _linked_git_dir(worktree: Path, common_git: Path) -> Path:
    marker = worktree / ".git"
    try:
        value = marker.read_text()
    except OSError as error:
        raise NeedsAttentionError("linked worktree Git marker is unavailable") from error
    if len(value) > 8192 or not value.startswith("gitdir: "):
        raise NeedsAttentionError("linked worktree Git marker is invalid")
    linked = Path(value[8:].strip())
    if not linked.is_absolute():
        linked = marker.parent / linked
    linked = linked.resolve(strict=True)
    worktree_metadata = (common_git / "worktrees").resolve(strict=True)
    if not linked.is_relative_to(worktree_metadata):
        raise NeedsAttentionError("linked worktree Git directory escapes the common repository")
    return linked


def resolve_data_dirs(data_dirs: Any, repository: Path, *, data_root: Path | None = None) -> list[str]:
    """Resolve and validate a project's approved read-only data directories.

    Each entry must be an absolute path contained within the project repository
    (or an explicit shared data root). The returned list holds canonical,
    resolved absolute paths used to render the Fence ``allowRead`` entries.
    """
    if data_dirs is None:
        data_dirs = []
    if not isinstance(data_dirs, list):
        raise InvalidRequestError("data dirs must be a list")
    if len(data_dirs) != len(set(data_dirs)):
        raise InvalidRequestError("data dirs contain duplicates")
    allowed_roots = [Path(repository).resolve(strict=False)]
    if data_root is not None:
        allowed_roots.append(Path(data_root).resolve(strict=False))
    resolved_list: list[str] = []
    for entry in data_dirs:
        if not isinstance(entry, str) or not entry or len(entry) > 4096 or "\x00" in entry:
            raise InvalidRequestError("data dir is invalid")
        path = Path(entry)
        if not path.is_absolute():
            raise InvalidRequestError("data dir must be absolute")
        resolved = path.resolve(strict=False)
        if not any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots):
            raise InvalidRequestError("data dir is outside the approved project data root")
        resolved_list.append(str(resolved))
    return resolved_list


def _substitute(value: Any, replacements: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        if value in replacements:
            return replacements[value]
        rendered = value
        for marker, replacement in replacements.items():
            if isinstance(replacement, str):
                rendered = rendered.replace(marker, replacement)
        if "${" in rendered:
            raise InvalidRequestError("Fence template contains an unresolved placeholder")
        return rendered
    if isinstance(value, list):
        result: list[Any] = []
        for item in value:
            substituted = _substitute(item, replacements)
            if isinstance(substituted, list):
                result.extend(substituted)
            else:
                result.append(substituted)
        return result
    if isinstance(value, dict):
        return {key: _substitute(item, replacements) for key, item in value.items()}
    return value
def _gateway_port(config: Mapping[str, Any]) -> int:
    harness = config.get("harness") if isinstance(config, Mapping) else None
    harness_config = harness.get("config") if isinstance(harness, Mapping) else None
    gateway = harness_config.get("gateway") if isinstance(harness_config, Mapping) else None
    gateway_url = gateway.get("baseUrl") if isinstance(gateway, Mapping) else None
    try:
        parsed_gateway = urlsplit(gateway_url) if isinstance(gateway_url, str) else None
        gateway_port = parsed_gateway.port if parsed_gateway is not None else None
    except ValueError as error:
        raise InvalidRequestError("Pisec gateway configuration has an invalid port") from error
    if parsed_gateway is None or parsed_gateway.scheme != "http" or parsed_gateway.hostname != "127.0.0.1" or gateway_port is None:
        raise InvalidRequestError("Pisec gateway configuration must use loopback HTTP")
    return gateway_port



def render_policy(
    state_root: Path,
    scope: Mapping[str, Any],
    agent_dir: Path,
    config: Mapping[str, Any],
    *,
    harness_home: Path,
    adapter_replacements: Mapping[str, Any] | None = None,
    baseline_domains: tuple[str, ...] = (),
    template_root: Path | None = None,
) -> tuple[Path, str]:
    workstream_id = validate_id(scope["workstreamId"], prefix="ws")
    profile = scope.get("executionProfile")
    if not isinstance(profile, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,127}", profile):
        raise InvalidRequestError("Fence execution profile is invalid")
    template_path = (template_root if template_root is not None else _repo_root() / "pisec" / "fence") / f"{profile}.jsonc"
    if not template_path.is_file():
        raise InvalidRequestError("Fence execution profile is unavailable")
    template = json.loads(template_path.read_text())
    gateway_port = _gateway_port(config)
    runtime_base = runtime_root()
    supplied = dict(adapter_replacements or {})
    replacements: dict[str, Any] = {
        "${HOME}": str(Path.home()),
        "${AGENT_DIR}": str(agent_dir.absolute()),
        "${RUNTIME_SOCKET_DIR}": str((runtime_base / "runtime").absolute()),
        "${SECRETARY_SOCKET_DIR}": str((runtime_base / "secretary").absolute()),
        "${HARNESS_EXECUTABLE}": str(Path(supplied.pop("HARNESS_EXECUTABLE", config["harness"]["config"]["executablePath"])).absolute()),
        "${HARNESS_SCRIPT}": str(Path(supplied.pop("HARNESS_SCRIPT", supplied.get("HARNESS_EXECUTABLE", config["harness"]["config"]["executablePath"]))).absolute()),
        "${HARNESS_HOME}": str(harness_home.absolute()),
        "${HARNESS_EXTENSION}": str(Path(supplied.pop("HARNESS_EXTENSION", harness_home / "extension.ts")).absolute()),
        "${HARNESS_NATIVES}": str(Path(supplied.pop("HARNESS_NATIVES", harness_home / "natives")).absolute()),
        "${HARNESS_RUN}": str(Path(supplied.pop("HARNESS_RUN", harness_home / "run")).absolute()),
        "${WORKSPACE_CONFIG}": str(Path(supplied.pop("WORKSPACE_CONFIG", harness_home / "workspace-config")).absolute()),
        "${GATEWAY_PORT}": [gateway_port],
        "${EXTERNAL_DOMAINS}": [],
        "${DENIED_COMMANDS}": [
            "omp",
            "omp-admin",
            "codex",
            "treehouse",
            "herdr",
            "pisec",
            "pisec-broker",
            "pisec-auth-broker",
            "pisec-auth-gateway",
            str(Path.home() / ".local" / "bin" / "omp"),
            str(Path.home() / ".local" / "lib" / "pisec" / "bin" / "omp"),
            "${HARNESS_EXECUTABLE}",
            "${HARNESS_SCRIPT}",
            str(Path.home() / ".local" / "lib" / "pisec" / "bin" / "omp-admin"),
            str(Path.home() / ".local" / "lib" / "pisec" / "bin" / "herdr"),
            str(Path.home() / ".local" / "lib" / "pisec" / "bin" / "pisec"),
            str(Path.home() / ".local" / "lib" / "pisec" / "bin" / "pisec-broker"),
            str(Path.home() / ".local" / "lib" / "pisec" / "bin" / "pisec-auth-broker"),
            str(Path.home() / ".local" / "lib" / "pisec" / "bin" / "pisec-auth-gateway"),
        ],
    }
    replacements.update(supplied)
    def _resolve_scope_dir(raw: Any, label: str) -> str:
        if not isinstance(raw, str) or not raw or len(raw) > 4096 or "\x00" in raw:
            raise InvalidRequestError(f"approved {label} is invalid")
        path = Path(raw)
        if not path.is_absolute():
            raise InvalidRequestError(f"approved {label} must be absolute")
        resolved = path.resolve(strict=False)
        if resolved != path:
            raise NeedsAttentionError(f"approved {label} is a symlink or resolves elsewhere")
        return str(resolved)
    if profile == "secretary-project":
        assigned = Path(scope["worktreePath"]).resolve(strict=True)
        replacements["${ASSIGNED_ROOT}"] = str(assigned)
    elif profile == "first-mate":
        assigned = Path(scope["worktreePath"]).resolve(strict=True)
        replacements["${ASSIGNED_ROOT}"] = str(assigned)
        def _resolve_scope_dirs(raw: Any, label: str) -> list[str]:
            if not isinstance(raw, list) or len(raw) > 4096:
                raise NeedsAttentionError(f"approved {label} are missing or invalid")
            result: list[str] = []
            for item in raw:
                path = _resolve_scope_dir(item, label)
                if path not in result:
                    result.append(path)
            return result

        replacements["${FLEET_WORKTREES}"] = _resolve_scope_dirs(scope.get("fleetProjectWorktrees"), "fleet project worktree dirs")
        replacements["${FLEET_GIT_OBJECTS}"] = _resolve_scope_dirs(scope.get("fleetProjectGitObjects"), "fleet project Git object dirs")
        replacements["${FLEET_SOCKET_DIR}"] = str((runtime_base / "fleet").absolute())
    else:
        worktree = Path(scope["worktreePath"]).resolve(strict=True)
        common_objects = Path(scope["gitCommonObjectDir"]).resolve(strict=True)
        common_git = common_objects.parent
        private_objects = Path(scope["privateGitObjectDir"]).resolve(strict=True)
        linked_git = _linked_git_dir(worktree, common_git)
        expected_branch = f"pisec/{workstream_id}/work"
        if scope.get("branchName") != expected_branch:
            raise NeedsAttentionError("workstream branch is outside its approved namespace")
        ref_namespace = common_git / "refs" / "heads" / "pisec" / workstream_id
        log_namespace = common_git / "logs" / "refs" / "heads" / "pisec" / workstream_id
        domains = scope.get("externalDomains")
        if not isinstance(domains, list) or any(not isinstance(domain, str) for domain in domains):
            raise InvalidRequestError("approved external domains are invalid")
        if domains != sorted(set(domains)):
            raise InvalidRequestError("approved external domains are not canonical")
        if any(DOMAIN_RE.fullmatch(domain) is None for domain in domains):
            raise InvalidRequestError("approved external domains contain an invalid name")
        if baseline_domains and not set(baseline_domains).issubset(domains):
            raise InvalidRequestError("approved external domains omit the web search capability")
        replacements.update({
            "${WORKTREE}": str(worktree),
            "${GIT_WORKTREE_DIR}": str(linked_git),
            "${COMMON_OBJECTS}": str(common_objects),
            "${PRIVATE_OBJECTS}": str(private_objects),
            "${REF_NAMESPACE}": str(ref_namespace),
            "${LOG_NAMESPACE}": str(log_namespace),
            "${EXTERNAL_DOMAINS}": domains,
        })
        if profile == "worker-default" and baseline_domains and domains != sorted(set(baseline_domains)):
            raise InvalidRequestError("default worker has unapproved additional external domains")
    data_dirs = scope.get("dataDirs")
    if data_dirs is None:
        data_dirs = []
    if not isinstance(data_dirs, list):
        raise InvalidRequestError("approved data dirs are invalid")
    rendered_data: list[str] = []
    for entry in data_dirs:
        if not isinstance(entry, str) or not entry or len(entry) > 4096 or "\x00" in entry:
            raise InvalidRequestError("approved data dir is invalid")
        path = Path(entry)
        if not path.is_absolute():
            raise InvalidRequestError("approved data dir must be absolute")
        resolved = path.resolve(strict=False)
        if resolved != path:
            raise NeedsAttentionError("approved data dir is a symlink or resolves elsewhere")
        rendered_data.append(str(resolved))
    replacements["${DATA_DIRS}"] = rendered_data
    python_env = scope.get("pythonEnv")
    rendered_env: list[str] = []
    if python_env is not None:
        rendered_env = resolve_python_env_paths(python_env)
    replacements["${PYTHON_ENV}"] = rendered_env

    def _resolve_project_store(key: str, label: str) -> list[str]:
        raw = scope.get(key)
        if raw is None:
            return []
        if not isinstance(raw, str) or not raw or len(raw) > 4096 or "\x00" in raw:
            raise InvalidRequestError(f"approved {label} is invalid")
        path = Path(raw)
        if not path.is_absolute():
            raise InvalidRequestError(f"approved {label} must be absolute")
        resolved = path.resolve(strict=False)
        if resolved != path:
            raise NeedsAttentionError(f"approved {label} is a symlink or resolves elsewhere")
        return [str(resolved)]

    replacements["${PROJECT_WORKTREES}"] = _resolve_project_store("projectWorktreesDir", "project worktrees dir")
    replacements["${PROJECT_GIT_OBJECTS}"] = _resolve_project_store("projectGitObjectsDir", "project git objects dir")
    policy = _substitute(template, replacements)
    if is_macos():
        policy.pop("devices", None)
        network_policy = policy.get("network")
        if isinstance(network_policy, dict):
            network_policy.pop("allowLocalOutboundPorts", None)
    text = canonical_json(policy, max_bytes=256 * 1024, max_text=8192) + "\n"
    output = Path(state_root) / "fence" / f"{workstream_id}.json"
    _secure_tree(Path(state_root), output.parent)
    _atomic_write(output, text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return output, digest
