from pathlib import Path
import inspect
import os
import tempfile
import unittest
from unittest.mock import patch

from scripts.pisec.harnesses.omp import _copy_user_surface
from scripts.pisec.harnesses.omp import OmpHarnessAdapter
from scripts.pisec.cli import format_result
from scripts.pisec.models import PisecError
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import project_status, register_project
from scripts.pisec.research import build_committed_task_packet
from tests.pisec_fixture import make_repo


ROOT = Path(__file__).resolve().parents[1]


class Phase6ContractTests(unittest.TestCase):
    def test_raw_omp_user_config_is_not_a_runtime_input(self):
        source = inspect.getsource(__import__("scripts.pisec.harnesses.omp", fromlist=["module"]))
        self.assertNotIn("def _copy_user_config", source)
        self.assertNotIn('"user-config.yml"', source)

    def test_user_context_rejects_credential_basenames_and_private_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            agent = home / ".omp" / "agent" / "skills"
            agent.mkdir(parents=True)
            (agent / ".env.local").write_text("TOKEN=sentinel\n")
            with patch("scripts.pisec.harnesses.omp.Path.home", return_value=home):
                with self.assertRaises(PisecError):
                    _copy_user_surface(root / "surface")

    def test_user_context_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            agent = home / ".omp" / "agent" / "skills"
            agent.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            (agent / "linked").symlink_to(outside, target_is_directory=True)
            with patch("scripts.pisec.harnesses.omp.Path.home", return_value=home):
                with self.assertRaises(PisecError):
                    _copy_user_surface(root / "surface")

    def test_user_context_rejects_configured_secret_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            skills = home / ".omp" / "agent" / "skills"
            skills.mkdir(parents=True)
            (skills / "notes.md").write_text("approved text with provider sentinel\n")
            with patch("scripts.pisec.harnesses.omp.Path.home", return_value=home):
                with self.assertRaises(PisecError):
                    _copy_user_surface(root / "surface", forbidden_values=(b"provider sentinel",))

    def test_launchers_use_binding_local_temp_roots(self):
        for name in ("omp", "codex"):
            source = (ROOT / "pisec" / "runtime-bin" / name).read_text()
            self.assertIn('"TMPDIR"', source)
            self.assertIn('"TEMP"', source)
            self.assertIn('"TMP"', source)
        for name in ("worker-default", "secretary-project", "first-mate"):
            source = (ROOT / "pisec" / "fence" / f"{name}.jsonc").read_text()
            self.assertNotIn('"/tmp"', source)

    def test_binding_surface_and_state_roots_are_separate_and_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from tests.test_pisec_fence import make_config

            adapter = OmpHarnessAdapter(state_root=root / "state", config=make_config(root))
            worktree = root / "worktree"
            worktree.mkdir()
            with patch("scripts.pisec.harnesses.omp.Path.home", return_value=root / "home"):
                surface = adapter.prepare_runtime_surface()
                scope = {
                    "projectId": "prj_" + "a" * 32,
                    "workstreamId": "ws_" + "b" * 32,
                    "executionProfile": "worker-default",
                    "worktreePath": str(worktree),
                    "branchName": "pisec/ws_" + "b" * 32 + "/work",
                    "externalDomains": ["html.duckduckgo.com"],
                    "runtimeSurfaceId": "surface_" + surface.content_sha256[:32],
                    "runtimeSurfaceSha256": surface.content_sha256,
                    "runtimeSurfaceRoot": surface.root_path,
                }
                staged = adapter.stage_profile(scope, surface, root / "staging")
                artifacts = adapter.activate_profile(scope, staged)
            state = Path(artifacts.harness_home)
            immutable = Path(artifacts.adapter_data["surfaceRoot"])
            self.assertNotEqual(state, immutable)
            self.assertTrue((state / "sessions").is_dir())
            self.assertTrue((state / "tmp").is_dir())
            self.assertEqual(immutable.stat().st_mode & 0o777, 0o500)
            self.assertFalse(any(path.lstat().st_mode & 0o200 for path in immutable.rglob("*")))

    def test_committed_packet_contains_work_and_learning_controls(self):
        packet = build_committed_task_packet(
            {
                "schemaVersion": 1,
                "outcome": "Bounded outcome.",
                "boundaries": ["Only the approved files."],
                "acceptance": ["Checks pass."],
                "openQuestions": [],
                "evidence": ["The phase check."],
            },
            {
                "projectId": "prj_" + "a" * 32,
                "workstreamId": "ws_" + "b" * 32,
                "title": "Worker",
                "purpose": "Bounded purpose",
                "brief": "Bounded brief",
                "targetRef": "main",
                "baseCommitOid": "a" * 40,
                "branchName": "pisec/ws_" + "b" * 32 + "/work",
                "executionProfile": "worker-default",
                "workMode": "MAJOR",
                "learningOverlay": "OFF",
                "learningSeam": "phase-6",
                "harnessId": "omp",
                "workspaceAdapterId": "herdr",
                "implementationModel": None,
                "harnessModel": None,
                "reasoningEffort": "high",
                "nonEffects": ["No push."],
                "approvalScopeSha256": "c" * 64,
            },
        )
        execution = packet["execution"]
        self.assertEqual(execution["workMode"], "MAJOR")
        self.assertEqual(execution["learningOverlay"], "OFF")
        self.assertEqual(execution["learningSeam"], "phase-6")

    def test_active_pins_are_exact(self):
        install = (ROOT / "scripts" / "agent-workflow-install.sh").read_text()
        readme = (ROOT / "README.md").read_text()
        manifest = (ROOT / "herdr" / "plugins" / "pisec" / "herdr-plugin.toml").read_text()
        for text in (install, readme, manifest):
            self.assertNotIn("0.8.x", text)
            self.assertNotIn("0.28.x", text)
            self.assertNotIn("compatible", text)

    def test_status_exposes_semantic_task_and_runtime_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                status = project_status(store, project["project_id"])
                self.assertIn("taskState", status["project"])
                self.assertIn("runtimeState", status["project"])

    def test_default_status_and_refresh_hide_internal_ids(self):
        project_id = "prj_" + "a" * 32
        workstream_id = "ws_" + "b" * 32
        status = format_result(
            ("status",),
            {"projects": [{"display_name": "Demo", "project_id": project_id, "taskState": "active", "runtimeState": "idle", "attentionCount": 0, "nextAction": "Continue task"}]},
        )
        refresh = format_result(
            ("project", "refresh"),
            {"generation": "c" * 64, "upgraded": [{"project": "Demo", "workstreamId": workstream_id, "reason": "refreshed"}], "pending": [], "skipped": [], "failed": []},
        )
        self.assertNotIn(project_id, status)
        self.assertNotIn(workstream_id, refresh)
        self.assertNotIn("c" * 64, refresh)
        self.assertIn("TASK", status)
        self.assertIn("RESULT", refresh)


if __name__ == "__main__":
    unittest.main()
