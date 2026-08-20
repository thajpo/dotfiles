"""OMP harness adapter using only OMP's public configuration and launch surfaces."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from ..adapters import AdapterHealth, HarnessAdapter, HarnessArtifacts, HarnessManifest
from ..fsutil import _atomic_write, _read_runtime_secret, _secure_secret, _secure_tree
from ..models import InvalidRequestError, NeedsAttentionError, PisecError, canonical_json, validate_id

DOMAIN_RE = re.compile(r"^(?:\*\.)?[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
OMP_BASELINE_DOMAINS = ("html.duckduckgo.com",)
OMP_PROFILE_IDS = frozenset({"secretary-project", "worker-default", "worker-networked"})
COPY_NAMES = ("extensions", "skills", "rules", "commands", "themes", "agents")
COPY_FILES = ("AGENTS.md",)
PLUGIN_FILES = ("package.json", "bun.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "omp-plugins.lock.json")

def _expand_path(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise InvalidRequestError(f"{name} must be a path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InvalidRequestError(f"{name} must be absolute or home-relative")
    return str(path.absolute())


def _validate_gateway(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"baseUrl", "tokenFile"}:
        raise InvalidRequestError("OMP gateway configuration fields are invalid")
    base_url = value.get("baseUrl")
    try:
        parsed = urlsplit(base_url) if isinstance(base_url, str) else None
        port = parsed.port if parsed is not None else None
    except ValueError as error:
        raise InvalidRequestError("OMP gateway URL has an invalid port") from error
    if (
        parsed is None
        or parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is None
        or not 1 <= port <= 65535
    ):
        raise InvalidRequestError("OMP gateway configuration must use loopback HTTP")
    return {"baseUrl": base_url, "tokenFile": _expand_path(value["tokenFile"], "OMP gateway tokenFile")}


def _validate_roles(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not value or "smol" not in value:
        raise InvalidRequestError("OMP model roles are invalid or missing smol")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or any(ord(char) < 0x20 for char in key):
            raise InvalidRequestError("OMP model role names are invalid")
        if not isinstance(item, str) or "/" not in item or not item.split("/", 1)[0] or not item.split("/", 1)[1] or any(ord(char) < 0x20 for char in item):
            raise InvalidRequestError("OMP model role values are invalid")
        result[key] = item
    return result


def _validate_domains(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or DOMAIN_RE.fullmatch(item) is None for item in value):
        raise InvalidRequestError(f"{name} contains an invalid domain")
    if len(value) != len(set(value)):
        raise InvalidRequestError(f"{name} contains duplicate domains")
    return list(value)


def _validate_harness_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"executablePath", "gateway", "modelRoles", "network"}:
        raise InvalidRequestError("OMP harness configuration fields are invalid")
    network = value["network"]
    if not isinstance(network, dict) or set(network) != {"registryDomains", "developmentEndpoints"}:
        raise InvalidRequestError("OMP network configuration fields are invalid")
    registry = _validate_domains(network["registryDomains"], "OMP registryDomains")
    development = _validate_domains(network["developmentEndpoints"], "OMP developmentEndpoints")
    if set(registry) & set(development):
        raise InvalidRequestError("OMP network domains contain duplicates")
    return {
        "executablePath": _expand_path(value["executablePath"], "OMP executablePath"),
        "gateway": _validate_gateway(value["gateway"]),
        "modelRoles": _validate_roles(value["modelRoles"]),
        "network": {"registryDomains": registry, "developmentEndpoints": development},
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _normalize_owner_tree(root: Path) -> None:
    for path in [root, *sorted(root.rglob("*"))]:
        if path.is_symlink():
            raise PisecError("isolated OMP surface contains a symlink")
        info = path.lstat()
        if info.st_uid != os.geteuid():
            raise PisecError("isolated OMP surface is not user-owned")
        if stat.S_ISDIR(info.st_mode):
            os.chmod(path, 0o700)
        elif stat.S_ISREG(info.st_mode):
            os.chmod(path, 0o600 | (0o100 if info.st_mode & stat.S_IXUSR else 0))
        else:
            raise PisecError("isolated OMP surface contains an unsupported file")


def _activate_directory(staged: Path, target: Path) -> None:
    backup = target.with_name(f".{target.name}.previous-{secrets.token_hex(8)}")
    replaced = False
    try:
        if target.exists():
            os.replace(target, backup)
            replaced = True
        os.replace(staged, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        if replaced and not target.exists() and backup.exists():
            os.replace(backup, target)
        raise
    finally:
        if staged.exists():
            shutil.rmtree(staged)
        if backup.exists():
            shutil.rmtree(backup)


def _surface_source(origin: Path, kind: str) -> Path:
    try:
        info = origin.lstat()
    except OSError as error:
        raise PisecError("user OMP surface source is unavailable") from error
    if stat.S_ISLNK(info.st_mode):
        try:
            resolved = origin.resolve(strict=True)
        except OSError as error:
            raise PisecError("user OMP surface symlink target is unavailable") from error
    else:
        resolved = origin
    try:
        info = resolved.lstat()
    except OSError as error:
        raise PisecError("user OMP surface source is unavailable") from error
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise PisecError("user OMP surface source is not owner-controlled")
    if kind == "directory" and not stat.S_ISDIR(info.st_mode):
        raise PisecError("user OMP surface source is not a directory")
    if kind == "file" and not stat.S_ISREG(info.st_mode):
        raise PisecError("user OMP surface source is not a regular file")
    return resolved


def _copy_user_surface(destination: Path) -> None:
    source = Path.home() / ".omp" / "agent"
    if source.exists() or source.is_symlink():
        if source.is_symlink() or not source.is_dir():
            raise PisecError("user OMP agent surface is unsafe")
    for name in COPY_NAMES:
        origin = source / name
        target = destination / name
        if name == "skills":
            if not origin.exists() and not origin.is_symlink():
                if target.exists() and not target.is_symlink():
                    shutil.rmtree(target)
                continue
            origin = _surface_source(origin, "directory")
        else:
            if not origin.exists() or origin.is_symlink() or not origin.is_dir():
                if target.exists() and not target.is_symlink():
                    shutil.rmtree(target)
                continue
        if target.is_symlink():
            raise PisecError("isolated OMP surface contains an unsafe symlink")
        if target.exists() and not target.is_dir():
            raise PisecError("isolated OMP surface target is not a directory")
        ignore_patterns = shutil.ignore_patterns("*.db", "*.sqlite", "*.token", ".env", ".env.*", "sessions")

        def ignore_unsafe(directory: str, names: list[str]) -> list[str]:
            ignored = set(ignore_patterns(directory, names))
            if name == "skills":
                ignored.add("treehouse-worktrees")
            if name == "extensions":
                ignored.add("herdr-omp-agent-state.ts")
            ignored.update(item for item in names if (Path(directory) / item).is_symlink())
            return sorted(ignored)

        staged = target.with_name(f".{target.name}.staged-{secrets.token_hex(8)}")
        shutil.copytree(origin, staged, symlinks=False, ignore=ignore_unsafe)
        _normalize_owner_tree(staged)
        _activate_directory(staged, target)
    for name in COPY_FILES:
        origin = source / name
        target = destination / name
        if not origin.exists() and not origin.is_symlink():
            if target.exists() and not target.is_symlink():
                target.unlink()
            continue
        origin = _surface_source(origin, "file")
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise PisecError("isolated OMP instruction target is unsafe")
        _atomic_write(target, origin.read_text())


def _copy_user_config(destination: Path) -> Path | None:
    source = Path.home() / ".omp" / "agent" / "config.yml"
    target = destination / "user-config.yml"
    if not source.exists() and not source.is_symlink():
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                raise PisecError("isolated OMP user config target is unsafe")
            target.unlink()
        return None
    resolved = _surface_source(source, "file")
    _copy_safe_entry(resolved, target)
    return target


def _copy_safe_entry(source: Path, target: Path, active_dirs: set[Path] | None = None) -> None:
    active = set() if active_dirs is None else active_dirs
    try:
        info = source.lstat()
    except OSError as error:
        raise PisecError("plugin snapshot source is unavailable") from error
    resolved = source.resolve(strict=True) if stat.S_ISLNK(info.st_mode) else source
    info = resolved.lstat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o002:
        raise PisecError("plugin snapshot source is not owner-controlled")
    if stat.S_ISDIR(info.st_mode):
        if resolved in active:
            raise PisecError("plugin snapshot contains a cyclic link")
        active.add(resolved)
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise PisecError("plugin snapshot target is unsafe")
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target, 0o700)
        for child in sorted(resolved.iterdir(), key=lambda item: item.name):
            _copy_safe_entry(child, target / child.name, active)
        active.remove(resolved)
        return
    if not stat.S_ISREG(info.st_mode):
        raise PisecError("plugin snapshot contains a device or socket")
    if target.is_symlink():
        raise PisecError("plugin snapshot target is a symlink")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with resolved.open("rb") as source_stream, target.open("wb") as target_stream:
        shutil.copyfileobj(source_stream, target_stream)
    os.chmod(target, 0o600 | (0o100 if info.st_mode & stat.S_IXUSR else 0))


def _plugin_source() -> Path | None:
    configured = os.environ.get("PISEC_OMP_PLUGIN_ROOT")
    candidates = [Path(configured).expanduser() if configured else None, Path.home() / ".omp" / "plugins"]
    if os.environ.get("XDG_DATA_HOME"):
        candidates.append(Path(os.environ["XDG_DATA_HOME"]) / "omp" / "plugins")
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            if candidate.is_symlink() or not candidate.is_dir():
                raise PisecError("OMP plugin root is unsafe")
            return candidate
    return None


def _copy_plugin_snapshot(destination: Path) -> dict[str, str]:
    data_home = destination / "xdg" / "data"
    state_home = destination / "xdg" / "state"
    cache_home = destination / "xdg" / "cache"
    config_home = destination / "xdg" / "config"
    for root in (data_home, state_home, cache_home, config_home):
        _secure_tree(destination, root)
    plugin_target = data_home / "omp" / "plugins"
    if plugin_target.is_symlink():
        raise PisecError("isolated plugin snapshot target is a symlink")
    if plugin_target.exists() and not plugin_target.is_dir():
        raise PisecError("isolated plugin snapshot target is not a directory")
    plugin_parent = plugin_target.parent
    _secure_tree(data_home, plugin_parent)
    staged = plugin_parent / f".plugins.staged-{secrets.token_hex(8)}"
    staged.mkdir(mode=0o700)
    try:
        source = _plugin_source()
        if source is not None:
            for name in PLUGIN_FILES:
                origin = source / name
                if origin.exists():
                    _copy_safe_entry(origin, staged / name)
            node_modules = source / "node_modules"
            if node_modules.exists():
                _copy_safe_entry(node_modules, staged / "node_modules")
        _normalize_owner_tree(staged)
        _activate_directory(staged, plugin_target)
    except Exception:
        if staged.exists():
            shutil.rmtree(staged)
        raise
    return {
        "xdg_data_home": str(data_home),
        "xdg_state_home": str(state_home),
        "xdg_cache_home": str(cache_home),
        "xdg_config_home": str(config_home),
        "plugin_root": str(plugin_target),
    }


def _provider_ids(model_roles: Mapping[str, str]) -> list[str]:
    return sorted({value.split("/", 1)[0] for value in model_roles.values()})


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        digest.update(b"missing\0")
        return digest.hexdigest()
    for path in [root, *sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())]:
        relative = "." if path == root else path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise PisecError("runtime generation input contains a symlink")
        if stat.S_ISDIR(info.st_mode):
            digest.update(f"d\0{relative}\0".encode("utf-8"))
        elif stat.S_ISREG(info.st_mode):
            digest.update(f"f\0{relative}\0{stat.S_IMODE(info.st_mode) & 0o700:o}\0".encode("utf-8"))
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            digest.update(b"\0")
        else:
            raise PisecError("runtime generation input contains an unsupported file")
    return digest.hexdigest()


def _file_digest(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError as error:
        raise PisecError("runtime generation input is unavailable") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
        raise PisecError("runtime generation input is unsafe")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_value(artifacts: HarnessArtifacts, key: str) -> str:
    value = artifacts.adapter_data.get(key)
    if not isinstance(value, str) or not value:
        raise NeedsAttentionError("OMP launch artifact is missing")
    return value


def _safe_owned_tree(root: Path) -> None:
    if not root.exists():
        return
    if root.is_symlink():
        raise NeedsAttentionError("OMP harness home is a symlink")
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise NeedsAttentionError("OMP harness home is unsafe")
    for child in sorted(root.rglob("*")):
        child_info = child.lstat()
        if child.is_symlink() or child_info.st_uid != os.geteuid() or child_info.st_mode & 0o022:
            raise NeedsAttentionError("OMP harness home contains an unsafe entry")


class OmpHarnessAdapter:
    manifest = HarnessManifest(adapter_id="omp", agent_kind="omp", version_label="17.3.4-compatible")

    def __init__(self, *, state_root: Path | str, config: Mapping[str, Any], policy_renderer: Any = None):
        self.state_root = Path(state_root)
        self.config = dict(config)
        harness = self.config.get("harness")
        if not isinstance(harness, Mapping) or harness.get("id") != self.manifest.adapter_id or not isinstance(harness.get("config"), Mapping):
            raise InvalidRequestError("OMP harness configuration is invalid")
        self.harness_config = _validate_harness_config(dict(harness["config"]))
        self.config["harness"] = {"id": self.manifest.adapter_id, "config": self.harness_config}
        self.policy_renderer = policy_renderer
        self._surface_digest_cache: tuple[float, str] | None = None

    def validate_execution_profile(self, profile: str, role: str) -> None:
        if profile not in OMP_PROFILE_IDS:
            raise InvalidRequestError("unknown OMP execution profile")
        expected_role = "secretary" if profile == "secretary-project" else "worker"
        if role != expected_role:
            raise InvalidRequestError("OMP execution profile does not match the workstream role")

    def profile_domains(self, profile: str, additional_domains: Sequence[str]) -> tuple[str, ...]:
        self.validate_execution_profile(profile, "secretary" if profile == "secretary-project" else "worker")
        if profile == "secretary-project":
            return ("*",)
        values = list(OMP_BASELINE_DOMAINS) + list(additional_domains)
        if any(not isinstance(item, str) or DOMAIN_RE.fullmatch(item) is None for item in values):
            raise InvalidRequestError("approved external domains contain an invalid name")
        if len(values) != len(set(values)):
            raise InvalidRequestError("approved external domains contain duplicates")
        return tuple(sorted(values))

    def desired_generation(self, scope: Mapping[str, Any]) -> str:
        profile = scope.get("executionProfile")
        role = "secretary" if profile == "secretary-project" else "worker"
        self.validate_execution_profile(str(profile), role)
        repository = _repo_root()
        cached = self._surface_digest_cache
        if cached is not None and time.monotonic() - cached[0] < 5.0:
            copied_surface_digest = cached[1]
        else:
            with tempfile.TemporaryDirectory(prefix="pisec-generation-") as temporary:
                snapshot = Path(temporary)
                _copy_user_surface(snapshot)
                _copy_user_config(snapshot)
                discovery = snapshot / "agent"
                discovery.mkdir(mode=0o700)
                _copy_user_surface(discovery)
                _copy_plugin_snapshot(snapshot)
                managed_agent = repository / "omp" / "agents" / "pisec-web-research.md"
                if managed_agent.exists():
                    for agents_dir in (snapshot / "agents", discovery / "agents"):
                        agents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                        _copy_safe_entry(managed_agent, agents_dir / managed_agent.name)
                copied_surface_digest = _tree_digest(snapshot)
            self._surface_digest_cache = (time.monotonic(), copied_surface_digest)
        policy_sources = repository / "pisec" / "fence"
        scope_dict = {
            key: scope.get(key)
            for key in ("executionProfile", "worktreePath", "privateGitObjectDir", "gitCommonObjectDir", "externalDomains")
        }
        if scope.get("dataDirs"):
            scope_dict["dataDirs"] = scope["dataDirs"]
        if scope.get("pythonEnv"):
            scope_dict["pythonEnv"] = scope["pythonEnv"]
        manifest = {
            "schemaVersion": 1,
            "adapter": self.manifest.adapter_id,
            "adapterVersion": self.manifest.version_label,
            "scope": scope_dict,
            "config": self.config,
            "copiedSurfaceSha256": copied_surface_digest,
            "pisecExtensionSha256": _file_digest(repository / "omp" / "extensions" / "pisec.ts"),
            "runtimeLauncherSha256": _file_digest(repository / "pisec" / "runtime-bin" / "omp"),
            "fencePoliciesSha256": _tree_digest(policy_sources),
            "harnessExecutableSha256": _file_digest(Path(self.harness_config["executablePath"])),
            "fenceExecutableSha256": _file_digest(Path(str(self.config["fencePath"]))),
        }
        return hashlib.sha256(canonical_json(manifest, max_bytes=256 * 1024, max_text=64 * 1024).encode("utf-8")).hexdigest()

    def materialize_profile(self, scope: Mapping[str, Any]) -> HarnessArtifacts:
        workstream_id = validate_id(scope["workstreamId"], prefix="ws")
        profile = scope.get("executionProfile")
        role = "secretary" if profile == "secretary-project" else "worker"
        self.validate_execution_profile(profile, role)
        generation_sha256 = self.desired_generation(scope)
        agent_dir = self.state_root / "omp" / workstream_id
        _secure_tree(self.state_root, agent_dir)
        _secure_tree(agent_dir, agent_dir / "sessions")
        _copy_user_surface(agent_dir)
        _copy_user_config(agent_dir)
        discovery_agent_dir = agent_dir / "agent"
        _secure_tree(agent_dir, discovery_agent_dir)
        _copy_user_surface(discovery_agent_dir)
        plugin_info = _copy_plugin_snapshot(agent_dir)
        managed_agent = _repo_root() / "omp" / "agents" / "pisec-web-research.md"
        if managed_agent.exists():
            for agents_dir in (agent_dir / "agents", discovery_agent_dir / "agents"):
                _secure_tree(agent_dir if agents_dir.parent == agent_dir else discovery_agent_dir, agents_dir)
                _copy_safe_entry(managed_agent, agents_dir / managed_agent.name)
                _normalize_owner_tree(agents_dir)
        secret_path = self.state_root / "secrets" / f"{workstream_id}.token"
        _secure_tree(self.state_root, secret_path.parent)
        if secret_path.exists() or secret_path.is_symlink():
            token = _read_runtime_secret(secret_path)
        else:
            token = secrets.token_urlsafe(48)
            _atomic_write(secret_path, token + "\n")
        gateway = self.harness_config["gateway"]
        gateway_token = _secure_secret(Path(gateway["tokenFile"]))
        roles = self.harness_config["modelRoles"]
        providers = {provider: {"baseUrl": gateway["baseUrl"], "apiKey": gateway_token, "transport": "pi-native"} for provider in _provider_ids(roles)}
        _atomic_write(agent_dir / "models.yml", json.dumps({"providers": providers}, indent=2, sort_keys=True) + "\n")
        overlay: dict[str, Any] = {
            "setupVersion": 2,
            "modelRoles": roles,
            "web_search": {"enabled": True},
            "providers": {"webSearchOrder": ["duckduckgo"], "webSearchTimeoutSeconds": 30},
            "tools": {"approvalMode": "yolo"},
        }
        if role == "secretary":
            overlay["mcp"] = {"enableProjectConfig": True}
            overlay["tools"]["approval"] = {
                "task": "allow",
                "pisec_list_worker_research_requests": "allow",
                "pisec_claim_worker_research": "allow",
                "pisec_request_worker_research_context": "allow",
                "pisec_answer_worker_research": "allow",
                "pisec_decline_worker_research": "allow",
            }
        overlay_path = agent_dir / "config.yml"
        _atomic_write(overlay_path, json.dumps(overlay, indent=2, sort_keys=True) + "\n")
        renderer = self.policy_renderer
        if renderer is None:
            from ..fence import render_policy
            renderer = render_policy
        policy_path, policy_digest = renderer(
            self.state_root,
            scope,
            agent_dir,
            self.config,
            harness_home=agent_dir,
            adapter_replacements={
                "HARNESS_EXECUTABLE": self.harness_config["executablePath"],
                "HARNESS_EXTENSION": _repo_root() / "omp" / "extensions" / "pisec.ts",
                "HARNESS_NATIVES": Path.home() / ".omp" / "natives",
                "HARNESS_RUN": Path.home() / ".omp" / "run",
                "WORKSPACE_CONFIG": Path.home() / ".config" / "herdr",
            },
            baseline_domains=OMP_BASELINE_DOMAINS,
        )
        adapter_data = {
            "overlayPath": str(overlay_path),
            "xdgDataHome": plugin_info["xdg_data_home"],
            "xdgStateHome": plugin_info["xdg_state_home"],
            "xdgCacheHome": plugin_info["xdg_cache_home"],
            "xdgConfigHome": plugin_info["xdg_config_home"],
            "pluginRoot": plugin_info["plugin_root"],
            "extensionPath": str((_repo_root() / "omp" / "extensions" / "pisec.ts").absolute()),
        }
        return HarnessArtifacts(
            harness_home=str(agent_dir),
            launch_secret_path=str(secret_path),
            policy_path=str(policy_path),
            policy_sha256=policy_digest,
            runtime_token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            generation_sha256=generation_sha256,
            adapter_data=adapter_data,
        )

    def _launcher_dir(self, workstream_id: str) -> Path:
        workstream_id = validate_id(workstream_id, prefix="ws")
        path = self.state_root / "launchers" / workstream_id
        _secure_tree(self.state_root, path)
        return path

    def commit_launch_binding(
        self,
        scope: Mapping[str, Any],
        artifacts: HarnessArtifacts,
        *,
        workspace_session_name: str,
        workspace_id: str,
        workspace_view_id: str,
        workspace_surface_id: str,
        replace: bool = False,
    ) -> Path:
        workstream_id = validate_id(scope["workstreamId"], prefix="ws")
        project_id = validate_id(scope["projectId"], prefix="prj")
        role = "secretary" if scope.get("executionProfile") == "secretary-project" else "worker"
        profile = scope.get("executionProfile")
        self.validate_execution_profile(profile, role)
        if not isinstance(workspace_session_name, str) or not workspace_session_name or len(workspace_session_name) > 128 or "\x00" in workspace_session_name:
            raise InvalidRequestError("workspace session name is invalid")
        for value, name, limit in ((workspace_id, "workspace id", 256), (workspace_view_id, "workspace view id", 256), (workspace_surface_id, "workspace surface id", 256)):
            if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
                raise InvalidRequestError(f"{name} is invalid")
        canonical_root = str(Path(scope["worktreePath"]).resolve(strict=True))
        runtime_root = Path(os.environ.get("PISEC_RUNTIME_ROOT", Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "pisec")).absolute()
        control_db = self.state_root / "control.db"
        try:
            control_info = control_db.lstat()
        except OSError as error:
            raise NeedsAttentionError("Pisec control database is missing") from error
        if stat.S_ISLNK(control_info.st_mode) or not stat.S_ISREG(control_info.st_mode) or control_info.st_uid != os.geteuid() or stat.S_IMODE(control_info.st_mode) != 0o600:
            raise NeedsAttentionError("Pisec control database is unsafe")
        descriptor: dict[str, Any] = {
            "schemaVersion": 2,
            "harnessId": self.manifest.adapter_id,
            "stateRoot": str(self.state_root.absolute()),
            "controlDbPath": str(control_db.absolute()),
            "workstreamId": workstream_id,
            "projectId": project_id,
            "role": role,
            "executionProfile": profile,
            "canonicalRoot": canonical_root,
            "harnessExecutablePath": str(Path(self.harness_config["executablePath"]).absolute()),
            "fencePath": str(Path(self.config["fencePath"]).absolute()),
            "workspaceSessionName": workspace_session_name,
            "workspaceId": workspace_id,
            "workspaceViewId": workspace_view_id,
            "workspaceSurfaceId": workspace_surface_id,
            "harnessHome": str(Path(artifacts.harness_home).absolute()),
            "overlayPath": str(Path(_artifact_value(artifacts, "overlayPath")).absolute()),
            "extensionPath": str(Path(_artifact_value(artifacts, "extensionPath")).absolute()),
            "policyPath": str(Path(artifacts.policy_path).absolute()),
            "policySha256": artifacts.policy_sha256,
            "generationSha256": artifacts.generation_sha256,
            "xdgDataHome": str(Path(_artifact_value(artifacts, "xdgDataHome")).absolute()),
            "xdgStateHome": str(Path(_artifact_value(artifacts, "xdgStateHome")).absolute()),
            "xdgCacheHome": str(Path(_artifact_value(artifacts, "xdgCacheHome")).absolute()),
            "xdgConfigHome": str(Path(_artifact_value(artifacts, "xdgConfigHome")).absolute()),
            "pluginRoot": str(Path(_artifact_value(artifacts, "pluginRoot")).absolute()),
            "runtimeSocketPath": str(runtime_root / "runtime" / "control.sock"),
            "secretarySocketPath": str(runtime_root / "secretary" / "control.sock") if role == "secretary" else None,
            "launchSecretPath": str(Path(artifacts.launch_secret_path).absolute()),
            "privateGitObjectDir": None if scope.get("privateGitObjectDir") is None else str(Path(scope["privateGitObjectDir"]).absolute()),
            "gitCommonObjectDir": None if scope.get("gitCommonObjectDir") is None else str(Path(scope["gitCommonObjectDir"]).absolute()),
        }
        descriptor["identitySha256"] = hashlib.sha256(
            canonical_json(descriptor, max_bytes=64 * 1024, max_text=8 * 1024).encode("utf-8")
        ).hexdigest()
        launcher_dir = self._launcher_dir(workstream_id)
        descriptor_path = launcher_dir / "binding.json"
        launcher_path = launcher_dir / "omp"
        existing = descriptor_path.exists() or descriptor_path.is_symlink() or launcher_path.exists() or launcher_path.is_symlink()
        if existing:
            if not descriptor_path.is_file() or descriptor_path.is_symlink() or not launcher_path.is_file() or launcher_path.is_symlink():
                raise NeedsAttentionError("launch binding directory is incomplete")
            descriptor_info = descriptor_path.lstat()
            launcher_info = launcher_path.lstat()
            if descriptor_info.st_uid != os.geteuid() or stat.S_IMODE(descriptor_info.st_mode) != 0o600 or launcher_info.st_uid != os.geteuid() or stat.S_IMODE(launcher_info.st_mode) != 0o700:
                raise NeedsAttentionError("launch binding files are unsafe")
            try:
                current = json.loads(descriptor_path.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise NeedsAttentionError("launch binding descriptor is invalid") from error
            if current != descriptor and not replace:
                raise NeedsAttentionError("launch binding identity drifted")
            if current == descriptor and not replace:
                return launcher_path
        template = _repo_root() / "pisec" / "runtime-bin" / "omp"
        try:
            template_info = template.lstat()
        except OSError as error:
            raise NeedsAttentionError("binding launcher template is missing") from error
        if stat.S_ISLNK(template_info.st_mode) or not stat.S_ISREG(template_info.st_mode) or template_info.st_uid != os.geteuid() or not template_info.st_mode & stat.S_IXUSR or template_info.st_mode & 0o022:
            raise NeedsAttentionError("binding launcher template is unsafe")
        _atomic_write(descriptor_path, canonical_json(descriptor, max_bytes=64 * 1024, max_text=8 * 1024) + "\n")
        _atomic_write(launcher_path, template.read_text(), mode=0o700)
        return launcher_path
    def launch_binding_path(self, workstream_id: str) -> Path:
        launcher_path = self._launcher_dir(validate_id(workstream_id, prefix="ws")) / "omp"
        try:
            info = launcher_path.lstat()
        except OSError as error:
            raise NeedsAttentionError("binding launcher is missing") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise NeedsAttentionError("binding launcher is unsafe")
        return launcher_path


    def _remove_state_path(self, value: str) -> None:
        path = Path(value).absolute()
        root = self.state_root.absolute().resolve(strict=False)
        target = path.resolve(strict=False)
        if target == root or target == root / "control.db" or not target.is_relative_to(root):
            raise NeedsAttentionError("OMP cleanup path escapes the state root")
        if not path.exists() and not path.is_symlink():
            return
        info = path.lstat()
        if info.st_uid != os.geteuid() or stat.S_ISLNK(info.st_mode):
            raise NeedsAttentionError("OMP cleanup path is unsafe")
        if stat.S_ISDIR(info.st_mode):
            _safe_owned_tree(path)
            shutil.rmtree(path)
        elif stat.S_ISREG(info.st_mode) and not info.st_mode & 0o022:
            path.unlink()
        else:
            raise NeedsAttentionError("OMP cleanup path is unsupported")

    def _remove_runtime_artifacts(self, binding: Mapping[str, Any]) -> None:
        values: list[str] = [str(binding["launch_secret_path"]), str(binding["policy_path"])]
        try:
            document = json.loads(str(binding["adapter_artifacts_json"]))
        except (TypeError, json.JSONDecodeError) as error:
            raise NeedsAttentionError("OMP launch artifacts are invalid") from error
        artifact_values = document.get("values") if isinstance(document, Mapping) else None
        if isinstance(artifact_values, Mapping):
            for key, value in artifact_values.items():
                if key != "extensionPath" and isinstance(value, str):
                    values.append(value)
        for value in sorted(set(values), key=lambda item: (len(Path(item).parts), item), reverse=True):
            self._remove_state_path(value)

    def cleanup_binding(self, binding: Mapping[str, Any]) -> None:
        harness_home = Path(str(binding["harness_home"])).absolute()
        _safe_owned_tree(harness_home)
        sessions = harness_home / "sessions"
        if not sessions.is_dir() or sessions.is_symlink():
            raise NeedsAttentionError("OMP session root is missing or unsafe")
        _safe_owned_tree(sessions)
        for child in sorted(harness_home.iterdir(), key=lambda item: item.name):
            if child.name == "sessions":
                continue
            if child.is_symlink():
                raise NeedsAttentionError("OMP harness home contains an unsafe entry")
            if child.is_dir():
                _safe_owned_tree(child)
                shutil.rmtree(child)
            elif child.is_file() and child.lstat().st_uid == os.geteuid() and not child.lstat().st_mode & 0o022:
                child.unlink()
            else:
                raise NeedsAttentionError("OMP harness home contains an unsupported entry")
        self._remove_runtime_artifacts(binding)
        launcher_dir = self._launcher_dir(str(binding["workstream_id"]))
        _safe_owned_tree(launcher_dir)
        if launcher_dir.exists():
            shutil.rmtree(launcher_dir)

    def validate_native_session(self, binding: Mapping[str, Any], kind: str | None, value: str | None) -> None:
        if kind not in {None, "path", "id"} or (kind is None) != (value is None):
            raise InvalidRequestError("native session reference is invalid")
        if kind == "path":
            if not isinstance(value, str) or not Path(value).is_absolute() or len(value) > 4096 or "\x00" in value or not value.endswith(".jsonl"):
                raise InvalidRequestError("native session path is invalid")
            session_path = Path(value)
            session_root = Path(str(binding["harness_home"])) / "sessions"
            try:
                root_info = session_root.lstat()
                target_info = session_path.lstat()
                resolved_root = session_root.resolve(strict=True)
                resolved_target = session_path.resolve(strict=True)
            except OSError as error:
                raise NeedsAttentionError("native session path is unavailable") from error
            if (
                stat.S_ISLNK(root_info.st_mode)
                or not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != os.geteuid()
                or stat.S_IMODE(root_info.st_mode) != 0o700
            ):
                raise NeedsAttentionError("native session root is unsafe")
            if (
                stat.S_ISLNK(target_info.st_mode)
                or not stat.S_ISREG(target_info.st_mode)
                or target_info.st_uid != os.geteuid()
                or target_info.st_mode & 0o022
                or resolved_target != session_path.resolve(strict=False)
                or not resolved_target.is_relative_to(resolved_root)
            ):
                raise NeedsAttentionError("native session path is unsafe")
        if kind == "id" and (not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value):
            raise InvalidRequestError("native session id is invalid")

    def health_checks(self, binding: Mapping[str, Any], workstream: Mapping[str, Any]) -> Sequence[AdapterHealth]:
        checks: list[AdapterHealth] = []

        def check(name: str, ok: bool, detail: str) -> None:
            checks.append(AdapterHealth(name, ok, detail[:256]))

        def owner_directory(path: Path, *, required: bool = True) -> bool:
            try:
                info = path.lstat()
            except FileNotFoundError:
                return not required
            except OSError:
                return False
            return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700

        def owner_file(path: Path, *, executable: bool = False, mode: int | None = None) -> bool:
            try:
                info = path.lstat()
            except OSError:
                return False
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o002:
                return False
            if executable and not info.st_mode & stat.S_IXUSR:
                return False
            return mode is None or stat.S_IMODE(info.st_mode) == mode

        state_ok = owner_directory(self.state_root)
        check("harness state root", state_ok, str(self.state_root))

        executable = Path(self.harness_config["executablePath"])
        executable_ok = owner_file(executable, executable=True)
        version_ok = False
        version_detail = str(executable)
        if executable_ok:
            try:
                result = subprocess.run([str(executable), "--version"], text=True, capture_output=True, timeout=5, check=False)
                version = (result.stdout or result.stderr).strip()
                version_ok = result.returncode == 0 and "17.3" in version
                version_detail = version or f"exit={result.returncode}"
            except (OSError, subprocess.SubprocessError) as error:
                version_detail = str(error)
        check("harness executable", executable_ok, str(executable))
        check("harness API range", version_ok, version_detail)

        roles = self.harness_config.get("modelRoles")
        roles_ok = isinstance(roles, Mapping) and isinstance(roles.get("smol"), str) and "/" in roles["smol"]
        check("model roles", roles_ok, "smol" if roles_ok else "missing smol")
        source_surface = Path.home() / ".omp" / "agent"
        surface_ok = not source_surface.exists() or owner_directory(source_surface, required=False)
        check("user surface source", surface_ok, str(source_surface))
        check("web search baseline", "html.duckduckgo.com" in OMP_BASELINE_DOMAINS, ",".join(OMP_BASELINE_DOMAINS))

        launcher_root = self.state_root / "launchers"
        launcher_root_ok = owner_directory(launcher_root, required=False)
        check("binding launcher root", launcher_root_ok, str(launcher_root))

        if not binding:
            check("copied surface", True, "no durable harness bindings")
            check("plugin snapshot", True, "no durable harness bindings")
            check("overlay and MCP/search", True, "no durable harness bindings")
            check("policy digest", True, "no durable harness bindings")
            check("native session root", True, "no durable harness bindings")
            return checks
        workstream_id = str(binding.get("workstream_id", ""))
        descriptor_dir = launcher_root / workstream_id
        descriptor_path = descriptor_dir / "binding.json"
        launcher_path = descriptor_dir / "omp"
        descriptor_ok = (
            bool(workstream_id)
            and owner_directory(descriptor_dir)
            and owner_file(descriptor_path, mode=0o600)
            and owner_file(launcher_path, executable=True, mode=0o700)
        )
        if descriptor_ok:
            try:
                descriptor = json.loads(descriptor_path.read_text())
                identity_hash = descriptor.get("identitySha256")
                identity_payload = {key: value for key, value in descriptor.items() if key != "identitySha256"}
                descriptor_ok = (
                    isinstance(descriptor, Mapping)
                    and descriptor.get("schemaVersion") == 2
                    and descriptor.get("harnessId") == self.manifest.adapter_id
                    and descriptor.get("workstreamId") == workstream_id
                    and descriptor.get("workspaceSessionName") == binding.get("workspace_session_name")
                    and descriptor.get("workspaceId") == binding.get("workspace_id")
                    and descriptor.get("workspaceViewId") == binding.get("workspace_view_id")
                    and descriptor.get("workspaceSurfaceId") == binding.get("workspace_surface_id")
                    and descriptor.get("harnessHome") == str(Path(str(binding.get("harness_home", ""))).absolute())
                    and descriptor.get("generationSha256") == binding.get("desired_generation_sha256")
                    and isinstance(identity_hash, str)
                    and hashlib.sha256(canonical_json(identity_payload, max_bytes=64 * 1024, max_text=8 * 1024).encode("utf-8")).hexdigest() == identity_hash
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                descriptor_ok = False
        check("binding descriptor", descriptor_ok, str(descriptor_path))

        try:
            artifact_document_value = json.loads(str(binding["adapter_artifacts_json"]))
            values = artifact_document_value["values"]
            artifacts_ok = (
                isinstance(artifact_document_value, Mapping)
                and artifact_document_value.get("schemaVersion") == 2
                and artifact_document_value.get("adapterId") == self.manifest.adapter_id
                and artifact_document_value.get("generationSha256") == binding.get("desired_generation_sha256")
                and isinstance(values, Mapping)
                and set(values) == {"overlayPath", "xdgDataHome", "xdgStateHome", "xdgCacheHome", "xdgConfigHome", "pluginRoot", "extensionPath"}
                and all(isinstance(value, str) and value for value in values.values())
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            values = {}
            artifacts_ok = False
        harness_home = Path(str(binding.get("harness_home", "")))
        copied_ok = artifacts_ok and owner_directory(harness_home)
        plugin_ok = copied_ok
        overlay_ok = copied_ok
        if artifacts_ok:
            for name in ("xdgDataHome", "xdgStateHome", "xdgCacheHome", "xdgConfigHome", "pluginRoot"):
                path = Path(str(values[name]))
                copied_ok = copied_ok and path.is_relative_to(harness_home) and owner_directory(path)
            plugin_ok = copied_ok and owner_directory(Path(str(values["pluginRoot"])))
            overlay_path = Path(str(values["overlayPath"]))
            extension_path = Path(str(values["extensionPath"]))
            overlay_ok = copied_ok and owner_file(overlay_path, mode=0o600) and owner_file(extension_path)
            if overlay_ok:
                try:
                    overlay = json.loads(overlay_path.read_text())
                    search_config = overlay.get("web_search") if isinstance(overlay, Mapping) else None
                    provider_config = overlay.get("providers") if isinstance(overlay, Mapping) else None
                    overlay_ok = (
                        isinstance(search_config, Mapping)
                        and search_config.get("enabled") is True
                        and isinstance(provider_config, Mapping)
                        and "duckduckgo" in provider_config.get("webSearchOrder", [])
                    )
                    if workstream.get("workstream_execution_profile") == "secretary-project":
                        mcp_config = overlay.get("mcp") if isinstance(overlay, Mapping) else None
                        overlay_ok = overlay_ok and isinstance(mcp_config, Mapping) and mcp_config.get("enableProjectConfig") is True
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    overlay_ok = False
        check("copied surface", copied_ok, str(harness_home))
        competing = [harness_home / "extensions" / "herdr-omp-agent-state.ts", harness_home / "agent" / "extensions" / "herdr-omp-agent-state.ts"]
        check("single lifecycle reporter", copied_ok and not any(path.exists() or path.is_symlink() for path in competing), str(harness_home))
        check("plugin snapshot", plugin_ok, str(values.get("pluginRoot", "")))
        check("overlay and MCP/search", overlay_ok, str(values.get("overlayPath", "")))

        policy_path = Path(str(binding.get("policy_path", "")))
        policy_ok = owner_file(policy_path, mode=0o600)
        if policy_ok:
            try:
                policy_ok = hashlib.sha256(policy_path.read_bytes()).hexdigest() == binding.get("policy_sha256")
            except OSError:
                policy_ok = False
        check("policy digest", policy_ok, str(policy_path))
        session_root = harness_home / "sessions"
        check("native session root", owner_directory(session_root), str(session_root))
        return checks


def default_harness_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    harness = config.get("harness")
    if not isinstance(harness, Mapping) or not isinstance(harness.get("config"), Mapping):
        raise InvalidRequestError("harness configuration is invalid")
    return harness["config"]
