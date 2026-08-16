"""Declarative surface bootstrap and safe project rename."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.pi_control.pi_client import PiControllerClient
from scripts.pi_control.pi_store import PiStore
from tests.pi_test_build import allow_test_only_registered_build_rows
from tests.test_pi_core import _repo

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git_env() -> dict[str, str]:
    return dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid", GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid")


def _write_config(root: Path, entries: list[dict], surfaces: dict | None = None, *, version: int = 1) -> Path:
    path = root / "surfaces.json"
    path.write_text(json.dumps({"version": version, "projects": entries, "surfaces": surfaces or {"pisec": [e["alias"] for e in entries], "pi-personal": [e["alias"] for e in entries]}}, indent=1), encoding="utf-8")
    return path


class SurfaceBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = allow_test_only_registered_build_rows()
        patcher.start()
        self.addCleanup(patcher.stop)

    def _env(self, root: Path) -> dict[str, str]:
        env = dict(os.environ)
        env["PI_SYSTEM_STATE_ROOT"] = str(root / "state")
        env["PI_SYSTEM_DATA_ROOT"] = str(root / "data")
        data = root / "data"
        data.mkdir(parents=True, exist_ok=True)
        (data / "activation.json").write_text(json.dumps({"buildId": "build_" + "a" * 32}), encoding="utf-8")
        (data / "bin").mkdir(exist_ok=True)
        for launcher in ("pi-control", "pi-system-secretary", "pi-system-container-run", "pi-system-workstream-run", "pi-system-reviewer", "pi-system-investigator"):
            target = data / "bin" / launcher
            if not target.exists():
                target.symlink_to(REPO_ROOT / "bin" / launcher)
        state = root / "state"
        state.mkdir(mode=0o700, exist_ok=True)
        os.chmod(state, 0o700)
        with PiStore(state):
            pass
        return env

    def _bootstrap(self, root: Path, config: Path, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(REPO_ROOT / "scripts" / "pi-surface.py"), "bootstrap", "--config", str(config), *extra]
        return subprocess.run(command, capture_output=True, text=True, env=env or self._env(root), cwd=str(REPO_ROOT))

    def _controller(self, root: Path) -> PiControllerClient:
        return PiControllerClient(root / "state")

    def test_fresh_bootstrap_registers_aliases_and_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repos = {}
            for name in ("alpha", "beta"):
                repos[name] = _repo(root, name)
            config = _write_config(root, [
                {"alias": "alpha", "repository": str(repos["alpha"])},
                {"alias": "beta", "repository": str(repos["beta"])},
            ], {"pisec": ["alpha", "beta"], "pi-personal": ["beta"]})
            first = self._bootstrap(root, config)
            self.assertEqual(first.returncode, 0, first.stderr)
            client = self._controller(root)
            projects = client.dispatch("project.status", {"projectId": "unused"}) if False else None
            with PiStore(root / "state") as store:
                rows = {r["display_name"]: r for r in store.conn.execute("SELECT project_id,display_name FROM projects")}
                self.assertEqual(sorted(rows), ["alpha", "beta"])
                alpha_id = rows["alpha"]["project_id"]
                beta_id = rows["beta"]["project_id"]
            preference = json.loads((root / "state" / "surface" / "preferences.json").read_text(encoding="utf-8"))
            self.assertEqual(preference["pisec"], [alpha_id, beta_id])
            self.assertEqual(preference["pi-personal"], [beta_id])
            second = self._bootstrap(root, config)
            self.assertEqual(second.returncode, 0, second.stderr)
            plan = json.loads(second.stdout)["plan"]
            self.assertEqual(plan, {"register": [], "rename": []})

    def test_bootstrap_renames_mismatched_display_names(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = _repo(root, "project")
            env = self._env(root)
            wrong = self._bootstrap(root, _write_config(root, [{"alias": "wrong", "repository": str(repo)}]), env=env)
            self.assertEqual(wrong.returncode, 0, wrong.stderr)
            client = self._controller(root)
            with PiStore(root / "state") as store:
                before = store.conn.execute("SELECT project_id FROM projects").fetchone()[0]
            config = _write_config(root, [{"alias": "right", "repository": str(repo)}])
            outcome = self._bootstrap(root, config, env=env)
            self.assertEqual(outcome.returncode, 0, outcome.stderr)
            plan = json.loads(outcome.stdout)["plan"]
            self.assertEqual(plan["rename"], [{"alias": "right", "from": "wrong"}])
            with PiStore(root / "state") as store:
                row = store.conn.execute("SELECT project_id,display_name FROM projects").fetchone()
                self.assertEqual(row["display_name"], "right")
                self.assertEqual(row["project_id"], before)
                secretary = store.conn.execute("SELECT display_name FROM conversations WHERE role='secretary'").fetchone()
                self.assertEqual(secretary["display_name"], "right secretary")

    def test_bootstrap_fails_on_missing_repository_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = _repo(root, "exists")
            config = _write_config(root, [
                {"alias": "exists", "repository": str(repo)},
                {"alias": "missing", "repository": str(root / "nope")},
            ], {"pisec": ["exists", "missing"]})
            outcome = self._bootstrap(root, config)
            self.assertNotEqual(outcome.returncode, 0)
            self.assertIn("not a Git checkout", outcome.stderr)
            with PiStore(root / "state") as store:
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
            self.assertFalse((root / "state" / "surface" / "preferences.json").exists())

    def test_bootstrap_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = _repo(root, "project")
            config = _write_config(root, [{"alias": "project", "repository": str(repo)}])
            outcome = self._bootstrap(root, config, "--dry-run")
            self.assertEqual(outcome.returncode, 0, outcome.stderr)
            self.assertTrue(json.loads(outcome.stdout)["dryRun"])
            with PiStore(root / "state") as store:
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
            self.assertFalse((root / "state" / "surface" / "preferences.json").exists())

    def test_bootstrap_rejects_duplicate_aliases_and_common_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = _repo(root, "project")
            dup_alias = self._bootstrap(root, _write_config(root, [{"alias": "x", "repository": str(repo)}, {"alias": "x", "repository": str(repo)}]))
            self.assertNotEqual(dup_alias.returncode, 0)
            self.assertIn("unique", dup_alias.stderr)
            dup_common = self._bootstrap(root, _write_config(root, [{"alias": "x", "repository": str(repo)}, {"alias": "y", "repository": str(repo)}]))
            self.assertNotEqual(dup_common.returncode, 0)
            self.assertIn("same Git common directory", dup_common.stderr)

    def test_bootstrap_keep_extra_preserves_unconfigured_active_projects(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = _repo(root, "project")
            env = self._env(root)
            self._bootstrap(root, _write_config(root, [{"alias": "project", "repository": str(repo)}]), env=env)
            with PiStore(root / "state") as store:
                project_id = store.conn.execute("SELECT project_id FROM projects").fetchone()[0]
            extra = _repo(root, "extra")
            extra_registered = self._bootstrap(root, _write_config(root, [{"alias": "extra", "repository": str(extra)}]), env=env)
            self.assertEqual(extra_registered.returncode, 0, extra_registered.stderr)
            with PiStore(root / "state") as store:
                extra_id = store.conn.execute("SELECT project_id FROM projects WHERE display_name='extra'").fetchone()[0]
            env["PI_SURFACES_CONFIG"] = str(_write_config(root, [{"alias": "project", "repository": str(repo)}], {"pisec": ["project"], "pi-personal": []}))
            kept = self._bootstrap(root, Path(env["PI_SURFACES_CONFIG"]), "--keep-extra", env=env)
            self.assertEqual(kept.returncode, 0, kept.stderr)
            preference = json.loads((root / "state" / "surface" / "preferences.json").read_text(encoding="utf-8"))
            self.assertIn(project_id, preference["pisec"])
            self.assertIn(extra_id, preference["pisec"])
            dropped = self._bootstrap(root, Path(env["PI_SURFACES_CONFIG"]), env=env)
            self.assertEqual(dropped.returncode, 0, dropped.stderr)
            preference = json.loads((root / "state" / "surface" / "preferences.json").read_text(encoding="utf-8"))
            self.assertEqual(preference["pisec"], [project_id])

    def test_bootstrap_canonicalizes_subdirectory_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = _repo(root, "toplevel")
            subdir = repo / "sub"
            subdir.mkdir()
            config = _write_config(root, [{"alias": "toplevel", "repository": str(subdir)}])
            outcome = self._bootstrap(root, config)
            self.assertEqual(outcome.returncode, 0, outcome.stderr)
            with PiStore(root / "state") as store:
                row = store.conn.execute("SELECT primary_checkout FROM projects").fetchone()
                self.assertEqual(Path(row["primary_checkout"]).resolve(), repo.resolve())

    def test_rename_project_preserves_identity_and_auto_secretary_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = client.register_project(str(_repo(root, "one")), "one")
            client.register_project(str(_repo(root, "other")), "two")
            with PiStore(root / "state") as store:
                one_secretary = store.conn.execute("SELECT conversation_id FROM conversations WHERE role='secretary' AND display_name='one secretary'").fetchone()[0]
                with self.assertRaises(ValueError):
                    client.rename_project(project_id=project["project_id"], display_name="two")
                renamed = client.rename_project(project_id=project["project_id"], display_name="renamed")
                self.assertEqual(renamed["project_id"], project["project_id"])
                self.assertEqual(renamed["display_name"], "renamed")
                auto = store.conn.execute("SELECT display_name FROM conversations WHERE conversation_id=?", (one_secretary,)).fetchone()
                self.assertEqual(auto["display_name"], "renamed secretary")
                untouched = store.conn.execute("SELECT display_name FROM conversations WHERE role='secretary' AND conversation_id<>?", (one_secretary,)).fetchone()
                self.assertEqual(untouched["display_name"], "two secretary")

    def test_rename_preserves_user_customized_conversation_names(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = client.register_project(str(_repo(root, "one")), "one")
            with PiStore(root / "state") as store:
                secretary = store.conn.execute("SELECT conversation_id FROM conversations WHERE role='secretary'").fetchone()[0]
                store.conn.execute("UPDATE conversations SET display_name='my custom name' WHERE conversation_id=?", (secretary,))
                client.rename_project(project_id=project["project_id"], display_name="renamed")
                custom = store.conn.execute("SELECT display_name FROM conversations WHERE conversation_id=?", (secretary,)).fetchone()
                self.assertEqual(custom["display_name"], "my custom name")


if __name__ == "__main__":
    unittest.main()
