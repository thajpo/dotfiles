from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from scripts.pi_control.staged_build import (
    create_build_manifest,
    load_build_manifest,
    stage_build,
    write_build_manifest,
)


class BuildManifestTests(unittest.TestCase):
    def _git(self, repo: Path, *args: str) -> None:
        env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Build",
            "GIT_AUTHOR_EMAIL": "build@example.invalid",
            "GIT_COMMITTER_NAME": "Build",
            "GIT_COMMITTER_EMAIL": "build@example.invalid",
        }
        subprocess.run(
            ["git", *args],
            cwd=repo,
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_serialized_envelope_loads_and_recomputes_without_self_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir(mode=0o700)
            (root / "package-lock.json").write_text('{"name":"fixture"}\n', encoding="utf-8")
            (root / "launcher").write_text("#!/bin/sh\n", encoding="utf-8")
            (root / "alias").symlink_to("launcher")
            repo = Path(temporary) / "repo"
            repo.mkdir()
            self._git(repo, "init", "-q", "-b", "main")
            self._git(repo, "config", "user.name", "Build")
            self._git(repo, "config", "user.email", "build@example.invalid")
            (repo / "tracked").write_text("tracked\n", encoding="utf-8")
            self._git(repo, "add", "tracked")
            self._git(repo, "commit", "-qm", "base")

            destination = root / "build-manifest.json"
            manifest = create_build_manifest(
                root,
                repository=repo,
                manifest_path=destination,
                metadata={"imageDigest": "sha256:" + "a" * 64},
                test_outcomes={"unit": "pass"},
            )
            saved = write_build_manifest(manifest, destination)
            serialized = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(serialized["manifestDigest"], manifest.digest)
            self.assertEqual(serialized["buildId"], manifest.build_id)
            self.assertNotIn("manifestPath", serialized)
            self.assertNotIn("build-manifest.json", {entry["path"] for entry in serialized["files"]})
            loaded = load_build_manifest(destination)
            self.assertEqual(loaded.payload, manifest.payload)
            self.assertEqual(loaded.recompute_digest(), loaded.digest)
            loaded.verify_files(root, exclude_paths=[destination])
            self.assertEqual(saved.path, str(destination))

            changed = json.loads(destination.read_text(encoding="utf-8"))
            destination.chmod(0o600)
            changed["metadata"]["imageDigest"] = "sha256:" + "b" * 64
            destination.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_build_manifest(destination)

    def test_exact_tree_rejects_extra_missing_special_and_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            (root / "file").write_text("value\n", encoding="utf-8")
            manifest = create_build_manifest(root)
            manifest_path = Path(temporary) / "manifest.json"
            write_build_manifest(manifest, manifest_path)

            manifest.verify_files(root)
            (root / "extra").write_text("unexpected\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                manifest.verify_files(root)
            (root / "extra").unlink()
            (root / "file").unlink()
            with self.assertRaises(RuntimeError):
                manifest.verify_files(root)

            (root / "file").write_text("value\n", encoding="utf-8")
            fifo = root / "fifo"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                manifest.verify_files(root)
            fifo.unlink()

            serialized = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_path.chmod(0o600)
            serialized["files"][0]["path"] = "../escape"
            manifest_path.write_text(json.dumps(serialized), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_build_manifest(manifest_path)

            del serialized["manifestDigest"]
            manifest_path.write_text(json.dumps(serialized), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_build_manifest(manifest_path)

    def test_manifest_destination_can_be_excluded_when_root_contains_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir(mode=0o700)
            (root / "file").write_text("value\n", encoding="utf-8")
            destination = root / "build-manifest.json"
            first = create_build_manifest(root, manifest_path=destination)
            write_build_manifest(first, destination)
            second = create_build_manifest(root, manifest_path=destination)
            self.assertEqual(first.digest, second.digest)
            load_build_manifest(destination).verify_files(root, exclude=["build-manifest.json"])

    def test_stage_build_creates_user_only_disposable_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            (root / "file").write_text("value\n", encoding="utf-8")
            stage = stage_build(root, Path(temporary) / "stage", files=["file"])
            self.assertTrue(Path(stage.path).is_file())
            self.assertEqual(Path(stage.path).stat().st_mode & 0o777, 0o400)
            self.assertEqual((Path(temporary) / "stage").stat().st_mode & 0o777, 0o700)

    def test_installed_launcher_imports_only_activated_controller_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_root = root / "control"
            install_root.mkdir()
            shutil.copytree(Path("scripts/pi_control"), install_root / "pi_control")
            launcher = root / "pi-control"
            shutil.copy2(Path("bin/pi-control"), launcher)
            launcher.chmod(0o755)
            state = root / "state"
            from scripts.pi_control.greenfield_store import GreenfieldStore
            with GreenfieldStore(state):
                pass
            env = {**os.environ, "HOME": str(root / "home"), "PI_SYSTEM_CONTROL_ROOT": str(install_root)}
            result = subprocess.run(
                [sys.executable, str(launcher), "--state-root", str(state), "--json", "schema", "status"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"schema_version"', result.stdout)

            shutil.rmtree(install_root / "pi_control")
            result = subprocess.run(
                [sys.executable, str(launcher), "--state-root", str(state), "--json", "schema", "status"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("installed package is missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
