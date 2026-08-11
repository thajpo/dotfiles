"""Canonical P2 run-manifest fixture and persistence tests."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.run_manifest import ManifestError, manifest_digest, read_manifest, validate_manifest, write_manifest


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/control-plane/run-manifest.v2.json"


class RunManifestTests(unittest.TestCase):
    def test_python_accepts_the_unchanged_cross_language_fixture(self) -> None:
        body = FIXTURE.read_text(encoding="utf-8").rstrip("\n")
        manifest = json.loads(body)
        self.assertEqual(validate_manifest(manifest)["manifestDigest"], manifest["manifestDigest"])
        self.assertEqual(manifest_digest(manifest), manifest["manifestDigest"])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            write_manifest(path, manifest)
            self.assertEqual(path.read_text(encoding="utf-8"), body)
            self.assertEqual(read_manifest(path).manifest, manifest)
            with self.assertRaises(ManifestError):
                write_manifest(path, manifest)

    def test_semantic_changes_fail_even_with_a_recomputed_digest(self) -> None:
        base = json.loads(FIXTURE.read_text(encoding="utf-8"))
        mutations = (
            lambda value: value["conversation"].update(authorityProfile="writer-container"),
            lambda value: value["installedBuild"].update(piVersion="unknown"),
            lambda value: value["session"].update(piSessionId="caller-session"),
            lambda value: value["scope"].update(projectId="prj_" + "9" * 32),
            lambda value: value.update(toolRuntime={"specVersion": 1}),
            lambda value: value["scope"].update(projectResourceVersion=2),
            lambda value: value["hostProcess"].update(executableSha256="sha256:not-a-digest"),
            lambda value: value.update(unknown=True),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ManifestError):
                value = json.loads(json.dumps(base))
                mutation(value)
                value["manifestDigest"] = manifest_digest(value)
                validate_manifest(value)
        changed = json.loads(json.dumps(base))
        changed["hostProcess"]["argv"].append("--changed")
        with self.assertRaises(ManifestError):
            validate_manifest(changed)
        executable_changed = json.loads(json.dumps(base))
        executable_changed["hostProcess"]["executableSha256"] = "sha256:" + "9" * 64
        self.assertNotEqual(manifest_digest(executable_changed), base["manifestDigest"])
        with self.assertRaises(ManifestError):
            validate_manifest(executable_changed)
        executable_changed["manifestDigest"] = manifest_digest(executable_changed)
        self.assertEqual(validate_manifest(executable_changed)["hostProcess"]["executableSha256"], "sha256:" + "9" * 64)


if __name__ == "__main__":
    unittest.main()
