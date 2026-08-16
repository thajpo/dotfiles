"""Submission, review, and integration through the authenticated channel."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts.pi_control.controller_channel import ControllerChannelError, PROTOCOL_VERSION
from scripts.pi_control.pi_client import PiControllerClient
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.host_supervisor import _ROLE_OPERATIONS, _rpc
from scripts.pi_control.launch import prepare_run
from scripts.pi_control.models import new_id
from tests.pi_test_build import allow_test_only_registered_build_rows
from tests.test_pi_core import _BUILD_ID, _host, _repo


def _generation_fixture(root: Path) -> Path:
    generation = root / "generation"
    (generation / "bin").mkdir(parents=True)
    for name in ("pi-system-run", "pi-system-container-run"):
        launcher = generation / "bin" / name
        launcher.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        launcher.chmod(0o755)
    return generation


def _register_generation_build(store: PiStore, generation: Path) -> None:
    from scripts.pi_control.models import utc_now
    digest = "sha256:" + "a" * 64
    store.conn.execute(
        "INSERT INTO installed_builds(build_id,source_commit,source_tree_hash,build_manifest_path,build_manifest_digest,resource_manifest_path,resource_manifest_digest,pi_version,package_lock_hash,status,installed_at,activated_at,rollback_path,verification_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_BUILD_ID, None, digest, str(generation / "build-manifest.json"), digest, str(generation / "release-resources.json"), digest, "0.83.0", digest, "staged", utc_now(), None, None, json.dumps({"verified": True})),
    )


class ChangeFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = allow_test_only_registered_build_rows()
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fixture(self, root: Path, *, dirty: bool = False) -> tuple[PiStore, dict, dict]:
        generation = _generation_fixture(root)
        (generation / "build-manifest.json").write_text("fixture", encoding="utf-8")
        (generation / "release-resources.json").write_text("fixture", encoding="utf-8")
        repo = _repo(root, "flow")
        if dirty:
            (repo / "new.txt").write_text("work\n", encoding="utf-8")
        client = PiControllerClient(root / "state")
        project = client.register_project(str(repo))
        store = PiStore(root / "state").open()
        _register_generation_build(store, generation)
        working = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary'", (project["project_id"],)).fetchone()
        conversation = client.create_conversation(project_id=project["project_id"], role="personal", display_name="writer", working_copy_id=working["working_copy_id"])
        store.conn.execute("UPDATE conversations SET observed_state='ready' WHERE conversation_id=?", (conversation["conversation_id"],))
        return store, dict(project), dict(conversation)

    def _attested_writer_run(self, store: PiStore, client: PiControllerClient, conversation: dict) -> dict:
        from tests.control_plane.test_p2_contract import tool_runtime
        working = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (conversation["working_copy_id"],)).fetchone()
        run_id = new_id("run")
        prepared = prepare_run(store, conversation_id=conversation["conversation_id"], build_id=_BUILD_ID, host_process=_host("personal"), tool_runtime=tool_runtime(run_id, {"project_id": conversation["project_id"]}, working), run_id=run_id)
        client.attest_run(run_id=prepared.run["run_id"], manifest_digest=prepared.manifest["manifestDigest"])
        return prepared

    def _attested_secretary_run(self, store: PiStore, client: PiControllerClient, project: dict) -> dict:
        conversation = store.conn.execute("SELECT * FROM conversations WHERE project_id=? AND role='secretary'", (project["project_id"],)).fetchone()
        prepared = prepare_run(store, conversation_id=conversation["conversation_id"], build_id=_BUILD_ID, host_process=_host("secretary"))
        client.attest_run(run_id=prepared.run["run_id"], manifest_digest=prepared.manifest["manifestDigest"])
        return prepared

    def test_change_submit_and_list_through_channel(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, project, conversation = self._fixture(root, dirty=True)
            try:
                client = PiControllerClient(root / "state")
                writer = self._attested_writer_run(store, client, conversation)
                base = {"protocolVersion": PROTOCOL_VERSION, "type": "request", "requestId": "request-1"}
                submission = _rpc(store, writer.run["run_id"], writer.manifest["manifestDigest"], {**base, "operation": "change.submit", "payload": {"title": "channel change", "summary": "submitted over the channel", "targetRef": "refs/heads/master", "captureMode": "dirty", "selectedPaths": ["new.txt"], "excludedPaths": [], "idempotencyKey": "submit-1"}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                self.assertEqual(submission["revision"], 1)
                self.assertEqual(submission["captureMode"], "temporary-index")
                self.assertIn("new.txt", submission["changedPaths"])
                replay = _rpc(store, writer.run["run_id"], writer.manifest["manifestDigest"], {**base, "operation": "change.submit", "payload": {"title": "channel change", "summary": "submitted over the channel", "targetRef": "refs/heads/master", "captureMode": "dirty", "selectedPaths": ["new.txt"], "excludedPaths": [], "idempotencyKey": "submit-1"}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                self.assertEqual(replay["changeId"], submission["changeId"])
                writer.close()

                secretary = self._attested_secretary_run(store, client, project)
                listing = _rpc(store, secretary.run["run_id"], secretary.manifest["manifestDigest"], {**base, "operation": "change.list", "payload": {}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                self.assertTrue(any(row["change_id"] == submission["changeId"] for row in listing))
                secretary.close()
            finally:
                store.close()

    def test_review_request_launches_detached_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, project, conversation = self._fixture(root, dirty=True)
            try:
                client = PiControllerClient(root / "state")
                writer = self._attested_writer_run(store, client, conversation)
                base = {"protocolVersion": PROTOCOL_VERSION, "type": "request", "requestId": "request-1"}
                submission = _rpc(store, writer.run["run_id"], writer.manifest["manifestDigest"], {**base, "operation": "change.submit", "payload": {"title": "review me", "summary": "needs a review", "targetRef": "refs/heads/master", "captureMode": "dirty", "selectedPaths": ["new.txt"], "excludedPaths": [], "idempotencyKey": "submit-2"}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                writer.close()

                secretary = self._attested_secretary_run(store, client, project)
                launched = []
                with mock.patch("scripts.pi_control.subagents._spawn_detached", side_effect=lambda argv, log: launched.append(argv) or 4246):
                    assignment = _rpc(store, secretary.run["run_id"], secretary.manifest["manifestDigest"], {**base, "operation": "review.request", "payload": {"changeId": submission["changeId"], "revision": 1}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                self.assertEqual(assignment["changeId"], submission["changeId"])
                self.assertTrue(assignment["launched"])
                argv = launched[0]
                self.assertIn("--expected-role", argv)
                self.assertIn("reviewer", argv)
                self.assertEqual(argv[argv.index("--conversation-id") + 1], assignment["conversationId"])
                secretary.close()
            finally:
                store.close()

    def test_integration_analysis_and_authority_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, project, conversation = self._fixture(root, dirty=True)
            try:
                client = PiControllerClient(root / "state")
                writer = self._attested_writer_run(store, client, conversation)
                base = {"protocolVersion": PROTOCOL_VERSION, "type": "request", "requestId": "request-1"}
                submission = _rpc(store, writer.run["run_id"], writer.manifest["manifestDigest"], {**base, "operation": "change.submit", "payload": {"title": "analyze me", "summary": "target analysis", "targetRef": "refs/heads/master", "captureMode": "dirty", "selectedPaths": ["new.txt"], "excludedPaths": [], "idempotencyKey": "submit-3"}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                writer.close()

                secretary = self._attested_secretary_run(store, client, project)
                analysis = _rpc(store, secretary.run["run_id"], secretary.manifest["manifestDigest"], {**base, "operation": "integration.analyze", "payload": {"changeId": submission["changeId"], "revision": 1, "targetRef": "refs/heads/master"}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                self.assertIn("strategy", analysis)
                # The secretary cannot submit changes; writers cannot request
                # reviews or integrate through the channel.
                self.assertNotIn("change.submit", _ROLE_OPERATIONS["secretary"])
                self.assertNotIn("integration.integrate", _ROLE_OPERATIONS["secretary"])
                self.assertNotIn("integration.integrate", _ROLE_OPERATIONS["personal"])
                with self.assertRaisesRegex(ControllerChannelError, "not granted"):
                    _rpc(store, secretary.run["run_id"], secretary.manifest["manifestDigest"], {**base, "operation": "change.submit", "payload": {"title": "x", "summary": "y", "targetRef": "refs/heads/master", "captureMode": "dirty", "selectedPaths": ["new.txt"], "excludedPaths": [], "idempotencyKey": "no"}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                secretary.close()
            finally:
                store.close()

    def test_cross_project_review_request_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, project, conversation = self._fixture(root, dirty=True)
            try:
                client = PiControllerClient(root / "state")
                other = client.register_project(str(_repo(root, "other")))
                secretary = self._attested_secretary_run(store, client, project)
                base = {"protocolVersion": PROTOCOL_VERSION, "type": "request", "requestId": "request-1"}
                with self.assertRaisesRegex(ControllerChannelError, "crosses the authenticated project"):
                    _rpc(store, secretary.run["run_id"], secretary.manifest["manifestDigest"], {**base, "operation": "review.request", "payload": {"changeId": "chg_" + other["project_id"][4:], "revision": 1}}, child_context={"buildId": _BUILD_ID, "model": "scripted/scripted-1"})
                secretary.close()
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
