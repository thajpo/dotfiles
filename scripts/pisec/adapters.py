"""Product-neutral Pisec adapter contracts and explicit registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence

from .models import InvalidRequestError, NeedsAttentionError, canonical_json

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

    def __post_init__(self) -> None:
        validate_adapter_id(self.adapter_id)
        if not self.agent_kind or not self.version_label:
            raise ValueError("harness manifest fields must not be empty")


@dataclass(frozen=True)
class WorkspaceManifest:
    adapter_id: str
    session_name: str
    version_label: str
    protocol_version: int | None

    def __post_init__(self) -> None:
        validate_adapter_id(self.adapter_id)
        if not self.session_name or not self.version_label:
            raise ValueError("workspace manifest fields must not be empty")
        if self.protocol_version is not None and self.protocol_version < 1:
            raise ValueError("workspace protocol version must be positive")


@dataclass(frozen=True)
class HarnessArtifacts:
    harness_home: str
    launch_secret_path: str
    policy_path: str
    policy_sha256: str
    runtime_token_sha256: str
    generation_sha256: str
    adapter_data: Mapping[str, str] = field(default_factory=dict)

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


@dataclass(frozen=True)
class RuntimeSurfaceArtifacts:
    content_sha256: str
    manifest: Mapping[str, Any]
    root_path: str

    def __post_init__(self) -> None:
        if not isinstance(self.content_sha256, str) or len(self.content_sha256) != 64:
            raise InvalidRequestError("runtime surface digest is invalid")
        if not isinstance(self.root_path, str) or not self.root_path:
            raise InvalidRequestError("runtime surface root is invalid")


RuntimeReleaseArtifacts = RuntimeSurfaceArtifacts

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
    interactive_ready: bool
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

    def materialize_profile(self, scope: Mapping[str, Any], surface: RuntimeSurfaceArtifacts | None = None) -> HarnessArtifacts: ...
    def validate_execution_profile(self, profile: str, role: str) -> None: ...

    def profile_domains(self, profile: str, additional_domains: Sequence[str]) -> tuple[str, ...]: ...

    def desired_generation(self, scope: Mapping[str, Any], surface: RuntimeSurfaceArtifacts | None = None) -> str: ...

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
        if adapter_id in self._harnesses:
            raise InvalidRequestError("harness adapter is registered more than once", detail={"adapterId": adapter_id})
        self._harnesses[adapter_id] = adapter

    def register_workspace(self, adapter: WorkspaceAdapter) -> None:
        adapter_id = validate_adapter_id(adapter.manifest.adapter_id)
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
