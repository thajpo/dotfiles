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
    RuntimeProcessObservation,
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

    def desired_generation(self, scope: Mapping[str, Any]) -> str:
        return hashlib.sha256(("fixture-generation:" + str(scope["executionProfile"])).encode()).hexdigest()

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
            generation_sha256=self.desired_generation(scope),
            adapter_data={"fixtureRoot": str(home)},
        )

    def commit_launch_binding(self, scope: Mapping[str, Any], artifacts: HarnessArtifacts, **_: Any) -> Path:
        workstream_id = str(scope["workstreamId"])
        self.calls.append(("launch", workstream_id))
        self.launched.append(workstream_id)
        path = Path(artifacts.harness_home) / "binding.json"
        path.write_text(workstream_id + "\n")
        os.chmod(path, 0o600)
        return path

    def launch_binding_path(self, workstream_id: str) -> Path:
        return self.root / "harness" / workstream_id / "binding.json"

    def cleanup_binding(self, binding: Mapping[str, Any]) -> None:
        workstream_id = str(binding["workstream_id"])
        self.calls.append(("cleanup", workstream_id))
        self.cleaned.append(workstream_id)
        home = Path(str(binding["harness_home"]))
        for child in home.iterdir():
            if child.name == "sessions":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

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
        self.renamed: list[tuple[str, str]] = []
        self.project_workspace_id: str | None = None
        self.project_workspace_ids: dict[str, str] = {}
        self.runtime_states: dict[str, str] = {}
        self._counter = 0
        self._runtime_counter = 0
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
        path = str(Path(cwd).resolve(strict=False))
        observation = self._observation(path)
        self.project_workspace_ids[path] = observation.workspace_id
        self.project_workspace_id = observation.workspace_id
        return observation
    def create_tab(self, *, workspace_id: str, cwd: str, label: str, focus: bool = False) -> WorkspaceObservation:
        self.calls.append(("create_tab", (workspace_id, cwd, label, focus)))
        if self.project_workspace_id not in {None, *self.project_workspace_ids.values()} or workspace_id not in self.project_workspace_ids.values():
            raise RuntimeError("fixture tab is outside project workspace")
        path = str(Path(cwd).resolve(strict=False))
        existing = self.worktrees.get(path)
        if existing is not None:
            return existing
        self._counter += 1
        observation = WorkspaceObservation(
            workspace_id=workspace_id,
            view_id=f"fixture-view-{self._counter}",
            surface_id=f"fixture-surface-{self._counter}",
            worktree_path=path,
            branch_name=None,
            agent=None,
        )
        self.worktrees[path] = observation
        return observation


    def rename_tab(self, view_id: str, label: str) -> Mapping[str, Any]:
        self.calls.append(("rename_tab", (view_id, label)))
        self.renamed.append((view_id, label))
        return {"renamed": view_id, "label": label}

    def observe_tab(self, *, workspace_id: str, cwd: str) -> WorkspaceObservation | None:
        path = str(Path(cwd).resolve(strict=False))
        observed = self.worktrees.get(path)
        if observed is None or observed.workspace_id != workspace_id:
            return None
        agent = next((item for item in self.agents.values() if item.surface_id == observed.surface_id), None)
        return WorkspaceObservation(observed.workspace_id, observed.view_id, observed.surface_id, observed.worktree_path, observed.branch_name, agent)


    def observe_workstream(self, *, path: str, agent_name: str) -> WorkspaceObservation | None:
        observed = self.worktrees.get(path)
        if observed is None:
            return None
        agent = self.agents.get(agent_name)
        return WorkspaceObservation(observed.workspace_id, observed.view_id, observed.surface_id, observed.worktree_path, observed.branch_name, agent)

    def observe_surface(self, *, workspace_id: str, view_id: str, surface_id: str, cwd: str) -> WorkspaceObservation | None:
        observed = next(
            (
                item
                for item in self.worktrees.values()
                if item.workspace_id == workspace_id
                and item.view_id == view_id
                and item.surface_id == surface_id
                and str(Path(str(item.worktree_path)).resolve(strict=False)) == str(Path(cwd).resolve(strict=False))
            ),
            None,
        )
        if observed is None:
            return None
        agent = next((item for item in self.agents.values() if item.surface_id == surface_id), None)
        return WorkspaceObservation(observed.workspace_id, observed.view_id, observed.surface_id, observed.worktree_path, observed.branch_name, agent)

    def observe_runtime(self, surface_id: str, process_identity: str) -> RuntimeProcessObservation:
        state = self.runtime_states.get(surface_id)
        if state is None:
            state = "live" if any(agent.surface_id == surface_id for agent in self.agents.values()) else "stopped"
        return RuntimeProcessObservation(state, "fixture")

    def stop_runtime(self, surface_id: str) -> Mapping[str, Any]:
        self.calls.append(("stop", surface_id))
        self.runtime_states[surface_id] = "stopped"
        for name, agent in list(self.agents.items()):
            if agent.surface_id == surface_id:
                del self.agents[name]
        return {"type": "ok"}

    def start_agent(self, surface_id: str, name: str, agent_kind: str) -> Mapping[str, Any]:
        self.calls.append(("start", (surface_id, name, agent_kind)))
        agent = AgentObservation(name, surface_id, True, "working")
        self.agents[name] = agent
        self.runtime_states.pop(surface_id, None)
        self._runtime_counter += 1
        runtime_instance = f"fixture-runtime-{self._runtime_counter}-{secrets.token_hex(4)}"
        if self.store is None:
            from scripts.pisec.pi_store import PiStore
            with PiStore(self.root / "state") as store:
                store.conn.execute("UPDATE runtime_bindings SET runtime_instance_id=?,report_seq=1,observed_state='idle',applied_generation_sha256=COALESCE(launch_generation_sha256,applied_generation_sha256),launch_generation_sha256=NULL WHERE workspace_surface_id=?", (runtime_instance, surface_id))
        else:
            row = self.store.conn.execute("SELECT workstream_id FROM runtime_bindings WHERE workspace_surface_id=?", (surface_id,)).fetchone()
            if row is not None:
                self.store.conn.execute("UPDATE runtime_bindings SET runtime_instance_id=?,report_seq=1,observed_state='idle',applied_generation_sha256=COALESCE(launch_generation_sha256,applied_generation_sha256),launch_generation_sha256=NULL WHERE workstream_id=?", (runtime_instance, row["workstream_id"]))
        return {"started": True, "name": name, "surfaceId": surface_id}

    def run_command(self, surface_id: str, argv: Sequence[str], env: Mapping[str, str] | None = None) -> Mapping[str, Any]:
        self.calls.append(("run", (surface_id, tuple(argv), dict(env or {}))))
        if self.store is None:
            from scripts.pisec.pi_store import PiStore
            with PiStore(self.root / "state") as store:
                row = store.conn.execute("SELECT agent_name FROM runtime_bindings WHERE workspace_surface_id=?", (surface_id,)).fetchone()
        else:
            row = self.store.conn.execute("SELECT agent_name FROM runtime_bindings WHERE workspace_surface_id=?", (surface_id,)).fetchone()
        if row is not None:
            self.start_agent(surface_id, row["agent_name"], "omp")
        return {"started": True, "surfaceId": surface_id}

    def prompt_agent(self, surface_id: str, text: str, wait_until: tuple[str, ...], timeout_ms: int) -> Mapping[str, Any]:
        self.calls.append(("prompt", (surface_id, text, wait_until, timeout_ms)))
        self.prompts.append((surface_id, text))
        return {"prompted": True}

    def prompt_agent_nowait(self, surface_id: str, text: str) -> Mapping[str, Any]:
        self.calls.append(("prompt_nowait", (surface_id, text)))
        self.prompts.append((surface_id, text))
        return {"prompted": True}

    def focus_pane(self, surface_id: str) -> Mapping[str, Any]:
        self.calls.append(("focus", surface_id))
        return {"focused": surface_id}

    def focus_agent(self, surface_id: str) -> Mapping[str, Any]:
        return self.focus_pane(surface_id)

    def close_tab(self, view_id: str) -> Mapping[str, Any]:
        self.calls.append(("close_tab", view_id))
        self.closed.append(view_id)
        return {"closed": view_id}
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
        row = self.store.conn.execute(
            "SELECT w.kind FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workspace_surface_id=?",
            (surface_id,),
        ).fetchone()
        if row is not None and row["kind"] == "secretary":
            self.store.conn.execute(
                "UPDATE runtime_bindings SET runtime_instance_id='fixture-runtime',report_seq=1,observed_state='idle',applied_generation_sha256=COALESCE(launch_generation_sha256,applied_generation_sha256),launch_generation_sha256=NULL WHERE workspace_surface_id=?",
                (surface_id,),
            )
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
