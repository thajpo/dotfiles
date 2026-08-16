"""P7 durable workstream and controller-child lifecycle tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.pi_control.conversations import create_conversation
from scripts.pi_control.pi_client import PiControllerClient
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.pi_workstreams import create_workstream
from scripts.pi_control.projects import work_index
from scripts.pi_control.investigators import bind_investigation_run
from scripts.pi_control.launch import attest_run, prepare_run, stop_run
from scripts.pi_control.models import canonical_json, new_id
from scripts.pi_control.subagents import bind_child_run, create_child_assignment, record_child_terminal
from tests.control_plane.helpers import ConfiguredFailpointController, FailpointRaised
from tests.control_plane.test_p2_contract import tool_runtime
from tests.pi_test_build import allow_test_only_registered_build_rows
from tests.test_pi_core import _BUILD_ID, _host, _register_build, _repo


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repository), *args], env={"PATH": os.defpath, "HOME": "/nonexistent", "LC_ALL": "C"}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    return result.stdout.strip()


class P7WorkstreamTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = allow_test_only_registered_build_rows()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_personal_is_idempotent_and_controller_derived_from_primary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p7-personal-") as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = client.register_project(str(_repo(root, "repository")))
            with PiStore(root / "state") as store:
                primary = dict(store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary'", (project["project_id"],)).fetchone())
                first = create_conversation(store, project_id=project["project_id"], role="personal", display_name="personal", idempotency_key="p7-personal")
                replay = create_conversation(store, project_id=project["project_id"], role="personal", display_name="personal", working_copy_id=primary["working_copy_id"], idempotency_key="p7-personal")
                self.assertEqual(first["conversation_id"], replay["conversation_id"])
                self.assertEqual(first["working_copy_id"], primary["working_copy_id"])
                secretary = store.conn.execute("SELECT conversation_id FROM conversations WHERE project_id=? AND role='secretary'", (project["project_id"],)).fetchone()[0]
                self.assertNotEqual(first["conversation_id"], secretary)
                self.assertTrue((root / "state" / "environments" / primary["working_copy_id"]).is_dir())

    def test_workstream_faults_replay_before_and_after_git_and_database_effects(self) -> None:
        boundaries = ("operation.intent.after", "worktree.create.before", "worktree.create.after", "event.commit.before", "event.commit.after")
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory(prefix="p7-saga-") as raw:
                root = Path(raw)
                repository = _repo(root, "repository")
                client = PiControllerClient(root / "state")
                project = client.register_project(str(repository))
                failpoint = ConfiguredFailpointController(boundary, raise_exception=FailpointRaised)
                with PiStore(root / "state") as store:
                    target_before = _git(repository, "rev-parse", "HEAD")
                    with self.assertRaises(FailpointRaised):
                        create_workstream(store, project_id=project["project_id"], title="fault", idempotency_key="p7-fault", failpoint=failpoint)
                    operation = dict(store.conn.execute("SELECT * FROM operations WHERE idempotency_key='p7-fault'").fetchone())
                    intent = json.loads(operation["request_json"])
                    self.assertEqual(intent["baseOid"], target_before)
                    self.assertEqual(intent["targetRef"], _git(repository, "symbolic-ref", "--quiet", "HEAD"))
                    workstream = create_workstream(store, project_id=project["project_id"], title="fault", idempotency_key="p7-fault")
                    self.assertEqual(workstream["observed_state"], "ready")
                    self.assertEqual(workstream["worktree_path"], intent["worktreePath"])
                    self.assertEqual(_git(repository, "rev-parse", "HEAD"), target_before)
                    self.assertEqual(_git(Path(intent["worktreePath"]), "rev-parse", "HEAD"), target_before)
                    self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0], 1)
                    self.assertEqual(store.conn.execute("SELECT state FROM operations WHERE operation_id=?", (operation["operation_id"],)).fetchone()[0], "succeeded")

    def test_two_workstreams_have_distinct_git_session_environment_and_writer_authority(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p7-workstreams-") as raw:
            root = Path(raw)
            repository = _repo(root, "repository")
            client = PiControllerClient(root / "state")
            project = client.register_project(str(repository))
            with PiStore(root / "state") as store:
                first = create_workstream(store, project_id=project["project_id"], title="one", idempotency_key="p7-one")
                second = create_workstream(store, project_id=project["project_id"], title="two", idempotency_key="p7-two")
                rows = [dict(row) for row in store.conn.execute("SELECT w.*,c.pi_session_id,c.session_file FROM workstreams w JOIN conversations c ON c.conversation_id=w.conversation_id ORDER BY w.title")]
                self.assertEqual(len(rows), 2)
                for field in ("working_copy_id", "conversation_id", "branch_ref", "worktree_path", "package_environment_root", "pi_session_id", "session_file"):
                    self.assertNotEqual(rows[0][field], rows[1][field], field)
                self.assertEqual(first["target_ref"], second["target_ref"])
                self.assertNotEqual(first["branch_ref"], first["target_ref"])
                self.assertTrue(all(Path(row["package_environment_root"]).is_dir() for row in rows))
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM presentation_assignments WHERE observed_state='unknown'").fetchone()[0], 2)

    def test_headless_workstream_has_no_presentation_and_work_index_bucket(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p7-headless-") as raw:
            root = Path(raw)
            repository = _repo(root, "repository")
            client = PiControllerClient(root / "state")
            project = client.register_project(str(repository))
            with PiStore(root / "state") as store:
                headful = create_workstream(store, project_id=project["project_id"], title="headful", idempotency_key="p7-headful")
                headless = create_workstream(store, project_id=project["project_id"], title="headless", brief={"kind": "headless-worker", "task": "work"}, idempotency_key="p7-headless", headful=False)
                headful_assignment = store.conn.execute("SELECT desired_state FROM presentation_assignments WHERE conversation_id=?", (headful["conversation_id"],)).fetchone()
                headless_assignment = store.conn.execute("SELECT 1 FROM presentation_assignments WHERE conversation_id=?", (headless["conversation_id"],)).fetchone()
                self.assertEqual(headful_assignment["desired_state"], "present")
                self.assertIsNone(headless_assignment)
                index = work_index(store, project["project_id"])
                working_ids = [item["id"] for item in index["Working now"]]
                headless_ids = [item["id"] for item in index["Headless workers"]]
                self.assertIn(headful["conversation_id"], working_ids)
                self.assertNotIn(headless["conversation_id"], working_ids)
                self.assertEqual(headless_ids, [headless["conversation_id"]])
                self.assertTrue(all(item["headful"] for item in index["Working now"] if item["agentType"] == "workstream"))
                self.assertFalse(index["Headless workers"][0]["headful"])

    def test_child_snapshots_and_terminal_records_are_independent_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p7-child-") as raw:
            root = Path(raw)
            repository = _repo(root, "repository")
            client = PiControllerClient(root / "state")
            project = client.register_project(str(repository))
            with PiStore(root / "state") as store:
                _register_build(store, root)
                primary = dict(store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary'", (project["project_id"],)).fetchone())
                writer = create_conversation(store, project_id=project["project_id"], role="personal", display_name="parent", idempotency_key="p7-parent")
                parent_id = new_id("run")
                parent = prepare_run(store, conversation_id=writer["conversation_id"], build_id=_BUILD_ID, host_process=_host("personal"), tool_runtime=tool_runtime(parent_id, project, primary), run_id=parent_id)
                attest_run(store, run_id=parent_id, manifest_digest=parent.manifest["manifestDigest"])
                assignments = [
                    create_child_assignment(store, parent_run_id=parent_id, semantic_role="reviewer", task="review snapshot", idempotency_key="p7-child-review"),
                    create_child_assignment(store, parent_run_id=parent_id, semantic_role="scout", task="scout snapshot", idempotency_key="p7-child-scout"),
                ]
                self.assertNotEqual(assignments[0]["child_conversation_id"], assignments[1]["child_conversation_id"])
                self.assertNotEqual(assignments[0]["snapshot_ref"], assignments[1]["snapshot_ref"])
                self.assertEqual({item["snapshot_commit_oid"] for item in assignments}, {primary["expected_head_oid"]})
                for assignment in assignments:
                    source = {"snapshotId": assignment["snapshot_id"], "snapshotRef": assignment["snapshot_ref"], "snapshotCommitOid": assignment["snapshot_commit_oid"], "snapshotTreeOid": assignment["snapshot_tree_oid"], "sourceHeadOid": assignment["snapshot_commit_oid"], "sourceTreeOid": assignment["snapshot_tree_oid"], "authority": "read-only"}
                    child = prepare_run(store, conversation_id=assignment["child_conversation_id"], build_id=_BUILD_ID, host_process=_host(assignment["runtime_role"]), parent_run_id=parent_id, child_source=source)
                    bind_child_run(store, child_request_id=assignment["child_request_id"], child_run_id=child.run["run_id"])
                    if assignment["runtime_role"] == "investigator":
                        bind_investigation_run(store, conversation_id=assignment["child_conversation_id"], run_id=child.run["run_id"])
                    attest_run(store, run_id=child.run["run_id"], manifest_digest=child.manifest["manifestDigest"])
                    stop_run(store, run_id=child.run["run_id"], reason="test-complete")
                    terminal = record_child_terminal(store, child_request_id=assignment["child_request_id"], terminal_class="success", result={"ok": True})
                    self.assertEqual(terminal["parent_run_id"], parent_id)
                    self.assertEqual(terminal["terminal_class"], "success")
                    run = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (child.run["run_id"],)).fetchone()
                    self.assertEqual(json.loads(run["child_source_json"])["snapshotRef"], assignment["snapshot_ref"])
                    child.close()
                self.assertEqual(store.conn.execute("SELECT COUNT(DISTINCT manifest_path) FROM runs WHERE parent_run_id=?", (parent_id,)).fetchone()[0], 2)
                parent.close()


    def test_child_replay_does_not_terminalize_live_bound_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p7-replay-") as raw:
            root = Path(raw)
            repository = _repo(root, "repository")
            client = PiControllerClient(root / "state")
            project = client.register_project(str(repository))
            with PiStore(root / "state") as store:
                _register_build(store, root)
                primary = dict(store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary'", (project["project_id"],)).fetchone())
                writer = create_conversation(store, project_id=project["project_id"], role="personal", display_name="parent", idempotency_key="p7-replay-parent")
                parent_id = new_id("run")
                parent = prepare_run(store, conversation_id=writer["conversation_id"], build_id=_BUILD_ID, host_process=_host("personal"), tool_runtime=tool_runtime(parent_id, project, primary), run_id=parent_id)
                attest_run(store, run_id=parent_id, manifest_digest=parent.manifest["manifestDigest"])
                assignment = create_child_assignment(store, parent_run_id=parent_id, semantic_role="reviewer", task="review snapshot", idempotency_key="p7-replay-child")
                source = {"snapshotId": assignment["snapshot_id"], "snapshotRef": assignment["snapshot_ref"], "snapshotCommitOid": assignment["snapshot_commit_oid"], "snapshotTreeOid": assignment["snapshot_tree_oid"], "sourceHeadOid": assignment["snapshot_commit_oid"], "sourceTreeOid": assignment["snapshot_tree_oid"], "authority": "read-only"}
                first_child = prepare_run(store, conversation_id=assignment["child_conversation_id"], build_id=_BUILD_ID, host_process=_host(assignment["runtime_role"]), parent_run_id=parent_id, child_source=source)
                bind_child_run(store, child_request_id=assignment["child_request_id"], child_run_id=first_child.run["run_id"])
                attest_run(store, run_id=first_child.run["run_id"], manifest_digest=first_child.manifest["manifestDigest"])
                from unittest import mock
                with mock.patch("scripts.pi_control.host_supervisor.launch_host_pi", side_effect=RuntimeError("bind failure replay")):
                    with self.assertRaises(RuntimeError):
                        from scripts.pi_control.subagents import run_controller_child
                        run_controller_child(store, parent_run_id=parent_id, semantic_role="reviewer", task="review snapshot", idempotency_key="p7-replay-child", build_id=_BUILD_ID, model="test/model")
                child_request = store.conn.execute("SELECT * FROM child_requests WHERE child_request_id=?", (assignment["child_request_id"],)).fetchone()
                self.assertEqual(child_request["child_run_id"], first_child.run["run_id"])
                self.assertEqual(child_request["state"], "running")
                self.assertIsNone(child_request["completed_at"])
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM child_terminal_records WHERE child_run_id=?", (first_child.run["run_id"],)).fetchone()[0], 0)
                first_child.close()
                parent.close()

    def test_child_launch_failure_without_run_identity_keeps_cause(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p7-noidentity-") as raw:
            root = Path(raw)
            repository = _repo(root, "repository")
            client = PiControllerClient(root / "state")
            project = client.register_project(str(repository))
            with PiStore(root / "state") as store:
                _register_build(store, root)
                primary = dict(store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary'", (project["project_id"],)).fetchone())
                writer = create_conversation(store, project_id=project["project_id"], role="personal", display_name="parent", idempotency_key="p7-noid-parent")
                parent_id = new_id("run")
                parent = prepare_run(store, conversation_id=writer["conversation_id"], build_id=_BUILD_ID, host_process=_host("personal"), tool_runtime=tool_runtime(parent_id, project, primary), run_id=parent_id)
                attest_run(store, run_id=parent_id, manifest_digest=parent.manifest["manifestDigest"])
                create_child_assignment(store, parent_run_id=parent_id, semantic_role="reviewer", task="review snapshot", idempotency_key="p7-noid-child")
                from unittest import mock
                from scripts.pi_control.subagents import SubagentError, run_controller_child
                with mock.patch("scripts.pi_control.host_supervisor.launch_host_pi", side_effect=RuntimeError("launch exploded")):
                    with self.assertRaises(SubagentError) as caught:
                        run_controller_child(store, parent_run_id=parent_id, semantic_role="reviewer", task="review snapshot", idempotency_key="p7-noid-child", build_id=_BUILD_ID, model="test/model")
                self.assertIsInstance(caught.exception.__cause__, RuntimeError)
                self.assertIn("launch exploded", str(caught.exception.__cause__))
                parent.close()


if __name__ == "__main__":
    unittest.main()
