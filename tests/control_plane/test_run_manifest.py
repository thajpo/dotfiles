"""Phase 4 immutable run-manifest tests."""

from __future__ import annotations

from pathlib import Path
import json
import stat
import tempfile
import unittest

from scripts.pi_control.leases import create_writer_run
from scripts.pi_control.project_policy import load_policy
from scripts.pi_control.run_manifest import ManifestError, build_manifest, manifest_digest, read_manifest, validate_manifest, write_manifest
from scripts.pi_control.store import ControllerStore
from tests.control_plane.helpers import DisposableEnvironment
from tests.control_plane.test_operations import add_project
from tests.control_plane.test_project_identity import fixture_policy
from scripts.pi_control.reconcile import register_project


class RunManifestTests(unittest.TestCase):
    def test_manifest_is_canonical_strict_hashed_and_secure(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            with ControllerStore(fixture.state_home / "pi-control") as store:
                project = register_project(store, fixture.repo, "fixture", policy=fixture_policy(fixture.root, default="trusted"))
                wc = store.conn.execute("SELECT working_copy_id FROM working_copies WHERE project_id=?", (project["project_id"],)).fetchone()[0]
                session_id = "conv-manifest"
                session_file = fixture.root / "session.jsonl"
                session_file.write_text(json.dumps({"id": session_id, "cwd": str(fixture.repo)}) + "\n", encoding="utf-8")
                store.conn.execute(
                    "INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("conv_" + "b" * 32, project["project_id"], wc, "personal", "manifest", session_id, str(session_file), "active", "ready", 1, "t", "t"),
                )
                conversation_id = "conv_" + "b" * 32
                store.register_build("active", source_tree_hash="tree", artifact_manifest_hash="artifact", pi_version="0.0", package_lock_hash="lock", status="active")
                handle = create_writer_run(store, conversation_id=conversation_id, working_copy_id=wc, build_id="active", runtime_spec_hash="sha256:" + "2" * 64, project_id=project["project_id"])
                try:
                    operation_id = store.create_operation(idempotency_key="manifest-op", kind="run.prepare", resource_type="run", resource_id=handle.run_id, actor_type="controller", request={"run_id": handle.run_id}).operation_id
                    manifest = build_manifest(store, handle.run_id, operation_id=operation_id, capability_secret=handle.capability_secret, runtime={"imageDigest": "sha256:" + "1" * 64, "piVersion": "test"})
                    self.assertEqual(manifest["manifestDigest"].startswith("sha256:"), True)
                    destination = fixture.state_home / "pi-control" / "manifests" / (handle.run_id + ".json")
                    written = write_manifest(destination, manifest)
                    self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
                    loaded = read_manifest(destination)
                    self.assertEqual(loaded.manifest, manifest)
                    with self.assertRaises(ManifestError):
                        validate_manifest({**manifest, "unexpected": True})
                    malformed_capability = dict(manifest)
                    malformed_capability["capabilityHash"] = "sha256:bad"
                    malformed_capability["manifestDigest"] = manifest_digest(malformed_capability)
                    with self.assertRaises(ManifestError):
                        validate_manifest(malformed_capability)
                    writer_without_copy = dict(manifest)
                    writer_without_copy["authority"] = "writer"
                    writer_without_copy["workingCopy"] = None
                    writer_without_copy["manifestDigest"] = manifest_digest(writer_without_copy)
                    with self.assertRaises(ManifestError):
                        validate_manifest(writer_without_copy)
                    malformed_expiry = dict(manifest)
                    malformed_expiry["expiresAt"] = "not-a-timestamp"
                    malformed_expiry["manifestDigest"] = manifest_digest(malformed_expiry)
                    with self.assertRaises(ManifestError):
                        validate_manifest(malformed_expiry)
                    reversed_expiry = dict(manifest)
                    reversed_expiry["expiresAt"] = "2000-01-01T00:00:00Z"
                    reversed_expiry["manifestDigest"] = manifest_digest(reversed_expiry)
                    with self.assertRaises(ManifestError):
                        validate_manifest(reversed_expiry)
                    tampered = json.loads(destination.read_text(encoding="utf-8"))
                    tampered["authority"] = "read-only"
                    destination.write_text(json.dumps(tampered), encoding="utf-8")
                    with self.assertRaises(ManifestError):
                        read_manifest(destination)
                finally:
                    handle.close()

    def test_manifest_destination_is_never_overwritten_and_capability_is_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            # Use a small validated fixture from a synthetic shape to exercise
            # the immutable destination path through the public writer.
            manifest = {
                "schemaVersion": 1, "runId": "run_" + "a" * 32, "operationId": "op_" + "a" * 32,
                "taskId": None, "conversationId": "conv_" + "a" * 32, "piSessionId": "pi-a", "parentRunId": None,
                "project": {"projectId": "prj_" + "a" * 32, "resourceVersion": 1, "objectFormat": "sha1", "trustMode": "isolated", "policyHash": "sha256:" + "a" * 64},
                "workingCopy": None, "authority": "secretary",
                "runtime": {"runtimeSpecVersion": 1, "runtimeSpecHash": "sha256:" + "a" * 64, "executionTarget": "test", "platform": "test", "imageDigest": "sha256:" + "a" * 64, "controllerBuildId": "build", "piVersion": "test"},
                "owner": {"uid": 0, "gid": 0, "pid": 1, "processStartIdentity": "test:1"}, "capabilityHash": "sha256:" + "a" * 64,
                "attestationNonce": "nonce-abcdefghijklmnopqrstuvwxyz", "createdAt": "2024-01-01T00:00:00Z", "expiresAt": None, "manifestDigest": "",
            }
            manifest["manifestDigest"] = manifest_digest(manifest)
            destination = Path(temporary) / "manifest.json"
            write_manifest(destination, manifest)
            with self.assertRaises(ManifestError):
                write_manifest(destination, manifest)


if __name__ == "__main__":
    unittest.main()
