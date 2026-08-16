from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import os
import secrets
import shutil
import subprocess
from typing import Any, Mapping, Sequence

from scripts.pisec.adapters import (
    AdapterHealth,
    AgentObservation,
    HarnessArtifacts,
    HarnessManifest,
    WorkspaceManifest,
    WorkspaceObservation,
)


def make_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Pisec Test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "pisec@example.invalid"], check=True)
    (path / "README").write_text("fixture\n")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "fixture"], check=True)


class FixtureHarness:
    manifest = HarnessManifest("fixture-harness", "fixture-agent", "fixture-1")

    def __init__(self, root: Path):
        self.root = root
        self.calls: list[tuple[str, Any]] = []
        self.profiles: list[str] = []
        self.launched: list[str] = []
        self.cleaned: list[str] = []

    def validate_execution_profile(self, profile: str, role: str) -> None:
        self.calls.append(("validate", (profile, role)))
        expected = "secretary" if profile == "secretary-project" else "worker"
        if profile not in {"secretary-project", "worker-default", "worker-networked"} or role != expected:
            raise ValueError("fixture profile is invalid")

    def profile_domains(self, profile: str, additional_domains: Sequence[str]) -> tuple[str, ...]:
        self.calls.append(("domains", (profile, tuple(additional_domains))))
        self.validate_execution_profile(profile, "secretary" if profile == "secretary-project" else "worker")
        return tuple(sorted({"fixture.test", *additional_domains}))

    def materialize_profile(self, scope: Mapping[str, Any]) -> HarnessArtifacts:
        workstream_id = str(scope["workstreamId"])
        self.calls.append(("materialize", workstream_id))
        self.profiles.append(workstream_id)
        home = self.root / "harness" / workstream_id
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(home, 0o700)
        session_root = home / "sessions"
        session_root.mkdir(mode=0o700, exist_ok=True)
        secret = home / "launch.secret"
        if not secret.exists():
            secret.write_text(secrets.token_urlsafe(48) + "\n")
            os.chmod(secret, 0o600)
        policy = home / "policy.json"
        policy.write_text("{}\n")
        os.chmod(policy, 0o600)
        token = secret.read_text().strip()
        return HarnessArtifacts(
            harness_home=str(home),
            launch_secret_path=str(secret),
            policy_path=str(policy),
            policy_sha256=hashlib.sha256(policy.read_bytes()).hexdigest(),
            runtime_token_sha256=hashlib.sha256(token.encode()).hexdigest(),
            adapter_data={"fixtureRoot": str(home)},
        )

    def commit_launch_binding(self, scope: Mapping[str, Any], artifacts: HarnessArtifacts) -> Path:
        workstream_id = str(scope["workstreamId"])
        self.calls.append(("launch", workstream_id))
        self.launched.append(workstream_id)
        path = Path(artifacts.harness_home) / "binding.json"
        path.write_text(workstream_id + "\n")
        os.chmod(path, 0o600)
        return path

    def cleanup_binding(self, binding: Mapping[str, Any]) -> None:
        workstream_id = str(binding["workstream_id"])
        self.calls.append(("cleanup", workstream_id))
        self.cleaned.append(workstream_id)
        shutil.rmtree(Path(str(binding["harness_home"])), ignore_errors=False)

    def validate_native_session(self, binding: Mapping[str, Any], kind: str | None, value: str | None) -> None:
        if kind is None and value is None:
            return
        if kind == "id" and isinstance(value, str) and value and len(value) <= 128:
            return
        if kind == "path" and isinstance(value, str) and Path(value).is_absolute() and Path(value).resolve(strict=False).is_relative_to((Path(str(binding["harness_home"])) / "sessions").resolve()):
            return
        raise ValueError("fixture session reference is invalid")

    def health_checks(self, binding: Mapping[str, Any], workstream: Mapping[str, Any]) -> Sequence[AdapterHealth]:
        return (AdapterHealth("fixture harness", True, "fixture"),)


class FixtureWorkspace:
    manifest = WorkspaceManifest("fixture-workspace", "fixture-session", "fixture-1", None)

    def __init__(self, root: Path, store: Any | None = None):
        self.root = root
        self.store = store
        self.worktrees: dict[str, WorkspaceObservation] = {}
        self.agents: dict[str, AgentObservation] = {}
        self.prompts: list[tuple[str, str]] = []
        self.calls: list[tuple[str, Any]] = []
        self.closed: list[str] = []
        self._counter = 0

    def _observation(self, path: str, branch: str | None = None) -> WorkspaceObservation:
        current = self.worktrees.get(path)
        if current is not None:
            return current
        self._counter += 1
        current = WorkspaceObservation(
            workspace_id=f"fixture-workspace-{self._counter}",
            view_id=f"fixture-view-{self._counter}",
            surface_id=f"fixture-surface-{self._counter}",
            worktree_path=path,
            branch_name=branch,
            agent=None,
        )
        self.worktrees[path] = current
        return current

    def create_workspace(self, cwd: str, label: str, focus: bool = False) -> WorkspaceObservation:
        self.calls.append(("create_workspace", (cwd, label, focus)))
        return self._observation(str(Path(cwd).resolve(strict=False)))

    def create_worktree(self, *, cwd: str, branch: str, base: str, path: str, label: str, focus: bool = False) -> WorkspaceObservation:
        self.calls.append(("create_worktree", (cwd, branch, base, path, label, focus)))
        existing = self.worktrees.get(path)
        if existing is None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "-C", cwd, "worktree", "add", "-q", "-b", branch, path, base], check=True)
            existing = self._observation(path, branch)
        return existing

    def observe_workstream(self, *, path: str, agent_name: str) -> WorkspaceObservation | None:
        observed = self.worktrees.get(path)
        if observed is None:
            return None
        agent = self.agents.get(agent_name)
        return WorkspaceObservation(observed.workspace_id, observed.view_id, observed.surface_id, observed.worktree_path, observed.branch_name, agent)

    def start_agent(self, surface_id: str, name: str, agent_kind: str) -> Mapping[str, Any]:
        self.calls.append(("start", (surface_id, name, agent_kind)))
        agent = AgentObservation(name, surface_id, True, "working")
        self.agents[name] = agent
        if self.store is None:
            from scripts.pisec.pi_store import PiStore
            with PiStore(self.root / "state") as store:
                store.conn.execute("UPDATE runtime_bindings SET runtime_instance_id='fixture-runtime',report_seq=1 WHERE workspace_surface_id=?", (surface_id,))
        else:
            row = self.store.conn.execute("SELECT workstream_id FROM runtime_bindings WHERE workspace_surface_id=?", (surface_id,)).fetchone()
            if row is not None:
                self.store.conn.execute("UPDATE runtime_bindings SET runtime_instance_id='fixture-runtime',report_seq=1 WHERE workstream_id=?", (row["workstream_id"],))
        return {"started": True, "name": name, "surfaceId": surface_id}

    def prompt_agent(self, surface_id: str, text: str, wait_until: tuple[str, ...], timeout_ms: int) -> Mapping[str, Any]:
        self.calls.append(("prompt", (surface_id, text, wait_until, timeout_ms)))
        self.prompts.append((surface_id, text))
        return {"prompted": True}

    def prompt_agent_nowait(self, surface_id: str, text: str) -> Mapping[str, Any]:
        self.calls.append(("prompt_nowait", (surface_id, text)))
        self.prompts.append((surface_id, text))
        return {"prompted": True}

    def focus_agent(self, surface_id: str) -> Mapping[str, Any]:
        self.calls.append(("focus", surface_id))
        return {"focused": surface_id}

    def close_workspace(self, workspace_id: str) -> Mapping[str, Any]:
        self.calls.append(("close", workspace_id))
        self.closed.append(workspace_id)
        return {"closed": workspace_id}

    def report_session(self, surface_id: str, native_session: tuple[str, str], seq: int, start_source: str, runtime_instance_id: str, harness: HarnessManifest) -> Mapping[str, Any]:
        self.calls.append(("report_session", (surface_id, native_session, seq, start_source, runtime_instance_id, harness.agent_kind)))
        return {"reported": True}

    def report_state(self, surface_id: str, state: str, message: str | None, seq: int, runtime_instance_id: str, harness: HarnessManifest) -> Mapping[str, Any]:
        self.calls.append(("report_state", (surface_id, state, message, seq, runtime_instance_id, harness.agent_kind)))
        return {"reported": True}

    def release_agent(self, surface_id: str, seq: int, runtime_instance_id: str, harness: HarnessManifest) -> Mapping[str, Any]:
        self.calls.append(("release", (surface_id, seq, runtime_instance_id, harness.agent_kind)))
        return {"released": True}

    def reconcile(self, store: Any, event: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        self.calls.append(("reconcile", event))
        return {"reconciled": True, "updated": 0, "missing": 0, "eventAccepted": event is not None}

    def health_checks(self) -> Sequence[AdapterHealth]:
        return (AdapterHealth("fixture workspace", True, "fixture"),)


class UnattestedFixtureWorkspace(FixtureWorkspace):
    def start_agent(self, surface_id: str, name: str, agent_kind: str) -> Mapping[str, Any]:
        self.calls.append(("start", (surface_id, name, agent_kind)))
        self.agents[name] = AgentObservation(name, surface_id, True, "working")
        return {"started": True, "name": name, "surfaceId": surface_id}


class DelayedFixtureWorkspace(FixtureWorkspace):
    def __init__(self, root: Path, store: Any):
        super().__init__(root, store)
        self.observations = 0

    def observe_workstream(self, *, path: str, agent_name: str) -> WorkspaceObservation | None:
        observed = super().observe_workstream(path=path, agent_name=agent_name)
        if observed is None or observed.agent is None:
            return observed
        self.observations += 1
        agent = AgentObservation(observed.agent.name, observed.agent.surface_id, self.observations >= 2, observed.agent.state)
        return WorkspaceObservation(observed.workspace_id, observed.view_id, observed.surface_id, observed.worktree_path, observed.branch_name, agent)


class FixtureGitObjects:
    def __init__(self):
        self.calls: list[str] = []

    def materialize(self, scope: Mapping[str, Any]) -> Path:
        path = Path(str(scope["privateGitObjectDir"]))
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        (path / "info").mkdir(mode=0o700, exist_ok=True)
        (path / "pack").mkdir(mode=0o700, exist_ok=True)
        common = Path(str(scope["gitCommonObjectDir"]))
        (path / "info" / "alternates").write_text(str(common.resolve(strict=False)) + "\n")
        self.calls.append(str(path))
        return path

    def promote(self, scope: Mapping[str, Any], source_oid: str) -> Mapping[str, Any]:
        self.calls.append(f"promote:{source_oid}")
        return {"sourceOid": source_oid, "promoted": True}
