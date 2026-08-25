from pathlib import Path
import hashlib
import json
import os
import runpy
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.pisec.adapters import RuntimeSurfaceArtifacts, artifact_document
from scripts.pisec.events import append_event_in_transaction
from scripts.pisec.fence import render_policy
from scripts.pisec.models import AuthorizationError, ConflictError, InvalidRequestError, NeedsAttentionError, new_id
from scripts.pisec.pi_store import PiStore
from scripts.pisec.operations import create_operation
from scripts.pisec.projects import register_project
from scripts.pisec.runtime import prepare_runtime_turn, report_runtime, usable_runtime_binding
from scripts.pisec.secretary import ensure_secretary
from scripts.pisec.workstreams import authorize_apply_workstream, prepare_workstream
from scripts.pisec.harnesses.omp import OmpHarnessAdapter, _copy_user_surface
WEB_SEARCH_DOMAINS = ("html.duckduckgo.com",)
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace, make_repo


def make_config(root: Path) -> dict:
    gateway = root / "gateway.token"
    gateway.write_text("g" * 48 + "\n")
    os.chmod(gateway, 0o600)
    executable = root / "omp-fixture"
    executable.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(executable, 0o700)
    return {
        "fencePath": str(executable),
        "schemaVersion": 3,
        "harness": {
            "id": "omp",
            "config": {
                "executablePath": str(executable),
                "gateway": {"baseUrl": "http://127.0.0.1:4000", "tokenFile": str(gateway)},
                "modelRoles": {"default": "openai-codex/model", "task": "deepseek/model", "smol": "deepseek/smol"},
                "network": {"registryDomains": [], "developmentEndpoints": []},
            },
        },
        "workspace": {"id": "fixture", "config": {"socketPath": str(root / "workspace.sock"), "sessionName": "pisec"}},
    }


def render(scope: dict, root: Path, agent: Path, config: dict, *, baseline=()):
    return render_policy(root / "state", scope, agent, config, harness_home=agent, adapter_replacements={"HARNESS_EXECUTABLE": "/usr/bin/false"}, baseline_domains=baseline)


def with_surface(adapter: OmpHarnessAdapter, scope: dict) -> dict:
    target = adapter.state_root / "runtime-current" / adapter.manifest.adapter_id
    test_home = adapter.state_root / "test-home"
    (test_home / ".omp" / "agent").mkdir(parents=True, exist_ok=True)
    with patch("scripts.pisec.harnesses.omp.Path.home", return_value=test_home):
        surface = adapter.current_runtime_surface() if target.exists() else adapter.prepare_runtime_surface()
    return {**scope, "runtimeSurfaceId": "surface_" + surface.content_sha256[:32], "runtimeSurfaceSha256": surface.content_sha256, "runtimeSurfaceRoot": surface.root_path}


def stage_and_activate(adapter: OmpHarnessAdapter, scope: dict):
    surface = adapter.current_runtime_surface()
    stage_root = adapter.state_root / "test-staging" / new_id("op")
    staged = adapter.stage_profile({**scope, "operationId": new_id("op")}, surface, stage_root)
    return adapter.activate_profile(scope, staged)


class RuntimeMaterializationTests(unittest.TestCase):
    def test_cleanup_unseals_readonly_policy_parent_before_unlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            workstream = "ws_" + "0" * 32
            policy = state / "binding-surfaces" / "omp" / workstream / "fence" / (workstream + ".json")
            policy.parent.mkdir(parents=True)
            policy.write_text("policy\n")
            os.chmod(policy.parent, 0o500)
            os.chmod(policy, 0o400)
            adapter = OmpHarnessAdapter(state_root=state, config=make_config(root))

            adapter._remove_state_path(str(policy))

            self.assertFalse(policy.exists())

    def test_surface_isolated_from_later_user_surface_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            rules = home / ".omp" / "agent" / "rules"
            rules.mkdir(parents=True)
            source = rules / "custom.md"
            source.write_text("first\n")
            adapter = OmpHarnessAdapter(state_root=root / "state", config=make_config(root))
            worktree = root / "worktree"
            worktree.mkdir()
            scope = {"projectId": new_id("prj"), "workstreamId": new_id("ws"), "executionProfile": "secretary-project", "worktreePath": str(worktree)}
            with patch("scripts.pisec.harnesses.omp.Path.home", return_value=home):
                first_surface = adapter.prepare_runtime_surface()
                first_scope = {**scope, "runtimeSurfaceId": "surface_" + first_surface.content_sha256[:32], "runtimeSurfaceSha256": first_surface.content_sha256, "runtimeSurfaceRoot": first_surface.root_path}
                source.write_text("second\n")
                artifacts = stage_and_activate(adapter, first_scope)
                second_surface = adapter.current_runtime_surface()
            self.assertEqual((Path(artifacts.adapter_data["agentRoot"]) / "rules" / "custom.md").read_text(), "first\n")
            self.assertEqual(first_surface.content_sha256, second_surface.content_sha256)

    def test_config_validation_and_gateway_only_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root)
            adapter = OmpHarnessAdapter(state_root=root / "state", config=config, policy_renderer=lambda *args, **kwargs: render_policy(*args, **kwargs))
            assigned = root / "assigned"
            assigned.mkdir()
            scope = {"projectId": new_id("prj"), "workstreamId": new_id("ws"), "executionProfile": "secretary-project", "worktreePath": str(assigned)}
            artifacts = stage_and_activate(adapter, with_surface(adapter, scope))
            models = json.loads((Path(artifacts.adapter_data["agentRoot"]) / "models.yml").read_text())
            self.assertEqual(set(models["providers"]), {"openai-codex", "deepseek"})
            for provider in models["providers"].values():
                self.assertEqual(provider["baseUrl"], "http://127.0.0.1:4000")
                self.assertEqual(provider["transport"], "pi-native")
                self.assertEqual(provider["apiKey"], "g" * 48)
            overlay = json.loads((Path(artifacts.adapter_data["agentRoot"]) / "config.yml").read_text())
            self.assertTrue(overlay["mcp"]["enableProjectConfig"])
            self.assertTrue(overlay["web_search"]["enabled"])
            self.assertEqual(overlay["tools"]["approvalMode"], "yolo")
            self.assertEqual(Path(artifacts.launch_secret_path).stat().st_mode & 0o777, 0o600)
            self.assertFalse((Path(artifacts.adapter_data["agentRoot"]) / "extensions" / "herdr-omp-agent-state.ts").exists())
            self.assertFalse((Path(artifacts.adapter_data["agentRoot"]) / "agent" / "extensions" / "herdr-omp-agent-state.ts").exists())
            self.assertEqual(Path(artifacts.adapter_data["extensionPath"]).name, "pisec.ts")
            self.assertIn(artifacts.adapter_data["extensionPath"], json.dumps(json.loads(Path(artifacts.policy_path).read_text())))

    def test_user_surface_omits_competing_herdr_omp_lifecycle_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            extensions = home / ".omp" / "agent" / "extensions"
            extensions.mkdir(parents=True)
            (extensions / "herdr-omp-agent-state.ts").write_text("competing lifecycle reporter\n")
            (extensions / "custom.ts").write_text("custom extension\n")
            destination = root / "isolated"
            destination.mkdir()
            with patch("scripts.pisec.harnesses.omp.Path.home", return_value=home):
                _copy_user_surface(destination)
            self.assertFalse((destination / "extensions" / "herdr-omp-agent-state.ts").exists())
            self.assertFalse((destination / "extensions" / "custom.ts").exists())

    def test_desired_generation_reflects_python_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            worktree = root / "worktree"
            workstream_id = new_id("ws")
            branch = f"pisec/{workstream_id}/work"
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", branch, str(worktree), "main"], check=True)
            common = (repo / ".git").resolve()
            private = root / "state" / "git-objects" / new_id("prj") / workstream_id / "objects"
            (private / "info").mkdir(parents=True)
            (private / "pack").mkdir()
            adapter = OmpHarnessAdapter(state_root=root / "state", config=make_config(root))
            scope = {"projectId": new_id("prj"), "workstreamId": workstream_id, "executionProfile": "worker-default", "worktreePath": str(worktree), "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common / "objects"), "branchName": branch, "externalDomains": list(WEB_SEARCH_DOMAINS)}
            env_dir = root / "venv"
            env_dir.mkdir()
            surface_scope = with_surface(adapter, scope)
            self.assertNotEqual(adapter.desired_generation(surface_scope), adapter.desired_generation({**surface_scope, "pythonEnv": str(env_dir)}))

    def test_profile_replay_reuses_runtime_token_and_agent_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            worktree = root / "worktree"
            workstream_id = new_id("ws")
            branch = f"pisec/{workstream_id}/work"
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", branch, str(worktree), "main"], check=True)
            common = (repo / ".git").resolve()
            private = root / "state" / "git-objects" / new_id("prj") / workstream_id / "objects"
            (private / "info").mkdir(parents=True)
            (private / "pack").mkdir()
            adapter = OmpHarnessAdapter(state_root=root / "state", config=make_config(root))
            scope = {"projectId": new_id("prj"), "workstreamId": workstream_id, "executionProfile": "worker-default", "worktreePath": str(worktree), "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common / "objects"), "branchName": branch, "externalDomains": list(WEB_SEARCH_DOMAINS)}
            surface_scope = with_surface(adapter, scope)
            first = stage_and_activate(adapter, surface_scope)
            custom = Path(first.harness_home) / "custom.txt"
            custom.write_text("preserve\n")
            second = stage_and_activate(adapter, surface_scope)
            self.assertEqual(Path(first.launch_secret_path).read_text(), Path(second.launch_secret_path).read_text())
            self.assertEqual(first.runtime_token_sha256, second.runtime_token_sha256)
            self.assertEqual(custom.read_text(), "preserve\n")
            overlay = json.loads((Path(first.adapter_data["agentRoot"]) / "config.yml").read_text())
            self.assertTrue((Path(first.adapter_data["agentRoot"]) / "agents" / "pisec-web-research.md").is_file())
            self.assertTrue((Path(first.adapter_data["agentRoot"]) / "agents" / "pisec-web-research.md").is_file())
            self.assertTrue(overlay["web_search"]["enabled"])
            self.assertEqual(overlay["tools"]["approvalMode"], "yolo")
            self.assertEqual(overlay["providers"]["webSearchOrder"], ["duckduckgo"])

    def test_private_binding_descriptor_is_atomic_and_contains_no_runtime_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / "managed"
            managed.mkdir()
            adapter = OmpHarnessAdapter(state_root=root / "state", config=make_config(root))
            scope = {"projectId": new_id("prj"), "workstreamId": new_id("ws"), "executionProfile": "secretary-project", "worktreePath": str(managed)}
            control_db = root / "state" / "control.db"
            control_db.parent.mkdir(parents=True, exist_ok=True)
            control_db.touch()
            os.chmod(control_db, 0o600)
            surface_scope = with_surface(adapter, scope)
            artifacts = stage_and_activate(adapter, surface_scope)
            launcher = adapter.commit_launch_binding(surface_scope, artifacts, workspace_session_name="main", workspace_id="w1", workspace_view_id="w1:t1", workspace_surface_id="w1:p1")
            descriptor_path = launcher.parent / "binding.json"
            document = json.loads(descriptor_path.read_text())
            launcher_schema = runpy.run_path(str(launcher))["DESCRIPTOR_FIELDS"]
            self.assertEqual(set(document), launcher_schema)
            self.assertEqual(document["schemaVersion"], 3)
            self.assertEqual(document["harnessId"], "omp")
            self.assertEqual(document["runtimeSurfaceId"], surface_scope["runtimeSurfaceId"])
            self.assertEqual(document["canonicalRoot"], str(managed.resolve()))
            self.assertEqual(document["workspaceSessionName"], "main")
            self.assertEqual(document["workspaceSurfaceId"], "w1:p1")
            self.assertNotIn(Path(artifacts.launch_secret_path).read_text().strip(), descriptor_path.read_text())
            self.assertEqual(descriptor_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o700)
            binding = {
                "workstream_id": scope["workstreamId"],
                "workspace_session_name": "main",
                "workspace_id": "w1",
                "workspace_view_id": "w1:t1",
                "workspace_surface_id": "w1:p1",
                "harness_home": artifacts.harness_home,
                "desired_generation_sha256": artifacts.generation_sha256,
                "adapter_artifacts_json": artifact_document(adapter.manifest, artifacts),
                "policy_path": artifacts.policy_path,
                "policy_sha256": artifacts.policy_sha256,
            }
            checks = adapter.health_checks(binding, {"workstream_execution_profile": "secretary-project"})
            health = {check.name: check for check in checks}
            self.assertTrue(health["copied surface"].ok, health["copied surface"].detail)
            self.assertTrue(health["plugin snapshot"].ok, health["plugin snapshot"].detail)
            self.assertTrue(health["overlay and MCP/search"].ok, health["overlay and MCP/search"].detail)
            self.assertTrue(health["policy digest"].ok, health["policy digest"].detail)
            descriptor_check = next(check for check in checks if check.name == "binding descriptor")
            self.assertTrue(descriptor_check.ok, descriptor_check.detail)


class FencePolicyAndShimTests(unittest.TestCase):
    def make_linked_repo(self, root):
        repo = root / "repo"
        worktree = root / "worktree"
        make_repo(repo)
        workstream_id = new_id("ws")
        branch = f"pisec/{workstream_id}/work"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", branch, str(worktree), "main"], check=True)
        common_value = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-common-dir"], check=True, text=True, capture_output=True).stdout.strip()
        common = (repo / common_value).resolve() if not Path(common_value).is_absolute() else Path(common_value).resolve()
        return repo, worktree, common, workstream_id, branch

    def test_rendered_first_mate_policy_does_not_expose_fleet_repositories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assigned = root / "assigned"
            assigned.mkdir()
            agent = root / "agent"
            agent.mkdir()
            fleet_worktree = root / "worktrees" / new_id("prj")
            fleet_objects = root / "git-objects" / new_id("prj")
            scope = {
                "projectId": new_id("prj"),
                "workstreamId": new_id("ws"),
                "executionProfile": "first-mate",
                "worktreePath": str(assigned),
                "fleetProjectWorktrees": [str(fleet_worktree)],
                "fleetProjectGitObjects": [str(fleet_objects)],
            }
            policy_path, digest = render(scope, root, agent, make_config(root))
            policy = json.loads(policy_path.read_text())
            allow_read = policy["filesystem"]["allowRead"]
            self.assertNotIn(str(fleet_worktree), allow_read)
            self.assertNotIn(str(fleet_objects), allow_read)
            self.assertEqual(hashlib.sha256(policy_path.read_bytes()).hexdigest(), digest)

    def test_rendered_worker_policy_validates_with_fence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, worktree, common, workstream_id, branch = self.make_linked_repo(root)
            state = root / "state"
            private = state / "git-objects" / new_id("prj") / workstream_id / "objects"
            (private / "info").mkdir(parents=True)
            (private / "pack").mkdir()
            agent = state / "omp" / workstream_id
            agent.mkdir(parents=True)
            scope = {"projectId": new_id("prj"), "workstreamId": workstream_id, "executionProfile": "worker-default", "worktreePath": str(worktree), "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common / "objects"), "branchName": branch, "externalDomains": list(WEB_SEARCH_DOMAINS)}
            policy_path, digest = render(scope, root, agent, make_config(root), baseline=WEB_SEARCH_DOMAINS)
            policy = json.loads(policy_path.read_text())
            self.assertEqual(policy["network"]["allowedDomains"], list(WEB_SEARCH_DOMAINS))
            self.assertEqual(policy["network"]["allowLocalOutboundPorts"], [4000])
            self.assertEqual(policy["command"]["runtimeExecPolicy"], "argv")
            self.assertEqual(hashlib.sha256(policy_path.read_bytes()).hexdigest(), digest)
            checked = subprocess.run(["fence", "config", "show", "--settings", str(policy_path)], text=True, capture_output=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_rendered_worker_policy_strips_linux_only_keys_on_macos(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, worktree, common, workstream_id, branch = self.make_linked_repo(root)
            state = root / "state"
            private = state / "git-objects" / new_id("prj") / workstream_id / "objects"
            (private / "info").mkdir(parents=True)
            (private / "pack").mkdir()
            agent = state / "omp" / workstream_id
            agent.mkdir(parents=True)
            scope = {"projectId": new_id("prj"), "workstreamId": workstream_id, "executionProfile": "worker-default", "worktreePath": str(worktree), "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common / "objects"), "branchName": branch, "externalDomains": list(WEB_SEARCH_DOMAINS)}
            with patch("scripts.pisec.fence.is_macos", return_value=True):
                policy_path, digest = render(scope, root, agent, make_config(root), baseline=WEB_SEARCH_DOMAINS)
            policy = json.loads(policy_path.read_text())
            self.assertNotIn("devices", policy)
            self.assertNotIn("allowLocalOutboundPorts", policy["network"])
            self.assertEqual(policy["network"]["allowedDomains"], list(WEB_SEARCH_DOMAINS))
            self.assertIn("command", policy)
            self.assertEqual(hashlib.sha256(policy_path.read_bytes()).hexdigest(), digest)

    def test_rendered_worker_policy_exposes_approved_data_dirs_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, worktree, common, workstream_id, branch = self.make_linked_repo(root)
            state = root / "state"
            project_id = new_id("prj")
            private = state / "git-objects" / project_id / workstream_id / "objects"
            (private / "info").mkdir(parents=True)
            (private / "pack").mkdir()
            agent = state / "omp" / workstream_id
            agent.mkdir(parents=True)
            data_dir = worktree / "data"
            data_dir.mkdir()
            (data_dir / "jobos.db").write_text("x")
            scope = {"projectId": project_id, "workstreamId": workstream_id, "executionProfile": "worker-default", "worktreePath": str(worktree), "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common / "objects"), "branchName": branch, "externalDomains": list(WEB_SEARCH_DOMAINS), "dataDirs": [str(data_dir)]}
            policy_path, digest = render(scope, root, agent, make_config(root), baseline=WEB_SEARCH_DOMAINS)
            policy = json.loads(policy_path.read_text())
            self.assertIn(str(data_dir.resolve()), policy["filesystem"]["allowRead"])
            self.assertNotIn(str(data_dir.resolve()), policy["filesystem"]["allowWrite"])
            self.assertEqual(hashlib.sha256(policy_path.read_bytes()).hexdigest(), digest)
            checked = subprocess.run(["fence", "config", "show", "--settings", str(policy_path)], text=True, capture_output=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_rendered_worker_policy_exposes_approved_python_env_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, worktree, common, workstream_id, branch = self.make_linked_repo(root)
            state = root / "state"
            project_id = new_id("prj")
            private = state / "git-objects" / project_id / workstream_id / "objects"
            (private / "info").mkdir(parents=True)
            (private / "pack").mkdir()
            agent = state / "omp" / workstream_id
            agent.mkdir(parents=True)
            env_dir = root / "shared-venv"
            env_dir.mkdir()
            (env_dir / "pyvenv.cfg").write_text("home = /usr/bin\n")
            scope = {"projectId": project_id, "workstreamId": workstream_id, "executionProfile": "worker-default", "worktreePath": str(worktree), "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common / "objects"), "branchName": branch, "externalDomains": list(WEB_SEARCH_DOMAINS), "pythonEnv": str(env_dir)}
            policy_path, digest = render(scope, root, agent, make_config(root), baseline=WEB_SEARCH_DOMAINS)
            policy = json.loads(policy_path.read_text())
            self.assertIn(str(env_dir.resolve()), policy["filesystem"]["allowRead"])
            self.assertNotIn(str(env_dir.resolve()), policy["filesystem"]["allowWrite"])
            self.assertEqual(hashlib.sha256(policy_path.read_bytes()).hexdigest(), digest)

    def test_rendered_worker_policy_omits_python_env_when_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, worktree, common, workstream_id, branch = self.make_linked_repo(root)
            state = root / "state"
            project_id = new_id("prj")
            private = state / "git-objects" / project_id / workstream_id / "objects"
            (private / "info").mkdir(parents=True)
            (private / "pack").mkdir()
            agent = state / "omp" / workstream_id
            agent.mkdir(parents=True)
            scope = {"projectId": project_id, "workstreamId": workstream_id, "executionProfile": "worker-default", "worktreePath": str(worktree), "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common / "objects"), "branchName": branch, "externalDomains": list(WEB_SEARCH_DOMAINS)}
            policy_path, _ = render(scope, root, agent, make_config(root), baseline=WEB_SEARCH_DOMAINS)
            policy = json.loads(policy_path.read_text())
            for entry in policy["filesystem"]["allowRead"]:
                self.assertNotIn("venv", entry)

    def test_rendered_worker_policy_rejects_symlinked_python_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, worktree, common, workstream_id, branch = self.make_linked_repo(root)
            state = root / "state"
            project_id = new_id("prj")
            private = state / "git-objects" / project_id / workstream_id / "objects"
            (private / "info").mkdir(parents=True)
            (private / "pack").mkdir()
            agent = state / "omp" / workstream_id
            agent.mkdir(parents=True)
            real_env = root / "real-venv"
            real_env.mkdir()
            env_link = root / "venv-link"
            env_link.symlink_to(real_env, target_is_directory=True)
            scope = {"projectId": project_id, "workstreamId": workstream_id, "executionProfile": "worker-default", "worktreePath": str(worktree), "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common / "objects"), "branchName": branch, "externalDomains": list(WEB_SEARCH_DOMAINS), "pythonEnv": str(env_link)}
            with self.assertRaises(NeedsAttentionError):
                render(scope, root, agent, make_config(root), baseline=WEB_SEARCH_DOMAINS)

    def test_rendered_worker_policy_expands_venv_interpreter_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, worktree, common, workstream_id, branch = self.make_linked_repo(root)
            state = root / "state"
            project_id = new_id("prj")
            private = state / "git-objects" / project_id / workstream_id / "objects"
            (private / "info").mkdir(parents=True)
            (private / "pack").mkdir()
            agent = state / "omp" / workstream_id
            agent.mkdir(parents=True)
            interpreter = root / "interpreter"
            (interpreter / "bin").mkdir(parents=True)
            env_dir = root / "venv"
            env_dir.mkdir()
            (env_dir / "pyvenv.cfg").write_text(f"home = {interpreter / 'bin'}\n")
            scope = {"projectId": project_id, "workstreamId": workstream_id, "executionProfile": "worker-default", "worktreePath": str(worktree), "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common / "objects"), "branchName": branch, "externalDomains": list(WEB_SEARCH_DOMAINS), "pythonEnv": str(env_dir)}
            policy_path, digest = render(scope, root, agent, make_config(root), baseline=WEB_SEARCH_DOMAINS)
            policy = json.loads(policy_path.read_text())
            self.assertIn(str(env_dir.resolve()), policy["filesystem"]["allowRead"])
            self.assertIn(str(interpreter.resolve()), policy["filesystem"]["allowRead"])
            self.assertNotIn(str(env_dir.resolve()), policy["filesystem"]["allowWrite"])
            self.assertNotIn(str(interpreter.resolve()), policy["filesystem"]["allowWrite"])
            self.assertEqual(hashlib.sha256(policy_path.read_bytes()).hexdigest(), digest)

    def test_rendered_secretary_policy_omits_project_stores_when_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = root / "state" / "omp" / new_id("ws")
            agent.mkdir(parents=True)
            assigned = root / "assigned"
            assigned.mkdir()
            scope = {"projectId": new_id("prj"), "workstreamId": new_id("ws"), "executionProfile": "secretary-project", "worktreePath": str(assigned)}
            policy_path, _ = render(scope, root, agent, make_config(root))
            policy = json.loads(policy_path.read_text())
            for entry in policy["filesystem"]["allowRead"]:
                self.assertNotIn("worktrees", entry)
                self.assertNotIn("git-objects", entry)

    def test_rendered_worker_policy_omits_project_stores(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            worktree = root / "worktree"
            workstream_id = new_id("ws")
            branch = f"pisec/{workstream_id}/work"
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", branch, str(worktree), "main"], check=True)
            common = (repo / ".git").resolve()
            private = root / "state" / "git-objects" / new_id("prj") / workstream_id / "objects"
            (private / "info").mkdir(parents=True)
            (private / "pack").mkdir()
            stores = root / "stores"
            stores.mkdir()
            agent = root / "state" / "omp" / workstream_id
            agent.mkdir(parents=True)
            scope = {"projectId": new_id("prj"), "workstreamId": workstream_id, "executionProfile": "worker-default", "worktreePath": str(worktree), "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common / "objects"), "branchName": branch, "externalDomains": list(WEB_SEARCH_DOMAINS), "projectWorktreesDir": str(stores / "worktrees"), "projectGitObjectsDir": str(stores / "objects")}
            policy_path, _ = render(scope, root, agent, make_config(root), baseline=WEB_SEARCH_DOMAINS)
            policy = json.loads(policy_path.read_text())
            for entry in policy["filesystem"]["allowRead"]:
                self.assertNotIn(str(stores), entry)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, worktree, common, workstream_id, branch = self.make_linked_repo(root)
            state = root / "state"
            project_id = new_id("prj")
            private = state / "git-objects" / project_id / workstream_id / "objects"
            (private / "info").mkdir(parents=True)
            (private / "pack").mkdir()
            agent = state / "omp" / workstream_id
            agent.mkdir(parents=True)
            env_dir = root / "venv"
            env_dir.mkdir()
            (env_dir / "pyvenv.cfg").write_text("home = /nonexistent/interpreter/bin\n")
            scope = {"projectId": project_id, "workstreamId": workstream_id, "executionProfile": "worker-default", "worktreePath": str(worktree), "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common / "objects"), "branchName": branch, "externalDomains": list(WEB_SEARCH_DOMAINS), "pythonEnv": str(env_dir)}
            with self.assertRaises(NeedsAttentionError):
                render(scope, root, agent, make_config(root), baseline=WEB_SEARCH_DOMAINS)

    def test_rendered_worker_policy_rejects_symlinked_interpreter_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, worktree, common, workstream_id, branch = self.make_linked_repo(root)
            state = root / "state"
            project_id = new_id("prj")
            private = state / "git-objects" / project_id / workstream_id / "objects"
            (private / "info").mkdir(parents=True)
            (private / "pack").mkdir()
            agent = state / "omp" / workstream_id
            agent.mkdir(parents=True)
            real_interpreter = root / "real-interpreter"
            (real_interpreter / "bin").mkdir(parents=True)
            interpreter_link = root / "interpreter-link"
            interpreter_link.symlink_to(real_interpreter, target_is_directory=True)
            env_dir = root / "venv"
            env_dir.mkdir()
            (env_dir / "pyvenv.cfg").write_text(f"home = {interpreter_link / 'bin'}\n")
            scope = {"projectId": project_id, "workstreamId": workstream_id, "executionProfile": "worker-default", "worktreePath": str(worktree), "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common / "objects"), "branchName": branch, "externalDomains": list(WEB_SEARCH_DOMAINS), "pythonEnv": str(env_dir)}
            with self.assertRaises(NeedsAttentionError):
                render(scope, root, agent, make_config(root), baseline=WEB_SEARCH_DOMAINS)

    def test_all_profiles_are_gateway_only_and_schema_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, worktree, common, workstream_id, branch = self.make_linked_repo(root)
            state = root / "state"
            agent = state / "omp" / workstream_id
            agent.mkdir(parents=True)
            project_id = new_id("prj")
            private = state / "git-objects" / project_id / workstream_id / "objects"
            (private / "info").mkdir(parents=True)
            (private / "pack").mkdir()
            base = {"projectId": project_id, "workstreamId": workstream_id, "worktreePath": str(worktree), "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common / "objects"), "branchName": branch}
            for profile, domains in (("worker-default", list(WEB_SEARCH_DOMAINS)),):
                scope = {**base, "executionProfile": profile, "externalDomains": domains}
                policy_path, digest = render(scope, root, agent, make_config(root), baseline=WEB_SEARCH_DOMAINS)
                policy = json.loads(policy_path.read_text())
                self.assertEqual(policy["network"]["defaultAction"], "deny")
                self.assertEqual(policy["network"]["allowedDomains"], domains)
                self.assertEqual(policy["network"]["allowLocalOutboundPorts"], [4000])
                self.assertEqual(hashlib.sha256(policy_path.read_bytes()).hexdigest(), digest)
                checked = subprocess.run(["fence", "config", "show", "--settings", str(policy_path)], text=True, capture_output=True)
                self.assertEqual(checked.returncode, 0, checked.stderr)
            assigned = root / "assigned"
            assigned.mkdir()
            secretary_scope = {"projectId": project_id, "workstreamId": new_id("ws"), "executionProfile": "secretary-project", "worktreePath": str(assigned)}
            policy_path, _ = render(secretary_scope, root, agent, make_config(root))
            policy = json.loads(policy_path.read_text())
            self.assertEqual(policy["network"]["allowedDomains"], ["*"])
            self.assertIn(str(assigned.resolve()), policy["filesystem"]["allowRead"])
            self.assertIn(str(assigned.resolve()), policy["filesystem"]["allowWrite"])
            with self.assertRaises(Exception):
                render({**base, "executionProfile": "worker-default", "externalDomains": sorted([*WEB_SEARCH_DOMAINS, "example.com"])}, root, agent, make_config(root), baseline=WEB_SEARCH_DOMAINS)

    def test_real_fence_allows_assigned_write_and_denies_primary_checkout(self):
        features = subprocess.run(["fence", "--linux-features"], text=True, capture_output=True, check=True).stdout
        network_row = next((line.strip().lower() for line in features.splitlines() if line.strip().lower().startswith("network namespace")), "")
        if " ok " not in f" {network_row} ":
            self.skipTest("Fence reports unavailable network namespaces; installer must refuse this host")
        with tempfile.TemporaryDirectory(dir=Path.home()) as tmp:
            root = Path(tmp)
            repo, worktree, common, workstream_id, branch = self.make_linked_repo(root)
            state = root / "state"
            project_id = new_id("prj")
            private = state / "git-objects" / project_id / workstream_id / "objects"
            (private / "info").mkdir(parents=True)
            (private / "pack").mkdir()
            agent = state / "omp" / workstream_id
            agent.mkdir(parents=True)
            scope = {"projectId": project_id, "workstreamId": workstream_id, "executionProfile": "worker-default", "worktreePath": str(worktree), "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common / "objects"), "branchName": branch, "externalDomains": list(WEB_SEARCH_DOMAINS)}
            policy_path, _ = render(scope, root, agent, make_config(root), baseline=WEB_SEARCH_DOMAINS)
            allowed = subprocess.run(["fence", "--settings", str(policy_path), "--", "python3", "-c", "from pathlib import Path; Path('owned').write_text('ok')"], cwd=worktree, text=True, capture_output=True)
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            denied = subprocess.run(["fence", "--settings", str(policy_path), "--", "python3", "-c", f"from pathlib import Path; Path({str(repo / 'blocked')!r}).write_text('bad')"], cwd=worktree, text=True, capture_output=True)
            self.assertNotEqual(denied.returncode, 0)
            self.assertFalse((repo / "blocked").exists())
            command_denied = subprocess.run(["fence", "--settings", str(policy_path), "--", "git", "push", "origin", "main"], cwd=worktree, text=True, capture_output=True)
            self.assertNotEqual(command_denied.returncode, 0)
            curl_denied = subprocess.run(["fence", "--settings", str(policy_path), "--", "curl", "--version"], cwd=worktree, text=True, capture_output=True)
            self.assertNotEqual(curl_denied.returncode, 0)

    def test_omp_binding_temp_root_keeps_fence_socket_paths_short(self):
        workstream_id = "ws_" + "a" * 32
        temp_root = Path.home() / ".local" / "state" / "pisec" / "tmp" / workstream_id
        argv_exec_socket = temp_root / "fence-argv-exec-1234567890" / "control.sock"
        self.assertLessEqual(len(str(argv_exec_socket)), 107)
        self.assertFalse(str(temp_root).startswith("/tmp/"))

    def make_shim_binding(
        self,
        root: Path,
        *,
        role: str,
        private: Path | None = None,
        common: Path | None = None,
        selected: bool = False,
    ) -> tuple[Path, dict, Path]:
        managed = root / "repo"
        nested = managed / "nested"
        nested.mkdir(parents=True)
        os.chmod(managed, 0o755)
        state = root / "state"
        launch_dir = state / "launchers"
        launch_dir.mkdir(parents=True)
        agent = state / "surface"
        state_binding = state / "binding-state"
        (state_binding / "sessions").mkdir(parents=True)
        os.chmod(state_binding / "sessions", 0o700)
        os.chmod(state_binding, 0o700)
        xdg = {"data": agent / "xdg" / "data", "state": state_binding / "xdg" / "state", "cache": state_binding / "xdg" / "cache", "config": state_binding / "xdg" / "config"}
        for path in xdg.values():
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        os.chmod(agent, 0o700)
        plugin_root = xdg["data"] / "omp" / "plugins"
        plugin_root.mkdir(parents=True)
        os.chmod(plugin_root, 0o700)
        user_config = root / "home" / ".omp" / "agent" / "config.yml"
        user_config.parent.mkdir(parents=True)
        user_config.write_text("tools:\n  approvalMode: yolo\n")
        os.chmod(user_config, 0o600)
        overlay = agent / "config.yml"
        overlay.write_text("{}\n")
        policy = agent / "policy.json"
        policy.write_text("{}\n")
        secret = state / "secret"
        secret.write_text("r" * 48 + "\n")
        session = state_binding / "sessions" / "one.jsonl"
        session.write_text("session\n")
        extension = Path(__file__).resolve().parents[1] / "omp" / "extensions" / "pisec.ts"
        fake_omp = root / "real-omp"
        fake_omp.write_text("#!/bin/sh\nexit 0\n")
        fake_fence = root / "fake-fence"
        fake_fence.write_text("#!/usr/bin/python3\nimport json, os, sys\nprint(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}))\n")
        for path in (overlay, policy):
            os.chmod(path, 0o400)
        for path in (secret, session):
            os.chmod(path, 0o600)
        for path in (fake_omp, fake_fence):
            os.chmod(path, 0o755)
        for path in (agent, *sorted(agent.rglob("*"))):
            os.chmod(path, 0o500 if path.is_dir() else 0o400)
        workstream_id = new_id("ws")
        project_id = new_id("prj")
        temp_root = state / "tmp" / workstream_id
        temp_root.mkdir(parents=True)
        os.chmod(temp_root, 0o700)
        workspace_id, view_id, surface_id = "w1", "w1:t1", "w1:p1"
        if private is not None:
            private.mkdir(parents=True, exist_ok=True)
            os.chmod(private, 0o700)
        if common is not None:
            common.mkdir(parents=True, exist_ok=True)
            os.chmod(common, 0o755)
        control_db = state / "control.db"
        connection = sqlite3.connect(control_db)
        connection.executescript(
            """
            CREATE TABLE workstreams (
                workstream_id TEXT PRIMARY KEY,
                project_id TEXT,
                kind TEXT,
                execution_profile TEXT,
                worktree_path TEXT,
                desired_state TEXT,
                provisioning_state TEXT
            );
            CREATE TABLE runtime_bindings (
                workstream_id TEXT PRIMARY KEY,
                workspace_session_name TEXT,
                workspace_id TEXT,
                workspace_view_id TEXT,
                workspace_surface_id TEXT,
                harness_id TEXT,
                harness_home TEXT,
                launch_secret_path TEXT,
                policy_path TEXT,
                policy_sha256 TEXT,
                desired_generation_sha256 TEXT,
                applied_generation_sha256 TEXT,
                launch_generation_sha256 TEXT,
                private_git_object_dir TEXT,
                native_session_kind TEXT,
                native_session_value TEXT,
                observed_state TEXT
            );
            """
        )
        os.chmod(control_db, 0o600)
        session_kind = "path" if selected else None
        session_value = str(session) if selected else None
        connection.execute(
            "INSERT INTO workstreams VALUES(?,?,?,?,?,?,?)",
            (workstream_id, project_id, role, "secretary-project" if role == "secretary" else "worker-default", str(managed.resolve()), "active", "bound"),
        )
        connection.execute(
            "INSERT INTO runtime_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (workstream_id, "main", workspace_id, view_id, surface_id, "omp", str(state_binding), str(secret), str(policy), hashlib.sha256(policy.read_bytes()).hexdigest(), "a" * 64, "a" * 64, "a" * 64, None if private is None else str(private), session_kind, session_value, "starting"),
        )
        connection.commit()
        connection.close()
        descriptor = {
            "schemaVersion": 3,
            "harnessId": "omp",
            "stateRoot": str(state.resolve()),
            "controlDbPath": str(control_db.resolve()),
            "workstreamId": workstream_id,
            "projectId": project_id,
            "role": role,
            "executionProfile": "secretary-project" if role == "secretary" else "worker-default",
            "canonicalRoot": str(managed.resolve()),
            "workspaceSessionName": "main",
            "workspaceId": workspace_id,
            "workspaceViewId": view_id,
            "workspaceSurfaceId": surface_id,
            "harnessExecutablePath": str(fake_omp),
            "fencePath": str(fake_fence),
            "harnessHome": str(state_binding),
            "tmpDir": str(state / "tmp" / workstream_id),
            "surfaceRoot": str(agent),
            "agentRoot": str(agent),
            "overlayPath": str(overlay),
            "extensionPath": str(extension),
            "policyPath": str(policy),
            "policySha256": hashlib.sha256(policy.read_bytes()).hexdigest(),
            "runtimeSurfaceId": "surface_fixture",
            "generationSha256": "a" * 64,
            "launchSecretPath": str(secret),
            "xdgDataHome": str(xdg["data"]),
            "xdgStateHome": str(xdg["state"]),
            "xdgCacheHome": str(xdg["cache"]),
            "xdgConfigHome": str(xdg["config"]),
            "pluginRoot": str(plugin_root),
            "runtimeSocketPath": str(root / "runtime.sock"),
            "secretarySocketPath": str(root / "secretary.sock") if role == "secretary" else None,
            "fleetSocketPath": None,
        }
        descriptor["identitySha256"] = hashlib.sha256(json.dumps({key: value for key, value in descriptor.items()}, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        descriptor_dir = launch_dir / workstream_id
        descriptor_dir.mkdir()
        descriptor_path = descriptor_dir / "binding.json"
        os.chmod(descriptor_dir, 0o700)
        descriptor_path.write_text(json.dumps(descriptor, sort_keys=True) + "\n")
        os.chmod(descriptor_path, 0o600)
        launcher = descriptor_dir / "omp"
        template = Path(__file__).resolve().parents[1] / "pisec" / "runtime-bin" / "omp"
        launcher.write_text(template.read_text())
        os.chmod(launcher, 0o700)
        os.chmod(state, 0o700)
        os.chmod(launch_dir, 0o700)
        return nested, descriptor, launcher

    def test_binding_selects_descriptor_and_sanitizes_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested, entry, launcher = self.make_shim_binding(root, role="secretary", selected=True)
            environment = os.environ.copy()
            environment.update({"HOME": str(root / "home"), "HERDR_SESSION": "main", "HERDR_PANE_ID": "w1:p1", "SSH_AUTH_SOCK": "/tmp/agent.sock", "OPENAI_API_KEY": "forbidden"})
            result = subprocess.run([str(launcher), f"--resume={Path(entry['harnessHome']) / 'sessions' / 'one.jsonl'}"], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            captured = json.loads(result.stdout)
            self.assertIn("--approval-mode=yolo", captured["argv"])
            self.assertIn(entry["extensionPath"], captured["argv"])
            self.assertIn(entry["overlayPath"], captured["argv"])
            self.assertNotIn(str(Path(entry["harnessHome"]) / "user-config.yml"), captured["argv"])
            self.assertEqual(captured["env"]["PISEC_SESSION_START_SOURCE"], "resume")
            self.assertEqual(captured["argv"][0:3], ["--settings", entry["policyPath"], "--"])
            self.assertEqual(captured["argv"][3], str(root / "real-omp"))
            self.assertEqual(captured["env"]["PISEC_RUNTIME_SOCKET"], entry["runtimeSocketPath"])
            self.assertEqual(captured["env"]["PISEC_SECRETARY_SOCKET"], entry["secretarySocketPath"])
            self.assertEqual(captured["env"]["PI_CODING_AGENT_DIR"], entry["harnessHome"])
            self.assertNotIn("OPENAI_API_KEY", captured["env"])
            os.chmod(Path(entry["canonicalRoot"]), 0o775)
            unsafe_root = subprocess.run([str(launcher), f"--resume={Path(entry['harnessHome']) / 'sessions' / 'one.jsonl'}"], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertNotEqual(unsafe_root.returncode, 0)
            self.assertIn("binding root is unsafe", unsafe_root.stderr)
            os.chmod(Path(entry["canonicalRoot"]), 0o755)
            unsupported_args = subprocess.run([str(launcher), "--shell"], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertNotEqual(unsupported_args.returncode, 0)
            self.assertIn("does not match the selected durable session", unsupported_args.stderr)

    def test_binding_rejects_database_surface_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested, entry, launcher = self.make_shim_binding(root, role="secretary")
            connection = sqlite3.connect(root / "state" / "control.db")
            connection.execute("UPDATE runtime_bindings SET workspace_surface_id='w1:p9'")
            connection.commit()
            connection.close()
            environment = os.environ.copy()
            environment.update({"HOME": str(root / "home"), "HERDR_SESSION": "main", "HERDR_PANE_ID": "w1:p1"})
            result = subprocess.run([str(launcher)], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("durable binding identity", result.stderr)

    def test_binding_allows_selected_applied_generation_while_desired_is_newer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested, entry, launcher = self.make_shim_binding(root, role="secretary")
            connection = sqlite3.connect(root / "state" / "control.db")
            connection.execute(
                "UPDATE runtime_bindings SET desired_generation_sha256=?,applied_generation_sha256=?,launch_generation_sha256=?",
                ("b" * 64, "a" * 64, "a" * 64),
            )
            connection.commit()
            connection.close()
            environment = os.environ.copy()
            environment.update({"HOME": str(root / "home"), "HERDR_SESSION": "main", "HERDR_PANE_ID": "w1:p1"})
            result = subprocess.run([str(launcher)], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            captured = json.loads(result.stdout)
            self.assertEqual(captured["env"]["PISEC_RUNTIME_GENERATION"], "a" * 64)

    def test_binding_falls_back_to_applied_generation_after_session_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested, entry, launcher = self.make_shim_binding(root, role="secretary")
            connection = sqlite3.connect(root / "state" / "control.db")
            connection.execute(
                "UPDATE runtime_bindings SET desired_generation_sha256=?,applied_generation_sha256=?,launch_generation_sha256=NULL",
                ("b" * 64, "a" * 64),
            )
            connection.commit()
            connection.close()
            environment = os.environ.copy()
            environment.update({"HOME": str(root / "home"), "HERDR_SESSION": "main", "HERDR_PANE_ID": "w1:p1"})
            result = subprocess.run([str(launcher)], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            captured = json.loads(result.stdout)
            self.assertEqual(captured["env"]["PISEC_RUNTIME_GENERATION"], "a" * 64)

    def test_binding_recovers_from_needs_attention_error_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested, entry, launcher = self.make_shim_binding(root, role="secretary", selected=True)
            connection = sqlite3.connect(root / "state" / "control.db")
            connection.execute("UPDATE workstreams SET provisioning_state='needs_attention'")
            connection.execute("UPDATE runtime_bindings SET observed_state='error'")
            connection.commit()
            connection.close()
            environment = os.environ.copy()
            environment.update({"HOME": str(root / "home"), "HERDR_SESSION": "main", "HERDR_PANE_ID": "w1:p1"})
            result = subprocess.run([str(launcher), f"--resume={Path(entry['harnessHome']) / 'sessions' / 'one.jsonl'}"], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            captured = json.loads(result.stdout)
            self.assertIn("--approval-mode=yolo", captured["argv"])
            # busy runtimes remain refused
            connection = sqlite3.connect(root / "state" / "control.db")
            connection.execute("UPDATE workstreams SET provisioning_state='bound'")
            connection.execute("UPDATE runtime_bindings SET observed_state='blocked'")
            connection.commit()
            connection.close()
            busy = subprocess.run([str(launcher), f"--resume={Path(entry['harnessHome']) / 'sessions' / 'one.jsonl'}"], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertNotEqual(busy.returncode, 0)
            self.assertIn("durable binding identity", busy.stderr)

    def test_worker_binding_sets_sanitized_git_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "state" / "objects"
            common = root / "common"
            nested, entry, launcher = self.make_shim_binding(root, role="worker", private=private, common=common)
            environment = os.environ.copy()
            environment.update({"HOME": str(root / "home"), "HERDR_SESSION": "main", "HERDR_PANE_ID": "w1:p1"})
            result = subprocess.run([str(launcher)], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            captured = json.loads(result.stdout)
            self.assertIn("--approval-mode=yolo", captured["argv"])
            self.assertEqual(captured["argv"][0:3], ["--settings", entry["policyPath"], "--"])
            self.assertNotIn("GIT_OBJECT_DIRECTORY", captured["env"])
            self.assertNotIn("GIT_ALTERNATE_OBJECT_DIRECTORIES", captured["env"])
            self.assertEqual(captured["env"]["GIT_CONFIG_COUNT"], "6")
            self.assertNotIn("PISEC_SECRETARY_SOCKET", captured["env"])

class RuntimeReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        repo = self.root / "repo"
        make_repo(repo)
        self.store = PiStore(self.root / "state")
        project = register_project(self.store, repo)
        self.harness = FixtureHarness(self.root)
        self.workspace = FixtureWorkspace(self.root, self.store)
        ensure_secretary(self.store, project["project_id"], self.harness, self.workspace)
        packet = {"schemaVersion": 1, "outcome": "Runtime report behavior is verified.", "boundaries": ["Change runtime reporting only."], "acceptance": ["Runtime reports are monotonic."], "openQuestions": [], "evidence": ["Test output."]}
        prepared = prepare_workstream(self.store, project_id=project["project_id"], title="Runtime", purpose="Verify runtime", brief="Verify runtime reports.", task_packet=packet, idempotency_key="runtime", harness=self.harness, workspace=self.workspace, work_root=self.root / "worktrees")
        result = authorize_apply_workstream(self.store, scope=prepared["approvalScope"], harness=self.harness, workspace=self.workspace)
        self.workstream_id = result["workstream"]["workstream_id"]
        binding = self.store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()
        self.binding = dict(binding)
        self.token = Path(self.binding["launch_secret_path"]).read_text().strip()
        self.session = Path(self.binding["harness_home"]) / "sessions" / "one.jsonl"
        self.session.write_text("session\n")
        self.store.conn.execute("UPDATE runtime_bindings SET runtime_instance_id=NULL,report_seq=0,observed_state='starting' WHERE workstream_id=?", (self.workstream_id,))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def payload(self, **changes):
        value = {"workstreamId": self.workstream_id, "runtimeInstanceId": "instance-1", "seq": 1, "event": "session_start", "reason": None, "state": "starting", "nativeSessionKind": "path", "nativeSessionValue": str(self.session), "startSource": "startup", "surfaceId": self.binding["workspace_surface_id"], "token": self.token, "generation": self.binding["desired_generation_sha256"]}
        value.update(changes)
        return value

    def test_instance_sequence_session_and_release_contract(self):
        first = report_runtime(self.store, self.payload(), self.harness, self.workspace)
        self.assertEqual(first["workspaceReportSeq"], 2)
        report_runtime(self.store, self.payload(seq=2, event="lifecycle", state="blocked", nativeSessionKind=None, nativeSessionValue=None), self.harness, self.workspace)
        final = report_runtime(self.store, self.payload(seq=3, event="session_shutdown", state="stopped", nativeSessionKind=None, nativeSessionValue=None), self.harness, self.workspace)
        self.assertEqual(final["workspaceReportSeq"], 4)
        self.assertEqual([call[0] for call in self.workspace.calls if call[0] in {"report_session", "report_state", "release"}], ["report_session", "report_state", "report_state", "release"])
        with self.assertRaises(ConflictError):
            report_runtime(self.store, self.payload(seq=2, event="lifecycle", state="idle", nativeSessionKind=None, nativeSessionValue=None), self.harness, self.workspace)

    def test_session_start_requires_a_owned_refresh_reservation(self):
        self.store.conn.execute(
            "UPDATE workstreams SET provisioning_state='needs_attention' WHERE workstream_id=?",
            (self.workstream_id,),
        )
        self.store.conn.execute(
            "UPDATE runtime_bindings SET launch_generation_sha256=desired_generation_sha256 WHERE workstream_id=?",
            (self.workstream_id,),
        )
        self.assertIsNotNone(self.store.conn.execute("SELECT launch_generation_sha256 FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()[0])
        with self.assertRaises(AuthorizationError):
            report_runtime(self.store, self.payload(), self.harness, self.workspace)

        operation, _created = create_operation(self.store, kind="runtime.refresh", project_id=self.store.conn.execute("SELECT project_id FROM workstreams WHERE workstream_id=?", (self.workstream_id,)).fetchone()[0], workstream_id=self.workstream_id, idempotency_key="reserved-session-start", request={"workstreamId": self.workstream_id, "desiredGenerationSha256": self.binding["desired_generation_sha256"]})
        self.store.conn.execute("UPDATE runtime_bindings SET refresh_pending=1,refresh_operation_id=?,refresh_started_at='2026-08-25T00:00:00Z' WHERE workstream_id=?", (operation.operation_id, self.workstream_id))
        result = report_runtime(self.store, self.payload(), self.harness, self.workspace)
        self.assertEqual(result["workstreamId"], self.workstream_id)

        self.store.conn.execute("UPDATE runtime_bindings SET launch_generation_sha256=NULL WHERE workstream_id=?", (self.workstream_id,))
        with self.assertRaises(AuthorizationError):
            report_runtime(self.store, self.payload(runtimeInstanceId="instance-2"), self.harness, self.workspace)

    def test_usable_runtime_requires_idle_state_and_exact_session_event(self):
        report_runtime(self.store, self.payload(state="idle"), self.harness, self.workspace)
        turn = {
            "workstreamId": self.workstream_id,
            "runtimeInstanceId": "instance-1",
            "surfaceId": self.binding["workspace_surface_id"],
            "token": self.token,
            "generation": self.binding["desired_generation_sha256"],
            "sessionKey": "runtime-contract-test",
        }
        self.assertTrue(usable_runtime_binding(self.store, self.workstream_id, self.workspace, self.harness))
        project_id = self.store.conn.execute("SELECT project_id FROM workstreams WHERE workstream_id=?", (self.workstream_id,)).fetchone()[0]
        forged = append_event_in_transaction(self.store.conn, kind="runtime.session_started", project_id=project_id, workstream_id=self.workstream_id, payload={"generationSha256": "f" * 64, "reportSeq": 1, "runtimeInstanceId": "forged-runtime"})
        self.store.conn.execute("UPDATE runtime_bindings SET session_start_event_sequence=? WHERE workstream_id=?", (forged["sequence"], self.workstream_id))
        self.assertFalse(usable_runtime_binding(self.store, self.workstream_id, self.workspace, self.harness))
        with self.assertRaises(ConflictError):
            prepare_runtime_turn(self.store, turn, self.workspace, self.harness)

    def test_runtime_turn_rejects_working_binding(self):
        report_runtime(self.store, self.payload(state="idle"), self.harness, self.workspace)
        report_runtime(self.store, self.payload(seq=2, event="lifecycle", state="working", nativeSessionKind=None, nativeSessionValue=None), self.harness, self.workspace)
        with self.assertRaises(ConflictError):
            prepare_runtime_turn(self.store, {"workstreamId": self.workstream_id, "runtimeInstanceId": "instance-1", "surfaceId": self.binding["workspace_surface_id"], "token": self.token, "generation": self.binding["desired_generation_sha256"], "sessionKey": "working-runtime-contract-test"}, self.workspace, self.harness)

    def test_done_is_not_an_authenticated_pisec_runtime_state(self):
        report_runtime(self.store, self.payload(), self.harness, self.workspace)
        with self.assertRaises(InvalidRequestError):
            report_runtime(self.store, self.payload(seq=2, event="lifecycle", state="done", nativeSessionKind=None, nativeSessionValue=None), self.harness, self.workspace)
        self.assertEqual(self.store.conn.execute("SELECT observed_state FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()[0], "starting")

    def test_token_surface_instance_and_session_scope_fail_closed(self):
        for changes in ({"token": "x" * 48}, {"surfaceId": "other"}, {"nativeSessionValue": "/tmp/escape"}):
            with self.subTest(changes=changes), self.assertRaises((AuthorizationError, ValueError)):
                report_runtime(self.store, self.payload(**changes), self.harness, self.workspace)
        report_runtime(self.store, self.payload(), self.harness, self.workspace)
        with self.assertRaises(ConflictError):
            report_runtime(self.store, self.payload(runtimeInstanceId="old", seq=2, event="lifecycle", state="idle", nativeSessionKind=None, nativeSessionValue=None), self.harness, self.workspace)


if __name__ == "__main__":
    unittest.main()
