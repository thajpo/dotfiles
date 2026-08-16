from pathlib import Path
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.pisec.fence import render_policy
from scripts.pisec.git_objects import GitObjectManager
from scripts.pisec.models import AuthorizationError, ConflictError, new_id
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import register_project
from scripts.pisec.runtime import report_runtime
from scripts.pisec.workstreams import authorize_apply_workstream, prepare_workstream
from scripts.pisec.harnesses.omp import OmpHarnessAdapter
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


class RuntimeMaterializationTests(unittest.TestCase):
    def test_config_validation_and_gateway_only_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root)
            adapter = OmpHarnessAdapter(state_root=root / "state", config=config, policy_renderer=lambda *args, **kwargs: render_policy(*args, **kwargs))
            assigned = root / "assigned"
            assigned.mkdir()
            scope = {"projectId": new_id("prj"), "workstreamId": new_id("ws"), "executionProfile": "secretary-project", "worktreePath": str(assigned)}
            artifacts = adapter.materialize_profile(scope)
            models = json.loads((Path(artifacts.harness_home) / "models.yml").read_text())
            self.assertEqual(set(models["providers"]), {"openai-codex", "deepseek"})
            for provider in models["providers"].values():
                self.assertEqual(provider["baseUrl"], "http://127.0.0.1:4000")
                self.assertEqual(provider["transport"], "pi-native")
                self.assertEqual(provider["apiKey"], "g" * 48)
            overlay = json.loads((Path(artifacts.harness_home) / "config.yml").read_text())
            self.assertTrue(overlay["mcp"]["enableProjectConfig"])
            self.assertTrue(overlay["web_search"]["enabled"])
            self.assertEqual(Path(artifacts.launch_secret_path).stat().st_mode & 0o777, 0o600)

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
            first = adapter.materialize_profile(scope)
            custom = Path(first.harness_home) / "custom.txt"
            custom.write_text("preserve\n")
            second = adapter.materialize_profile(scope)
            self.assertEqual(Path(first.launch_secret_path).read_text(), Path(second.launch_secret_path).read_text())
            self.assertEqual(first.runtime_token_sha256, second.runtime_token_sha256)
            self.assertEqual(custom.read_text(), "preserve\n")
            overlay = json.loads((Path(first.harness_home) / "config.yml").read_text())
            self.assertTrue((Path(first.harness_home) / "agents" / "pisec-web-research.md").is_file())
            self.assertTrue((Path(first.harness_home) / "agent" / "agents" / "pisec-web-research.md").is_file())
            self.assertTrue(overlay["web_search"]["enabled"])
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

    def test_launch_map_is_atomic_and_contains_no_runtime_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / "managed"
            managed.mkdir()
            adapter = OmpHarnessAdapter(state_root=root / "state", config=make_config(root))
            scope = {"projectId": new_id("prj"), "workstreamId": new_id("ws"), "executionProfile": "secretary-project", "worktreePath": str(managed)}
            artifacts = adapter.materialize_profile(scope)
            launch_map = adapter.commit_launch_binding(scope, artifacts)
            document = json.loads(launch_map.read_text())
            self.assertEqual(document["version"], 2)
            self.assertEqual(document["harnessId"], "omp")
            self.assertEqual(document["entries"][0]["canonicalRoot"], str(managed.resolve()))
            self.assertNotIn(Path(artifacts.launch_secret_path).read_text().strip(), launch_map.read_text())
            self.assertEqual(launch_map.stat().st_mode & 0o777, 0o600)


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

    def make_shim_binding(self, root: Path, *, role: str, private: Path | None = None, common: Path | None = None) -> tuple[Path, dict, Path]:
        managed = root / "repo"
        nested = managed / "nested"
        nested.mkdir(parents=True)
        os.chmod(managed, 0o755)
        state = root / "state"
        agent = state / "agent"
        agent.mkdir(parents=True)
        os.chmod(agent, 0o700)
        xdg = {name: agent / "xdg" / name for name in ("data", "state", "cache", "config")}
        for path in xdg.values():
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        plugin_root = xdg["data"] / "omp" / "plugins"
        plugin_root.mkdir(parents=True)
        os.chmod(plugin_root, 0o700)
        overlay = agent / "config.yml"
        overlay.write_text("{}\n")
        policy = state / "policy.json"
        policy.write_text("{}\n")
        secret = state / "secret"
        secret.write_text("r" * 48 + "\n")
        extension = Path(__file__).resolve().parents[1] / "omp" / "extensions" / "pisec.ts"
        fake_omp = root / "real-omp"
        fake_omp.write_text("#!/bin/sh\nexit 0\n")
        fake_fence = root / "fake-fence"
        fake_fence.write_text("#!/usr/bin/python3\nimport json, os, sys\nprint(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}))\n")
        for path in (overlay, policy, secret):
            os.chmod(path, 0o600)
        for path in (fake_omp, fake_fence):
            os.chmod(path, 0o755)
        workstream_id = new_id("ws")
        project_id = new_id("prj")
        entry = {"canonicalRoot": str(managed.resolve()), "projectId": project_id, "workstreamId": workstream_id, "role": role, "executionProfile": "secretary-project" if role == "secretary" else "worker-default", "harnessHome": str(agent), "xdgDataHome": str(xdg["data"]), "xdgStateHome": str(xdg["state"]), "xdgCacheHome": str(xdg["cache"]), "xdgConfigHome": str(xdg["config"]), "pluginRoot": str(plugin_root), "overlayPath": str(overlay), "policyPath": str(policy), "policySha256": hashlib.sha256(policy.read_bytes()).hexdigest(), "extensionPath": str(extension), "runtimeSocketPath": str(root / "runtime.sock"), "secretarySocketPath": str(root / "secretary.sock") if role == "secretary" else None, "launchSecretPath": str(secret), "privateGitObjectDir": None if private is None else str(private), "gitCommonObjectDir": None if common is None else str(common)}
        map_path = state / "launch-map.json"
        map_path.write_text(json.dumps({"version": 2, "harnessId": "omp", "executablePath": str(fake_omp), "fencePath": str(fake_fence), "entries": [entry]}))
        os.chmod(state, 0o700)
        os.chmod(map_path, 0o600)
        return nested, entry, map_path

    def test_private_shim_selects_binding_and_sanitizes_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested, entry, map_path = self.make_shim_binding(root, role="secretary")
            environment = os.environ.copy()
            environment.update({"PISEC_LAUNCH_MAP": str(map_path), "HERDR_PANE_ID": "w1:p1", "SSH_AUTH_SOCK": "/tmp/agent.sock", "OPENAI_API_KEY": "forbidden"})
            shim = Path(__file__).resolve().parents[1] / "pisec" / "runtime-bin" / "omp"
            result = subprocess.run([str(shim), "--resume=/sessions/one"], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            captured = json.loads(result.stdout)
            self.assertIn("--approval-mode=always-ask", captured["argv"])
            self.assertIn(entry["extensionPath"], captured["argv"])
            self.assertIn(entry["overlayPath"], captured["argv"])
            self.assertEqual(captured["env"]["PISEC_SESSION_START_SOURCE"], "resume")
            self.assertEqual(captured["argv"][0:3], ["--settings", entry["policyPath"], "--"])
            self.assertEqual(captured["argv"][3], str(Path(map_path).parent.parent / "real-omp"))
            self.assertEqual(captured["env"]["PISEC_RUNTIME_SOCKET"], entry["runtimeSocketPath"])
            self.assertEqual(captured["env"]["PISEC_SECRETARY_SOCKET"], entry["secretarySocketPath"])
            self.assertEqual(captured["env"]["GIT_CONFIG_KEY_0"], "gc.auto")
            self.assertNotIn("OPENAI_API_KEY", captured["env"])
            os.chmod(Path(entry["canonicalRoot"]), 0o775)
            unsafe_root = subprocess.run([str(shim)], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertNotEqual(unsafe_root.returncode, 0)
            self.assertIn("launch root is unsafe", unsafe_root.stderr)
            os.chmod(Path(entry["canonicalRoot"]), 0o755)
            unsupported_args = subprocess.run([str(shim), "--shell"], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertNotEqual(unsupported_args.returncode, 0)
            self.assertIn("unsupported OMP arguments", unsupported_args.stderr)

    def test_worker_shim_sets_private_git_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "state" / "objects"
            private.mkdir(parents=True)
            os.chmod(private, 0o700)
            common = root / "common"
            common.mkdir()
            os.chmod(common, 0o755)
            nested, entry, map_path = self.make_shim_binding(root, role="worker", private=private, common=common)
            environment = os.environ.copy()
            environment.update({"PISEC_LAUNCH_MAP": str(map_path), "HERDR_PANE_ID": "w1:p1"})
            shim = Path(__file__).resolve().parents[1] / "pisec" / "runtime-bin" / "omp"
            result = subprocess.run([str(shim)], cwd=nested, env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            captured = json.loads(result.stdout)
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
        value = {"workstreamId": self.workstream_id, "runtimeInstanceId": "instance-1", "seq": 1, "event": "session_start", "state": "starting", "nativeSessionKind": "path", "nativeSessionValue": str(self.session), "startSource": "startup", "surfaceId": self.binding["workspace_surface_id"], "token": self.token}
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
