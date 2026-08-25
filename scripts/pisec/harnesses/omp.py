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
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from ..adapters import AdapterHealth, HarnessAdapter, HarnessArtifacts, HarnessManifest, RuntimeSurfaceArtifacts, StagedHarnessArtifacts, artifact_document
from ..fsutil import _atomic_write, _read_runtime_secret, _secure_secret, _secure_tree
from ..models import InvalidRequestError, NeedsAttentionError, PisecError, canonical_json, validate_id, validate_sha256

DOMAIN_RE = re.compile(r"^(?:\*\.)?[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
OMP_BASELINE_DOMAINS = ("html.duckduckgo.com",)
OPENCODE_ZEN_BASE_URL = "https://opencode.ai/zen/v1"
OMP_PROFILE_IDS = frozenset({"secretary-project", "first-mate", "worker-default"})


def _profile_role(profile: str) -> str:
    if profile == "secretary-project":
        return "secretary"
    if profile == "first-mate":
        return "first_mate"
    return "worker"
COPY_NAMES = ("skills", "rules", "commands", "themes", "agents")
COPY_FILES = ("AGENTS.md",)
SURFACE_NAMES = ("extensions", *COPY_NAMES)
USER_CONTEXT_MAX_FILE_BYTES = 256 * 1024
USER_CONTEXT_MAX_TOTAL_BYTES = 8 * 1024 * 1024
_CREDENTIAL_BASENAMES = frozenset({
    ".aws",
    ".gnupg",
    ".ssh",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "access_token",
    "api_key",
    "auth_token",
    "key",
    "keys",
    "private_key",
    "provider",
    "secret",
    "secrets",
    "token",
    "tokens",
})
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


def _normalize_owner_tree(root: Path, *, readonly: bool = False) -> None:
    for path in [root, *sorted(root.rglob("*"))]:
        if path.is_symlink():
            raise PisecError("isolated OMP surface contains a symlink")
        info = path.lstat()
        if info.st_uid != os.geteuid():
            raise PisecError("isolated OMP surface is not user-owned")
        if stat.S_ISDIR(info.st_mode):
            os.chmod(path, 0o500 if readonly else 0o700)
        elif stat.S_ISREG(info.st_mode):
            os.chmod(path, (0o400 if readonly else 0o600) | (0o100 if info.st_mode & stat.S_IXUSR else 0))
        else:
            raise PisecError("isolated OMP surface contains an unsupported file")


def _activate_directory(staged: Path, target: Path) -> None:
    backup = target.with_name(f".{target.name}.previous-{secrets.token_hex(8)}")
    replaced = False
    try:
        # A staged immutable tree must be made writable at its own root while
        # it is moved; the contents remain immutable after activation.
        if staged.is_dir() and not staged.is_symlink():
            os.chmod(staged, 0o700)
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
            if staged.is_dir() and not staged.is_symlink():
                _normalize_owner_tree(staged, readonly=False)
            shutil.rmtree(staged)
        if backup.exists():
            if backup.is_dir() and not backup.is_symlink():
                _normalize_owner_tree(backup, readonly=False)
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


def _user_context_name_allowed(name: str) -> bool:
    lowered = name.casefold()
    return lowered not in _CREDENTIAL_BASENAMES and not lowered.startswith(".env") and not any(
        marker in lowered for marker in ("credential", "private-key", "private_key", "access-token", "api-key")
    )


def _copy_user_context_entry(source: Path, target: Path, source_root: Path, manifest: list[dict[str, str]], total: list[int], active: set[Path], forbidden_values: Sequence[bytes] = ()) -> None:
    try:
        info = source.lstat()
    except OSError as error:
        raise PisecError("approved OMP user context is unavailable") from error
    if source.is_symlink():
        raise PisecError("approved OMP user context contains a symlink")
    if not _user_context_name_allowed(source.name):
        raise PisecError(f"approved OMP user context contains a credential-like basename: {source.name}")
    resolved = source.resolve(strict=False)
    if resolved != source or not resolved.is_relative_to(source_root):
        raise PisecError("approved OMP user context escapes its source root")
    if stat.S_ISDIR(info.st_mode):
        if resolved in active:
            raise PisecError("approved OMP user context contains a cycle")
        active.add(resolved)
        if target.exists() and (target.is_symlink() or not target.is_dir()):
            raise PisecError("approved OMP user context target is unsafe")
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        for child in sorted(source.iterdir(), key=lambda item: item.name):
            _copy_user_context_entry(child, target / child.name, source_root, manifest, total, active, forbidden_values)
        active.remove(resolved)
        return
    if not stat.S_ISREG(info.st_mode):
        raise PisecError("approved OMP user context contains a special file")
    if info.st_size > USER_CONTEXT_MAX_FILE_BYTES or total[0] + info.st_size > USER_CONTEXT_MAX_TOTAL_BYTES:
        raise PisecError("approved OMP user context is too large")
    content = source.read_bytes()
    if any(value and value in content for value in forbidden_values):
        raise PisecError(f"approved OMP user context contains a configured credential: {source}")
    if b"-----BEGIN " in content and b" PRIVATE KEY-----" in content:
        raise PisecError("approved OMP user context contains a private-key header")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PisecError("approved OMP user context is not valid UTF-8") from error
    _atomic_write(target, decoded, mode=0o600)
    total[0] += len(content)
    manifest.append({"path": str(source.relative_to(source_root)), "sha256": hashlib.sha256(content).hexdigest()})


def _copy_user_surface(destination: Path, *, forbidden_values: Sequence[bytes] = ()) -> list[dict[str, str]]:
    source = Path.home() / ".omp" / "agent"
    if source.exists() or source.is_symlink():
        if source.is_symlink() or not source.is_dir():
            raise PisecError("user OMP agent surface is unsafe")
    manifest: list[dict[str, str]] = []
    total = [0]
    for name in COPY_NAMES:
        origin = source / name
        target = destination / name
        if not origin.exists() and not origin.is_symlink():
            if target.exists() and not target.is_symlink():
                shutil.rmtree(target)
            continue
        if not origin.is_dir():
            raise PisecError(f"approved OMP user context directory is unsafe: {name}")
        if target.is_symlink():
            raise PisecError("isolated OMP surface contains an unsafe symlink")
        if target.exists() and not target.is_dir():
            raise PisecError("isolated OMP surface target is not a directory")
        if target.exists():
            shutil.rmtree(target)
        _copy_user_context_entry(origin, target, source, manifest, total, set(), forbidden_values)
    for name in COPY_FILES:
        origin = source / name
        target = destination / name
        if not origin.exists() and not origin.is_symlink():
            if target.exists() and not target.is_symlink():
                target.unlink()
            continue
        if origin.is_symlink() or not origin.is_file():
            raise PisecError("isolated OMP instruction target is unsafe")
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise PisecError("isolated OMP instruction target is unsafe")
        _copy_user_context_entry(origin, target, source, manifest, total, set(), forbidden_values)
    return manifest


def _configured_secret_values(gateway_token_file: Path) -> tuple[bytes, ...]:
    values: list[bytes] = []
    for name in (
        "OMP_AUTH_BROKER_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
    ):
        value = os.environ.get(name)
        if value:
            values.append(value.encode("utf-8"))
    for path in (Path.home() / ".omp" / "auth-broker.token", gateway_token_file):
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o022:
                value = path.read_bytes().strip()
                if value:
                    values.append(value)
        except OSError:
            continue
    return tuple(dict.fromkeys(value for value in values if len(value) >= 8))


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


def _copy_plugin_snapshot(destination: Path, source: Path | None = None) -> dict[str, str]:
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
        source = _plugin_source() if source is None else source
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


def _copy_surface(source: Path, destination: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise PisecError("runtime surface agent is unavailable")
    for name in SURFACE_NAMES:
        origin = source / name
        target = destination / name
        if not origin.exists():
            if target.exists() and not target.is_symlink():
                shutil.rmtree(target)
            continue
        staged = target.with_name(f".{target.name}.staged-{secrets.token_hex(8)}")
        shutil.copytree(origin, staged, symlinks=False)
        _normalize_owner_tree(staged)
        _activate_directory(staged, target)
    for name in COPY_FILES:
        origin = source / name
        target = destination / name
        if origin.exists():
            _copy_safe_entry(origin, target)
        elif target.exists() and not target.is_symlink():
            target.unlink()


def _copy_plugins(surface_root: Path, destination: Path) -> dict[str, str]:
    for name in ("data", "state", "cache", "config"):
        target = destination / "xdg" / name
        source = surface_root / "xdg" / name
        if target.exists() and not target.is_symlink():
            shutil.rmtree(target)
        _copy_safe_entry(source, target)
    return {
        "xdg_data_home": str(destination / "xdg" / "data"),
        "xdg_state_home": str(destination / "xdg" / "state"),
        "xdg_cache_home": str(destination / "xdg" / "cache"),
        "xdg_config_home": str(destination / "xdg" / "config"),
        "plugin_root": str(destination / "xdg" / "data" / "omp" / "plugins"),
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


def _safe_owned_tree(root: Path, *, readonly: bool = False) -> None:
    if not root.exists():
        return
    if root.is_symlink():
        raise NeedsAttentionError("OMP harness home is a symlink")
    info = root.lstat()
    allowed_modes = {0o700, 0o500} if readonly else {0o700}
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in allowed_modes:
        raise NeedsAttentionError("OMP harness home is unsafe")
    for child in sorted(root.rglob("*")):
        child_info = child.lstat()
        if child.is_symlink() or child_info.st_uid != os.geteuid() or child_info.st_mode & 0o022 or (readonly and child_info.st_mode & 0o200):
            raise NeedsAttentionError("OMP harness home contains an unsafe entry")


class OmpHarnessAdapter:
    manifest = HarnessManifest(adapter_id="omp", agent_kind="omp", version_label="17.3.4", interface_version=1, supported_role_profiles=(("worker", "worker-default"), ("secretary", "secretary-project"), ("first_mate", "first-mate")))

    def __init__(self, *, state_root: Path | str, config: Mapping[str, Any], policy_renderer: Any = None):
        self.state_root = Path(state_root)
        self.config = dict(config)
        harness = self.config.get("harness")
        if not isinstance(harness, Mapping) or harness.get("id") != self.manifest.adapter_id or not isinstance(harness.get("config"), Mapping):
            raise InvalidRequestError("OMP harness configuration is invalid")
        self.harness_config = _validate_harness_config(dict(harness["config"]))
        self.config["harness"] = {"id": self.manifest.adapter_id, "config": self.harness_config}
        self.policy_renderer = policy_renderer

    def validate_execution_profile(self, profile: str, role: str) -> None:
        if profile not in OMP_PROFILE_IDS:
            raise InvalidRequestError("unknown OMP execution profile")
        expected_role = _profile_role(profile)
        if role != expected_role:
            raise InvalidRequestError("OMP execution profile does not match the workstream role")

    def profile_domains(self, profile: str, additional_domains: Sequence[str]) -> tuple[str, ...]:
        self.validate_execution_profile(profile, _profile_role(profile))
        if profile in {"secretary-project", "first-mate"}:
            return ("*",)
        values = list(OMP_BASELINE_DOMAINS) + list(additional_domains)
        if any(not isinstance(item, str) or DOMAIN_RE.fullmatch(item) is None for item in values):
            raise InvalidRequestError("approved external domains contain an invalid name")
        if len(values) != len(set(values)):
            raise InvalidRequestError("approved external domains contain duplicates")
        return tuple(sorted(values))

    def prepare_runtime_surface(self) -> RuntimeSurfaceArtifacts:
        surfaces_root = self.state_root / "runtime-current"
        _secure_tree(self.state_root, surfaces_root)
        staged = surfaces_root / f".staged-{secrets.token_hex(8)}"
        staged.mkdir(mode=0o700)
        try:
            surface = staged / "agent"
            surface.mkdir(mode=0o700)
            user_context = _copy_user_surface(surface, forbidden_values=_configured_secret_values(Path(self.harness_config["gateway"]["tokenFile"])))
            extensions = surface / "extensions"
            agents = surface / "agents"
            extensions.mkdir(mode=0o700, exist_ok=True)
            agents.mkdir(mode=0o700, exist_ok=True)
            repository = _repo_root()
            _copy_safe_entry(repository / "omp" / "extensions" / "pisec.ts", extensions / "pisec.ts")
            _copy_safe_entry(repository / "omp" / "extensions" / "pisec-operation-catalogue.generated.ts", extensions / "pisec-operation-catalogue.generated.ts")
            managed_agent = repository / "omp" / "agents" / "pisec-web-research.md"
            if managed_agent.exists():
                _copy_safe_entry(managed_agent, agents / managed_agent.name)
            _copy_plugin_snapshot(staged)
            managed = staged / "managed"
            managed.mkdir(mode=0o700)
            _copy_safe_entry(repository / "pisec" / "fence", managed / "fence")
            _copy_safe_entry(repository / "pisec" / "runtime-bin" / "omp", managed / "omp")
            manifest = {
                "schemaVersion": 1,
                "adapter": self.manifest.adapter_id,
                "adapterVersion": self.manifest.version_label,
                "config": self.config,
                "userContext": user_context,
                "harnessExecutableSha256": _file_digest(Path(self.harness_config["executablePath"])),
                "fenceExecutableSha256": _file_digest(Path(str(self.config["fencePath"]))),
            }
            _atomic_write(staged / "surface.json", canonical_json(manifest, max_bytes=256 * 1024, max_text=64 * 1024) + "\n")
            _normalize_owner_tree(staged, readonly=True)
            digest = _tree_digest(staged)
            target = surfaces_root / self.manifest.adapter_id
            if target.exists():
                if target.is_symlink() or not target.is_dir():
                    raise PisecError("existing runtime surface is unsafe or corrupt")
            _activate_directory(staged, target)
            _normalize_owner_tree(target, readonly=True)
            return RuntimeSurfaceArtifacts(digest, manifest, str(target.absolute()))
        except Exception:
            if staged.exists():
                shutil.rmtree(staged)
            raise

    def current_runtime_surface(self) -> RuntimeSurfaceArtifacts:
        target = self.state_root / "runtime-current" / self.manifest.adapter_id
        try:
            _safe_owned_tree(target, readonly=True)
            manifest = json.loads((target / "surface.json").read_text(encoding="utf-8"))
            digest = _tree_digest(target)
        except (NeedsAttentionError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise NeedsAttentionError("current runtime surface is missing or corrupt; run pisec update") from error
        if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1 or manifest.get("adapter") != self.manifest.adapter_id or not target.is_dir():
            raise NeedsAttentionError("current runtime surface is missing or corrupt; run pisec update")
        return RuntimeSurfaceArtifacts(digest, manifest, str(target.absolute()))


    def _surface_root(self, scope: Mapping[str, Any]) -> Path:
        root_value = scope.get("runtimeSurfaceRoot")
        digest = scope.get("runtimeSurfaceSha256")
        if not isinstance(root_value, str):
            raise InvalidRequestError("runtime surface scope is incomplete")
        validate_sha256(digest, "runtime surface digest")
        root = Path(root_value).absolute()
        expected_parent = (self.state_root / "runtime-current").absolute()
        if root.parent != expected_parent or root.name != self.manifest.adapter_id or root.is_symlink() or not root.is_dir():
            raise NeedsAttentionError("runtime surface root is invalid")
        if _tree_digest(root) != digest:
            raise NeedsAttentionError("runtime surface contents do not match their digest")
        return root

    def desired_generation(self, scope: Mapping[str, Any], surface: RuntimeSurfaceArtifacts | None = None) -> str:
        if surface is None and not isinstance(scope.get("runtimeSurfaceRoot"), str):
            surface = self.current_runtime_surface()
        if surface is not None:
            scope = {**scope, "runtimeSurfaceSha256": surface.content_sha256, "runtimeSurfaceRoot": surface.root_path, "runtimeSurfaceId": "surface_" + surface.content_sha256[:32]}
        profile = scope.get("executionProfile")
        role = _profile_role(str(profile))
        self.validate_execution_profile(str(profile), role)
        self._surface_root(scope)
        scope_dict = {
            key: scope.get(key)
            for key in ("executionProfile", "worktreePath", "externalDomains", "implementationModel", "harnessModel", "reasoningEffort")
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
            "runtimeSurfaceSha256": scope.get("runtimeSurfaceSha256"),
        }
        return hashlib.sha256(canonical_json(manifest, max_bytes=256 * 1024, max_text=256 * 1024).encode("utf-8")).hexdigest()

    def _model_providers(self, profile: str, external_domains: Sequence[str] | None = None) -> dict[str, dict[str, str]]:
        del profile, external_domains
        gateway = self.harness_config["gateway"]
        token = Path(str(gateway["tokenFile"])).read_text(encoding="utf-8").strip()
        if not token:
            raise InvalidRequestError("OMP gateway token is empty")
        providers: dict[str, dict[str, str]] = {}
        for model in self.harness_config["modelRoles"].values():
            provider = str(model).split("/", 1)[0]
            providers[provider] = {
                "baseUrl": str(gateway["baseUrl"]),
                "transport": "pi-native",
                "apiKey": token,
            }
        return providers
    def _build_profile(
        self,
        scope: Mapping[str, Any],
        surface: RuntimeSurfaceArtifacts | None = None,
        *,
        state_root: Path | None = None,
        preserved_token: str | None = None,
    ) -> HarnessArtifacts:
        if surface is None and not isinstance(scope.get("runtimeSurfaceRoot"), str):
            surface = self.current_runtime_surface()
        if surface is not None:
            scope = {**scope, "runtimeSurfaceSha256": surface.content_sha256, "runtimeSurfaceRoot": surface.root_path, "runtimeSurfaceId": "surface_" + surface.content_sha256[:32]}
        workstream_id = validate_id(scope["workstreamId"], prefix="ws")
        state_root = Path(state_root or self.state_root)
        profile = scope.get("executionProfile")
        role = _profile_role(str(profile))
        self.validate_execution_profile(profile, role)
        surface_root = self._surface_root(scope)
        generation_sha256 = self.desired_generation(scope)
        state_binding = state_root / "binding-state" / "omp" / workstream_id
        tmp_dir = state_root / "tmp" / workstream_id
        surface_binding = state_root / "binding-surfaces" / "omp" / workstream_id
        prior_state_binding = self.state_root / "binding-state" / "omp" / workstream_id
        if state_root != self.state_root and prior_state_binding.exists():
            if prior_state_binding.is_symlink() or any(path.is_symlink() for path in prior_state_binding.rglob("*")):
                raise PisecError("existing OMP binding state contains a symlink")
            _secure_tree(state_root, state_binding.parent)
            _copy_safe_entry(prior_state_binding, state_binding)
            _normalize_owner_tree(state_binding)
        else:
            _secure_tree(state_root, state_binding)
        _secure_tree(state_binding, state_binding / "sessions")
        _secure_tree(state_binding, state_binding / "tmp")
        _secure_tree(state_binding, state_binding / "run")
        _secure_tree(state_binding, state_binding / "xdg" / "state")
        _secure_tree(state_binding, state_binding / "xdg" / "cache")
        _secure_tree(state_binding, state_binding / "xdg" / "config")
        _secure_tree(state_root, tmp_dir)
        _secure_tree(state_root, surface_binding)
        surface_agent = surface_binding / "agent"
        _secure_tree(surface_binding, surface_agent)
        _copy_surface(surface_root / "agent", surface_agent)
        secret_path = state_root / "secrets" / f"{workstream_id}.token"
        _secure_tree(state_root, secret_path.parent)
        if preserved_token is not None:
            token = preserved_token
            _atomic_write(secret_path, token + "\n")
        elif secret_path.exists() or secret_path.is_symlink():
            token = _read_runtime_secret(secret_path)
        else:
            token = secrets.token_urlsafe(48)
            _atomic_write(secret_path, token + "\n")
        roles = self.harness_config["modelRoles"]
        providers = self._model_providers(str(profile), scope.get("externalDomains") if isinstance(scope.get("externalDomains"), list) else [])
        _atomic_write(surface_agent / "models.yml", json.dumps({"providers": providers}, indent=2, sort_keys=True) + "\n")
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
        overlay_path = surface_agent / "config.yml"
        _atomic_write(overlay_path, json.dumps(overlay, indent=2, sort_keys=True) + "\n")
        renderer = self.policy_renderer
        if renderer is None:
            from ..fence import render_policy
            renderer = render_policy
        policy_path, policy_digest = renderer(
            surface_binding,
            scope,
            surface_agent,
            self.config,
            harness_home=state_binding,
            adapter_replacements={
                "HARNESS_EXECUTABLE": self.harness_config["executablePath"],
                "HARNESS_EXTENSION": surface_agent / "extensions" / "pisec.ts",
                "HARNESS_NATIVES": Path.home() / ".omp" / "natives",
                "HARNESS_RUN": state_binding / "run",
                "WORKSPACE_CONFIG": Path.home() / ".config" / "herdr",
                "TMP_ROOT": tmp_dir,
            },
            baseline_domains=OMP_BASELINE_DOMAINS,
            template_root=surface_root / "managed" / "fence",
        )
        extension_path = surface_agent / "extensions" / "pisec.ts"
        adapter_data = {
            "overlayPath": str(overlay_path),
            "xdgDataHome": str(surface_root / "xdg" / "data"),
            "xdgStateHome": str(state_binding / "xdg" / "state"),
            "xdgCacheHome": str(state_binding / "xdg" / "cache"),
            "xdgConfigHome": str(state_binding / "xdg" / "config"),
            "pluginRoot": str(surface_root / "xdg" / "data" / "omp" / "plugins"),
            "extensionPath": str(extension_path.absolute()),
            "agentRoot": str(surface_agent.absolute()),
            "surfaceRoot": str(surface_binding.absolute()),
            "launcherTemplate": str((surface_root / "managed" / "omp").absolute()),
            "runtimeSurfaceId": str(scope["runtimeSurfaceId"]),
            "tmpDir": str(tmp_dir),
        }
        _normalize_owner_tree(surface_binding, readonly=True)
        return HarnessArtifacts(
            harness_home=str(state_binding),
            launch_secret_path=str(secret_path),
            policy_path=str(policy_path),
            policy_sha256=policy_digest,
            runtime_token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            generation_sha256=generation_sha256,
            adapter_data=adapter_data,
        )

    def stage_profile(self, scope: Mapping[str, Any], surface: RuntimeSurfaceArtifacts, staging_root: Path) -> StagedHarnessArtifacts:
        if not isinstance(surface, RuntimeSurfaceArtifacts):
            raise InvalidRequestError("runtime surface snapshot is required")
        root = Path(staging_root).resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        candidate_root = root / "candidate-state"
        prior_secret = self.state_root / "secrets" / f"{validate_id(scope['workstreamId'], prefix='ws')}.token"
        preserved_token = _read_runtime_secret(prior_secret) if prior_secret.exists() or prior_secret.is_symlink() else None
        workstream_id = validate_id(scope["workstreamId"], prefix="ws")
        prior_home = self.state_root / "binding-state" / "omp" / workstream_id
        prior_policy = self.state_root / "binding-surfaces" / "omp" / workstream_id / "fence" / f"{workstream_id}.json"
        prior = None
        if prior_home.is_dir() and prior_secret.is_file() and prior_policy.is_file():
            prior = HarnessArtifacts(
                harness_home=str(prior_home),
                launch_secret_path=str(prior_secret),
                policy_path=str(prior_policy),
                policy_sha256=_file_digest(prior_policy),
                runtime_token_sha256=hashlib.sha256(preserved_token.encode("utf-8")).hexdigest() if preserved_token is not None else "0" * 64,
                generation_sha256=self.desired_generation(scope, surface),
                adapter_data={},
            )
        candidate = self._build_profile(
            {**scope, "runtimeSurfaceSha256": surface.content_sha256, "runtimeSurfaceRoot": surface.root_path, "runtimeSurfaceId": "surface_" + surface.content_sha256[:32]},
            surface,
            state_root=candidate_root,
            preserved_token=preserved_token,
        )
        return StagedHarnessArtifacts(
            operation_id=str(scope.get("operationId", "op_stage")),
            workstream_id=validate_id(scope["workstreamId"], prefix="ws"),
            staging_root=str(root),
            candidate_manifest_json=artifact_document(self.manifest, candidate),
            candidate_content_sha256=surface.content_sha256,
            candidate=candidate,
            prior=prior,
            compensation_json=canonical_json({"paths": [candidate.harness_home, candidate.adapter_data["surfaceRoot"], candidate.policy_path], "pointer": str(self.state_root / "binding-state" / "omp" / str(scope["workstreamId"]))}),
        )

    def activate_profile(self, scope: Mapping[str, Any], staged: StagedHarnessArtifacts) -> HarnessArtifacts:
        workstream_id = validate_id(scope["workstreamId"], prefix="ws")
        staging_root = Path(staged.staging_root).resolve(strict=False)
        candidate_root = staging_root / "candidate-state"
        candidate_paths = [
            staged.candidate.harness_home,
            staged.candidate.launch_secret_path,
            staged.candidate.policy_path,
            staged.candidate.adapter_data["surfaceRoot"],
            staged.candidate.adapter_data["agentRoot"],
            staged.candidate.adapter_data["overlayPath"],
            staged.candidate.adapter_data["extensionPath"],
            staged.candidate.adapter_data["xdgStateHome"],
            staged.candidate.adapter_data["xdgCacheHome"],
            staged.candidate.adapter_data["xdgConfigHome"],
            staged.candidate.adapter_data["tmpDir"],
        ]
        if not candidate_root.is_relative_to(staging_root) or any(not Path(value).resolve(strict=False).is_relative_to(staging_root) for value in candidate_paths):
            raise NeedsAttentionError("staged OMP profile escapes its operation root")
        active_root = self.state_root
        for relative in (
            Path("binding-state") / "omp" / workstream_id,
            Path("binding-surfaces") / "omp" / workstream_id,
            Path("tmp") / workstream_id,
            Path("secrets") / f"{workstream_id}.token",
        ):
            source = candidate_root / relative
            target = active_root / relative
            if not source.exists():
                continue
            _secure_tree(active_root, target.parent)
            if source.is_dir():
                _activate_directory(source, target)
            else:
                backup = target.with_name(f".{target.name}.previous-{secrets.token_hex(8)}")
                if target.exists():
                    os.replace(target, backup)
                os.replace(source, target)
                if backup.exists():
                    backup.unlink()
        prefix = str(candidate_root)
        def rebase(value: str) -> str:
            return value.replace(prefix, str(active_root), 1)
        candidate = staged.candidate
        active_surface = Path(rebase(candidate.adapter_data["surfaceRoot"]))
        _normalize_owner_tree(active_surface, readonly=False)
        active_policy = Path(rebase(candidate.policy_path))
        policy_text = active_policy.read_text(encoding="utf-8").replace(prefix, str(active_root))
        _atomic_write(active_policy, policy_text)
        policy_digest = hashlib.sha256(policy_text.encode("utf-8")).hexdigest()
        _normalize_owner_tree(active_surface, readonly=True)
        return HarnessArtifacts(
            harness_home=rebase(candidate.harness_home), launch_secret_path=rebase(candidate.launch_secret_path),
            policy_path=rebase(candidate.policy_path), policy_sha256=policy_digest,
            runtime_token_sha256=candidate.runtime_token_sha256, generation_sha256=candidate.generation_sha256,
            adapter_data={key: rebase(value) for key, value in candidate.adapter_data.items()},
        )

    def restore_profile(self, scope: Mapping[str, Any], previous: HarnessArtifacts) -> HarnessArtifacts:
        del scope
        return previous

    def discard_staged_profile(self, staged: StagedHarnessArtifacts) -> None:
        root = Path(staged.staging_root)
        if root.exists() and root.is_dir():
            shutil.rmtree(root)

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
        profile = scope.get("executionProfile")
        role = _profile_role(str(profile))
        self.validate_execution_profile(profile, role)
        if not isinstance(workspace_session_name, str) or not workspace_session_name or len(workspace_session_name) > 128 or "\x00" in workspace_session_name:
            raise InvalidRequestError("workspace session name is invalid")
        for value, name, limit in ((workspace_id, "workspace id", 256), (workspace_view_id, "workspace view id", 256), (workspace_surface_id, "workspace surface id", 256)):
            if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
                raise InvalidRequestError(f"{name} is invalid")
        canonical_root = str(Path(scope["worktreePath"]).resolve(strict=True))
        from ..platform import runtime_root

        runtime_root_path = runtime_root().absolute()
        control_db = self.state_root / "control.db"
        try:
            control_info = control_db.lstat()
        except OSError as error:
            raise NeedsAttentionError("Pisec control database is missing") from error
        if stat.S_ISLNK(control_info.st_mode) or not stat.S_ISREG(control_info.st_mode) or control_info.st_uid != os.geteuid() or stat.S_IMODE(control_info.st_mode) != 0o600:
            raise NeedsAttentionError("Pisec control database is unsafe")
        descriptor: dict[str, Any] = {
            "schemaVersion": 3,
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
            "tmpDir": str(Path(_artifact_value(artifacts, "tmpDir")).absolute()),
            "surfaceRoot": str(Path(_artifact_value(artifacts, "surfaceRoot")).absolute()),
            "agentRoot": str(Path(_artifact_value(artifacts, "agentRoot")).absolute()),
            "overlayPath": str(Path(_artifact_value(artifacts, "overlayPath")).absolute()),
            "extensionPath": str(Path(_artifact_value(artifacts, "extensionPath")).absolute()),
            "policyPath": str(Path(artifacts.policy_path).absolute()),
            "policySha256": artifacts.policy_sha256,
            "runtimeSurfaceId": _artifact_value(artifacts, "runtimeSurfaceId"),
            "generationSha256": artifacts.generation_sha256,
            "xdgDataHome": str(Path(_artifact_value(artifacts, "xdgDataHome")).absolute()),
            "xdgStateHome": str(Path(_artifact_value(artifacts, "xdgStateHome")).absolute()),
            "xdgCacheHome": str(Path(_artifact_value(artifacts, "xdgCacheHome")).absolute()),
            "xdgConfigHome": str(Path(_artifact_value(artifacts, "xdgConfigHome")).absolute()),
            "pluginRoot": str(Path(_artifact_value(artifacts, "pluginRoot")).absolute()),
            "runtimeSocketPath": str(runtime_root_path / "runtime" / "control.sock"),
            "secretarySocketPath": str(runtime_root_path / "secretary" / "control.sock") if role == "secretary" else None,
            "fleetSocketPath": str(runtime_root_path / "fleet" / "control.sock") if role == "first_mate" else None,
            "launchSecretPath": str(Path(artifacts.launch_secret_path).absolute()),
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
        template = Path(_artifact_value(artifacts, "launcherTemplate"))
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
            readonly = "binding-surfaces" in path.parts
            _safe_owned_tree(path, readonly=readonly)
            if readonly:
                _normalize_owner_tree(path, readonly=False)
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
                if key in {"surfaceRoot", "xdgStateHome", "xdgCacheHome", "xdgConfigHome", "tmpDir"} and isinstance(value, str):
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

        def owner_directory(path: Path, *, required: bool = True, readonly: bool = False) -> bool:
            try:
                info = path.lstat()
            except FileNotFoundError:
                return not required
            except OSError:
                return False
            modes = {0o700, 0o500} if readonly else {0o700}
            return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) in modes

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
                version_ok = result.returncode == 0 and re.search(r"(?<![0-9.])17\.3\.4(?![0-9.])", version) is not None
                version_detail = version or f"exit={result.returncode}"
            except (OSError, subprocess.SubprocessError) as error:
                version_detail = str(error)
        check("harness executable", executable_ok, str(executable))
        check("harness version", version_ok, version_detail)

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
                    and descriptor.get("schemaVersion") == 3
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
            and set(values) == {"overlayPath", "xdgDataHome", "xdgStateHome", "xdgCacheHome", "xdgConfigHome", "pluginRoot", "extensionPath", "agentRoot", "surfaceRoot", "launcherTemplate", "runtimeSurfaceId"}
                and all(isinstance(value, str) and value for value in values.values())
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            values = {}
            artifacts_ok = False
        harness_home = Path(str(binding.get("harness_home", "")))
        surface_root = Path(str(values.get("surfaceRoot", ""))) if artifacts_ok else Path("/")
        copied_ok = artifacts_ok and owner_directory(harness_home) and owner_directory(surface_root, readonly=True)
        plugin_ok = copied_ok
        overlay_ok = copied_ok
        if artifacts_ok:
            for name in ("xdgDataHome", "xdgStateHome", "xdgCacheHome", "xdgConfigHome", "pluginRoot"):
                path = Path(str(values[name]))
                expected_root = surface_root if name in {"xdgDataHome", "pluginRoot"} else harness_home
                copied_ok = copied_ok and (path.is_relative_to(expected_root) or path == expected_root) and owner_directory(path, readonly=name in {"xdgDataHome", "pluginRoot"})
            plugin_ok = copied_ok and owner_directory(Path(str(values["pluginRoot"])), readonly=True)
            overlay_path = Path(str(values["overlayPath"]))
            extension_path = Path(str(values["extensionPath"]))
            overlay_ok = copied_ok and owner_file(overlay_path, mode=None) and not overlay_path.stat().st_mode & 0o200 and owner_file(extension_path) and not extension_path.stat().st_mode & 0o200
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
        check("copied surface", copied_ok, str(surface_root))
        competing = [surface_root / "agent" / "extensions" / "herdr-omp-agent-state.ts", harness_home / "extensions" / "herdr-omp-agent-state.ts"]
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
