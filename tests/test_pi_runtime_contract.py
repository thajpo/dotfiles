import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pi_runtime", ROOT / "scripts/pi-runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(runtime)


class PiRuntimeContractTests(unittest.TestCase):
    def test_manifest_contract_excludes_host_venv_and_untracked_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            worktree = Path(temporary)
            (worktree / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
            (worktree / "uv.lock").write_text("version = 1\n")
            venv = worktree / ".venv"
            venv.mkdir()
            sentinel = venv / "host-native-sentinel"
            sentinel.write_text("darwin-arm64\n")
            paths = runtime.safe_manifest_paths(worktree)
            self.assertEqual([path.name for path in paths], ["pyproject.toml", "uv.lock"])
            self.assertEqual(sentinel.read_text(), "darwin-arm64\n")

    def test_path_and_workspace_dependencies_fail_closed_to_task_local(self):
        self.assertTrue(runtime.has_workspace_or_path_dependency("[tool.uv.workspace]\nmembers = ['pkg']"))
        self.assertTrue(runtime.has_workspace_or_path_dependency("foo = { path = '../shared' }"))

    def test_prepare_without_uv_lock_does_not_touch_worktree(self):
        with tempfile.TemporaryDirectory() as temporary:
            worktree = Path(temporary)
            sentinel = worktree / ".venv" / "sentinel"
            sentinel.parent.mkdir()
            sentinel.write_text("host\n")
            route = worktree / "route.json"
            route.write_text(json.dumps({"worktree": str(worktree), "image": "unused"}))
            result = runtime.prepare(route)
            self.assertEqual(result["mode"], "task-local")
            self.assertEqual(result["environmentKey"], "task-local")
            self.assertEqual(sentinel.read_text(), "host\n")

    def test_route_version_and_platform_contract_are_host_independent(self):
        route_script = ROOT / "scripts/pi-workspace.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "feature"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.name", "Pi Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "pi@example.invalid"], cwd=repo, check=True)
            (repo / "file.txt").write_text("x\n")
            subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            home = root / "home"
            policy = home / ".config/pi/repository-policy.json"
            policy.parent.mkdir(parents=True)
            policy.write_text(json.dumps({
                "version": 1,
                "defaultMode": "isolated",
                "trustedRoots": [str(repo)],
                "isolatedRoots": [],
                "controlPlaneRepositories": [],
                "protectedBranches": ["main", "master"],
                "worktreeRoot": str(home / ".local/share/pi/worktrees"),
            }))
            policy.chmod(stat.S_IRUSR | stat.S_IWUSR)
            result = subprocess.run(
                ["python3", str(route_script), "prepare", "--cwd", str(repo), "--owner-pid", str(__import__("os").getpid())],
                env={**__import__("os").environ, "HOME": str(home)},
                text=True,
                capture_output=True,
                check=True,
            )
            route = json.loads(result.stdout)["route"]
            data = json.loads(Path(route).read_text())
            self.assertEqual(data["version"], 2)
            self.assertEqual(data["executionTarget"], "linux-container")
            self.assertTrue(data["containerPlatform"].startswith("linux/"))
            self.assertTrue(data["runtimeHelper"].endswith("pi-runtime.py"))


if __name__ == "__main__":
    unittest.main()
