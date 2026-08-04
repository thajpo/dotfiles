import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pi_secretary_control_herdr", ROOT / "scripts/pi-secretary-control.py")
secretary = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(secretary)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True,
                          capture_output=True, check=True).stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Secretary Herdr Test")
    git(path, "config", "user.email", "secretary-herdr@example.invalid")
    (path / "tracked").write_text("initial\n")
    git(path, "add", "tracked")
    git(path, "commit", "-m", "initial")
    return path


class HerdrBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.source = make_repo(root / "source")
        worktrees = root / "worktrees"
        worktrees.mkdir()
        worktrees.chmod(0o700)
        policy = self.home / ".config/pi/repository-policy.json"
        policy.parent.mkdir(parents=True)
        policy.write_text(json.dumps({
            "version": 1, "defaultMode": "isolated", "trustedRoots": [str(root)],
            "isolatedRoots": [], "controlPlaneRepositories": [],
            "protectedBranches": ["main"], "worktreeRoot": str(worktrees),
        }))
        policy.chmod(0o600)
        self.env = {
            "HOME": str(self.home), "XDG_STATE_HOME": str(root / "state"),
            "PI_CODING_AGENT_DIR": str(self.home / ".pi/agent"),
        }
        self.patch = mock.patch.dict(os.environ, self.env, clear=False)
        self.patch.start()
        initial = secretary.init_project(self.source)
        self.capability = initial["capability"]
        self.registered = secretary.register_project(self.source, "herdr-project")
        self.fake_herdr = root / "herdr"
        self.fake_herdr.write_text("#!/bin/sh\nexit 0\n")
        self.fake_herdr.chmod(0o700)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def herdr_env(self):
        return {
            "PI_SECRETARY_BACKEND": "herdr", "HERDR_ENV": "1",
            "HERDR_WORKSPACE_ID": "w1", "HERDR_TAB_ID": "w1:t1", "HERDR_PANE_ID": "w1:p1",
            "PI_SECRETARY_HERDR_BIN": str(self.fake_herdr),
            "PI_SECRETARY_HERDR_WORKER": str(ROOT / "bin/pi-herdr-workstream"),
        }

    def make_workstream(self, *, backend="herdr"):
        environment = self.herdr_env() if backend == "herdr" else {"PI_SECRETARY_BACKEND": "tmux"}
        with mock.patch.dict(os.environ, environment, clear=False):
            brief = secretary.create_brief(self.source, self.capability, "Herdr task", "Do the bounded task")
            return secretary.create_workstream(self.source, self.capability, "Herdr task", "feature", brief["briefId"])

    def patch_surface(self, states, *, split_workspace="w1"):
        panes = [{"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1",
                  "cwd": str(self.source), "label": "secretary/herdr-project"}]
        surface = {"workspace_id": "w1", "label": "secretary/herdr-project"}
        split = {"pane": {"pane_id": "w1:p2", "workspace_id": split_workspace, "tab_id": "w1:t1"}}
        state = mock.patch.object(secretary, "_herdr_worker_state", side_effect=states)
        surface_patch = mock.patch.object(secretary, "_herdr_surface", return_value=(surface, panes))
        json_patch = mock.patch.object(secretary, "_herdr_json", return_value=split)
        ok_patch = mock.patch.object(secretary, "_herdr_ok")
        path_patch = mock.patch.object(secretary, "_herdr_path", return_value=self.fake_herdr)
        worker_patch = mock.patch.object(secretary, "_herdr_worker_path", return_value=ROOT / "bin/pi-herdr-workstream")
        return state, surface_patch, json_patch, ok_patch, path_patch, worker_patch

    def test_new_herdr_workstream_uses_pane_run_not_pidev_or_tmux(self):
        workstream = self.make_workstream()
        patches = self.patch_surface(["missing", "live"])
        with mock.patch.dict(os.environ, self.herdr_env(), clear=False), \
             mock.patch.object(secretary, "_pidev_path", side_effect=AssertionError("tmux backend used")):
            with patches[0], patches[1], patches[2], patches[3] as herdr_ok, patches[4], patches[5]:
                launched = secretary.launch_workstream(self.registered["projectId"], workstream["workstreamId"])
        self.assertEqual(launched["backend"], "herdr")
        self.assertEqual(launched["herdrWorkspace"], "w1")
        self.assertEqual(launched["herdrPane"], "w1:p2")
        self.assertIsNone(launched["tmuxSession"])
        self.assertEqual(launched["launchState"], "launched")
        run_calls = [call for call in herdr_ok.call_args_list if call.args[0][0:2] == ["pane", "run"]]
        self.assertEqual(len(run_calls), 1)
        command = run_calls[0].args[0][-1]
        self.assertIn("pi-herdr-workstream", command)
        self.assertIn("--workstream-id", command)
        self.assertNotIn("pidev", command)
        self.assertNotIn("tmux", command)

    def test_herdr_recovery_reuses_exact_worker_pane(self):
        workstream = self.make_workstream()
        first = self.patch_surface(["missing", "live"])
        with mock.patch.dict(os.environ, self.herdr_env(), clear=False), \
             first[0], first[1], first[2], first[3], first[4], first[5]:
            secretary.launch_workstream(self.registered["projectId"], workstream["workstreamId"])
        second = self.patch_surface(["live"])
        with mock.patch.dict(os.environ, self.herdr_env(), clear=False), \
             second[0], second[1], second[2], second[3] as herdr_ok, second[4], second[5]:
            recovered = secretary.launch_workstream(self.registered["projectId"], workstream["workstreamId"])
        self.assertEqual(recovered["herdrPane"], "w1:p2")
        self.assertFalse(any(call.args[0][0:2] == ["pane", "run"] for call in herdr_ok.call_args_list))
        self.assertFalse(any(call.args[0][0:2] == ["pane", "split"] for call in herdr_ok.call_args_list))

    def test_mismatched_created_pane_fails_closed_before_run(self):
        workstream = self.make_workstream()
        patches = self.patch_surface(["missing"], split_workspace="wrong")
        with mock.patch.dict(os.environ, self.herdr_env(), clear=False), \
             patches[0], patches[1], patches[2], patches[3] as herdr_ok, patches[4], patches[5]:
            with self.assertRaisesRegex(secretary.SecretaryError, "wrong project surface"):
                secretary.launch_workstream(self.registered["projectId"], workstream["workstreamId"])
        self.assertFalse(any(call.args[0][0:2] == ["pane", "run"] for call in herdr_ok.call_args_list))

    def test_surface_identity_failure_does_not_replace_unknown_pane(self):
        workstream = self.make_workstream()
        with mock.patch.dict(os.environ, self.herdr_env(), clear=False), \
             mock.patch.object(secretary, "_herdr_surface", side_effect=secretary.SecretaryError("identity mismatch")), \
             mock.patch.object(secretary, "_herdr_ok") as herdr_ok:
            with self.assertRaisesRegex(secretary.SecretaryError, "identity mismatch"):
                secretary.launch_workstream(self.registered["projectId"], workstream["workstreamId"])
        herdr_ok.assert_not_called()

    def test_tmux_workstream_is_not_implicitly_migrated_by_herdr_surface(self):
        workstream = self.make_workstream(backend="tmux")
        with mock.patch.dict(os.environ, self.herdr_env(), clear=False), \
             mock.patch.object(secretary, "_pidev_path", side_effect=AssertionError("must not relaunch tmux worker")), \
             mock.patch.object(secretary, "_herdr_ok") as herdr_ok:
            with self.assertRaisesRegex(secretary.SecretaryError, "will not migrate"):
                secretary.launch_workstream(self.registered["projectId"], workstream["workstreamId"])
        herdr_ok.assert_not_called()


if __name__ == "__main__":
    unittest.main()
