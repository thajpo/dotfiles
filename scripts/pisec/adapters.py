"""Product-neutral Pisec adapter contracts and explicit registry."""

from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import json
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence

from .models import InvalidRequestError, NeedsAttentionError, canonical_json, validate_sha256

ADAPTER_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
def validate_adapter_id(value: Any) -> str:
    if not isinstance(value, str) or ADAPTER_ID_RE.fullmatch(value) is None:
        raise InvalidRequestError("adapter id is invalid")
    return value


@dataclass(frozen=True)
class HarnessManifest:
    adapter_id: str
    agent_kind: str
    version_label: str
    interface_version: int = 1
    supported_role_profiles: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        validate_adapter_id(self.adapter_id)
        if not self.agent_kind or not self.version_label:
            raise ValueError("harness manifest fields must not be empty")
        if self.interface_version != 1:
            raise InvalidRequestError("Pisec harness interface version must be 1")
        if any(not isinstance(role, str) or not isinstance(profile, str) or not role or not profile for role, profile in self.supported_role_profiles):
            raise InvalidRequestError("harness role/profile metadata is invalid")


@dataclass(frozen=True)
class WorkspaceManifest:
    adapter_id: str
    session_name: str
    version_label: str
    protocol_version: int | None
    interface_version: int = 1

    def __post_init__(self) -> None:
        validate_adapter_id(self.adapter_id)
        if not self.session_name or not self.version_label:
            raise ValueError("workspace manifest fields must not be empty")
        if self.protocol_version is not None and self.protocol_version < 1:
            raise ValueError("workspace protocol version must be positive")
        if self.interface_version != 1:
            raise InvalidRequestError("Pisec workspace interface version must be 1")


@dataclass(frozen=True)
class HarnessArtifacts:
    harness_home: str
    launch_secret_path: str
    policy_path: str
    policy_sha256: str
    runtime_token_sha256: str
    generation_sha256: str
    adapter_data: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, name in ((self.policy_sha256, "policy digest"), (self.runtime_token_sha256, "runtime token digest"), (self.generation_sha256, "runtime generation")):
            validate_sha256(value, name)
        if any(not isinstance(value, str) or not value for value in (self.harness_home, self.launch_secret_path, self.policy_path)):
            raise InvalidRequestError("harness artifact paths are invalid")

    def as_mapping(self) -> dict[str, Any]:
        return {
            "harnessHome": self.harness_home,
            "launchSecretPath": self.launch_secret_path,
            "policyPath": self.policy_path,
            "policySha256": self.policy_sha256,
            "runtimeTokenSha256": self.runtime_token_sha256,
            "generationSha256": self.generation_sha256,
            "adapterData": dict(self.adapter_data),
        }


def artifacts_from_mapping(value: Mapping[str, Any]) -> HarnessArtifacts:
    """Reconstruct persisted, non-secret harness artifact identity."""
    if not isinstance(value, Mapping):
        raise InvalidRequestError("harness artifacts are invalid")
    adapter_data = value.get("adapterData", {})
    if not isinstance(adapter_data, Mapping):
        raise InvalidRequestError("harness adapter artifacts are invalid")
    return HarnessArtifacts(
        harness_home=str(value.get("harnessHome", "")),
        launch_secret_path=str(value.get("launchSecretPath", "")),
        policy_path=str(value.get("policyPath", "")),
        policy_sha256=str(value.get("policySha256", "")),
        runtime_token_sha256=str(value.get("runtimeTokenSha256", "")),
        generation_sha256=str(value.get("generationSha256", "")),
        adapter_data=dict(adapter_data),
    )


@dataclass(frozen=True)
class RuntimeSurfaceArtifacts:
    content_sha256: str
    manifest: str | Mapping[str, Any]
    root_path: str

    def __post_init__(self) -> None:
        validate_sha256(self.content_sha256, "runtime surface digest")
        if not isinstance(self.root_path, str) or not self.root_path:
            raise InvalidRequestError("runtime surface root is invalid")
        root = Path(self.root_path)
        if not root.is_absolute() or root.is_symlink() or root.resolve(strict=False) != root:
            raise InvalidRequestError("runtime surface root must be canonical and absolute")
        try:
            manifest_json = canonical_json(json.loads(self.manifest) if isinstance(self.manifest, str) else self.manifest, max_bytes=256 * 1024, max_text=64 * 1024)
            manifest = json.loads(manifest_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise InvalidRequestError("runtime surface manifest is invalid") from error
        if not isinstance(manifest, dict):
            raise InvalidRequestError("runtime surface manifest is invalid")
        adapter_id = manifest.get("adapter")
        interface_version = manifest.get("interfaceVersion")
        version_label = manifest.get("adapterVersion")
        if not isinstance(adapter_id, str) or ADAPTER_ID_RE.fullmatch(adapter_id) is None:
            raise InvalidRequestError("runtime surface manifest adapter is invalid")
        if isinstance(interface_version, bool) or not isinstance(interface_version, int) or interface_version != 1:
            raise InvalidRequestError("runtime surface manifest interface version must be 1")
        if not isinstance(version_label, str) or not version_label:
            raise InvalidRequestError("runtime surface manifest adapter version is invalid")
        object.__setattr__(self, "manifest", manifest_json)

    def _manifest_mapping(self) -> Mapping[str, Any]:
        value = json.loads(self.manifest)
        if not isinstance(value, dict):
            raise InvalidRequestError("runtime surface manifest is invalid")
        return value

    @property
    def adapter_id(self) -> str:
        manifest = self._manifest_mapping()
        return str(manifest["adapter"])

    @property
    def interface_version(self) -> int:
        return int(self._manifest_mapping()["interfaceVersion"])

    @property
    def version_label(self) -> str:
        manifest = self._manifest_mapping()
        return str(manifest["adapterVersion"])

    @property
    def manifest_json(self) -> str:
        return self.manifest


@dataclass(frozen=True)
class StagedHarnessArtifacts:
    operation_id: str
    workstream_id: str
    staging_root: str
    candidate_manifest_json: str
    candidate_content_sha256: str
    candidate: HarnessArtifacts
    prior: HarnessArtifacts | None
    compensation_json: str

    def __post_init__(self) -> None:
        if not self.operation_id or not self.workstream_id:
            raise InvalidRequestError("staged harness identity is invalid")
        staging_root = Path(self.staging_root)
        if not staging_root.is_absolute() or staging_root.resolve(strict=False) != staging_root:
            raise InvalidRequestError("staged harness root must be canonical and absolute")
        if not isinstance(self.candidate, HarnessArtifacts) or (self.prior is not None and not isinstance(self.prior, HarnessArtifacts)):
            raise InvalidRequestError("staged harness artifacts are invalid")
        validate_sha256(self.candidate_content_sha256, "staged harness content digest")
        if canonical_json(__import__("json").loads(self.candidate_manifest_json), max_bytes=256 * 1024, max_text=64 * 1024) != self.candidate_manifest_json:
            raise InvalidRequestError("staged candidate manifest is not canonical")
        if canonical_json(__import__("json").loads(self.compensation_json), max_bytes=64 * 1024, max_text=64 * 1024) != self.compensation_json:
            raise InvalidRequestError("staged compensation is not canonical")



def artifact_document(manifest: HarnessManifest, artifacts: HarnessArtifacts) -> str:
    if not isinstance(artifacts, HarnessArtifacts):
        raise InvalidRequestError("harness artifacts are invalid")
    values = dict(artifacts.adapter_data)
    if any(not isinstance(key, str) or not key or not isinstance(value, str) for key, value in values.items()):
        raise InvalidRequestError("harness adapter artifact values are invalid")
    return canonical_json(
        {"schemaVersion": 2, "adapterId": manifest.adapter_id, "generationSha256": artifacts.generation_sha256, "values": values},
        max_bytes=64 * 1024,
        max_text=64 * 1024,
    )


@dataclass(frozen=True)
class AgentObservation:
    name: str
    surface_id: str
    identity_usable: bool
    state: str


@dataclass(frozen=True)
class RuntimeProcessObservation:
    state: str
    detail: str

    def __post_init__(self) -> None:
        if self.state not in {"live", "stopped", "unknown"}:
            raise ValueError("runtime process observation state is invalid")


@dataclass(frozen=True)
class WorkspaceObservation:
    workspace_id: str
    view_id: str
    surface_id: str
    worktree_path: str | None
    branch_name: str | None
    agent: AgentObservation | None


@dataclass(frozen=True)
class AdapterHealth:
    name: str
    ok: bool
    detail: str



class HarnessAdapter(Protocol):
    manifest: HarnessManifest

    def prepare_runtime_surface(self) -> RuntimeSurfaceArtifacts: ...

    def current_runtime_surface(self) -> RuntimeSurfaceArtifacts: ...

    def desired_generation(self, scope: Mapping[str, Any], surface: RuntimeSurfaceArtifacts | None = None) -> str: ...

    def stage_profile(self, scope: Mapping[str, Any], surface: RuntimeSurfaceArtifacts, staging_root: Path) -> StagedHarnessArtifacts: ...
    def activate_profile(self, scope: Mapping[str, Any], staged: StagedHarnessArtifacts) -> HarnessArtifacts: ...
    def restore_profile(self, scope: Mapping[str, Any], previous: HarnessArtifacts) -> HarnessArtifacts: ...
    def discard_staged_profile(self, staged: StagedHarnessArtifacts) -> None: ...
    def validate_execution_profile(self, profile: str, role: str) -> None: ...

    def profile_domains(self, profile: str, additional_domains: Sequence[str]) -> tuple[str, ...]: ...


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
    ) -> Path: ...

    def launch_binding_path(self, workstream_id: str) -> Path: ...

    def cleanup_binding(self, binding: Mapping[str, Any]) -> None: ...

    def validate_native_session(self, binding: Mapping[str, Any], kind: str | None, value: str | None) -> None: ...

    def health_checks(self, binding: Mapping[str, Any], workstream: Mapping[str, Any]) -> Sequence[AdapterHealth]: ...


class WorkspaceAdapter(Protocol):
    manifest: WorkspaceManifest

    def create_workspace(self, cwd: str, label: str, focus: bool = False) -> WorkspaceObservation: ...

    def create_tab(self, *, workspace_id: str, cwd: str, label: str, focus: bool = False) -> WorkspaceObservation: ...
    def rename_tab(self, view_id: str, label: str) -> Mapping[str, Any]: ...
    def move_surface_to_tab(self, *, surface_id: str, workspace_id: str, label: str, focus: bool = False) -> WorkspaceObservation: ...

    def observe_tab(self, *, workspace_id: str, cwd: str) -> WorkspaceObservation | None: ...


    def observe_workstream(self, *, path: str, agent_name: str) -> WorkspaceObservation | None: ...

    def observe_surface(self, *, workspace_id: str, view_id: str, surface_id: str, cwd: str) -> WorkspaceObservation | None: ...

    def observe_runtime(self, surface_id: str, process_identity: str) -> RuntimeProcessObservation: ...

    def stop_runtime(self, surface_id: str) -> Mapping[str, Any]: ...

    def run_command(self, surface_id: str, argv: Sequence[str], env: Mapping[str, str] | None = None) -> Mapping[str, Any]: ...

    def prompt_agent(self, surface_id: str, text: str, wait_until: tuple[str, ...], timeout_ms: int) -> Mapping[str, Any]: ...

    def prompt_agent_nowait(self, surface_id: str, text: str) -> Mapping[str, Any]: ...

    def trigger_agent_nowait(self, surface_id: str, trigger: str, process_identity: str) -> Mapping[str, Any]: ...

    def prompt_eligible(self, agent_observation: AgentObservation) -> bool: ...

    def focus_pane(self, surface_id: str) -> Mapping[str, Any]: ...

    def close_tab(self, view_id: str) -> Mapping[str, Any]: ...

    def close_workspace(self, workspace_id: str) -> Mapping[str, Any]: ...

    def report_session(self, surface_id: str, native_session: tuple[str, str], seq: int, start_source: str, runtime_instance_id: str, harness: HarnessManifest) -> Mapping[str, Any]: ...

    def report_state(self, surface_id: str, state: str, message: str | None, seq: int, runtime_instance_id: str, harness: HarnessManifest) -> Mapping[str, Any]: ...

    def release_agent(self, surface_id: str, seq: int, runtime_instance_id: str, harness: HarnessManifest) -> Mapping[str, Any]: ...

    def reconcile(self, store: Any, event: Mapping[str, Any] | None = None) -> Mapping[str, Any]: ...

    def health_checks(self) -> Sequence[AdapterHealth]: ...



class AdapterRegistry:
    """Explicit adapter maps; IDs never fall back to the active adapter."""

    def __init__(self) -> None:
        self._harnesses: dict[str, HarnessAdapter] = {}
        self._workspaces: dict[str, WorkspaceAdapter] = {}

    def register_harness(self, adapter: HarnessAdapter) -> None:
        adapter_id = validate_adapter_id(adapter.manifest.adapter_id)
        _validate_adapter_surface(adapter, HARNESS_METHODS, "harness")
        if adapter_id in self._harnesses:
            raise InvalidRequestError("harness adapter is registered more than once", detail={"adapterId": adapter_id})
        self._harnesses[adapter_id] = adapter

    def register_workspace(self, adapter: WorkspaceAdapter) -> None:
        adapter_id = validate_adapter_id(adapter.manifest.adapter_id)
        _validate_adapter_surface(adapter, WORKSPACE_METHODS, "workspace")
        if adapter_id in self._workspaces:
            raise InvalidRequestError("workspace adapter is registered more than once", detail={"adapterId": adapter_id})
        self._workspaces[adapter_id] = adapter

    def resolve_harness(self, adapter_id: str) -> HarnessAdapter:
        validate_adapter_id(adapter_id)
        adapter = self._harnesses.get(adapter_id)
        if adapter is None:
            raise NeedsAttentionError("configured adapter is unavailable", detail={"adapterId": adapter_id})
        return adapter

    def resolve_workspace(self, adapter_id: str) -> WorkspaceAdapter:
        validate_adapter_id(adapter_id)
        adapter = self._workspaces.get(adapter_id)
        if adapter is None:
            raise NeedsAttentionError("configured adapter is unavailable", detail={"adapterId": adapter_id})
        return adapter

    def harness_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._harnesses))

    def workspace_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._workspaces))


def validate_configured_routes(config: Mapping[str, Any], registry: AdapterRegistry) -> None:
    """Resolve every configured harness before any workstream is prepared."""
    harness = config.get("harness")
    workspace = config.get("workspace")
    if not isinstance(harness, Mapping) or not isinstance(workspace, Mapping):
        raise InvalidRequestError("Pisec primary adapter configuration is invalid")
    primary = registry.resolve_harness(str(harness.get("id")))
    registry.resolve_workspace(str(workspace.get("id")))
    supported = set(primary.manifest.supported_role_profiles)
    if supported and not {("secretary", "secretary-project"), ("first_mate", "first-mate")} <= supported:
        raise InvalidRequestError("configured primary harness cannot run supervisor profiles")
    routing = config.get("workerRouting", {})
    if not isinstance(routing, Mapping):
        raise InvalidRequestError("worker routing configuration is invalid")
    target_ids = {str(routing.get("fallbackHarness"))} if routing.get("fallbackHarness") else set()
    routes = routing.get("routes", {})
    if isinstance(routes, Mapping):
        for key, route in routes.items():
            if not isinstance(route, Mapping) or not isinstance(route.get("harness"), str):
                raise InvalidRequestError(f"worker route {key} is invalid")
            target_ids.add(str(route["harness"]))
    for adapter_id in sorted(target_ids):
        adapter = registry.resolve_harness(adapter_id)
        if adapter.manifest.supported_role_profiles and ("worker", "worker-default") not in set(adapter.manifest.supported_role_profiles):
            raise InvalidRequestError(f"worker route harness {adapter_id} cannot run worker-default")


HARNESS_METHODS = (
    "prepare_runtime_surface", "current_runtime_surface", "desired_generation", "stage_profile",
    "activate_profile", "restore_profile", "discard_staged_profile", "validate_execution_profile",
    "profile_domains", "commit_launch_binding", "launch_binding_path", "cleanup_binding",
    "validate_native_session", "health_checks",
)
WORKSPACE_METHODS = (
    "create_workspace", "create_tab", "rename_tab", "move_surface_to_tab", "observe_tab",
    "observe_workstream", "observe_surface", "observe_runtime", "run_command", "stop_runtime",
    "prompt_agent", "prompt_agent_nowait", "trigger_agent_nowait", "prompt_eligible", "focus_pane", "close_tab", "close_workspace",
    "report_session", "report_state", "release_agent", "reconcile", "health_checks",
)


def _validate_adapter_surface(adapter: Any, methods: Sequence[str], kind: str) -> None:
    missing = [name for name in methods if not callable(getattr(adapter, name, None))]
    if missing:
        raise InvalidRequestError(f"{kind} adapter is missing required methods", detail={"missing": missing})
    manifest = getattr(adapter, "manifest", None)
    if kind == "harness" and (not isinstance(manifest, HarnessManifest) or manifest.interface_version != 1):
        raise InvalidRequestError("harness adapter manifest is invalid")
