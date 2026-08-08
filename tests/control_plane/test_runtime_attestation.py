"""Phase 5A fake runtime-attestation rejection matrix."""

from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.runtime_adapter import (
    FakeRuntimeAdapter,
    RuntimeSpecError,
    attestation_digest,
    build_runtime_spec,
    validate_attestation,
)
from scripts.pi_control.run_manifest import manifest_digest
from tests.control_plane.test_runtime_spec import RuntimeSpecTests


class RuntimeAttestationTests(unittest.TestCase):
    def _fixture(self, root: Path):
        helper = RuntimeSpecTests()
        manifest = helper._manifest(root)
        spec = build_runtime_spec(manifest, image="registry.example/pi@sha256:" + "5" * 64, supplementary_groups=[1000, 1001])
        adapter = FakeRuntimeAdapter()
        handle = adapter.prepare(spec, manifest)
        return manifest, spec, adapter, handle, adapter.attest(handle.run_id)

    @staticmethod
    def _with_digest(attestation: dict) -> dict:
        changed = copy.deepcopy(attestation)
        changed["attestationDigest"] = attestation_digest(changed)
        return changed

    def test_nonce_is_bound_to_current_manifest_and_manifest_is_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, spec, _adapter, _handle, attestation = self._fixture(Path(temporary))
            with self.assertRaises(TypeError):
                validate_attestation(attestation, spec)  # type: ignore[call-arg]
            wrong_manifest = copy.deepcopy(manifest)
            wrong_manifest["attestationNonce"] = "a-different-nonce-abcdefghijklmnopqrstuvwxyz"
            wrong_manifest["manifestDigest"] = manifest_digest(wrong_manifest)
            with self.assertRaises(RuntimeSpecError):
                validate_attestation(attestation, spec, wrong_manifest)

    def test_non_running_container_and_wrong_identity_are_not_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, spec, _adapter, _handle, attestation = self._fixture(Path(temporary))
            stopped = copy.deepcopy(attestation)
            stopped["container"]["running"] = False
            with self.assertRaises(RuntimeSpecError):
                validate_attestation(self._with_digest(stopped), spec, manifest)
            wrong_uid = copy.deepcopy(attestation)
            wrong_uid["identity"]["uid"] += 1
            with self.assertRaises(RuntimeSpecError):
                validate_attestation(self._with_digest(wrong_uid), spec, manifest)
            wrong_image_id = copy.deepcopy(attestation)
            wrong_image_id["container"]["imageId"] = "sha256:" + "9" * 64
            with self.assertRaises(RuntimeSpecError):
                validate_attestation(self._with_digest(wrong_image_id), spec, manifest)

    def test_spec_cannot_be_rebound_to_different_manifest_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, spec, _adapter, _handle, _attestation = self._fixture(Path(temporary))
            for surface, mutation in (
                ("policy", lambda value: value["project"].update({"policyHash": "sha256:" + "9" * 64})),
                ("OID", lambda value: value["workingCopy"].update({"headOid": "c" * 40})),
                ("path", lambda value: value["workingCopy"].update({"sourcePath": "/different"})),
                ("helper", lambda value: value["helper"].update({"buildId": "build_" + "9" * 32})),
            ):
                changed = copy.deepcopy(spec)
                mutation(changed)
                with self.subTest(surface=surface):
                    with self.assertRaises(RuntimeSpecError):
                        validate_attestation(_attestation, changed, manifest)

    def test_project_worktree_git_mount_and_policy_surfaces_are_compared(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, spec, _adapter, _handle, attestation = self._fixture(Path(temporary))
            for surface, mutation in (
                ("project", lambda value: value["project"].update({"policyHash": "sha256:" + "9" * 64})),
                ("working copy", lambda value: value["workingCopy"].update({"headOid": "c" * 40})),
                ("mount", lambda value: value["mounts"].__setitem__(0, {**value["mounts"][0], "mode": "ro"})),
                ("helper", lambda value: value["helper"].update({"buildId": "build_" + "9" * 32})),
            ):
                changed = copy.deepcopy(attestation)
                mutation(changed)
                with self.subTest(surface=surface):
                    with self.assertRaises(RuntimeSpecError):
                        validate_attestation(self._with_digest(changed), spec, manifest)

    def test_network_security_environment_and_working_directory_are_compared(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, spec, _adapter, _handle, attestation = self._fixture(Path(temporary))
            for key, mutation in (
                ("network", lambda value: value["network"].update({"mode": "loopback"})),
                ("security", lambda value: value["security"].update({"securityOptions": ["no-new-privileges"]})),
                ("environment", lambda value: value["environment"].update({"allowlist": ["SAFE"]})),
                ("workingDirectory", lambda value: value.update({"workingDirectory": "/other"})),
            ):
                changed = copy.deepcopy(attestation)
                mutation(changed)
                with self.subTest(surface=key):
                    with self.assertRaises(RuntimeSpecError):
                        validate_attestation(self._with_digest(changed), spec, manifest)

    def test_labels_or_container_name_alone_never_satisfy_attestation(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, spec, _adapter, _handle, attestation = self._fixture(Path(temporary))
            changed = copy.deepcopy(attestation)
            changed["container"]["name"] = "the-right-name"
            changed["container"]["imageDigest"] = "sha256:" + "8" * 64
            with self.assertRaises(RuntimeSpecError):
                validate_attestation(self._with_digest(changed), spec, manifest)

    def test_fake_gate_has_no_tool_ready_state_before_attestation(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, spec, adapter, handle, _attestation = self._fixture(Path(temporary))
            self.assertEqual(adapter.state(handle.run_id), "ready")
            adapter.stop(manifest["runId"])
            with self.assertRaises(RuntimeSpecError):
                adapter.attest(manifest["runId"])


if __name__ == "__main__":
    unittest.main()
