"""Phase 5A pure runtime specification and fake attestation tests."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.runtime_adapter import (
    FakeRuntimeAdapter,
    RuntimeSpecError,
    attestation_digest,
    build_runtime_spec,
    runtime_spec_digest,
    validate_attestation,
    validate_runtime_spec,
)
from scripts.pi_control.run_manifest import manifest_digest, validate_manifest


class RuntimeSpecTests(unittest.TestCase):
    def _manifest(self, root: Path, *, authority: str = "writer") -> dict:
        checkout = root / "checkout"
        git_common = root / "common.git"
        git_dir = checkout / ".git"
        checkout.mkdir()
        git_common.mkdir()
        git_dir.mkdir()
        project_id = "prj_" + "a" * 32
        working_id = "wc_" + "b" * 32
        manifest = {
            "schemaVersion": 1,
            "runId": "run_" + "c" * 32,
            "operationId": "op_" + "d" * 32,
            "taskId": None,
            "conversationId": "conv_" + "e" * 32,
            "piSessionId": "pi-runtime-spec",
            "parentRunId": None,
            "project": {
                "projectId": project_id,
                "resourceVersion": 1,
                "objectFormat": "sha1",
                "trustMode": "isolated",
                "policyHash": "sha256:" + "1" * 64,
            },
            "workingCopy": None,
            "authority": authority,
            "runtime": {
                "runtimeSpecVersion": 1,
                "runtimeSpecHash": "sha256:" + "2" * 64,
                "executionTarget": "linux-container",
                "platform": "linux/amd64",
                "imageDigest": "sha256:" + "5" * 64,
                "controllerBuildId": "build_" + "f" * 32,
                "piVersion": "test",
            },
            "owner": {"uid": 1000, "gid": 1000, "pid": 123, "processStartIdentity": "fake:123"},
            "capabilityHash": "sha256:" + "4" * 64,
            "attestationNonce": "runtime-spec-nonce-abcdefghijklmnopqrstuvwxyz",
            "createdAt": "2024-01-01T00:00:00Z",
            "expiresAt": None,
            "manifestDigest": "",
        }
        if authority == "writer":
            manifest["workingCopy"] = {
                "workingCopyId": working_id,
                "resourceVersion": 2,
                "kind": "primary",
                "purpose": "personal",
                "effectiveMode": "isolated",
                "hostPath": str(checkout),
                "gitCommonDir": str(git_common),
                "gitDir": str(git_dir),
                "branchRef": "refs/heads/topic",
                "headOid": "a" * 40,
                "treeOid": "b" * 40,
                "dirtyFingerprint": None,
                "writerEpoch": 1,
            }
        manifest["manifestDigest"] = manifest_digest(manifest)
        return validate_manifest(manifest)

    def test_canonical_spec_hash_rejects_unknown_fields_and_bare_digest_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._manifest(Path(temporary))
            spec = build_runtime_spec(manifest, image="registry.example/pi@sha256:" + "5" * 64)
            self.assertEqual(runtime_spec_digest(spec), runtime_spec_digest(dict(reversed(list(spec.items())))))
            self.assertEqual(len(runtime_spec_digest(spec)), 71)
            with self.assertRaises(RuntimeSpecError):
                validate_runtime_spec({**spec, "unexpected": True})
            with self.assertRaises(RuntimeSpecError):
                build_runtime_spec(manifest, image="sha256:" + "5" * 64)
            with self.assertRaises(RuntimeSpecError):
                build_runtime_spec(manifest, image="Registry.example/pi@sha256:" + "5" * 64)
            port_image = build_runtime_spec(manifest, image="registry.example:5000/pi@sha256:" + "5" * 64)
            self.assertEqual(port_image["image"]["repository"], "registry.example:5000/pi")
            with self.assertRaises(RuntimeSpecError):
                build_runtime_spec(manifest, image="registry.example/pi/@sha256:" + "5" * 64)

    def test_expired_manifest_cannot_prepare_a_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._manifest(Path(temporary))
            manifest["expiresAt"] = "2024-01-02T00:00:00Z"
            manifest["manifestDigest"] = manifest_digest(manifest)
            validate_manifest(manifest)  # historical shape remains inspectable
            with self.assertRaises(RuntimeSpecError):
                build_runtime_spec(manifest, image="registry.example/pi@sha256:" + "5" * 64)

    def test_spec_contains_exact_source_identity_and_least_privilege_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._manifest(Path(temporary))
            spec = build_runtime_spec(manifest, image="registry.example/pi@sha256:" + "5" * 64)
            self.assertEqual(spec["workingCopy"]["workingCopyId"], manifest["workingCopy"]["workingCopyId"])
            self.assertEqual(spec["workingCopy"]["headOid"], manifest["workingCopy"]["headOid"])
            self.assertEqual(spec["mounts"][0]["source"], manifest["workingCopy"]["hostPath"])
            self.assertEqual(spec["mounts"][0]["mode"], "rw")
            self.assertEqual(spec["network"]["mode"], "none")
            self.assertTrue(spec["filesystem"]["readOnlyRootfs"])
            self.assertTrue(spec["git"]["identityReadOnly"])

    def test_fake_adapter_never_becomes_ready_before_attestation_and_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._manifest(Path(temporary))
            spec = build_runtime_spec(manifest, image="registry.example/pi@sha256:" + "5" * 64)
            adapter = FakeRuntimeAdapter()
            handle = adapter.prepare(spec, manifest)
            self.assertEqual(handle.state, "created")
            self.assertEqual(adapter.state(manifest["runId"]), "created")
            attestation = adapter.attest(manifest["runId"])
            self.assertEqual(adapter.state(manifest["runId"]), "ready")
            self.assertEqual(validate_attestation(attestation, spec, manifest), attestation)
            altered = dict(attestation)
            altered["attestationDigest"] = attestation_digest({**altered, "container": {**altered["container"], "platform": "linux/arm64"}})
            altered["container"] = {**altered["container"], "platform": "linux/arm64"}
            with self.assertRaises(RuntimeSpecError):
                validate_attestation(altered, spec, manifest)
            adapter.stop(manifest["runId"])
            self.assertEqual(adapter.state(manifest["runId"]), "stopped")

    def test_fake_adapter_does_not_reuse_a_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._manifest(Path(temporary))
            spec = build_runtime_spec(manifest, image="registry.example/pi@sha256:" + "5" * 64)
            adapter = FakeRuntimeAdapter()
            adapter.prepare(spec, manifest)
            with self.assertRaises(RuntimeSpecError):
                adapter.prepare(spec, manifest)
            with self.assertRaises(RuntimeSpecError):
                adapter.attest("run_" + "0" * 32)

    def test_secretary_spec_has_no_working_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._manifest(Path(temporary), authority="secretary")
            spec = build_runtime_spec(manifest, image="registry.example/pi@sha256:" + "5" * 64)
            self.assertIsNone(spec["workingCopy"])
            attestation = FakeRuntimeAdapter()
            handle = attestation.prepare(spec, manifest)
            self.assertIsNone(validate_attestation(attestation.attest(handle.run_id), spec, manifest)["workingCopy"])


if __name__ == "__main__":
    unittest.main()
