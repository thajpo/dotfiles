"""P2 host/tool runtime split; Docker runtime behavior belongs to P5."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import unittest

from scripts.pi_control.run_manifest import ManifestError, manifest_digest, validate_manifest
from scripts.pi_control.models import canonical_json


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/control-plane/run-manifest.v2.json"


class RuntimeSpecTests(unittest.TestCase):
    def manifest(self) -> dict:
        return json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_host_role_has_no_tool_runtime(self) -> None:
        manifest = validate_manifest(self.manifest())
        self.assertEqual(manifest["conversation"]["authorityProfile"], "host-read-only")
        self.assertIsNone(manifest["toolRuntime"])
        self.assertIsNone(manifest["workingCopy"])

    def test_writer_requires_complete_distinct_image_identities(self) -> None:
        manifest = self.manifest()
        manifest["conversation"] = {**manifest["conversation"], "role": "personal", "authorityProfile": "writer-container"}
        manifest["scope"] = {**manifest["scope"], "source": "assigned-working-copy"}
        manifest["workingCopy"] = {"workingCopyId": manifest["scope"]["workingCopyId"], "projectId": manifest["project"]["projectId"], "resourceVersion": manifest["scope"]["workingCopyResourceVersion"], "kind": "primary", "purpose": "personal", "effectiveMode": "isolated", "hostPath": manifest["scope"]["rootPath"], "gitDir": "/workspace/.git", "writerEpoch": 1}
        manifest["hostProcess"] = {**manifest["hostProcess"], "toolProfile": "personal"}
        image_digest = "sha256:" + "e" * 64
        manifest["toolRuntime"] = {
            "specVersion": 2, "specHash": "", "platform": "linux/amd64", "imageReference": "registry.invalid/pi@" + image_digest,
            "imageConfigId": "sha256:" + "f" * 64, "registryDigest": image_digest, "command": ["python3", "-c", "idle"],
            "uid": 1000, "gid": 1000, "workdir": "/workspace", "readOnlyRoot": True,
            "mounts": [
                {"kind": "working-copy", "source": manifest["scope"]["rootPath"], "target": "/workspace", "readOnly": False, "sourceDevice": 1, "sourceInode": 2},
                {"kind": "git-mask", "source": "/state/git-mask", "target": "/workspace/.git", "readOnly": True, "sourceDevice": 1, "sourceInode": 3},
                {"kind": "package-environment", "source": f"/state/environments/{manifest['scope']['workingCopyId']}", "target": "/environments", "readOnly": False, "sourceDevice": 1, "sourceInode": 4},
            ],
            "tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777"}, "networkMode": "none", "capDrop": ["ALL"],
            "securityOpt": ["no-new-privileges:true"], "environment": {"HOME": "/tmp"},
            "labels": {"pi.control.managed": "true", "pi.control.run-id": manifest["runId"], "pi.control.project-id": manifest["project"]["projectId"], "pi.control.working-copy-id": manifest["scope"]["workingCopyId"], "pi.control.writer-epoch": "1", "pi.control.controller-build-id": manifest["installedBuild"]["buildId"]},
            "resources": {"memoryBytes": 1, "nanoCpus": 1, "pidsLimit": 1},
        }
        hash_body = dict(manifest["toolRuntime"])
        hash_body.pop("specHash")
        manifest["toolRuntime"]["specHash"] = "sha256:" + hashlib.sha256(canonical_json(hash_body).encode()).hexdigest()
        manifest["manifestDigest"] = manifest_digest(manifest)
        self.assertEqual(validate_manifest(manifest)["toolRuntime"]["platform"], "linux/amd64")
        incomplete = copy.deepcopy(manifest)
        incomplete["toolRuntime"]["imageConfigId"] = image_digest
        incomplete["manifestDigest"] = manifest_digest(incomplete)
        with self.assertRaises(ManifestError):
            validate_manifest(incomplete)

    def test_host_and_tool_targets_cannot_be_collapsed(self) -> None:
        manifest = self.manifest()
        manifest["executionTarget"] = "container"
        manifest["manifestDigest"] = manifest_digest(manifest)
        with self.assertRaises(ManifestError):
            validate_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
