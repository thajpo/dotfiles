from pathlib import Path
import contextlib
import io
import json
import os
import runpy
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.pisec.access import effective_runtime_scope
from scripts.pisec.access import authorize_apply_project_permissions, prepare_project_permissions
from scripts.pisec.adapters import AdapterRegistry, AgentObservation
from scripts.pisec.broker import BrokerDispatcher, BrokerService
from scripts.pisec import codex_hook
from scripts.pisec.first_mate import ensure_first_mate
from scripts.pisec.models import ConflictError, NeedsAttentionError, ScopeMismatchError, canonical_json
from scripts.pisec.pi_store import PiStore
from scripts.pisec.protocol import request
from scripts.pisec.projects import register_project
from scripts.pisec.refresh import mark_stale_bindings
from scripts.pisec.runtime import report_runtime, usable_runtime_binding
from scripts.pisec.secretary import ensure_secretary
from scripts.pisec.workstreams import prepare_workstream
from scripts.pisec.harnesses.codex import CodexHarnessAdapter
from scripts.pisec.harnesses.omp import OmpHarnessAdapter
from tests.test_pisec_fence import make_config
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace, make_repo


class Phase10ScopeParityTests(unittest.TestCase):
    def test_codex_launcher_accepts_system_owned_pinned_node(self):
        node = Path("/usr/bin/node")
        if not node.is_file() or node.is_symlink() or node.stat().st_uid != 0:
            self.skipTest("Linux acceptance host does not provide a system-owned /usr/bin/node")
        launcher = Path(__file__).resolve().parents[1] / "pisec" / "runtime-bin" / "codex"
        namespace = runpy.run_path(str(launcher))
        with self.assertRaises(SystemExit):
            namespace["secure_file"](node, executable=True)
        namespace["secure_file"](node, executable=True, allow_system_owner=True)

    def test_production_omp_and_codex_worker_materialization_preserves_additions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worker"
            worktree.mkdir()
            extra = "example.com"
            for adapter_id, adapter in self._production_worker_adapters(root):
                with self.subTest(adapter=adapter_id):
                    context = patch("scripts.pisec.harnesses.omp.Path.home", return_value=root / "omp-home") if adapter_id == "omp" else contextlib.nullcontext()
                    with context:
                        surface = adapter.prepare_runtime_surface()
                        scope = {
                            "projectId": "prj_" + "a" * 32,
                            "workstreamId": "ws_" + ("b" if adapter_id == "omp" else "c") * 32,
                            "executionProfile": "worker-default",
                            "worktreePath": str(worktree),
                            "branchName": "pisec/ws_" + ("b" if adapter_id == "omp" else "c") * 32 + "/work",
                            "externalDomains": sorted([*adapter.profile_domains("worker-default", (extra,))]),
                            "runtimeSurfaceSha256": surface.content_sha256,
                            "runtimeSurfaceRoot": surface.root_path,
                            "runtimeSurfaceId": "surface_" + surface.content_sha256[:32],
                        }
                        staged = adapter.stage_profile(scope, surface, root / (adapter_id + "-staging"))
                        try:
                            policy = json.loads(Path(staged.candidate.policy_path).read_text())
                            self.assertEqual(policy["network"]["allowedDomains"], scope["externalDomains"])
                            self.assertIn(extra, policy["network"]["allowedDomains"])
                            if adapter_id == "codex":
                                codex_package_root = Path(adapter.harness_config["executablePath"]).parent.parent
                                self.assertIn(str(codex_package_root), policy["filesystem"]["allowRead"])
                                self.assertNotIn("codex", policy["command"]["deny"])
                                self.assertNotIn(str(adapter.harness_config["nodePath"]), policy["command"]["deny"])
                                self.assertNotIn(str(adapter.harness_config["executablePath"]), policy["command"]["deny"])
                            self.assertEqual(staged.candidate.generation_sha256, adapter.desired_generation(scope, surface))
                            if adapter_id == "codex":
                                self.assertTrue(Path(surface.root_path, "managed", "codex_model_catalog.json").is_file())
                            activated = adapter.activate_profile(scope, staged)
                            active_policy = Path(activated.policy_path)
                            self.assertEqual(json.loads(active_policy.read_text())["network"]["allowedDomains"], scope["externalDomains"])
                            self.assertEqual(stat.S_IMODE(active_policy.stat().st_mode), 0o400)
                            self.assertEqual(stat.S_IMODE(Path(activated.adapter_data["surfaceRoot"]).stat().st_mode), 0o500)
                            os.chmod(Path(activated.adapter_data["surfaceRoot"]), 0o500)
                            replacement_scope = {**scope, "permissionBackupRoot": str(root / (adapter_id + "-backup"))}
                            replacement = adapter.stage_profile(replacement_scope, surface, root / (adapter_id + "-replacement"))
                            try:
                                replaced = adapter.activate_profile(replacement_scope, replacement)
                                self.assertEqual(stat.S_IMODE(Path(replaced.adapter_data["surfaceRoot"]).stat().st_mode), 0o500)
                            finally:
                                adapter.discard_staged_profile(replacement)
                        finally:
                            adapter.discard_staged_profile(staged)

    @staticmethod
    def _production_worker_adapters(root, *, codex_node_path=None):
        omp_home = root / "omp-home"
        (omp_home / ".omp" / "agent").mkdir(parents=True)
        omp = OmpHarnessAdapter(state_root=root / "omp-state", config=make_config(root))
        codex_exec = root / "codex-fixture"
        codex_exec.write_text("#!/bin/sh\nexit 0\n")
        codex_exec.chmod(0o700)
        token = root / "codex-gateway.token"
        token.write_text("g" * 48 + "\n")
        token.chmod(0o600)
        codex_config = {
            "fencePath": make_config(root)["fencePath"],
            "harness": {"config": {"executablePath": str(codex_exec), "gateway": {"baseUrl": "http://127.0.0.1:4000", "tokenFile": str(token)}}},
            "workerHarnesses": {"codex": {"id": "codex", "config": {"executablePath": str(codex_exec), "versionPrefix": "0.150.0"}}},
        }
        node_lookup = patch("scripts.pisec.harnesses.codex.shutil.which", return_value=codex_node_path) if codex_node_path is not None else contextlib.nullcontext()
        with node_lookup:
            codex = CodexHarnessAdapter(state_root=root / "codex-state", config=codex_config)
        return (("omp", omp), ("codex", codex))

    def test_production_codex_launcher_accepts_immutable_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worker"
            worktree.mkdir()
            system_node = Path("/usr/bin/node")
            node_path = str(system_node) if system_node.is_file() and not system_node.is_symlink() and system_node.stat().st_uid == 0 else None
            codex = dict(self._production_worker_adapters(root, codex_node_path=node_path))["codex"]
            capture = root / "fence-argv.json"
            fence = Path(codex.root_config["fencePath"])
            fence.write_text(
                "#!/usr/bin/python3\n"
                "import json\n"
                "from pathlib import Path\n"
                "import sys\n"
                f"Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            )
            fence.chmod(0o700)
            surface = codex.prepare_runtime_surface()
            scope = {
                "projectId": "prj_" + "d" * 32,
                "workstreamId": "ws_" + "e" * 32,
                "executionProfile": "worker-default",
                "worktreePath": str(worktree),
                "branchName": "pisec/ws_" + "e" * 32 + "/work",
                "externalDomains": sorted(codex.profile_domains("worker-default", ("example.com",))),
                "runtimeSurfaceSha256": surface.content_sha256,
                "runtimeSurfaceRoot": surface.root_path,
                "runtimeSurfaceId": "surface_" + surface.content_sha256[:32],
            }
            staged = codex.stage_profile(scope, surface, root / "codex-staging")
            activated = codex.activate_profile(scope, staged)
            launcher = codex.commit_launch_binding(
                scope,
                activated,
                workspace_session_name="main",
                workspace_id="w1",
                workspace_view_id="w1:t1",
                workspace_surface_id="w1:p1",
            )
            config_text = Path(activated.adapter_data["configPath"]).read_text()
            hooks_text = Path(activated.adapter_data["hooksPath"]).read_text()
            self.assertNotIn(str(Path(staged.staging_root).resolve()), config_text)
            self.assertNotIn(str(Path(staged.staging_root).resolve()), hooks_text)
            self.assertIn(activated.adapter_data["mcpPath"], config_text)
            self.assertIn(activated.adapter_data["hookPath"], hooks_text)
            result = subprocess.run([str(launcher)], cwd=worktree, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = json.loads(capture.read_text())
            codex_argv = argv[argv.index("--") + 1 :]
            self.assertIn("--dangerously-bypass-hook-trust", codex_argv)

    def test_codex_launch_separates_writable_state_and_trusted_provider_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worker"
            worktree.mkdir()
            codex = dict(self._production_worker_adapters(root))["codex"]
            capture = root / "fence-capture.json"
            fence = Path(codex.root_config["fencePath"])
            fence.write_text(
                "#!/usr/bin/python3\n"
                "import json\n"
                "import os\n"
                "from pathlib import Path\n"
                "import sys\n"
                f"Path({str(capture)!r}).write_text(json.dumps({{'argv': sys.argv[1:], 'env': {{key: os.environ.get(key) for key in ('CODEX_HOME', 'OPENAI_BASE_URL')}}}}))\n"
            )
            fence.chmod(0o700)
            surface = codex.prepare_runtime_surface()
            scope = {
                "projectId": "prj_" + "f" * 32,
                "workstreamId": "ws_" + "1" * 32,
                "executionProfile": "worker-default",
                "worktreePath": str(worktree),
                "branchName": "pisec/ws_" + "1" * 32 + "/work",
                "externalDomains": sorted(codex.profile_domains("worker-default", ("example.com",))),
                "runtimeSurfaceSha256": surface.content_sha256,
                "runtimeSurfaceRoot": surface.root_path,
                "runtimeSurfaceId": "surface_" + surface.content_sha256[:32],
            }
            staged = codex.stage_profile(scope, surface, root / "codex-staging")
            activated = codex.activate_profile(scope, staged)
            launcher = codex.commit_launch_binding(
                scope,
                activated,
                workspace_session_name="main",
                workspace_id="w1",
                workspace_view_id="w1:t1",
                workspace_surface_id="w1:p1",
            )

            descriptor = json.loads((launcher.parent / "binding.json").read_text())
            self.assertEqual(descriptor["codexHome"], activated.harness_home)
            self.assertTrue(Path(descriptor["codexHome"]).is_relative_to(root / "codex-state" / "binding-state"))
            self.assertTrue(Path(descriptor["configPath"]).is_relative_to(Path(activated.adapter_data["surfaceRoot"])))
            self.assertNotEqual(descriptor["codexHome"], str(Path(activated.adapter_data["configPath"]).parent))
            config_text = Path(descriptor["configPath"]).read_text()
            self.assertIn('model = "openai-codex/gpt-5.6-luna"', config_text)
            self.assertIn('openai_base_url = "http://127.0.0.1:4000/v1"', config_text)
            self.assertIn('model_provider = "openai-codex"', config_text)
            self.assertIn('model_providers.openai-codex.base_url = "http://127.0.0.1:4000/v1"', config_text)
            self.assertIn('model_providers.openai-codex.wire_api = "responses"', config_text)
            self.assertIn('model_providers.openai-codex.env_key = "OPENAI_API_KEY"', config_text)
            self.assertIn(
                f'model_catalog_json = {json.dumps(str(Path(activated.adapter_data["surfaceRoot"]) / "managed" / "codex_model_catalog.json"))}',
                config_text,
            )

            result = subprocess.run([str(launcher)], cwd=worktree, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            captured = json.loads(capture.read_text())
            self.assertEqual(captured["env"]["CODEX_HOME"], activated.harness_home)
            self.assertEqual(
                Path(activated.harness_home, "config.toml").read_text(),
                f'[projects.{json.dumps(str(worktree))}]\ntrust_level = "trusted"\n',
            )
            codex_argv = captured["argv"][captured["argv"].index("--") + 1 :]
            config_values = [codex_argv[index + 1] for index, value in enumerate(codex_argv[:-1]) if value == "--config"]
            self.assertEqual(codex_argv[codex_argv.index("--model") + 1], "openai-codex/gpt-5.6-luna")
            self.assertIn('openai_base_url="http://127.0.0.1:4000/v1"', config_values)
            self.assertIn('model_provider="openai-codex"', config_values)
            self.assertIn('model_providers.openai-codex.base_url="http://127.0.0.1:4000/v1"', config_values)
            self.assertIn('model_providers.openai-codex.wire_api="responses"', config_values)
            self.assertIn('model_providers.openai-codex.env_key="OPENAI_API_KEY"', config_values)
            self.assertIn('model_providers.openai-codex.requires_openai_auth=false', config_values)
            self.assertIn(
                f'model_catalog_json={json.dumps(str(Path(activated.adapter_data["surfaceRoot"]) / "managed" / "codex_model_catalog.json"))}',
                config_values,
            )
            self.assertIn(f'projects.{json.dumps(str(worktree))}.trust_level="trusted"', config_values)
            self.assertIn("features.hooks=true", config_values)
            self.assertTrue(any(value.startswith("hooks.SessionStart=") for value in config_values))
            self.assertTrue(any(value.startswith("mcp_servers.pisec.command=") for value in config_values))

    def test_codex_model_catalog_preserves_native_worker_tool_contract(self):
        catalog = json.loads((Path(__file__).resolve().parents[1] / "scripts" / "pisec" / "codex_model_catalog.json").read_text())
        model = next(item for item in catalog["models"] if item["slug"] == "gpt-5.6-luna")
        self.assertEqual(model["shell_type"], "shell_command")
        self.assertEqual(model["tool_mode"], "code_mode_only")
        self.assertTrue(model["supports_parallel_tool_calls"])
        self.assertEqual(model["input_modalities"], ["text", "image"])
        self.assertEqual(model["default_reasoning_summary"], "none")
        self.assertEqual(model["model_messages"]["instructions_template"], "You are Codex, an agent based on GPT-5.")
        self.assertEqual(model["model_messages"]["token_budget"]["reminder_threshold_tokens"], 6144)
        self.assertNotIn("base_instructions", model)

    def test_codex_refresh_discards_only_volatile_temp_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worker"
            worktree.mkdir()
            codex = dict(self._production_worker_adapters(root))['codex']
            surface = codex.prepare_runtime_surface()
            workstream_id = "ws_" + "2" * 32
            prior_home = root / "codex-state" / "binding-state" / "codex" / workstream_id
            (prior_home / "sessions").mkdir(parents=True)
            (prior_home / "sessions" / "resume.jsonl").write_text("durable\n")
            (prior_home / "tmp" / "arg0").mkdir(parents=True)
            (prior_home / "tmp" / "arg0" / "codex").symlink_to("/usr/bin/node")
            scope = {
                "projectId": "prj_" + "3" * 32,
                "workstreamId": workstream_id,
                "executionProfile": "worker-default",
                "worktreePath": str(worktree),
                "branchName": "pisec/" + workstream_id + "/work",
                "externalDomains": sorted(codex.profile_domains("worker-default", ("example.com",))),
                "runtimeSurfaceSha256": surface.content_sha256,
                "runtimeSurfaceRoot": surface.root_path,
                "runtimeSurfaceId": "surface_" + surface.content_sha256[:32],
                "implementationModel": "openai-codex/gpt-5.6-luna:high",
                "harnessModel": "gpt-5.6-luna",
                "reasoningEffort": "high",
            }
            candidate_root = root / "candidate"
            artifacts = codex._build_profile(scope, surface, state_root=candidate_root)
            candidate_home = Path(artifacts.harness_home)
            self.assertEqual((candidate_home / "sessions" / "resume.jsonl").read_text(), "durable\n")
            self.assertFalse((candidate_home / "tmp").exists())

    def test_codex_refresh_rejects_nonvolatile_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worker"
            worktree.mkdir()
            codex = dict(self._production_worker_adapters(root))['codex']
            surface = codex.prepare_runtime_surface()
            workstream_id = "ws_" + "4" * 32
            prior_home = root / "codex-state" / "binding-state" / "codex" / workstream_id
            (prior_home / "sessions").mkdir(parents=True)
            (prior_home / "sessions" / "unsafe").symlink_to("/usr/bin/node")
            scope = {
                "projectId": "prj_" + "5" * 32,
                "workstreamId": workstream_id,
                "executionProfile": "worker-default",
                "worktreePath": str(worktree),
                "branchName": "pisec/" + workstream_id + "/work",
                "externalDomains": sorted(codex.profile_domains("worker-default", ("example.com",))),
                "runtimeSurfaceSha256": surface.content_sha256,
                "runtimeSurfaceRoot": surface.root_path,
                "runtimeSurfaceId": "surface_" + surface.content_sha256[:32],
            }
            with self.assertRaisesRegex(Exception, "existing Codex binding state contains a symlink"):
                codex._build_profile(scope, surface, state_root=root / "candidate")

    def test_codex_hook_classifies_native_session_start_as_working(self):
        class RecordingSocket:
            def __init__(self):
                self.payload = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _timeout):
                return None

            def connect(self, _path):
                return None

            def sendall(self, data):
                self.payload = json.loads(data.decode())

            def recv(self, _size):
                return b"{}"

        recording = RecordingSocket()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "PISEC_RUNTIME_SOCKET": str(Path(tmp) / "runtime.sock"),
                    "PISEC_RUNTIME_TOKEN": "t" * 48,
                    "PISEC_WORKSTREAM_ID": "ws_" + "2" * 32,
                    "PISEC_RUNTIME_INSTANCE_ID": "3" * 32,
                    "PISEC_SURFACE_ID": "surface_" + "4" * 32,
                    "PISEC_HARNESS_HOME": tmp,
                    "PISEC_SESSION_START_SOURCE": "startup",
                },
            ), patch("scripts.pisec.codex_hook.socket.socket", return_value=recording), patch(
                "sys.stdin", io.StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "native-session"}))
            ):
                self.assertEqual(codex_hook.main(), 0)
        self.assertIsNotNone(recording.payload)
        self.assertEqual(recording.payload["payload"]["state"], "working")
        self.assertEqual(recording.payload["payload"]["nativeSessionValue"], "native-session")

    def test_codex_hook_restarts_new_runtime_instance_with_fresh_session_start(self):
        class RecordingSocket:
            def __init__(self):
                self.payload = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _timeout):
                return None

            def connect(self, _path):
                return None

            def sendall(self, data):
                self.payload = json.loads(data.decode())

            def recv(self, _size):
                return b"{}"

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sessions").mkdir()
            reports = []
            for instance, event_name, native_session in (
                ("a" * 32, "SessionStart", "first-session"),
                ("a" * 32, "Stop", "first-session"),
                ("b" * 32, "SessionStart", "second-session"),
            ):
                recording = RecordingSocket()
                with patch.dict(
                    os.environ,
                    {
                        "PISEC_RUNTIME_SOCKET": str(Path(tmp) / "runtime.sock"),
                        "PISEC_RUNTIME_TOKEN": "t" * 48,
                        "PISEC_WORKSTREAM_ID": "ws_" + "2" * 32,
                        "PISEC_RUNTIME_INSTANCE_ID": instance,
                        "PISEC_SURFACE_ID": "surface_" + "4" * 32,
                        "PISEC_HARNESS_HOME": tmp,
                        "PISEC_SESSION_START_SOURCE": "resume",
                    },
                ), patch("scripts.pisec.codex_hook.socket.socket", return_value=recording), patch(
                    "sys.stdin", io.StringIO(json.dumps({"hook_event_name": event_name, "session_id": native_session}))
                ):
                    self.assertEqual(codex_hook.main(), 0)
                self.assertIsNotNone(recording.payload)
                reports.append(recording.payload["payload"])
        self.assertEqual(
            [(report["seq"], report["event"], report["state"]) for report in reports],
            [(1, "session_start", "working"), (2, "lifecycle", "idle"), (1, "session_start", "working")],
        )
        self.assertEqual([report["startSource"] for report in reports], ["resume", "resume", "resume"])

    def test_codex_restart_hook_attests_through_the_public_runtime_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            harness = FixtureHarness(root)
            workspace = FixtureWorkspace(root)
            with PiStore(root / "state") as store:
                project = register_project(store, repo, default_ref="main")
                secretary = ensure_secretary(store, project["project_id"], harness, workspace)
                workstream_id = str(secretary["workstream"]["workstream_id"])
                binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone())

            registry = AdapterRegistry()
            registry.register_harness(harness)
            registry.register_workspace(workspace)
            service = BrokerService(
                BrokerDispatcher(lambda: PiStore(root / "state"), registry=registry, harness=harness, workspace=workspace),
                runtime_root=root / "runtime",
            )
            service.start()
            try:
                def start_without_attestation(surface_id, name, agent_kind):
                    workspace.calls.append(("start_without_attestation", (surface_id, name, agent_kind)))
                    workspace.agents[name] = AgentObservation(name, surface_id, True, "working")
                    workspace.runtime_states.pop(surface_id, None)
                    return {"started": True, "name": name, "surfaceId": surface_id}

                workspace.start_agent = start_without_attestation

                def run_hook(instance, native_session):
                    with PiStore(root / "state") as store:
                        current = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone())
                    with patch.dict(
                        os.environ,
                        {
                            "PISEC_RUNTIME_SOCKET": str(service.paths["runtime"]),
                            "PISEC_RUNTIME_TOKEN": Path(str(current["launch_secret_path"])).read_text().strip(),
                            "PISEC_WORKSTREAM_ID": workstream_id,
                            "PISEC_RUNTIME_INSTANCE_ID": instance,
                            "PISEC_SURFACE_ID": str(current["workspace_surface_id"]),
                            "PISEC_HARNESS_HOME": str(current["harness_home"]),
                            "PISEC_SESSION_START_SOURCE": "startup",
                            "PISEC_RUNTIME_GENERATION": str(current["applied_generation_sha256"]),
                        },
                    ), patch("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": native_session}))):
                        self.assertEqual(codex_hook.main(), 0)

                workspace.stop_runtime(str(binding["workspace_surface_id"]))
                first_restart = request(service.paths["admin"], "runtime.ensure", {"workstreamId": workstream_id, "waitSeconds": 0})
                self.assertEqual(first_restart["action"], "startup_in_progress")
                run_hook("codex-runtime-first", "codex-session-first")
                with PiStore(root / "state") as store:
                    first = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone())
                self.assertEqual(first["runtime_instance_id"], "codex-runtime-first")
                self.assertEqual(first["report_seq"], 1)
                self.assertIsNotNone(first["session_start_event_sequence"])

                workspace.stop_runtime(str(binding["workspace_surface_id"]))
                second_restart = request(service.paths["admin"], "runtime.ensure", {"workstreamId": workstream_id, "waitSeconds": 0})
                self.assertEqual(second_restart["action"], "startup_in_progress")
                run_hook("codex-runtime-second", "codex-session-second")
                with PiStore(root / "state") as store:
                    second = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone())
                    event = store.conn.execute(
                        "SELECT payload_json FROM events WHERE sequence=?",
                        (second["session_start_event_sequence"],),
                    ).fetchone()
                self.assertEqual(second["runtime_instance_id"], "codex-runtime-second")
                self.assertEqual(second["report_seq"], 1)
                self.assertEqual(second["session_start_report_seq"], 1)
                self.assertEqual(second["observed_state"], "working")
                self.assertEqual(second["refresh_pending"], 0)
                self.assertIsNone(second["refresh_operation_id"])
                self.assertEqual(
                    json.loads(str(event["payload_json"])),
                    {"generationSha256": str(second["applied_generation_sha256"]), "reportSeq": 1, "runtimeInstanceId": "codex-runtime-second"},
                )
            finally:
                service.stop()

    def test_codex_launcher_rejects_worker_created_user_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worker"
            worktree.mkdir()
            codex = dict(self._production_worker_adapters(root))["codex"]
            capture = root / "fence-capture.json"
            fence = Path(codex.root_config["fencePath"])
            fence.write_text(
                "#!/usr/bin/python3\n"
                "import json\n"
                "import sys\n"
                "from pathlib import Path\n"
                f"Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            )
            fence.chmod(0o700)
            surface = codex.prepare_runtime_surface()
            scope = {
                "projectId": "prj_" + "8" * 32,
                "workstreamId": "ws_" + "9" * 32,
                "executionProfile": "worker-default",
                "worktreePath": str(worktree),
                "branchName": "pisec/ws_" + "9" * 32 + "/work",
                "externalDomains": sorted(codex.profile_domains("worker-default", ("example.com",))),
                "runtimeSurfaceSha256": surface.content_sha256,
                "runtimeSurfaceRoot": surface.root_path,
                "runtimeSurfaceId": "surface_" + surface.content_sha256[:32],
            }
            staged = codex.stage_profile(scope, surface, root / "codex-staging")
            activated = codex.activate_profile(scope, staged)
            launcher = codex.commit_launch_binding(
                scope,
                activated,
                workspace_session_name="main",
                workspace_id="w1",
                workspace_view_id="w1:t1",
                workspace_surface_id="w1:p1",
            )
            Path(activated.harness_home, "config.toml").write_text("openai_base_url = \"https://evil.invalid\"\n")
            result = subprocess.run([str(launcher)], cwd=worktree, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("user configuration", result.stderr)

    def test_fixture_generation_hashes_every_production_scope_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = FixtureHarness(Path(tmp))
            base = {
                "executionProfile": "worker-default",
                "worktreePath": str(Path(tmp) / "worktree"),
                "externalDomains": ["fixture.test"],
                "dataDirs": [str(Path(tmp) / "data")],
                "implementationModel": "model-a",
                "harnessModel": "harness-a",
                "reasoningEffort": "high",
                "pythonEnv": str(Path(tmp) / "venv"),
                "runtimeSurfaceSha256": "a" * 64,
            }
            for field, changed in (
                ("executionProfile", "secretary-project"),
                ("worktreePath", str(Path(tmp) / "other-worktree")),
                ("externalDomains", ["example.test"]),
                ("dataDirs", [str(Path(tmp) / "other-data")]),
                ("implementationModel", "model-b"),
                ("harnessModel", "harness-b"),
                ("reasoningEffort", "xhigh"),
                ("pythonEnv", str(Path(tmp) / "other-venv")),
                ("runtimeSurfaceSha256", "b" * 64),
            ):
                mutated = {**base, field: changed}
                self.assertNotEqual(
                    harness.desired_generation(base),
                    harness.desired_generation(mutated),
                    field,
                )

    def test_effective_secretary_scope_preserves_profile_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                ensured = ensure_secretary(store, project["project_id"], harness, workspace)
                binding = dict(
                    store.conn.execute(
                        "SELECT r.*,w.kind,w.project_id FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=?",
                        (ensured["workstream"]["workstream_id"],),
                    ).fetchone()
                )

                scope = effective_runtime_scope(store, binding, harness=harness)

                self.assertEqual(
                    scope["externalDomains"],
                    list(harness.profile_domains("secretary-project", ())),
                )
                self.assertFalse(usable_runtime_binding(store, binding["workstream_id"], workspace, None))

    def test_worker_approval_scope_contains_profile_composed_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                ensure_secretary(store, project["project_id"], harness, workspace)
                prepared = prepare_workstream(
                    store,
                    project_id=project["project_id"],
                    title="Scope parity worker",
                    purpose="Prove the immutable worker scope",
                    brief="Use only the approved scope.",
                    task_packet={
                        "schemaVersion": 1,
                        "outcome": "Scope parity is proven.",
                        "boundaries": ["Tests only."],
                        "acceptance": ["The approved domain scope is exact."],
                        "openQuestions": [],
                        "evidence": ["Fail-first test output."],
                    },
                    idempotency_key="phase10-worker-scope",
                    harness=harness,
                    workspace=workspace,
                    work_root=root / "worktrees",
                )

                self.assertIn("externalDomains", prepared["approvalScope"])
                self.assertEqual(
                    prepared["approvalScope"]["externalDomains"],
                    list(harness.profile_domains("worker-default", ())),
                )

    def test_secretary_materialization_and_reconcile_keep_one_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                ensured = ensure_secretary(store, project["project_id"], harness, workspace)
                workstream_id = ensured["workstream"]["workstream_id"]

                result = mark_stale_bindings(store, harness, workstream_ids=(workstream_id,))
                binding = store.conn.execute(
                    "SELECT desired_generation_sha256,applied_generation_sha256 FROM runtime_bindings WHERE workstream_id=?",
                    (workstream_id,),
                ).fetchone()

                self.assertEqual(result["stale"], [])
                self.assertEqual(result["failed"], [])
                self.assertEqual(binding["desired_generation_sha256"], binding["applied_generation_sha256"])

    def test_secretary_finalization_cannot_commit_after_generation_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)

                def reconcile_during_finalization(_failpoint, name, context):
                    if name != "before_secretary_final_event_commit":
                        return
                    with store.transaction():
                        store.conn.execute(
                            "UPDATE runtime_bindings SET desired_generation_sha256=? WHERE workstream_id=?",
                            ("b" * 64, context["workstreamId"]),
                        )

                with patch("scripts.pisec.secretary._hit", side_effect=reconcile_during_finalization):
                    with self.assertRaises(NeedsAttentionError):
                        ensure_secretary(store, project["project_id"], harness, workspace)

                project_row = store.conn.execute(
                    "SELECT active FROM projects WHERE project_id=?",
                    (project["project_id"],),
                ).fetchone()
                workstream_row = store.conn.execute(
                    "SELECT provisioning_state FROM workstreams WHERE project_id=? AND kind='secretary'",
                    (project["project_id"],),
                ).fetchone()
                operation_row = store.conn.execute(
                    "SELECT state FROM operations WHERE project_id=? AND kind='secretary.ensure'",
                    (project["project_id"],),
                ).fetchone()
                binding_row = store.conn.execute(
                    "SELECT desired_generation_sha256,applied_generation_sha256 FROM runtime_bindings WHERE workstream_id=(SELECT secretary_workstream_id FROM projects WHERE project_id=?)",
                    (project["project_id"],),
                ).fetchone()

                self.assertFalse(
                    project_row["active"]
                    and workstream_row["provisioning_state"] == "bound"
                    and operation_row["state"] == "succeeded"
                    and binding_row["desired_generation_sha256"] != binding_row["applied_generation_sha256"]
                )

    def test_first_mate_retry_preserves_precise_stale_generation_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                ensure_secretary(store, project["project_id"], harness, workspace)
                ensure_first_mate(store, project["project_id"], harness, workspace)
                with patch("scripts.pisec.first_mate._recover_start", side_effect=NeedsAttentionError("First Mate applied runtime generation is stale")):
                    with self.assertRaisesRegex(NeedsAttentionError, "applied runtime generation is stale"):
                        ensure_first_mate(store, project["project_id"], harness, workspace)

    def test_first_mate_materialization_and_reconcile_keep_one_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                ensure_secretary(store, project["project_id"], harness, workspace)
                first_mate = ensure_first_mate(store, project["project_id"], harness, workspace)
                result = mark_stale_bindings(store, harness, workstream_ids=(first_mate["workstream"]["workstream_id"],))
                binding = store.conn.execute(
                    "SELECT desired_generation_sha256,applied_generation_sha256 FROM runtime_bindings WHERE workstream_id=?",
                    (first_mate["workstream"]["workstream_id"],),
                ).fetchone()

                self.assertEqual(result["stale"], [])
                self.assertEqual(result["failed"], [])
                self.assertEqual(binding["desired_generation_sha256"], binding["applied_generation_sha256"])

    def test_permission_authorization_rechecks_drift_after_profile_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                ensure_secretary(store, project["project_id"], harness, workspace)
                prepared = prepare_project_permissions(
                    store,
                    project_id=project["project_id"],
                    data_dirs=[],
                    external_domains=["approved.example"],
                    issue_id=None,
                    idempotency_key="phase10-permission-drift",
                )

                class DriftingHarness(FixtureHarness):
                    def __init__(self, fixture_root, fixture_store, project_id):
                        super().__init__(fixture_root)
                        self.fixture_store = fixture_store
                        self.fixture_project_id = project_id

                    def stage_profile(self, scope, surface, staging_root):
                        staged = super().stage_profile(scope, surface, staging_root)
                        with self.fixture_store.transaction():
                            self.fixture_store.conn.execute(
                                "UPDATE projects SET external_domains=? WHERE project_id=?",
                                (canonical_json(["drifted.example"]), self.fixture_project_id),
                            )
                        return staged

                drifting = DriftingHarness(root, store, project["project_id"])
                with self.assertRaises(ScopeMismatchError):
                    authorize_apply_project_permissions(
                        store,
                        approval_scope=prepared["approvalScope"],
                        harness_resolver=lambda _workstream_id: drifting,
                        surface_resolver=lambda _harness_id: drifting.current_runtime_surface(),
                        workspace=workspace,
                        actor="secretary",
                    )
                current = store.conn.execute("SELECT external_domains FROM projects WHERE project_id=?", (project["project_id"],)).fetchone()
                self.assertEqual(json.loads(current["external_domains"]), ["drifted.example"])
                self.assertIsNone(store.conn.execute("SELECT 1 FROM authorizations WHERE operation_id=?", (prepared["operation"]["operation_id"],)).fetchone())

    def test_first_mate_startup_uses_authenticated_runtime_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)

                class AuthenticatedWorkspace(FixtureWorkspace):
                    def start_agent(self, surface_id, name, agent_kind):
                        row = self.store.conn.execute(
                            "SELECT r.*,w.kind,w.project_id FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workspace_surface_id=?",
                            (surface_id,),
                        ).fetchone()
                        if row is None or row["kind"] != "first_mate":
                            return super().start_agent(surface_id, name, agent_kind)
                        self.calls.append(("start", (surface_id, name, agent_kind)))
                        self.agents[name] = AgentObservation(name, surface_id, True, "working")
                        self.runtime_states.pop(surface_id, None)
                        report_runtime(
                            self.store,
                            {
                                "workstreamId": str(row["workstream_id"]),
                                "runtimeInstanceId": "authenticated-first-mate-runtime",
                                "seq": 1,
                                "event": "session_start",
                                "reason": None,
                                "state": "idle",
                                "nativeSessionKind": None,
                                "nativeSessionValue": None,
                                "startSource": "startup",
                                "surfaceId": surface_id,
                                "token": Path(str(row["launch_secret_path"])).read_text().strip(),
                                "generation": str(row["launch_generation_sha256"] or row["applied_generation_sha256"]),
                            },
                            harness,
                            self,
                        )
                        return {"started": True, "name": name, "surfaceId": surface_id}

                workspace = AuthenticatedWorkspace(root, store)
                ensure_secretary(store, project["project_id"], harness, workspace)
                first_mate = ensure_first_mate(store, project["project_id"], harness, workspace)
                self.assertEqual(first_mate["workstream"]["provisioning_state"], "bound")
                self.assertTrue(usable_runtime_binding(store, first_mate["workstream"]["workstream_id"], workspace, harness, allowed_states={"idle", "working", "blocked"}))

    def test_permission_apply_is_a_protected_batch_not_an_immediate_project_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                ensure_secretary(store, project["project_id"], harness, workspace)
                prepared = prepare_project_permissions(
                    store,
                    project_id=project["project_id"],
                    data_dirs=[],
                    external_domains=["approved.example"],
                    issue_id=None,
                    idempotency_key="phase10-protected-permission-batch",
                )
                applied = authorize_apply_project_permissions(
                    store,
                    approval_scope=prepared["approvalScope"],
                    harness_resolver=lambda _workstream_id: harness,
                    surface_resolver=lambda _harness_id: harness.current_runtime_surface(),
                    workspace=workspace,
                    actor="secretary",
                )
                operation = store.conn.execute("SELECT state,step FROM operations WHERE operation_id=?", (prepared["operation"]["operation_id"],)).fetchone()
                self.assertEqual(applied["operation"]["state"], "succeeded")
                self.assertEqual(operation["state"], "succeeded")
                self.assertEqual(operation["step"], "committed")
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM authorizations WHERE operation_id=?", (prepared["operation"]["operation_id"],)).fetchone()[0], 1)

    def test_permission_batch_compensates_before_relaunching_a_safe_old_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)

                class AmbiguousOnceWorkspace(FixtureWorkspace):
                    def __init__(self, fixture_root, fixture_store):
                        super().__init__(fixture_root, fixture_store)
                        self.ambiguous = False

                    def observe_runtime(self, surface_id, process_identity):
                        if self.ambiguous:
                            self.ambiguous = False
                            return type(super().observe_runtime(surface_id, process_identity))("unknown", "injected ambiguity")
                        return super().observe_runtime(surface_id, process_identity)

                workspace = AmbiguousOnceWorkspace(root, store)
                ensure_secretary(store, project["project_id"], harness, workspace)
                prepared = prepare_project_permissions(
                    store,
                    project_id=project["project_id"],
                    data_dirs=[],
                    external_domains=["approved.example"],
                    issue_id=None,
                    idempotency_key="phase10-permission-compensation",
                )
                workspace.ambiguous = True
                with self.assertRaises(NeedsAttentionError):
                    authorize_apply_project_permissions(
                        store,
                        approval_scope=prepared["approvalScope"],
                        harness_resolver=lambda _workstream_id: harness,
                        surface_resolver=lambda _harness_id: harness.current_runtime_surface(),
                        workspace=workspace,
                        actor="secretary",
                    )
                operation = store.conn.execute("SELECT state,step FROM operations WHERE operation_id=?", (prepared["operation"]["operation_id"],)).fetchone()
                binding = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=(SELECT secretary_workstream_id FROM projects WHERE project_id=?)", (project["project_id"],)).fetchone()
                current = store.conn.execute("SELECT data_dirs,external_domains FROM projects WHERE project_id=?", (project["project_id"],)).fetchone()
                self.assertEqual((operation["state"], operation["step"]), ("failed", "compensated"), store.conn.execute("SELECT error_message FROM operations WHERE operation_id=?", (prepared["operation"]["operation_id"],)).fetchone()[0])
                self.assertEqual(json.loads(current["external_domains"]), [])
                self.assertEqual(binding["refresh_pending"], 0)
                self.assertTrue(usable_runtime_binding(store, binding["workstream_id"], workspace, harness, allowed_states={"idle", "working", "blocked"}))

    def test_project_register_cannot_bypass_protected_permission_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo, external_domains=["approved.example"])
                with self.assertRaisesRegex((ConflictError, NeedsAttentionError), "permission"):
                    register_project(store, repo, data_dirs=[], external_domains=[])
                current = store.conn.execute("SELECT external_domains FROM projects WHERE project_id=?", (project["project_id"],)).fetchone()
                self.assertEqual(json.loads(current["external_domains"]), ["approved.example"])

    def test_secretary_finalization_rechecks_live_identity_inside_guarded_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)

                def invalidate_after_predicate(*args, **kwargs):
                    result = usable_runtime_binding(*args, **kwargs)
                    if result:
                        workspace.worktrees.clear()
                        workspace.agents.clear()
                    return result

                with patch("scripts.pisec.secretary.usable_runtime_binding", side_effect=invalidate_after_predicate):
                    with self.assertRaises(NeedsAttentionError):
                        ensure_secretary(store, project["project_id"], harness, workspace)
                self.assertFalse(store.conn.execute("SELECT active FROM projects WHERE project_id=?", (project["project_id"],)).fetchone()[0])
                self.assertNotEqual(store.conn.execute("SELECT state FROM operations WHERE kind='secretary.ensure'").fetchone()[0], "succeeded")

    def test_public_project_open_and_first_mate_ensure_hold_reconcile_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            harness = FixtureHarness(root)
            workspace = FixtureWorkspace(root)
            registry = AdapterRegistry()
            registry.register_harness(harness)
            registry.register_workspace(workspace)
            dispatcher = BrokerDispatcher(lambda: PiStore(root / "state"), registry=registry, harness=harness, workspace=workspace)
            try:
                project = dispatcher.dispatch("admin", "project.register", {"path": str(repo)})
                class ProbeLock:
                    def __init__(self):
                        self.entries = 0
                    def __enter__(self):
                        self.entries += 1
                        return self
                    def __exit__(self, *_args):
                        return False
                probe = ProbeLock()
                dispatcher._reconcile_lock = probe
                dispatcher.dispatch("admin", "project.open", {"project": project["project_id"]})
                dispatcher.dispatch("admin", "first_mate.ensure", {"project": project["project_id"]})
                self.assertEqual(probe.entries, 2)
            finally:
                dispatcher.stop_background()


if __name__ == "__main__":
    unittest.main()
