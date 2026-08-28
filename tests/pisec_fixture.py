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
    RuntimeSurfaceArtifacts,
    RuntimeProcessObservation,
    StagedHarnessArtifacts,
    WorkspaceManifest,
    WorkspaceObservation,
)
from scripts.pisec.events import append_event_in_transaction
from scripts.pisec.models import canonical_json


def make_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    path.chmod(0o755)
    (path / ".git" / "objects").chmod(0o700)
    (path / ".git" / "objects" / "pack").chmod(0o700)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Pisec Test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "pisec@example.invalid"], check=True)
    (path / "README").write_text("fixture\n")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "fixture"], check=True)


class FixtureHarness:
    manifest = HarnessManifest("fixture-harness", "fixture-agent", "fixture-1", 1, (("worker", "worker-default"), ("secretary", "secretary-project"), ("first_mate", "first-mate")))

    def __init__(self, root: Path):
        self.root = root
        self.calls: list[tuple[str, Any]] = []
        self.profiles: list[str] = []
        self.launched: list[str] = []
        self.launch_replacements: list[bool] = []
        self.cleaned: list[str] = []
        self.surface_calls = 0
        self.surface_extra = ""

    def validate_execution_profile(self, profile: str, role: str) -> None:
        self.calls.append(("validate", (profile, role)))
        expected = {"first-mate": "first_mate", "secretary-project": "secretary", "worker-default": "worker"}.get(profile)
        if expected is None or role != expected:
            raise ValueError("fixture profile is invalid")

    def profile_domains(self, profile: str, additional_domains: Sequence[str]) -> tuple[str, ...]:
        self.calls.append(("domains", (profile, tuple(additional_domains))))
        role = "first_mate" if profile == "first-mate" else "secretary" if profile == "secretary-project" else "worker"
        self.validate_execution_profile(profile, role)
        return tuple(sorted({"fixture.test", *additional_domains}))

    def current_runtime_surface(self) -> RuntimeSurfaceArtifacts:
        self.surface_calls += 1
        root = self.root / "runtime-surface"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        from scripts.pisec.runtime_surface import _tree_digest
        manifest = {"adapter": self.manifest.adapter_id, "adapterVersion": self.manifest.version_label, "interfaceVersion": 1}
        if self.surface_extra:
            manifest["surfaceExtra"] = self.surface_extra
        return RuntimeSurfaceArtifacts(_tree_digest(root), manifest, str(root.resolve()))

    def prepare_runtime_surface(self) -> RuntimeSurfaceArtifacts:
        return self.current_runtime_surface()

    def desired_generation(self, scope: Mapping[str, Any], surface: RuntimeSurfaceArtifacts | None = None) -> str:
        if surface is not None:
            scope = {
                **scope,
                "runtimeSurfaceSha256": surface.content_sha256,
                "runtimeSurfaceRoot": surface.root_path,
                "runtimeSurfaceId": "surface_" + surface.content_sha256[:32],
            }
        scope_dict = {
            key: scope.get(key)
            for key in (
                "executionProfile",
                "worktreePath",
                "externalDomains",
                "implementationModel",
                "harnessModel",
                "reasoningEffort",
            )
        }
        if scope.get("dataDirs"):
            scope_dict["dataDirs"] = scope["dataDirs"]
        if scope.get("pythonEnv"):
            scope_dict["pythonEnv"] = scope["pythonEnv"]
        values = {
            "adapter": self.manifest.adapter_id,
            "adapterVersion": self.manifest.version_label,
            "scope": scope_dict,
            "runtimeSurfaceSha256": scope.get("runtimeSurfaceSha256"),
        }
        return hashlib.sha256(("fixture-generation:" + canonical_json(values)).encode()).hexdigest()

    def materialize_profile(self, scope: Mapping[str, Any], *, root: Path | None = None, runtime_token: str | None = None, surface: RuntimeSurfaceArtifacts | None = None) -> HarnessArtifacts:
        workstream_id = str(scope["workstreamId"])
        self.calls.append(("materialize", workstream_id))
        self.profiles.append(workstream_id)
        home = (root or (self.root / "harness")) / workstream_id
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(home, 0o700)
        session_root = home / "sessions"
        session_root.mkdir(mode=0o700, exist_ok=True)
        secret = home / "launch.secret"
        if not secret.exists():
            secret.write_text((runtime_token or secrets.token_urlsafe(48)) + "\n")
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
            generation_sha256=self.desired_generation(scope, surface),
            adapter_data={"fixtureRoot": str(home)},
        )

    def stage_profile(self, scope: Mapping[str, Any], surface: RuntimeSurfaceArtifacts, staging_root: Path) -> StagedHarnessArtifacts:
        root = Path(staging_root).resolve()
        if root.exists() or root.is_symlink():
            if root.is_symlink() or not root.is_dir():
                raise RuntimeError("fixture staging root is unsafe")
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        candidate_root = root / "candidate-harness"
        prior_home = self.root / "harness" / str(scope["workstreamId"])
        candidate_home = candidate_root / str(scope["workstreamId"])
        if prior_home.is_dir():
            if prior_home.is_symlink() or any(path.is_symlink() for path in prior_home.rglob("*")):
                raise RuntimeError("fixture active profile contains a symlink")
            candidate_home.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copytree(prior_home, candidate_home, symlinks=False)
        prior_secret = self.root / "harness" / f"{scope['workstreamId']}" / "launch.secret"
        preserved_token = prior_secret.read_text().strip() if prior_secret.exists() else None
        candidate = self.materialize_profile({**scope, "runtimeSurfaceSha256": surface.content_sha256, "runtimeSurfaceRoot": surface.root_path}, root=candidate_root, runtime_token=preserved_token, surface=surface)
        prior = None
        prior_policy = prior_home / "policy.json"
        if prior_home.is_dir() and prior_secret.is_file() and prior_policy.is_file():
            prior = HarnessArtifacts(
                harness_home=str(prior_home), launch_secret_path=str(prior_secret), policy_path=str(prior_policy),
                policy_sha256=hashlib.sha256(prior_policy.read_bytes()).hexdigest(),
                runtime_token_sha256=hashlib.sha256(prior_secret.read_text().strip().encode()).hexdigest(),
                generation_sha256=self.desired_generation(scope, surface), adapter_data={},
            )
        return StagedHarnessArtifacts(
            operation_id=str(scope.get("operationId", "op_fixture")),
            workstream_id=str(scope["workstreamId"]),
            staging_root=str(root),
            candidate_manifest_json="{}",
            candidate_content_sha256=surface.content_sha256,
            candidate=candidate,
            prior=prior,
            compensation_json="{}",
        )

    def activate_profile(self, scope: Mapping[str, Any], staged: StagedHarnessArtifacts) -> HarnessArtifacts:
        workstream_id = str(scope["workstreamId"])
        candidate = staged.candidate
        staging_root = Path(staged.staging_root).resolve(strict=False)
        if any(not Path(value).resolve(strict=False).is_relative_to(staging_root) for value in (candidate.harness_home, candidate.launch_secret_path, candidate.policy_path)):
            raise RuntimeError("fixture staged profile escapes its operation root")
        active = self.root / "harness" / workstream_id
        if active.exists():
            backup_root = scope.get("permissionBackupRoot")
            if backup_root is not None:
                backup = Path(str(backup_root)) / "harness" / workstream_id
                backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if backup.exists():
                    shutil.rmtree(backup)
                shutil.move(str(active), str(backup))
            else:
                shutil.rmtree(active)
        active.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copytree(candidate.harness_home, active)
        prefix = str(Path(candidate.harness_home).parent)
        def rebase(value: str) -> str:
            return value.replace(prefix, str(active.parent), 1)
        return HarnessArtifacts(
            harness_home=str(active), launch_secret_path=rebase(candidate.launch_secret_path),
            policy_path=rebase(candidate.policy_path), policy_sha256=candidate.policy_sha256,
            runtime_token_sha256=candidate.runtime_token_sha256, generation_sha256=candidate.generation_sha256,
            adapter_data={key: rebase(value) for key, value in candidate.adapter_data.items()},
        )

    def restore_profile(self, scope: Mapping[str, Any], previous: HarnessArtifacts) -> HarnessArtifacts:
        backup_root = scope.get("permissionBackupRoot")
        if backup_root is not None:
            backup = Path(str(backup_root)) / "harness" / str(scope["workstreamId"])
            active = self.root / "harness" / str(scope["workstreamId"])
            if backup.exists():
                if active.exists():
                    shutil.rmtree(active)
                active.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.move(str(backup), str(active))
        return previous

    def discard_staged_profile(self, staged: StagedHarnessArtifacts) -> None:
        root = Path(staged.staging_root)
        if root.exists():
            shutil.rmtree(root)

    def commit_launch_binding(self, scope: Mapping[str, Any], artifacts: HarnessArtifacts, **kwargs: Any) -> Path:
        workstream_id = str(scope["workstreamId"])
        self.calls.append(("launch", workstream_id))
        self.launched.append(workstream_id)
        self.launch_replacements.append(bool(kwargs.get("replace", False)))
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
        self._observations: list[WorkspaceObservation] = []
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
        self._observations.append(current)
        return current
    def create_workspace(self, cwd: str, label: str, focus: bool = False) -> WorkspaceObservation:
        self.calls.append(("create_workspace", (cwd, label, focus)))
        path = str(Path(cwd).resolve(strict=False))
        self._counter += 1
        observation = WorkspaceObservation(
            workspace_id=f"fixture-workspace-{self._counter}",
            view_id=f"fixture-view-{self._counter}",
            surface_id=f"fixture-surface-{self._counter}",
            worktree_path=path,
            branch_name=None,
            agent=None,
        )
        self.worktrees[path] = observation
        self._observations.append(observation)
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
        self._observations.append(observation)
        return observation


    def rename_tab(self, view_id: str, label: str) -> Mapping[str, Any]:
        self.calls.append(("rename_tab", (view_id, label)))
        self.renamed.append((view_id, label))
        return {"renamed": view_id, "label": label}

    def move_surface_to_tab(self, *, surface_id: str, workspace_id: str, label: str, focus: bool = False) -> WorkspaceObservation:
        self.calls.append(("move_surface_to_tab", (surface_id, workspace_id, label, focus)))
        item = next((value for value in self._observations if value.surface_id == surface_id), None)
        if item is None:
            raise RuntimeError("fixture pane is missing")
        self._counter += 1
        moved = WorkspaceObservation(
            workspace_id=workspace_id,
            view_id=f"fixture-view-{self._counter}",
            surface_id=f"fixture-surface-{self._counter}",
            worktree_path=item.worktree_path,
            branch_name=item.branch_name,
            agent=None,
        )
        if item.worktree_path is not None:
            self.worktrees[str(Path(item.worktree_path).resolve(strict=False))] = moved
        self._observations.append(moved)
        previous_state = self.runtime_states.pop(surface_id, None)
        if previous_state is None:
            previous_state = "live" if any(agent.surface_id == surface_id for agent in self.agents.values()) else "stopped"
        self.runtime_states[moved.surface_id] = previous_state
        for name, agent in list(self.agents.items()):
            if agent.surface_id == surface_id:
                self.agents[name] = AgentObservation(agent.name, moved.surface_id, agent.identity_usable, agent.state)
        return moved

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
        active = list(self.worktrees.values())
        active.extend(item for item in self._observations if item.surface_id in self.runtime_states or any(agent.surface_id == item.surface_id for agent in self.agents.values()))
        observed = next((item for item in active if item.workspace_id == workspace_id and item.view_id == view_id and item.surface_id == surface_id and str(Path(str(item.worktree_path)).resolve(strict=False)) == str(Path(cwd).resolve(strict=False))), None)
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
        def attest(store: Any) -> None:
            row = store.conn.execute("SELECT r.workstream_id,w.project_id,r.desired_generation_sha256,r.launch_generation_sha256,r.refresh_operation_id,r.refresh_started_at FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workspace_surface_id=?", (surface_id,)).fetchone()
            if row is None:
                return
            generation = row["launch_generation_sha256"] or row["desired_generation_sha256"]
            permission_batch = store.conn.execute(
                "SELECT 1 FROM operations WHERE operation_id=? AND kind='project.permissions.update' AND state='applying'",
                (row["refresh_operation_id"],),
            ).fetchone() is not None if row["refresh_operation_id"] is not None else False
            with store.transaction():
                event = append_event_in_transaction(store.conn, kind="runtime.session_started", project_id=row["project_id"], workstream_id=row["workstream_id"], payload={"runtimeInstanceId": runtime_instance, "generationSha256": generation, "reportSeq": 1})
                store.conn.execute("UPDATE runtime_bindings SET runtime_instance_id=?,report_seq=1,observed_state='idle',applied_generation_sha256=?,launch_generation_sha256=CASE WHEN ? THEN launch_generation_sha256 ELSE NULL END,refresh_pending=CASE WHEN ? THEN 1 ELSE 0 END,refresh_operation_id=CASE WHEN ? THEN ? ELSE NULL END,refresh_started_at=CASE WHEN ? THEN ? ELSE NULL END,session_start_event_sequence=?,session_start_report_seq=1,session_started_at=? WHERE workstream_id=?", (runtime_instance, generation, permission_batch, permission_batch, permission_batch, row["refresh_operation_id"], permission_batch, row["refresh_started_at"], event["sequence"], "fixture", row["workstream_id"]))
        if self.store is None:
            from scripts.pisec.pi_store import PiStore
            with PiStore(self.root / "state") as store:
                attest(store)
        else:
            attest(self.store)
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

    def prompt_eligible(self, agent_observation: AgentObservation) -> bool:
        return bool(agent_observation.identity_usable and agent_observation.state in {"idle", "done"})

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
        row = self.store.conn.execute(
            "SELECT w.kind FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workspace_surface_id=?",
            (surface_id,),
        ).fetchone()
        if row is not None and row["kind"] == "secretary":
            return super().start_agent(surface_id, name, agent_kind)
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
