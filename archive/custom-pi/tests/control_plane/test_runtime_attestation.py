"""P2 identity rejection matrix before P5 runtime attestation exists."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from scripts.pi_control.run_manifest import ManifestError, manifest_digest, validate_manifest


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/control-plane/run-manifest.v2.json"


class RuntimeAttestationTests(unittest.TestCase):
    def test_manifest_digest_binds_host_process_and_channel(self) -> None:
        manifest = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for key, mutation in (
            ("host", lambda value: value["hostProcess"]["argv"].append("--changed")),
            ("channel", lambda value: value.update(channelBindingHash="sha256:" + "9" * 64)),
        ):
            changed = copy.deepcopy(manifest)
            mutation(changed)
            with self.subTest(key=key), self.assertRaises(ManifestError):
                validate_manifest(changed)

    def test_project_scope_and_resource_versions_are_bound(self) -> None:
        manifest = json.loads(FIXTURE.read_text(encoding="utf-8"))
        manifest["scope"]["workingCopyResourceVersion"] = 0
        manifest["manifestDigest"] = manifest_digest(manifest)
        with self.assertRaises(ManifestError):
            validate_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
