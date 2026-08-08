from __future__ import annotations

from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts.pi_control.migration import collect_inventory, shadow_import, shadow_reconcile
from scripts.pi_control.store import ControllerStore


class MigrationImportTests(unittest.TestCase):
    def _git(self, repo: Path, *args: str) -> None:
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_AUTHOR_NAME": "Migration", "GIT_AUTHOR_EMAIL": "migration@example.invalid", "GIT_COMMITTER_NAME": "Migration", "GIT_COMMITTER_EMAIL": "migration@example.invalid"}
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_shadow_import_records_one_idempotent_migration_and_reconciles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            self._git(repo, "init", "-q", "-b", "main")
            self._git(repo, "config", "user.name", "Migration")
            self._git(repo, "config", "user.email", "migration@example.invalid")
            (repo / "README").write_text("migration\n", encoding="utf-8")
            self._git(repo, "add", "README")
            self._git(repo, "commit", "-qm", "base")
            report = collect_inventory([repo])
            state = root / "shadow-state"
            first = shadow_import(report, state, idempotency_key="same-import")
            second = shadow_import(report, state, idempotency_key="same-import")
            self.assertEqual(first["migrationId"], second["migrationId"])
            self.assertTrue(second["idempotent"])
            with ControllerStore(state) as store:
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM migration_runs").fetchone()[0], 1)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 1)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM migration_manifests").fetchone()[0], 1)
                comparison = shadow_reconcile(store, report)
                self.assertEqual(comparison["state"], "matched")
            manifest = state / "migrations" / first["migrationId"] / "source-inventory.json"
            manifest.chmod(0o600)
            manifest.write_text("tampered\n", encoding="utf-8")
            tampered = shadow_import(report, state, idempotency_key="same-import")
            self.assertEqual(tampered["state"], "needs_attention")

    def test_shadow_import_requires_explicit_non_live_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "legacy.json").write_text('{"sessionId":"s"}\n', encoding="utf-8")
            report = collect_inventory([root])
            with self.assertRaises(ValueError):
                shadow_import(report, None)
            live_root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "pi-control"
            with self.assertRaises(ValueError):
                shadow_import(report, live_root)
            xdg = root / "xdg-state"
            xdg.mkdir()
            alias_parent = root / "state-alias"
            alias_parent.symlink_to(xdg, target_is_directory=True)
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(xdg)}):
                with self.assertRaises(ValueError):
                    shadow_import(report, alias_parent / ".." / "state-alias" / "pi-control")

    def test_existing_custom_controller_root_is_rejected_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "legacy.json").write_text('{"sessionId":"s"}\n', encoding="utf-8")
            report = collect_inventory([root])
            state = root / "existing-controller"
            with ControllerStore(state) as store:
                store.register_build("existing", source_tree_hash="tree", artifact_manifest_hash="manifest", pi_version="pi", package_lock_hash="lock", status="active")
                before = store.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
            with self.assertRaises(ValueError):
                shadow_import(report, state)
            with ControllerStore(state) as store:
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0], before)

    def test_contradictions_stop_import_without_project_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.json").write_text('{"projectId":"same","path":"a"}\n', encoding="utf-8")
            (root / "b.json").write_text('{"projectId":"same","path":"b"}\n', encoding="utf-8")
            report = collect_inventory([root])
            result = shadow_import(report, root / "shadow")
            self.assertEqual(result["state"], "needs_attention")
            with ControllerStore(root / "shadow") as store:
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
                self.assertEqual(store.conn.execute("SELECT state FROM migration_runs").fetchone()[0], "needs_attention")


if __name__ == "__main__":
    unittest.main()
