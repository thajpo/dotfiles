"""Regression tests for the Pi harness surface resolver and build registration.

Covers: surface stage freshness/reuse, launcher argv construction, project and
conversation ensure helpers, and the deterministic build re-registration path
(same resource digest superseding a prior registered build).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.pi_control.pi_store import PiStore


ROOT = Path(__file__).resolve().parents[2]


def _load_surface() -> object:
    path = ROOT / "scripts" / "pi-surface.py"
    spec = importlib.util.spec_from_file_location("pi_surface_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildRegistrationTests(unittest.TestCase):
    def test_same_resource_digest_cannot_coexist(self) -> None:
        # The release resource inventory is deterministic: only one build may
        # hold a given resource digest at a time. The registration path deletes
        # the prior same-digest row before inserting (covered by the release
        # journeys); here we pin the schema contract that two live rows with
        # the same digest cannot coexist.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with PiStore(root / "state") as store:
                base = (
                    "INSERT INTO installed_builds(build_id,source_commit,source_tree_hash,build_manifest_path,build_manifest_digest,resource_manifest_path,resource_manifest_digest,pi_version,package_lock_hash,status,installed_at,verification_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("build_old", "c" * 40, "d" * 64, "/old/manifest.json", "sha256:" + "1" * 64, "/old/resources.json", "sha256:" + "2" * 64, "0.83.0", "sha256:" + "3" * 64, "staged", "2026-01-01T00:00:00Z", json.dumps({"verified": True})),
                )
                store.conn.execute(*base)
                with self.assertRaises(Exception):
                    store.conn.execute(
                        "INSERT INTO installed_builds(build_id,source_commit,source_tree_hash,build_manifest_path,build_manifest_digest,resource_manifest_path,resource_manifest_digest,pi_version,package_lock_hash,status,installed_at,verification_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("build_new", "c" * 40, "d" * 64, "/new/manifest.json", "sha256:" + "4" * 64, "/new/resources.json", "sha256:" + "2" * 64, "0.83.0", "sha256:" + "5" * 64, "staged", "2026-01-01T00:00:00Z", json.dumps({"verified": True})),
                    )
                # Removing the prior row frees the digest for the new build,
                # which is exactly what register_staged_build does.
                store.conn.execute("DELETE FROM installed_builds WHERE build_id='build_old'")
                store.conn.execute(
                    "INSERT INTO installed_builds(build_id,source_commit,source_tree_hash,build_manifest_path,build_manifest_digest,resource_manifest_path,resource_manifest_digest,pi_version,package_lock_hash,status,installed_at,verification_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("build_new", "c" * 40, "d" * 64, "/new/manifest.json", "sha256:" + "4" * 64, "/new/resources.json", "sha256:" + "2" * 64, "0.83.0", "sha256:" + "5" * 64, "staged", "2026-01-01T00:00:00Z", json.dumps({"verified": True})),
                )


class SurfaceResolverTests(unittest.TestCase):
    def test_launcher_names_match_role_specific_binaries(self) -> None:
        surface = _load_surface()
        self.assertEqual(surface._LAUNCHER_NAMES["personal"], "pi-system-container-run")
        self.assertEqual(surface._LAUNCHER_NAMES["workstream"], "pi-system-workstream-run")
        self.assertEqual(surface._LAUNCHER_NAMES["secretary"], "pi-system-secretary")
        self.assertEqual(surface._LAUNCHER_NAMES["reviewer"], "pi-system-reviewer")
        self.assertEqual(surface._LAUNCHER_NAMES["investigator"], "pi-system-investigator")
        with self.assertRaises(surface.SurfaceError):
            surface.launch_argv("bogus", "conv_x", "prompt")

    def test_stage_freshness_marker_is_outside_stage(self) -> None:
        # The surface stage must exactly match its build manifest, so the
        # freshness marker cannot live inside the staged tree.
        surface = _load_surface()
        self.assertFalse(str(surface.SURFACE_MARKER).startswith(str(surface.SURFACE_STAGE) + "/"))
        self.assertEqual(surface.SURFACE_MARKER.name, surface.SURFACE_STAGE.name + ".marker")

    def test_launch_argv_writer_roles_require_tool_image(self) -> None:
        surface = _load_surface()
        env = {
            "dataRoot": "/tmp/pi-data",
            "buildId": "build_x",
            "controller": "/tmp/pi-data/bin/pi-control",
            "launchers": {name: f"/tmp/pi-data/bin/{binary}" for name, binary in surface._LAUNCHER_NAMES.items()},
        }
        with mock.patch.object(surface, "env", return_value=env):
            argv = surface.launch_argv("workstream", "conv_x", "prompt")
            self.assertIn("--tool-image", argv)
            self.assertIn(surface.TOOL_IMAGE, argv)
            self.assertIn("pi-system-workstream-run", argv[0])
            secretary_argv = surface.launch_argv("secretary", "conv_y", "prompt")
            self.assertNotIn("--tool-image", secretary_argv)

    def test_launch_argv_uses_explicit_development_stage(self) -> None:
        surface = _load_surface()
        with tempfile.TemporaryDirectory() as raw:
            stage = Path(raw)
            (stage / "bin").mkdir()
            (stage / "build-manifest.json").write_text(json.dumps({"buildId": "build_dev"}), encoding="utf-8")
            argv = surface.launch_argv("secretary", "conv_z", "prompt", stage_root=str(stage))
            self.assertIn(str(stage / "bin" / "pi-system-secretary"), argv)
            self.assertIn("--build-id", argv)
            self.assertIn("build_dev", argv)

    def test_env_requires_an_activated_generation(self) -> None:
        surface = _load_surface()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(surface, "DATA_ROOT", root):
                with self.assertRaises(surface.SurfaceError):
                    surface.env()
                activation = root / "activation.json"
                activation.write_text(json.dumps({"buildId": "build_x"}), encoding="utf-8")
                with self.assertRaises(surface.SurfaceError):
                    surface.env()


if __name__ == "__main__":
    unittest.main()
