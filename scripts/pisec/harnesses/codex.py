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

from ..adapters import AdapterHealth, HarnessArtifacts, HarnessManifest, RuntimeReleaseArtifacts, RuntimeSurfaceArtifacts
from ..fsutil import _atomic_write, _read_runtime_secret, _secure_secret, _secure_tree
from ..models import InvalidRequestError, NeedsAttentionError, PisecError, canonical_json, validate_id
from ..platform import runtime_root
from .omp import _copy_safe_entry, _file_digest, _normalize_owner_tree, _tree_digest


CODEX_PROFILE_IDS = frozenset({"worker-default", "worker-networked"})
CODEX_BASELINE_DOMAINS = ("html.duckduckgo.com",)


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


def _validate_config(config: Mapping[str, Any], root_config: Mapping[str, Any]) -> dict[str, Any]:
    harnesses = root_config.get("workerHarnesses")
    envelope = harnesses.get("codex") if isinstance(harnesses, Mapping) else None
    if not isinstance(envelope, Mapping) or envelope.get("id") != "codex" or not isinstance(envelope.get("config"), Mapping):
        raise InvalidRequestError("Codex worker harness configuration is missing")
    value = dict(envelope["config"])
    if set(value) != {"executablePath", "versionPrefix"}:
        raise InvalidRequestError("Codex worker harness configuration fields are invalid")
    version_prefix = value["versionPrefix"]
    if not isinstance(version_prefix, str) or not version_prefix or any(ord(char) < 0x20 for char in version_prefix):
        raise InvalidRequestError("Codex versionPrefix is invalid")
    gateway = config.get("harness", {}).get("config", {}).get("gateway") if isinstance(config.get("harness"), Mapping) else None
    if not isinstance(gateway, Mapping) or set(gateway) != {"baseUrl", "tokenFile"}:
        raise InvalidRequestError("Codex requires the configured loopback gateway")
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
            "Use the approved worktree only. Do not push, merge, publish, alter policy, or access sibling projects.",
            "Use the Pisec MCP tools for checkpoints, blockers, coordination, research, and ready_review.",
            "When implementation and verification are complete, commit the work and submit exactly one ready_review checkpoint with completion evidence.",
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
    manifest = HarnessManifest(adapter_id="codex", agent_kind="codex", version_label="0.147-compatible")
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

    def _release_root(self, scope: Mapping[str, Any]) -> Path:
        root_value = scope.get("runtimeReleaseRoot")
        digest = scope.get("runtimeReleaseSha256")
        if not isinstance(root_value, str) or not isinstance(digest, str) or len(digest) != 64:
            raise InvalidRequestError("Codex runtime release scope is incomplete")
        root = Path(root_value).absolute()
        expected_parent = (self.state_root / "runtime-releases").absolute()
        if root.parent != expected_parent or root.name != digest or root.is_symlink() or not root.is_dir() or _tree_digest(root) != digest:
            raise NeedsAttentionError("Codex runtime release root is invalid")
        return root

    def build_runtime_release(self) -> RuntimeReleaseArtifacts:
        executable_path, node_path = self._executables()
        releases_root = self.state_root / "runtime-releases"
        _secure_tree(self.state_root, releases_root)
        staged = releases_root / f".codex-staged-{secrets.token_hex(8)}"
        staged.mkdir(mode=0o700)
        try:
            managed = staged / "managed"
            managed.mkdir(mode=0o700)
            _copy_safe_entry(_repo_root() / "pisec" / "runtime-bin" / "codex", managed / "codex")
            _copy_safe_entry(_repo_root() / "scripts" / "pisec" / "codex_mcp.py", managed / "codex_mcp.py")
            _copy_safe_entry(_repo_root() / "scripts" / "pisec" / "codex_hook.py", managed / "codex_hook.py")
            fence = managed / "fence"
            _copy_safe_entry(_repo_root() / "pisec" / "fence", fence)
            manifest = {
                "schemaVersion": 1,
                "adapter": self.manifest.adapter_id,
                "adapterVersion": self.manifest.version_label,
                "config": {"harnesses": {"codex": self.harness_config}},
                "harnessExecutableSha256": _codex_file_digest(Path(executable_path)),
                "nodeExecutableSha256": _codex_file_digest(Path(node_path)),
                "fenceExecutableSha256": _file_digest(Path(str(self.root_config["fencePath"]))),
            }
            _atomic_write(staged / "release.json", canonical_json(manifest) + "\n")
            _normalize_owner_tree(staged)
            digest = _tree_digest(staged)
            target = releases_root / digest
            if target.exists():
                if target.is_symlink() or not target.is_dir() or _tree_digest(target) != digest:
                    raise PisecError("existing Codex runtime release is unsafe or corrupt")
                shutil.rmtree(staged)
            else:
                os.replace(staged, target)
            return RuntimeReleaseArtifacts(digest, manifest, str(target.absolute()))
        except Exception:
            if staged.exists():
                shutil.rmtree(staged)
            raise
    def prepare_runtime_surface(self) -> RuntimeSurfaceArtifacts:
        release = self.build_runtime_release()
        return RuntimeSurfaceArtifacts(release.content_sha256, release.manifest, str(release.root_path))

    def current_runtime_surface(self) -> RuntimeSurfaceArtifacts:
        return self.prepare_runtime_surface()

    def desired_generation(self, scope: Mapping[str, Any], surface: RuntimeSurfaceArtifacts | None = None) -> str:
        if surface is not None:
            scope = {**scope, "runtimeReleaseSha256": surface.content_sha256, "runtimeReleaseRoot": surface.root_path, "runtimeReleaseId": "surface_" + surface.content_sha256[:32]}
        profile = str(scope.get("executionProfile", ""))
        self.validate_execution_profile(profile, "worker")
        release_root = self._release_root(scope)
        values = {
            "adapter": self.manifest.adapter_id,
            "adapterVersion": self.manifest.version_label,
            "scope": {key: scope.get(key) for key in ("executionProfile", "worktreePath", "privateGitObjectDir", "gitCommonObjectDir", "externalDomains", "implementationModel", "harnessModel", "reasoningEffort", "pythonEnv")},
            "runtimeReleaseId": scope.get("runtimeReleaseId"),
            "runtimeReleaseSha256": scope.get("runtimeReleaseSha256"),
            "runtimeReleaseRoot": str(release_root),
        }
        return hashlib.sha256(canonical_json(values).encode()).hexdigest()

    def materialize_profile(self, scope: Mapping[str, Any], surface: RuntimeSurfaceArtifacts | None = None) -> HarnessArtifacts:
        if surface is not None:
            scope = {**scope, "runtimeReleaseSha256": surface.content_sha256, "runtimeReleaseRoot": surface.root_path, "runtimeReleaseId": "surface_" + surface.content_sha256[:32]}
        workstream_id = validate_id(scope["workstreamId"], prefix="ws")
        profile = str(scope.get("executionProfile"))
        self.validate_execution_profile(profile, "worker")
        release_root = self._release_root(scope)
        executable_path, node_path = self._executables()
        home = self.state_root / "codex" / workstream_id
        _secure_tree(self.state_root, home)
        _secure_tree(self.state_root, home / "sessions")
        codex_home = home / "home"
        _secure_tree(self.state_root, codex_home)
        prompt_path = home / "worker-prompt.md"
        _atomic_write(prompt_path, _prompt(scope), mode=0o600)
        runtime_secret = self.state_root / "secrets" / f"{workstream_id}.token"
        _secure_tree(self.state_root, runtime_secret.parent)
        if runtime_secret.exists() or runtime_secret.is_symlink():
            token = _read_runtime_secret(runtime_secret)
        else:
            token = secrets.token_urlsafe(48)
            _atomic_write(runtime_secret, token + "\n", mode=0o600)
        model = str(scope.get("harnessModel") or scope.get("implementationModel") or "gpt-5.6-luna")
        effort = str(scope.get("reasoningEffort") or "high")
        runtime_root = home / "runtime"
        _secure_tree(self.state_root, runtime_root)
        mcp_path = runtime_root / "codex_mcp.py"
        hook_path = runtime_root / "codex_hook.py"
        _copy_safe_entry(release_root / "managed" / "codex_mcp.py", mcp_path)
        _copy_safe_entry(release_root / "managed" / "codex_hook.py", hook_path)
        os.chmod(mcp_path, 0o700)
        os.chmod(hook_path, 0o700)
        _atomic_write(
            codex_home / "config.toml",
            "\n".join(
                (
                    f'model = {json.dumps(model)}',
                    f'model_reasoning_effort = {json.dumps(effort)}',
                    'approval_policy = "never"',
                    'sandbox_mode = "danger-full-access"',
                    "[features]",
                    "codex_hooks = true",
                    "[mcp_servers.pisec]",
                    f'command = {json.dumps(str(mcp_path))}',
                    "args = []",
                    "",
                )
            ),
            mode=0o600,
        )
        _atomic_write(
            codex_home / "hooks.json",
            json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": str(hook_path)}]}], "Stop": [{"hooks": [{"type": "command", "command": str(hook_path)}]}]}}, separators=(",", ":")) + "\n",
            mode=0o600,
        )
        policy_renderer = getattr(self, "policy_renderer", None)
        if policy_renderer is None:
            from ..fence import render_policy
            policy_renderer = render_policy
        policy_path, policy_digest = policy_renderer(
            self.state_root,
            scope,
            home,
            self.root_config,
            harness_home=home,
            adapter_replacements={
                "HARNESS_EXECUTABLE": node_path,
                "HARNESS_SCRIPT": executable_path,
                "HARNESS_EXTENSION": hook_path,
                "HARNESS_NATIVES": codex_home,
                "HARNESS_RUN": home / "run",
                "WORKSPACE_CONFIG": codex_home,
            },
            baseline_domains=CODEX_BASELINE_DOMAINS,
            template_root=release_root / "managed" / "fence",
        )
        return HarnessArtifacts(
            harness_home=str(home),
            launch_secret_path=str(runtime_secret),
            policy_path=str(policy_path),
            policy_sha256=policy_digest,
            runtime_token_sha256=hashlib.sha256(token.encode()).hexdigest(),
            generation_sha256=self.desired_generation(scope),
            adapter_data={
                "codexHome": str(codex_home),
                "configPath": str(codex_home / "config.toml"),
                "promptPath": str(prompt_path),
                "hooksPath": str(codex_home / "hooks.json"),
                "mcpPath": str(mcp_path),
                "hookPath": str(hook_path),
                "model": model,
                "reasoningEffort": effort,
                "gatewayTokenFile": self.harness_config["gateway"]["tokenFile"],
                "gatewayBaseUrl": self.harness_config["gateway"]["baseUrl"],
                "runtimeReleaseId": str(scope["runtimeReleaseId"]),
                "launcherTemplate": str((release_root / "managed" / "codex").absolute()),
            },
        )

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
            "runtimeReleaseId": values["runtimeReleaseId"],
            "generationSha256": artifacts.generation_sha256,
            "runtimeSocketPath": str((runtime_root() / "runtime" / "control.sock").absolute()),
            "launchSecretPath": str(Path(artifacts.launch_secret_path).absolute()),
            "privateGitObjectDir": str(Path(scope["privateGitObjectDir"]).absolute()),
            "gitCommonObjectDir": str(Path(scope["gitCommonObjectDir"]).absolute()),
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
        launcher_dir = self._launcher_dir(str(binding["workstream_id"]))
        if launcher_dir.exists():
            shutil.rmtree(launcher_dir)
        for raw in (binding.get("launch_secret_path"), binding.get("policy_path")):
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
                version_ok = result.returncode == 0 and self.harness_config["versionPrefix"] in detail
            except (OSError, subprocess.SubprocessError) as error:
                detail = str(error)
        elif "error_detail" in locals():
            detail = error_detail
        checks.append(AdapterHealth("Codex API range", version_ok, detail[:256]))
        checks.append(AdapterHealth("Codex gateway", True, self.harness_config["gateway"]["baseUrl"]))
        return checks
