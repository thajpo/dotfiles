from pathlib import Path
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.pisec.adapters import artifact_document
from scripts.pisec.fence import render_policy
from scripts.pisec.git_objects import GitObjectManager
from scripts.pisec.models import AuthorizationError, ConflictError, NeedsAttentionError, new_id
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import register_project
from scripts.pisec.runtime import report_runtime
from scripts.pisec.secretary import ensure_secretary
from scripts.pisec.workstreams import authorize_apply_workstream, prepare_workstream
from scripts.pisec.harnesses.omp import OmpHarnessAdapter, _copy_user_surface
WEB_SEARCH_DOMAINS = ("html.duckduckgo.com",)
from tests.pisec_fixture import FixtureGitObjects, FixtureHarness, FixtureWorkspace, make_repo


def make_config(root: Path) -> dict:
    gateway = root / "gateway.token"
    gateway.write_text("g" * 48 + "\n")
    os.chmod(gateway, 0o600)
    return {
        "schemaVersion": 3,
        "fencePath": "/usr/bin/false",
        "harness": {
            "id": "omp",
            "config": {
                "executablePath": "/usr/bin/false",
                "gateway": {"baseUrl": "http://127.0.0.1:4000", "tokenFile": str(gateway)},
                "modelRoles": {"default": "openai-codex/model", "task": "deepseek/model", "smol": "deepseek/smol"},
                "network": {"registryDomains": [], "developmentEndpoints": []},
            },
        },
        "workspace": {"id": "fixture", "config": {"socketPath": str(root / "workspace.sock"), "sessionName": "pisec"}},
    }


def render(scope: dict, root: Path, agent: Path, config: dict, *, baseline=()):
    return render_policy(root / "state", scope, agent, config, harness_home=agent, adapter_replacements={"HARNESS_EXECUTABLE": "/usr/bin/false"}, baseline_domains=baseline)


def with_release(adapter: OmpHarnessAdapter, scope: dict) -> dict:
    release = adapter.build_runtime_release()
    return {**scope, "runtimeReleaseId": "rel_" + release.content_sha256[:32], "runtimeReleaseSha256": release.content_sha256, "runtimeReleaseRoot": release.root_path}


class RuntimeMaterializationTests(unittest.TestCase):
    def test_built_release_isolated_from_later_user_surface_edits(self):
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
                first_release = adapter.build_runtime_release()
                first_scope = {**scope, "runtimeReleaseId": "rel_" + first_release.content_sha256[:32], "runtimeReleaseSha256": first_release.content_sha256, "runtimeReleaseRoot": first_release.root_path}
                source.write_text("second\n")
                artifacts = adapter.materialize_profile(first_scope)
                second_release = adapter.build_runtime_release()
            self.assertEqual((Path(artifacts.harness_home) / "rules" / "custom.md").read_text(), "first\n")
            self.assertNotEqual(first_release.content_sha256, second_release.content_sha256)

    def test_config_validation_and_gateway_only_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root)
            adapter = OmpHarnessAdapter(state_root=root / "state", config=config, policy_renderer=lambda *args, **kwargs: render_policy(*args, **kwargs))
            assigned = root / "assigned"
            assigned.mkdir()
            scope = {"projectId": new_id("prj"), "workstreamId": new_id("ws"), "executionProfile": "secretary-project", "worktreePath": str(assigned)}
            artifacts = adapter.materialize_profile(with_release(adapter, scope))
            models = json.loads((Path(artifacts.harness_home) / "models.yml").read_text())
            self.assertEqual(set(models["providers"]), {"openai-codex", "deepseek"})
            for provider in models["providers"].values():
                self.assertEqual(provider["baseUrl"], "http://127.0.0.1:4000")
                self.assertEqual(provider["transport"], "pi-native")
                self.assertEqual(provider["apiKey"], "g" * 48)
            overlay = json.loads((Path(artifacts.harness_home) / "config.yml").read_text())
            self.assertTrue(overlay["mcp"]["enableProjectConfig"])
            self.assertTrue(overlay["web_search"]["enabled"])
            self.assertEqual(overlay["tools"]["approvalMode"], "yolo")
            self.assertEqual(Path(artifacts.launch_secret_path).stat().st_mode & 0o777, 0o600)
            self.assertFalse((Path(artifacts.harness_home) / "extensions" / "herdr-omp-agent-state.ts").exists())
            self.assertFalse((Path(artifacts.harness_home) / "agent" / "extensions" / "herdr-omp-agent-state.ts").exists())
            self.assertEqual(Path(artifacts.adapter_data["extensionPath"]).name, "pisec.ts")

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
            self.assertEqual((destination / "extensions" / "custom.ts").read_text(), "custom extension\n")

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
            released_scope = with_release(adapter, scope)
            self.assertNotEqual(adapter.desired_generation(released_scope), adapter.desired_generation({**released_scope, "pythonEnv": str(env_dir)}))

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
            released_scope = with_release(adapter, scope)
            first = adapter.materialize_profile(released_scope)
            custom = Path(first.harness_home) / "custom.txt"
            custom.write_text("preserve\n")
            second = adapter.materialize_profile(released_scope)
            self.assertEqual(Path(first.launch_secret_path).read_text(), Path(second.launch_secret_path).read_text())
            self.assertEqual(first.runtime_token_sha256, second.runtime_token_sha256)
            self.assertEqual(custom.read_text(), "preserve\n")
            overlay = json.loads((Path(first.harness_home) / "config.yml").read_text())
            self.assertTrue((Path(first.harness_home) / "agents" / "pisec-web-research.md").is_file())
            self.assertTrue((Path(first.harness_home) / "agent" / "agents" / "pisec-web-research.md").is_file())
            self.assertTrue(overlay["web_search"]["enabled"])
            self.assertEqual(overlay["tools"]["approvalMode"], "yolo")
            self.assertEqual(overlay["providers"]["webSearchOrder"], ["duckduckgo"])

    def test_private_object_store_keeps_one_way_alternate_and_removes_legacy_pisec_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            common = root / "repo.git" / "objects"
            (common / "info").mkdir(parents=True)
            os.chmod(common, 0o755)
            os.chmod(common / "info", 0o755)
            project_id = new_id("prj")
            workstream_id = new_id("ws")
            private = state / "git-objects" / project_id / workstream_id / "objects"
            legacy_sibling = state / "git-objects" / project_id / new_id("ws") / "objects"
            external = root / "external-objects"
            external.mkdir()
            (common / "info" / "alternates").write_text(f"{external}\n{private}\n{legacy_sibling}\n")
            os.chmod(common / "info" / "alternates", 0o600)
            scope = {"projectId": project_id, "workstreamId": workstream_id, "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common)}
            manager = GitObjectManager(state_root=state)
            first = manager.materialize(scope)
            second = manager.materialize(scope)
            self.assertEqual(first, second)
            self.assertEqual(first["object_dir"], str(private))
            self.assertEqual((private / "info" / "alternates").read_text(), str(common.resolve()) + "\n")
            self.assertEqual((common / "info" / "alternates").read_text().splitlines(), [str(external)])

    def test_common_object_directory_group_write_is_tightened_not_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            common = root / "repo.git" / "objects"
            (common / "info").mkdir(parents=True)
            os.chmod(common, 0o775)
            os.chmod(common / "info", 0o775)
            project_id = new_id("prj")
            workstream_id = new_id("ws")
            private = state / "git-objects" / project_id / workstream_id / "objects"
            scope = {"projectId": project_id, "workstreamId": workstream_id, "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common)}
            manager = GitObjectManager(state_root=state)
            first = manager.materialize(scope)
            self.assertEqual(first["object_dir"], str(private))
            self.assertFalse((common / "info" / "alternates").exists())
            self.assertEqual(os.stat(common).st_mode & 0o022, 0)
            self.assertEqual(os.stat(common / "info").st_mode & 0o022, 0)
            self.assertEqual((private / "info" / "alternates").read_text().strip(), str(common.resolve()))

    def test_common_object_directory_symlink_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            real = root / "real-objects"
            real.mkdir()
            common = root / "repo.git" / "objects"
            common.parent.mkdir()
            common.symlink_to(real, target_is_directory=True)
            project_id = new_id("prj")
            workstream_id = new_id("ws")
            private = state / "git-objects" / project_id / workstream_id / "objects"
            scope = {"projectId": project_id, "workstreamId": workstream_id, "privateGitObjectDir": str(private), "gitCommonObjectDir": str(common)}
            manager = GitObjectManager(state_root=state)
            with self.assertRaises(NeedsAttentionError):
                manager.materialize(scope)

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
            released_scope = with_release(adapter, scope)
            artifacts = adapter.materialize_profile(released_scope)
            launcher = adapter.commit_launch_binding(released_scope, artifacts, workspace_session_name="main", workspace_id="w1", workspace_view_id="w1:t1", workspace_surface_id="w1:p1")
            descriptor_path = launcher.parent / "binding.json"
            document = json.loads(descriptor_path.read_text())
            self.assertEqual(document["schemaVersion"], 3)
            self.assertEqual(document["harnessId"], "omp")
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
                "desired_release_id": released_scope["runtimeReleaseId"],
                "desired_generation_sha256": artifacts.generation_sha256,
                "adapter_artifacts_json": artifact_document(adapter.manifest, artifacts),
                "policy_path": artifacts.policy_path,
                "policy_sha256": artifacts.policy_sha256,
            }
            checks = adapter.health_checks(binding, {"workstream_execution_profile": "secretary-project"})
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

    def test_rendered_first_mate_policy_exposes_only_fleet_project_roots(self):
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
            self.assertIn(str(fleet_worktree), allow_read)
            self.assertIn(str(fleet_objects), allow_read)
            self.assertNotIn(str(fleet_worktree.parent), allow_read)
            self.assertNotIn(str(fleet_objects.parent), allow_read)
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

    def test_rendered_secretary_policy_exposes_project_worktrees_and_git_objects_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = root / "state" / "omp" / new_id("ws")
            agent.mkdir(parents=True)
            assigned = root / "assigned"
            assigned.mkdir()
            worktrees = root / "worktrees" / "prj_0123456789abcdef0123456789abcdef"
            git_objects = root / "state" / "git-objects" / "prj_0123456789abcdef0123456789abcdef"
            worktrees.mkdir(parents=True)
            git_objects.mkdir(parents=True)
            scope = {
                "projectId": "prj_0123456789abcdef0123456789abcdef",
                "workstreamId": new_id("ws"),
                "executionProfile": "secretary-project",
                "worktreePath": str(assigned),
                "projectWorktreesDir": str(worktrees),
                "projectGitObjectsDir": str(git_objects),
            }
            policy_path, digest = render(scope, root, agent, make_config(root))
            policy = json.loads(policy_path.read_text())
            self.assertIn(str(worktrees.resolve()), policy["filesystem"]["allowRead"])
            self.assertIn(str(git_objects.resolve()), policy["filesystem"]["allowRead"])
            self.assertNotIn(str(worktrees.resolve()), policy["filesystem"]["allowWrite"])
            self.assertNotIn(str(git_objects.resolve()), policy["filesystem"]["allowWrite"])
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

    def test_rendered_secretary_policy_rejects_relative_project_store_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = root / "state" / "omp" / new_id("ws")
            agent.mkdir(parents=True)
            assigned = root / "assigned"
            assigned.mkdir()
            scope = {"projectId": new_id("prj"), "workstreamId": new_id("ws"), "executionProfile": "secretary-project", "worktreePath": str(assigned), "projectWorktreesDir": "worktrees/prj_x"}
            with self.assertRaises(Exception):
                render(scope, root, agent, make_config(root))

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
            for profile, domains in (("worker-default", list(WEB_SEARCH_DOMAINS)), ("worker-networked", sorted([*WEB_SEARCH_DOMAINS, "*.example.com", "registry.example.com"]))):
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
        agent = state / "agent"
        (agent / "sessions").mkdir(parents=True)
        os.chmod(agent / "sessions", 0o700)
        os.chmod(agent, 0o700)
        xdg = {name: agent / "xdg" / name for name in ("data", "state", "cache", "config")}
        for path in xdg.values():
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        plugin_root = xdg["data"] / "omp" / "plugins"
        plugin_root.mkdir(parents=True)
        os.chmod(plugin_root, 0o700)
        user_config = root / "home" / ".omp" / "agent" / "config.yml"
        user_config.parent.mkdir(parents=True)
        user_config.write_text("tools:\n  approvalMode: yolo\n")
        os.chmod(user_config, 0o600)
        overlay = agent / "config.yml"
        overlay.write_text("{}\n")
        policy = state / "policy.json"
        policy.write_text("{}\n")
        secret = state / "secret"
        secret.write_text("r" * 48 + "\n")
        session = agent / "sessions" / "one.jsonl"
        session.write_text("session\n")
        extension = Path(__file__).resolve().parents[1] / "omp" / "extensions" / "pisec.ts"
        fake_omp = root / "real-omp"
        fake_omp.write_text("#!/bin/sh\nexit 0\n")
        fake_fence = root / "fake-fence"
        fake_fence.write_text("#!/usr/bin/python3\nimport json, os, sys\nprint(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}))\n")
        for path in (overlay, policy, secret, session):
            os.chmod(path, 0o600)
        for path in (fake_omp, fake_fence):
            os.chmod(path, 0o755)
        workstream_id = new_id("ws")
        project_id = new_id("prj")
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
            (workstream_id, "main", workspace_id, view_id, surface_id, "omp", str(agent), str(secret), str(policy), hashlib.sha256(policy.read_bytes()).hexdigest(), "a" * 64, "a" * 64, "a" * 64, None if private is None else str(private), session_kind, session_value, "starting"),
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
            "harnessHome": str(agent),
            "overlayPath": str(overlay),
            "extensionPath": str(extension),
            "policyPath": str(policy),
            "policySha256": hashlib.sha256(policy.read_bytes()).hexdigest(),
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
            "privateGitObjectDir": None if private is None else str(private),
            "gitCommonObjectDir": None if common is None else str(common),
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

    def test_private_binding_selects_descriptor_and_sanitizes_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested, entry, launcher = self.make_shim_binding(root, role="secretary", selected=True)
            environment = os.environ.copy()
            environment.update({"HOME": str(root / "home"), "HERDR_SESSION": "main", "HERDR_PANE_ID": "w1:p1", "SSH_AUTH_SOCK": "/tmp/agent.sock", "OPENAI_API_KEY": "forbidden"})
            result = subprocess.run([str(launcher), f"--resume={root / 'state' / 'agent' / 'sessions' / 'one.jsonl'}"], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            captured = json.loads(result.stdout)
            self.assertIn("--approval-mode=yolo", captured["argv"])
            self.assertIn(entry["extensionPath"], captured["argv"])
            self.assertIn(entry["overlayPath"], captured["argv"])
            self.assertIn(str(Path(entry["harnessHome"]) / "user-config.yml"), captured["argv"])
            self.assertEqual(captured["env"]["PISEC_SESSION_START_SOURCE"], "resume")
            self.assertEqual(captured["argv"][0:3], ["--settings", entry["policyPath"], "--"])
            self.assertEqual(captured["argv"][3], str(root / "real-omp"))
            self.assertEqual(captured["env"]["PISEC_RUNTIME_SOCKET"], entry["runtimeSocketPath"])
            self.assertEqual(captured["env"]["PISEC_SECRETARY_SOCKET"], entry["secretarySocketPath"])
            self.assertNotIn("OPENAI_API_KEY", captured["env"])
            os.chmod(Path(entry["canonicalRoot"]), 0o775)
            unsafe_root = subprocess.run([str(launcher), f"--resume={root / 'state' / 'agent' / 'sessions' / 'one.jsonl'}"], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertNotEqual(unsafe_root.returncode, 0)
            self.assertIn("binding root is unsafe", unsafe_root.stderr)
            os.chmod(Path(entry["canonicalRoot"]), 0o755)
            unsupported_args = subprocess.run([str(launcher), "--shell"], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertNotEqual(unsupported_args.returncode, 0)
            self.assertIn("does not match the selected durable session", unsupported_args.stderr)

    def test_private_binding_rejects_database_surface_drift(self):
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

    def test_private_binding_allows_selected_applied_generation_while_desired_is_newer(self):
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

    def test_private_binding_falls_back_to_applied_generation_after_session_start(self):
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

    def test_private_binding_recovers_from_needs_attention_error_state(self):
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
            result = subprocess.run([str(launcher), f"--resume={root / 'state' / 'agent' / 'sessions' / 'one.jsonl'}"], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            captured = json.loads(result.stdout)
            self.assertIn("--approval-mode=yolo", captured["argv"])
            # busy runtimes remain refused
            connection = sqlite3.connect(root / "state" / "control.db")
            connection.execute("UPDATE workstreams SET provisioning_state='bound'")
            connection.execute("UPDATE runtime_bindings SET observed_state='blocked'")
            connection.commit()
            connection.close()
            busy = subprocess.run([str(launcher), f"--resume={root / 'state' / 'agent' / 'sessions' / 'one.jsonl'}"], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertNotEqual(busy.returncode, 0)
            self.assertIn("durable binding identity", busy.stderr)

    def test_worker_binding_sets_private_git_capabilities(self):
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
            self.assertEqual(captured["argv"][0:5], ["--settings", entry["policyPath"], "--expose-host-path", str(common), "--"])
            self.assertEqual(captured["env"]["GIT_OBJECT_DIRECTORY"], str(private))
            self.assertEqual(captured["env"]["GIT_ALTERNATE_OBJECT_DIRECTORIES"], str(common))
            self.assertEqual(captured["env"]["GIT_CONFIG_COUNT"], "2")
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
        prepared = prepare_workstream(self.store, project_id=project["project_id"], title="Runtime", purpose="Verify runtime", brief="Verify runtime reports.", task_packet=packet, idempotency_key="runtime", harness=self.harness, workspace=self.workspace, work_root=self.root / "worktrees", object_root=self.root / "objects")
        result = authorize_apply_workstream(self.store, scope=prepared["approvalScope"], harness=self.harness, workspace=self.workspace, git_objects=FixtureGitObjects())
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
        value = {"workstreamId": self.workstream_id, "runtimeInstanceId": "instance-1", "seq": 1, "event": "session_start", "reason": None, "state": "starting", "nativeSessionKind": "path", "nativeSessionValue": str(self.session), "startSource": "startup", "surfaceId": self.binding["workspace_surface_id"], "token": self.token}
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

    def test_token_surface_instance_and_session_scope_fail_closed(self):
        for changes in ({"token": "x" * 48}, {"surfaceId": "other"}, {"nativeSessionValue": "/tmp/escape"}):
            with self.subTest(changes=changes), self.assertRaises((AuthorizationError, ValueError)):
                report_runtime(self.store, self.payload(**changes), self.harness, self.workspace)
        report_runtime(self.store, self.payload(), self.harness, self.workspace)
        with self.assertRaises(ConflictError):
            report_runtime(self.store, self.payload(runtimeInstanceId="old", seq=2, event="lifecycle", state="idle", nativeSessionKind=None, nativeSessionValue=None), self.harness, self.workspace)


if __name__ == "__main__":
    unittest.main()
