"""Codex CLI worker harness adapter.

Codex is deliberately limited to worker workstreams. OMP remains the
orchestrator because it owns the interactive secretary and First Mate tools.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from ..adapters import AdapterHealth, HarnessArtifacts, HarnessManifest, RuntimeSurfaceArtifacts, StagedHarnessArtifacts, artifact_document
from ..fsutil import _atomic_write, _read_runtime_secret, _secure_secret, _secure_tree
from ..models import InvalidRequestError, NeedsAttentionError, PisecError, canonical_json, validate_id, validate_sha256
from ..platform import runtime_root
from ..prompt_contract import IMMEDIATE_START_WORKER_CONTRACT, MEDIUM_DETAIL_REPORTING_CONTRACT
from .omp import _copy_safe_entry, _file_digest, _normalize_owner_tree, _tree_digest


CODEX_PROFILE_IDS = frozenset({"worker-default"})
CODEX_BASELINE_DOMAINS = ("html.duckduckgo.com",)
CODEX_MODEL_CATALOG_NAME = "codex_model_catalog.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _expand_path(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise InvalidRequestError(f"{name} must be a path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InvalidRequestError(f"{name} must be absolute or home-relative")
    return str(path.absolute())


def _expand_executable(value: Any, name: str) -> str:
    return _expand_path(value, name)


def _verify_executable(value: str, name: str) -> str:
    path = Path(value)
    try:
        resolved = path.resolve(strict=True)
        info = resolved.lstat()
    except OSError as error:
        raise InvalidRequestError(f"{name} is unavailable") from error
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o002 or (info.st_mode & 0o020 and info.st_gid != os.getgid()) or not info.st_mode & stat.S_IXUSR:
        raise InvalidRequestError(f"{name} is unsafe")
    return str(resolved)


def _codex_file_digest(path: Path) -> str:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o002 or (info.st_mode & 0o020 and info.st_gid != os.getgid()):
        raise PisecError("Codex runtime input is unsafe")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_owned_tree(root: Path, *, readonly: bool = False) -> None:
    if not root.exists() or root.is_symlink():
        raise NeedsAttentionError("current Codex runtime surface is missing or unsafe")
    info = root.lstat()
    modes = {0o700, 0o500} if readonly else {0o700}
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in modes:
        raise NeedsAttentionError("current Codex runtime surface is unsafe")
    for child in sorted(root.rglob("*")):
        child_info = child.lstat()
        if child.is_symlink() or child_info.st_uid != os.geteuid() or child_info.st_mode & 0o022 or (readonly and child_info.st_mode & 0o200):
            raise NeedsAttentionError("current Codex runtime surface contains an unsafe entry")


def _copy_durable_codex_state(source: Path, target: Path) -> None:
    """Copy resumable Codex state and omit only its volatile temp tree."""
    if source.is_symlink() or not source.is_dir():
        raise PisecError("existing Codex binding state is unsafe")
    volatile = source / "tmp"
    for path in source.rglob("*"):
        if path.is_symlink() and not path.is_relative_to(volatile):
            raise PisecError("existing Codex binding state contains a symlink")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        if child == volatile:
            continue
        _copy_safe_entry(child, target / child.name)


def _remove_owned_tree(path: Path) -> None:
    """Remove an owner-controlled temporary tree without following links."""
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        if info.st_uid != os.geteuid():
            raise NeedsAttentionError("Codex temporary backup is unsafe")
        path.unlink()
        return
    if info.st_uid != os.geteuid() or info.st_mode & 0o002 or (info.st_mode & 0o020 and info.st_gid != os.getgid()):
        raise NeedsAttentionError("Codex temporary backup is unsafe")
    if stat.S_ISDIR(info.st_mode):
        os.chmod(path, 0o700)
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            _remove_owned_tree(child)
        path.rmdir()
    elif stat.S_ISREG(info.st_mode):
        path.unlink()
    else:
        raise NeedsAttentionError("Codex temporary backup contains an unsupported file")


def _activate_directory(staged: Path, target: Path, retained_backup: Path | None = None) -> None:
    backup = retained_backup or target.with_name(f".{target.name}.previous-{secrets.token_hex(8)}")
    replaced = False
    try:
        if staged.is_dir() and not staged.is_symlink():
            os.chmod(staged, 0o700)
        if target.exists():
            if retained_backup is not None:
                if backup.exists() or backup.is_symlink():
                    raise NeedsAttentionError("Codex permission backup already exists")
                backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(backup.parent, 0o700)
            if target.is_dir() and not target.is_symlink():
                os.chmod(target, 0o700)
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
        if backup.exists() and retained_backup is None:
            _remove_owned_tree(backup)


def _permission_backup_root(value: Any) -> Path | None:
    if value is None:
        return None
    root = Path(str(value)).absolute()
    if root.is_symlink() or root.resolve(strict=False) != root:
        raise NeedsAttentionError("Codex permission backup root is unsafe")
    if root.exists():
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise NeedsAttentionError("Codex permission backup root is unsafe")
    else:
        root.mkdir(parents=True, mode=0o700)
        os.chmod(root, 0o700)
    return root


def _remove_owned_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    if info.st_uid != os.geteuid() or stat.S_ISLNK(info.st_mode):
        raise NeedsAttentionError("Codex permission restore path is unsafe")
    if stat.S_ISDIR(info.st_mode):
        _normalize_owner_tree(path, readonly=False)
        shutil.rmtree(path)
    elif stat.S_ISREG(info.st_mode):
        path.unlink()
    else:
        raise NeedsAttentionError("Codex permission restore path is unsupported")


def _restore_permission_backup(state_root: Path, scope: Mapping[str, Any]) -> None:
    backup_root = _permission_backup_root(scope.get("permissionBackupRoot"))
    if backup_root is None:
        raise NeedsAttentionError("Codex permission backup is unavailable")
    relative_paths = (
        Path("binding-state") / "codex" / str(scope["workstreamId"]),
        Path("binding-surfaces") / "codex" / str(scope["workstreamId"]),
        Path("tmp") / str(scope["workstreamId"]),
        Path("secrets") / f"{scope['workstreamId']}.token",
    )
    for relative in relative_paths:
        backup = backup_root / relative
        target = state_root / relative
        if not backup.exists() and not backup.is_symlink():
            continue
        if backup.is_symlink() or not backup.resolve(strict=False).is_relative_to(backup_root):
            raise NeedsAttentionError("Codex permission backup is unsafe")
        _remove_owned_path(target)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        os.replace(backup, target)


def _validate_config(config: Mapping[str, Any], root_config: Mapping[str, Any]) -> dict[str, Any]:
    harnesses = root_config.get("workerHarnesses")
    envelope = harnesses.get("codex") if isinstance(harnesses, Mapping) else None
    if not isinstance(envelope, Mapping) or envelope.get("id") != "codex" or not isinstance(envelope.get("config"), Mapping):
        raise InvalidRequestError("Codex worker harness configuration is missing")
    value = dict(envelope["config"])
    if set(value) != {"executablePath", "versionPrefix"}:
        raise InvalidRequestError("Codex worker harness configuration fields are invalid")
    version_prefix = value["versionPrefix"]
    if version_prefix != "0.150.0":
        raise InvalidRequestError("Codex version pin must be exactly 0.150.0")
    gateway = config.get("harness", {}).get("config", {}).get("gateway") if isinstance(config.get("harness"), Mapping) else None
    if not isinstance(gateway, Mapping) or set(gateway) != {"baseUrl", "tokenFile"}:
        raise InvalidRequestError("Codex requires the configured loopback gateway")
    try:
        parsed = urlsplit(gateway["baseUrl"]) if isinstance(gateway.get("baseUrl"), str) else None
        port = parsed.port if parsed is not None else None
    except ValueError as error:
        raise InvalidRequestError("Codex gateway URL has an invalid port") from error
    if parsed is None or parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or port is None or not 1 <= port <= 65535:
        raise InvalidRequestError("Codex gateway must use loopback HTTP")
    return {
        "executablePath": _expand_executable(value["executablePath"], "Codex executablePath"),
        "nodePath": _expand_executable(shutil.which("node"), "Node executable"),
        "versionPrefix": version_prefix,
        "gateway": {"baseUrl": str(gateway["baseUrl"]), "tokenFile": _expand_path(gateway["tokenFile"], "Codex gateway tokenFile")},
    }


def _prompt(scope: Mapping[str, Any]) -> str:
    packet = json.dumps(scope.get("taskPacket", {}), indent=2, sort_keys=True)
    model = str(scope.get("harnessModel", scope.get("implementationModel", "")))
    effort = str(scope.get("reasoningEffort", "high"))
    return "\n".join(
        (
            "Pisec Codex worker contract",
            "You are an isolated implementation worker. The broker-authenticated task packet below is authoritative.",
            IMMEDIATE_START_WORKER_CONTRACT,
            "Use the approved worktree only. Do not push, merge, publish, alter policy, or access sibling projects.",
            "Use the Pisec MCP tools for checkpoints, blockers, coordination, research, and ready_review.",
            "When implementation and verification are complete, commit the work and submit exactly one completion packet through pisec_submit_completion.",
            MEDIUM_DETAIL_REPORTING_CONTRACT,
            f"Resolved model: {model}",
            f"Reasoning effort: {effort}",
            "\nIMMUTABLE_TASK_PACKET",
            packet,
            "\nPYTHON_ENVIRONMENT",
            str(scope.get("pythonEnv") or "No approved Python environment."),
            "\nWORKER_BRIEF",
            str(scope.get("brief", "")),
        )
    ) + "\n"


class CodexHarnessAdapter:
    manifest = HarnessManifest(adapter_id="codex", agent_kind="codex", version_label="0.150.0", interface_version=1, supported_role_profiles=(("worker", "worker-default"),))
    launches_with_brief = True
    allow_unidentified_agent = True

    def __init__(self, *, state_root: Path | str, config: Mapping[str, Any]):
        self.state_root = Path(state_root)
        self.root_config = dict(config)
        self.harness_config = _validate_config(config, config)

    def _executables(self) -> tuple[str, str]:
        return (
            _verify_executable(self.harness_config["executablePath"], "Codex executablePath"),
            _verify_executable(self.harness_config["nodePath"], "Node executable"),
        )

    def validate_execution_profile(self, profile: str, role: str) -> None:
        if role != "worker" or profile not in CODEX_PROFILE_IDS:
            raise InvalidRequestError("Codex supports worker execution profiles only")

    def profile_domains(self, profile: str, additional_domains: Sequence[str]) -> tuple[str, ...]:
        self.validate_execution_profile(profile, "worker")
        values = list(CODEX_BASELINE_DOMAINS) + list(additional_domains)
        if len(values) != len(set(values)):
            raise InvalidRequestError("approved external domains contain duplicates")
        return tuple(sorted(values))

    def _surface_root(self, scope: Mapping[str, Any]) -> Path:
        root_value = scope.get("runtimeSurfaceRoot")
        digest = scope.get("runtimeSurfaceSha256")
        if not isinstance(root_value, str):
            raise InvalidRequestError("Codex runtime surface scope is incomplete")
        validate_sha256(digest, "runtime surface digest")
        root = Path(root_value).absolute()
        expected_parent = (self.state_root / "runtime-current").absolute()
        if root.parent != expected_parent or root.name != self.manifest.adapter_id or root.is_symlink() or not root.is_dir() or _tree_digest(root) != digest:
            raise NeedsAttentionError("Codex runtime surface root is invalid")
        return root

    def prepare_runtime_surface(self) -> RuntimeSurfaceArtifacts:
        executable_path, node_path = self._executables()
        surfaces_root = self.state_root / "runtime-current"
        _secure_tree(self.state_root, surfaces_root)
        staged = surfaces_root / f".codex-staged-{secrets.token_hex(8)}"
        staged.mkdir(mode=0o700)
        try:
            managed = staged / "managed"
            managed.mkdir(mode=0o700)
            _copy_safe_entry(_repo_root() / "pisec" / "runtime-bin" / "codex", managed / "codex")
            _copy_safe_entry(_repo_root() / "scripts" / "pisec" / "codex_mcp.py", managed / "codex_mcp.py")
            _copy_safe_entry(_repo_root() / "scripts" / "pisec" / CODEX_MODEL_CATALOG_NAME, managed / CODEX_MODEL_CATALOG_NAME)
            _copy_safe_entry(_repo_root() / "scripts" / "pisec" / "operation_catalogue_generated.py", managed / "operation_catalogue_generated.py")
            _copy_safe_entry(_repo_root() / "scripts" / "pisec" / "codex_hook.py", managed / "codex_hook.py")
            fence = managed / "fence"
            _copy_safe_entry(_repo_root() / "pisec" / "fence", fence)
            manifest = {
                "schemaVersion": 1,
                "adapter": self.manifest.adapter_id,
                "adapterVersion": self.manifest.version_label,
                "interfaceVersion": 1,
                "config": {"harnesses": {"codex": self.harness_config}},
                "harnessExecutableSha256": _codex_file_digest(Path(executable_path)),
                "nodeExecutableSha256": _codex_file_digest(Path(node_path)),
                "fenceExecutableSha256": _file_digest(Path(str(self.root_config["fencePath"]))),
            }
            _atomic_write(staged / "surface.json", canonical_json(manifest) + "\n")
            _normalize_owner_tree(staged, readonly=True)
            digest = _tree_digest(staged)
            target = surfaces_root / self.manifest.adapter_id
            if target.exists():
                if target.is_symlink() or not target.is_dir():
                    raise PisecError("existing Codex runtime surface is unsafe or corrupt")
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


    def desired_generation(self, scope: Mapping[str, Any], surface: RuntimeSurfaceArtifacts | None = None) -> str:
        if surface is None and not isinstance(scope.get("runtimeSurfaceRoot"), str):
            surface = self.current_runtime_surface()
        if surface is not None:
            scope = {**scope, "runtimeSurfaceSha256": surface.content_sha256, "runtimeSurfaceRoot": surface.root_path, "runtimeSurfaceId": "surface_" + surface.content_sha256[:32]}
        profile = str(scope.get("executionProfile", ""))
        self.validate_execution_profile(profile, "worker")
        self._surface_root(scope)
        values = {
            "adapter": self.manifest.adapter_id,
            "adapterVersion": self.manifest.version_label,
            "scope": {key: scope.get(key) for key in ("executionProfile", "worktreePath", "externalDomains", "implementationModel", "harnessModel", "reasoningEffort", "pythonEnv")},
            "runtimeSurfaceSha256": scope.get("runtimeSurfaceSha256"),
        }
        return hashlib.sha256(canonical_json(values).encode()).hexdigest()


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
        profile = str(scope.get("executionProfile"))
        self.validate_execution_profile(profile, "worker")
        surface_root = self._surface_root(scope)
        executable_path, node_path = self._executables()
        home = state_root / "binding-state" / "codex" / workstream_id
        tmp_dir = state_root / "tmp" / workstream_id
        surface_binding = state_root / "binding-surfaces" / "codex" / workstream_id
        prior_home = self.state_root / "binding-state" / "codex" / workstream_id
        if state_root != self.state_root and prior_home.exists():
            _secure_tree(state_root, home.parent)
            _copy_durable_codex_state(prior_home, home)
            _normalize_owner_tree(home)
        else:
            _secure_tree(state_root, home)
        _secure_tree(home, home / "sessions")
        _secure_tree(home, home / "run")
        _secure_tree(state_root, tmp_dir)
        _secure_tree(state_root, surface_binding)
        _copy_safe_entry(surface_root / "managed", surface_binding / "managed")
        config_root = surface_binding / "home"
        _secure_tree(surface_binding, config_root)
        prompt_path = surface_binding / "worker-prompt.md"
        _atomic_write(prompt_path, _prompt(scope), mode=0o600)
        runtime_secret = state_root / "secrets" / f"{workstream_id}.token"
        _secure_tree(state_root, runtime_secret.parent)
        if preserved_token is not None:
            token = preserved_token
            _atomic_write(runtime_secret, token + "\n", mode=0o600)
        elif runtime_secret.exists() or runtime_secret.is_symlink():
            token = _read_runtime_secret(runtime_secret)
        else:
            token = secrets.token_urlsafe(48)
            _atomic_write(runtime_secret, token + "\n", mode=0o600)
        model = str(scope.get("harnessModel") or scope.get("implementationModel") or "gpt-5.6-luna")
        if "/" not in model:
            model = f"openai-codex/{model}"
        effort = str(scope.get("reasoningEffort") or "high")
        runtime_root = surface_binding / "runtime"
        _secure_tree(surface_binding, runtime_root)
        model_catalog_path = surface_binding / "managed" / CODEX_MODEL_CATALOG_NAME
        if model_catalog_path.is_symlink() or not model_catalog_path.is_file():
            raise NeedsAttentionError("Codex model catalog is missing from the immutable runtime surface")
        mcp_path = runtime_root / "codex_mcp.py"
        hook_path = runtime_root / "codex_hook.py"
        _copy_safe_entry(surface_root / "managed" / "codex_mcp.py", mcp_path)
        _copy_safe_entry(surface_root / "managed" / "operation_catalogue_generated.py", runtime_root / "operation_catalogue_generated.py")
        _copy_safe_entry(surface_root / "managed" / "codex_hook.py", hook_path)
        os.chmod(mcp_path, 0o700)
        os.chmod(hook_path, 0o700)
        _atomic_write(
            config_root / "config.toml",
            "\n".join(
                (
                    f'model = {json.dumps(model)}',
                    f'model_reasoning_effort = {json.dumps(effort)}',
                    'model_provider = "openai-codex"',
                    'model_providers.openai-codex.name = "openai-codex"',
                    f'model_providers.openai-codex.base_url = {json.dumps(str(self.harness_config["gateway"]["baseUrl"]).rstrip("/") + "/v1")}',
                    'model_providers.openai-codex.wire_api = "responses"',
                    'model_providers.openai-codex.env_key = "OPENAI_API_KEY"',
                    'model_providers.openai-codex.requires_openai_auth = false',
                    f'openai_base_url = {json.dumps(str(self.harness_config["gateway"]["baseUrl"]).rstrip("/") + "/v1")}',
                    'approval_policy = "never"',
                    'sandbox_mode = "danger-full-access"',
                    f'model_catalog_json = {json.dumps(str(model_catalog_path))}',
                    "[features]",
                    "hooks = true",
                    "[mcp_servers.pisec]",
                    f'command = {json.dumps(str(mcp_path))}',
                    "args = []",
                    "",
                )
            ),
            mode=0o600,
        )
        _atomic_write(
            config_root / "hooks.json",
            json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": str(hook_path)}]}], "Stop": [{"hooks": [{"type": "command", "command": str(hook_path)}]}]}}, separators=(",", ":")) + "\n",
            mode=0o600,
        )
        policy_renderer = getattr(self, "policy_renderer", None)
        if policy_renderer is None:
            from ..fence import render_policy
            policy_renderer = render_policy
        codex_executable = Path(executable_path)
        codex_package_root = codex_executable.parent.parent
        if not codex_package_root.is_dir() or codex_package_root.is_symlink():
            raise NeedsAttentionError("Codex package support directory is unavailable")
        policy_path, policy_digest = policy_renderer(
            surface_binding,
            scope,
            surface_binding,
            self.root_config,
            harness_home=home,
            adapter_replacements={
                "HARNESS_EXECUTABLE": node_path,
                # The Node entry point resolves the platform package at runtime;
                # Fence must expose that package directory for the initial launch.
                "HARNESS_SCRIPT": codex_package_root,
                "HARNESS_EXTENSION": hook_path,
                "HARNESS_NATIVES": home,
                "HARNESS_RUN": home / "run",
                "TMP_ROOT": tmp_dir,
                "WORKSPACE_CONFIG": Path.home() / ".config" / "herdr",
                # The generic worker policy denies a bare `codex` command.  The
                # Codex launcher itself must be able to invoke the pinned Node
                # entry point and spawn its native child; those exact approved
                # argv entries are therefore omitted from command deny while
                # the worker still has no unrelated command on PATH.
                "${DENIED_COMMANDS}": [
                    "omp",
                    "omp-admin",
                    "treehouse",
                    "herdr",
                    "pisec",
                    "pisec-broker",
                    "pisec-auth-broker",
                    "pisec-auth-gateway",
                    str(Path.home() / ".local" / "bin" / "omp"),
                    str(Path.home() / ".local" / "lib" / "pisec" / "bin" / "omp"),
                    str(Path.home() / ".local" / "lib" / "pisec" / "bin" / "omp-admin"),
                    str(Path.home() / ".local" / "lib" / "pisec" / "bin" / "herdr"),
                    str(Path.home() / ".local" / "lib" / "pisec" / "bin" / "pisec"),
                    str(Path.home() / ".local" / "lib" / "pisec" / "bin" / "pisec-broker"),
                    str(Path.home() / ".local" / "lib" / "pisec" / "bin" / "pisec-auth-broker"),
                    str(Path.home() / ".local" / "lib" / "pisec" / "bin" / "pisec-auth-gateway"),
                ],
            },
            baseline_domains=CODEX_BASELINE_DOMAINS,
            template_root=surface_root / "managed" / "fence",
        )
        _normalize_owner_tree(surface_binding, readonly=True)
        return HarnessArtifacts(
            harness_home=str(home),
            launch_secret_path=str(runtime_secret),
            policy_path=str(policy_path),
            policy_sha256=policy_digest,
            runtime_token_sha256=hashlib.sha256(token.encode()).hexdigest(),
            generation_sha256=self.desired_generation(scope),
            adapter_data={
                "codexHome": str(home),
                "agentRoot": str(surface_binding),
                "surfaceRoot": str(surface_binding),
                "configPath": str(config_root / "config.toml"),
                "promptPath": str(prompt_path),
                "hooksPath": str(config_root / "hooks.json"),
                "mcpPath": str(mcp_path),
                "hookPath": str(hook_path),
                "model": model,
                "reasoningEffort": effort,
                "gatewayTokenFile": self.harness_config["gateway"]["tokenFile"],
                "gatewayBaseUrl": self.harness_config["gateway"]["baseUrl"],
                "runtimeSurfaceId": str(scope["runtimeSurfaceId"]),
                "launcherTemplate": str((surface_root / "managed" / "codex").absolute()),
                "tmpDir": str(tmp_dir),
            },
        )

    def stage_profile(self, scope: Mapping[str, Any], surface: RuntimeSurfaceArtifacts, staging_root: Path) -> StagedHarnessArtifacts:
        if not isinstance(surface, RuntimeSurfaceArtifacts):
            raise InvalidRequestError("runtime surface snapshot is required")
        root = Path(staging_root).resolve()
        if root.exists() or root.is_symlink():
            info = root.lstat()
            if root.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise NeedsAttentionError("Codex staging root is unsafe")
            _normalize_owner_tree(root)
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        candidate_root = root / "candidate-state"
        prior_secret = self.state_root / "secrets" / f"{validate_id(scope['workstreamId'], prefix='ws')}.token"
        preserved_token = _read_runtime_secret(prior_secret) if prior_secret.exists() or prior_secret.is_symlink() else None
        workstream_id = validate_id(scope["workstreamId"], prefix="ws")
        prior_home = self.state_root / "binding-state" / "codex" / workstream_id
        prior_policy = self.state_root / "binding-surfaces" / "codex" / workstream_id / "fence" / f"{workstream_id}.json"
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
            compensation_json=canonical_json({"paths": [candidate.harness_home, candidate.adapter_data["surfaceRoot"], candidate.policy_path], "pointer": str(self.state_root / "binding-state" / "codex" / str(scope["workstreamId"]))}),
        )

    def activate_profile(self, scope: Mapping[str, Any], staged: StagedHarnessArtifacts) -> HarnessArtifacts:
        workstream_id = validate_id(scope["workstreamId"], prefix="ws")
        staging_root = Path(staged.staging_root).resolve(strict=False)
        candidate_root = staging_root / "candidate-state"
        backup_root = _permission_backup_root(scope.get("permissionBackupRoot"))
        candidate_paths = [
            staged.candidate.harness_home,
            staged.candidate.launch_secret_path,
            staged.candidate.policy_path,
            staged.candidate.adapter_data["surfaceRoot"],
            staged.candidate.adapter_data["agentRoot"],
            staged.candidate.adapter_data["codexHome"],
            staged.candidate.adapter_data["configPath"],
            staged.candidate.adapter_data["promptPath"],
            staged.candidate.adapter_data["hooksPath"],
            staged.candidate.adapter_data["mcpPath"],
            staged.candidate.adapter_data["hookPath"],
            staged.candidate.adapter_data["tmpDir"],
        ]
        if not candidate_root.is_relative_to(staging_root) or any(not Path(value).resolve(strict=False).is_relative_to(staging_root) for value in candidate_paths):
            raise NeedsAttentionError("staged Codex profile escapes its operation root")
        active_root = self.state_root
        for relative in (
            Path("binding-state") / "codex" / workstream_id,
            Path("binding-surfaces") / "codex" / workstream_id,
            Path("tmp") / workstream_id,
            Path("secrets") / f"{workstream_id}.token",
        ):
            source = candidate_root / relative
            target = active_root / relative
            if not source.exists():
                continue
            _secure_tree(active_root, target.parent)
            if source.is_dir():
                retained = backup_root / relative if backup_root is not None else None
                _activate_directory(source, target, retained)
            else:
                backup = backup_root / relative if backup_root is not None else target.with_name(f".{target.name}.previous-{secrets.token_hex(8)}")
                if target.exists():
                    if backup_root is not None:
                        if backup.exists() or backup.is_symlink():
                            raise NeedsAttentionError("Codex permission backup already exists")
                        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        os.chmod(backup.parent, 0o700)
                    os.replace(target, backup)
                os.replace(source, target)
                if backup_root is None and backup.exists():
                    backup.unlink()
        prefix = str(candidate_root)
        def rebase(value: str) -> str:
            return value.replace(prefix, str(active_root), 1)
        candidate = staged.candidate
        active_surface = Path(rebase(candidate.adapter_data["surfaceRoot"]))
        _normalize_owner_tree(active_surface, readonly=False)
        for key in ("configPath", "hooksPath"):
            generated = Path(rebase(candidate.adapter_data[key]))
            content = generated.read_text(encoding="utf-8")
            if prefix in content:
                _atomic_write(
                    generated,
                    content.replace(prefix, str(active_root)),
                    mode=stat.S_IMODE(generated.lstat().st_mode),
                )
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
        _restore_permission_backup(self.state_root, scope)
        return previous

    def discard_staged_profile(self, staged: StagedHarnessArtifacts) -> None:
        root = Path(staged.staging_root)
        if root.exists() or root.is_symlink():
            info = root.lstat()
            if root.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise NeedsAttentionError("Codex staging root is unsafe")
            _normalize_owner_tree(root)
            shutil.rmtree(root)

    def _launcher_dir(self, workstream_id: str) -> Path:
        path = self.state_root / "launchers" / validate_id(workstream_id, prefix="ws")
        _secure_tree(self.state_root, path)
        return path

    def commit_launch_binding(self, scope: Mapping[str, Any], artifacts: HarnessArtifacts, *, workspace_session_name: str, workspace_id: str, workspace_view_id: str, workspace_surface_id: str, replace: bool = False) -> Path:
        workstream_id = validate_id(scope["workstreamId"], prefix="ws")
        project_id = validate_id(scope["projectId"], prefix="prj")
        executable_path, node_path = self._executables()
        values = dict(artifacts.adapter_data)
        descriptor: dict[str, Any] = {
            "schemaVersion": 1,
            "harnessId": self.manifest.adapter_id,
            "stateRoot": str(self.state_root.absolute()),
            "controlDbPath": str((self.state_root / "control.db").absolute()),
            "workstreamId": workstream_id,
            "projectId": project_id,
            "role": "worker",
            "executionProfile": scope["executionProfile"],
            "canonicalRoot": str(Path(scope["worktreePath"]).resolve(strict=True)),
            "harnessExecutablePath": str(Path(executable_path).absolute()),
            "nodeExecutablePath": str(Path(node_path).absolute()),
            "fencePath": str(Path(self.root_config["fencePath"]).absolute()),
            "workspaceSessionName": workspace_session_name,
            "workspaceId": workspace_id,
            "workspaceViewId": workspace_view_id,
            "workspaceSurfaceId": workspace_surface_id,
            "harnessHome": str(Path(artifacts.harness_home).absolute()),
            "tmpDir": str(Path(values["tmpDir"]).absolute()),
            "surfaceRoot": str(Path(values["surfaceRoot"]).absolute()),
            "codexHome": str(Path(values["codexHome"]).absolute()),
            "configPath": str(Path(values["configPath"]).absolute()),
            "promptPath": str(Path(values["promptPath"]).absolute()),
            "hooksPath": str(Path(values["hooksPath"]).absolute()),
            "mcpPath": str(Path(values["mcpPath"]).absolute()),
            "hookPath": str(Path(values["hookPath"]).absolute()),
            "model": values["model"],
            "reasoningEffort": values["reasoningEffort"],
            "gatewayTokenFile": values["gatewayTokenFile"],
            "gatewayBaseUrl": values["gatewayBaseUrl"],
            "policyPath": str(Path(artifacts.policy_path).absolute()),
            "policySha256": artifacts.policy_sha256,
            "runtimeSurfaceId": values["runtimeSurfaceId"],
            "generationSha256": artifacts.generation_sha256,
            "runtimeSocketPath": str((runtime_root() / "runtime" / "control.sock").absolute()),
            "launchSecretPath": str(Path(artifacts.launch_secret_path).absolute()),
        }
        descriptor["identitySha256"] = hashlib.sha256(canonical_json(descriptor).encode()).hexdigest()
        launcher_dir = self._launcher_dir(workstream_id)
        descriptor_path = launcher_dir / "binding.json"
        launcher_path = launcher_dir / "codex"
        if descriptor_path.exists() or launcher_path.exists():
            if not descriptor_path.is_file() or not launcher_path.is_file():
                raise NeedsAttentionError("Codex launch binding is incomplete")
            current = json.loads(descriptor_path.read_text())
            if current != descriptor and not replace:
                raise NeedsAttentionError("Codex launch binding identity drifted")
            if current == descriptor and not replace:
                return launcher_path
        template = Path(values["launcherTemplate"])
        if not template.is_file() or template.is_symlink():
            raise NeedsAttentionError("Codex launcher template is missing")
        _atomic_write(descriptor_path, canonical_json(descriptor) + "\n", mode=0o600)
        _atomic_write(launcher_path, template.read_text(), mode=0o700)
        return launcher_path

    def launch_binding_path(self, workstream_id: str) -> Path:
        path = self._launcher_dir(workstream_id) / "codex"
        if not path.is_file() or path.is_symlink():
            raise NeedsAttentionError("Codex binding launcher is missing")
        return path

    def cleanup_binding(self, binding: Mapping[str, Any]) -> None:
        home = Path(str(binding["harness_home"])).absolute()
        root = self.state_root.absolute().resolve(strict=False)
        if not home.is_relative_to(root) or home == root:
            raise NeedsAttentionError("Codex cleanup path escapes the state root")
        if home.exists():
            shutil.rmtree(home)
        surface_value = binding.get("adapter_artifacts_json")
        if isinstance(surface_value, str):
            try:
                values = json.loads(surface_value).get("values", {})
            except (TypeError, ValueError, json.JSONDecodeError):
                values = {}
            surface = Path(str(values.get("surfaceRoot", ""))).absolute() if isinstance(values, Mapping) and values.get("surfaceRoot") else None
            if surface is not None and surface.is_relative_to(root) and surface.exists():
                _normalize_owner_tree(surface, readonly=False)
                shutil.rmtree(surface)
        launcher_dir = self._launcher_dir(str(binding["workstream_id"]))
        if launcher_dir.exists():
            shutil.rmtree(launcher_dir)
        values = dict(values) if isinstance(values, Mapping) else {}
        for raw in (binding.get("launch_secret_path"), binding.get("policy_path"), values.get("tmpDir")):
            if isinstance(raw, str):
                path = Path(raw).absolute()
                if path.is_relative_to(root) and path.exists():
                    path.unlink()

    def validate_native_session(self, binding: Mapping[str, Any], kind: str | None, value: str | None) -> None:
        if kind is None and value is None:
            return
        if kind != "id" or not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
            raise InvalidRequestError("Codex native session id is invalid")

    def health_checks(self, binding: Mapping[str, Any], workstream: Mapping[str, Any]) -> Sequence[AdapterHealth]:
        del binding, workstream
        checks: list[AdapterHealth] = []
        try:
            executable_path, node_path = self._executables()
            executable = Path(executable_path)
            node = Path(node_path)
            executable_ok = True
        except InvalidRequestError as error:
            executable = Path(self.harness_config["executablePath"])
            node = Path(self.harness_config["nodePath"])
            executable_ok = False
            error_detail = str(error)
        checks.append(AdapterHealth("Codex executable", executable_ok, str(executable)))
        version_ok = False
        detail = "unavailable"
        if executable_ok:
            try:
                result = subprocess.run([str(node), str(executable), "--version"], capture_output=True, text=True, timeout=5, check=False)
                detail = (result.stdout or result.stderr).strip()
                version_ok = result.returncode == 0 and detail.strip() in {"0.150.0", "codex 0.150.0"}
            except (OSError, subprocess.SubprocessError) as error:
                detail = str(error)
        elif "error_detail" in locals():
            detail = error_detail
        checks.append(AdapterHealth("Codex version", version_ok, detail[:256]))
        checks.append(AdapterHealth("Codex gateway", True, self.harness_config["gateway"]["baseUrl"]))
        return checks
