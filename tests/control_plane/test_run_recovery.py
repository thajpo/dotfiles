"""Exact lost-run recovery: process fencing, writer lock probe, and claim release.

The critical regression this guards: recovery previously checked
`authority == "writer"` while every real writer run stores
`authority == "writer-container"`, leaving the stale writer claim in place
and permanently blocking relaunch of the working copy.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.pi_control.conversations import create_conversation
from scripts.pi_control.launch import LaunchError, fail_run, prepare_run
from scripts.pi_control.pi_client import PiControllerClient
from scripts.pi_control.pi_reconcile import ReconcileError
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.process_adapter import process_start_identity
from scripts.pi_control.writer_lock import WriterLock
from tests.control_plane.test_p2_contract import tool_runtime
from tests.pi_test_build import allow_test_only_registered_build_rows
from tests.test_pi_core import _BUILD_ID, _host, _register_build, _repo

_DEAD_PID = 999999
_DEAD_IDENTITY = "linux:missing:1"


class RunRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = allow_test_only_registered_build_rows()
        patcher.start()
        self.addCleanup(patcher.stop)
        cleanup_patcher = mock.patch("scripts.pi_control.docker_runtime.cleanup_run_container", return_value={"absent": True, "errors": []})
        self.cleanup_container = cleanup_patcher.start()
        self.addCleanup(cleanup_patcher.stop)

    def _registered(self, root: Path, name: str) -> dict:
        client = PiControllerClient(root / "state")
        return client.register_project(str(_repo(root, name)), name)

    def _writer_conversation(self, store: PiStore, project: dict, name: str) -> dict:
        return create_conversation(store, project_id=project["project_id"], role="personal", display_name=name, idempotency_key=f"recovery-{name}")

    def _prepare_writer(self, store: PiStore, project: dict, conversation: dict, name: str) -> dict:
        from scripts.pi_control.models import new_id
        primary = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary'", (project["project_id"],)).fetchone()
        run_id = new_id("run")
        return prepare_run(store, conversation_id=conversation["conversation_id"], build_id=_BUILD_ID, host_process=_host("personal"), tool_runtime=tool_runtime(run_id, {"project_id": project["project_id"]}, primary), run_id=run_id)

    def _kill_run(self, store: PiStore, run_id: str) -> None:
        store.conn.execute("UPDATE runs SET owner_pid=?,owner_start_identity=?,child_pid=?,child_start_identity=?,observed_state='running' WHERE run_id=?", (_DEAD_PID, _DEAD_IDENTITY, _DEAD_PID + 1, _DEAD_IDENTITY, run_id))

    def test_writer_recovery_clears_claim_and_frees_working_copy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = self._registered(root, "writer")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = self._writer_conversation(store, project, "writer")
                prepared = self._prepare_writer(store, project, conversation, "writer")
                run_id = prepared.run["run_id"]
                claim = store.conn.execute("SELECT active_writer_run_id FROM working_copies WHERE working_copy_id=?", (conversation["working_copy_id"],)).fetchone()[0]
                self.assertEqual(claim, run_id)
                self._kill_run(store, run_id)
                prepared.close()
            attention = client.reconcile_run(run_id=run_id)
            self.assertEqual(attention["decision"], "needs-attention")
            recovered = client.recover_run(run_id=run_id, actor_id="test-writer-recovery")
            self.assertEqual(recovered["observed_state"], "lost")
            self.cleanup_container.assert_called_once_with(mock.ANY, run_id=run_id)
            with PiStore(root / "state") as store:
                claim = store.conn.execute("SELECT active_writer_run_id FROM working_copies WHERE working_copy_id=?", (conversation["working_copy_id"],)).fetchone()[0]
                self.assertIsNone(claim)
                fresh = self._prepare_writer(store, project, conversation, "writer-fresh")
                fresh.close()
                self.assertIsNotNone(fresh.run["run_id"])
                self.assertNotEqual(fresh.run["run_id"], run_id)

    def test_writer_recovery_before_manifest_needs_no_container_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = self._registered(root, "pre-manifest")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = self._writer_conversation(store, project, "pre-manifest")
                prepared = self._prepare_writer(store, project, conversation, "pre-manifest-run")
                run_id = prepared.run["run_id"]
                self._kill_run(store, run_id)
                store.conn.execute("UPDATE runs SET manifest_path=NULL,container_id=NULL,container_observation_json=NULL WHERE run_id=?", (run_id,))
                prepared.close()
            attention = client.reconcile_run(run_id=run_id)
            self.assertEqual(attention["decision"], "needs-attention")
            recovered = client.recover_run(run_id=run_id, actor_id="test-pre-manifest")
            self.assertEqual(recovered["observed_state"], "lost")
            self.cleanup_container.assert_not_called()
            with PiStore(root / "state") as store:
                claim = store.conn.execute("SELECT active_writer_run_id FROM working_copies WHERE working_copy_id=?", (conversation["working_copy_id"],)).fetchone()[0]
                self.assertIsNone(claim)

    def test_recovery_of_stopping_writer_proves_cleanup_and_releases_claim(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = self._registered(root, "stopping")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = self._writer_conversation(store, project, "stopping")
                prepared = self._prepare_writer(store, project, conversation, "stopping-run")
                run_id = prepared.run["run_id"]
                self._kill_run(store, run_id)
                store.conn.execute("UPDATE runs SET observed_state='stopping' WHERE run_id=?", (run_id,))
                prepared.close()
            outcome = client.recover_conversation(conversation_id=conversation["conversation_id"], actor_id="surface-repair")
            item = next(item for item in outcome["runs"] if item["runId"] == run_id)
            self.assertEqual(item["decision"], "recovered")
            self.assertEqual(item["run"]["observed_state"], "lost")
            self.cleanup_container.assert_called_once_with(mock.ANY, run_id=run_id)

    def test_writer_recovery_refuses_held_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = self._registered(root, "locked")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = self._writer_conversation(store, project, "writer")
                prepared = self._prepare_writer(store, project, conversation, "writer")
                run_id = prepared.run["run_id"]
                self._kill_run(store, run_id)
                prepared.close()
                with WriterLock.acquire(store.state_root, conversation["working_copy_id"], 1):
                    client.reconcile_run(run_id=run_id)
                    with self.assertRaises(ReconcileError):
                        client.recover_run(run_id=run_id, actor_id="test-held-lock")
                    run = store.conn.execute("SELECT observed_state FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    self.assertEqual(run["observed_state"], "needs_attention")
                    claim = store.conn.execute("SELECT active_writer_run_id FROM working_copies WHERE working_copy_id=?", (conversation["working_copy_id"],)).fetchone()[0]
                    self.assertEqual(claim, run_id)

    def test_writer_recovery_refuses_live_child(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = self._registered(root, "child")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = self._writer_conversation(store, project, "writer")
                prepared = self._prepare_writer(store, project, conversation, "writer")
                run_id = prepared.run["run_id"]
                store.conn.execute("UPDATE runs SET owner_pid=?,owner_start_identity=?,child_pid=?,child_start_identity=?,observed_state='running' WHERE run_id=?", (_DEAD_PID, _DEAD_IDENTITY, os.getpid(), process_start_identity(os.getpid()), run_id))
                prepared.close()
                client.reconcile_run(run_id=run_id)
                with self.assertRaises(ReconcileError):
                    client.recover_run(run_id=run_id, actor_id="test-live-child")
                run = store.conn.execute("SELECT observed_state FROM runs WHERE run_id=?", (run_id,)).fetchone()
                self.assertEqual(run["observed_state"], "needs_attention")

    def test_child_death_flags_needs_attention_with_live_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = self._registered(root, "childdeath")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = self._writer_conversation(store, project, "writer")
                prepared = self._prepare_writer(store, project, conversation, "writer")
                run_id = prepared.run["run_id"]
                store.conn.execute("UPDATE runs SET owner_pid=?,owner_start_identity=?,child_pid=?,child_start_identity=?,observed_state='running' WHERE run_id=?", (os.getpid(), process_start_identity(os.getpid()), _DEAD_PID + 2, _DEAD_IDENTITY, run_id))
                attention = client.reconcile_run(run_id=run_id)
                self.assertEqual(attention["decision"], "needs-attention")
            self.assertIn("child", attention["observation"].get("reason", ""))

    def test_live_writer_requires_exact_live_container(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = self._registered(root, "container-live")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = self._writer_conversation(store, project, "container-live")
                prepared = self._prepare_writer(store, project, conversation, "container-live")
                run_id = prepared.run["run_id"]
                container_id = "a" * 64
                store.conn.execute("UPDATE runs SET container_id=?,observed_state='running' WHERE run_id=?", (container_id, run_id))
                labels = dict(prepared.manifest["toolRuntime"]["labels"])
                prepared.close()
            observed = {"state": "running", "reason": "owner-identity-matches", "owner": {}, "child": {}}
            container = {"id": container_id, "name": "pi-tool-" + run_id.removeprefix("run_"), "labels": labels, "running": True}
            with mock.patch("scripts.pi_control.pi_reconcile._run_observation", return_value=observed), mock.patch("scripts.pi_control.pi_reconcile.inspect_container", return_value=container):
                result = client.reconcile_run(run_id=run_id)
            self.assertEqual(result["decision"], "continue")
            self.assertEqual(result["observation"]["container"]["state"], "running")

    def test_live_writer_with_missing_container_needs_attention_without_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = self._registered(root, "container-missing")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = self._writer_conversation(store, project, "container-missing")
                prepared = self._prepare_writer(store, project, conversation, "container-missing")
                run_id = prepared.run["run_id"]
                store.conn.execute("UPDATE runs SET container_id=?,observed_state='running' WHERE run_id=?", ("b" * 64, run_id))
                prepared.close()
            observed = {"state": "running", "reason": "owner-identity-matches", "owner": {}, "child": {}}
            with mock.patch("scripts.pi_control.pi_reconcile._run_observation", return_value=observed), mock.patch("scripts.pi_control.pi_reconcile.inspect_container", side_effect=RuntimeError("missing")) as inspect:
                result = client.reconcile_run(run_id=run_id)
            self.assertEqual(result["decision"], "needs-attention")
            self.assertEqual(result["run"]["error_code"], "CP_CONTAINER_UNCERTAIN")
            inspect.assert_called_once()

    def test_recovery_is_idempotent_after_lost(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = self._registered(root, "idem")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = self._writer_conversation(store, project, "writer")
                prepared = self._prepare_writer(store, project, conversation, "writer")
                run_id = prepared.run["run_id"]
                self._kill_run(store, run_id)
                prepared.close()
                client.reconcile_run(run_id=run_id)
                first = client.recover_run(run_id=run_id, actor_id="test-idem")
                second = client.recover_run(run_id=run_id, actor_id="test-idem")
                self.assertEqual(first["observed_state"], "lost")
                self.assertEqual(second["observed_state"], "lost")

    def test_host_read_only_recovery_unaffected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = self._registered(root, "secretary")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = store.conn.execute("SELECT * FROM conversations WHERE project_id=?", (project["project_id"],)).fetchone()
                prepared = prepare_run(store, conversation_id=conversation["conversation_id"], build_id=_BUILD_ID, host_process=_host("secretary"))
                run_id = prepared.run["run_id"]
                store.conn.execute("UPDATE runs SET owner_pid=?,owner_start_identity=?,observed_state='running' WHERE run_id=?", (_DEAD_PID, _DEAD_IDENTITY, run_id))
                client.reconcile_run(run_id=run_id)
            recovered = client.recover_run(run_id=run_id, actor_id="test-secretary")
            self.assertEqual(recovered["observed_state"], "lost")
            self.cleanup_container.assert_not_called()

    def test_writer_lock_probe_never_mutates_lock_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_root = root / "state"
            from scripts.pi_control.writer_lock import writer_lock_available
            with PiStore(state_root) as store:
                _register_build(store, root)
                project = self._registered(root, "probe")
                conversation = self._writer_conversation(store, project, "probe")
                prepared = self._prepare_writer(store, project, conversation, "probe")
                working_copy_id = conversation["working_copy_id"]
                lock_path = state_root / "locks" / f"{working_copy_id}.lock"
                self.assertTrue(lock_path.is_file())
                prepared.close()
                before = lock_path.read_text(encoding="utf-8")
                self.assertTrue(writer_lock_available(state_root, working_copy_id))
                after = lock_path.read_text(encoding="utf-8")
                self.assertEqual(before, after)
                with WriterLock.acquire(state_root, working_copy_id, 1):
                    self.assertFalse(writer_lock_available(state_root, working_copy_id))
                self.assertTrue(writer_lock_available(state_root, working_copy_id))


    def test_conversation_recovery_recovers_lost_and_preserves_live(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = self._registered(root, "conv")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = self._writer_conversation(store, project, "conv")
                lost = self._prepare_writer(store, project, conversation, "conv-lost")
                self._kill_run(store, lost.run["run_id"])
                lost.close()
            client.reconcile_run(run_id=lost.run["run_id"])
            recovered = client.recover_run(run_id=lost.run["run_id"], actor_id="test-conv")
            self.assertEqual(recovered["observed_state"], "lost")
            with PiStore(root / "state") as store:
                live = self._prepare_writer(store, project, conversation, "conv-live")
            outcome = client.recover_conversation(conversation_id=conversation["conversation_id"], actor_id="test-conv")
            decisions = {item["runId"]: item["decision"] for item in outcome["runs"]}
            self.assertEqual(decisions, {live.run["run_id"]: "live"})
            with PiStore(root / "state") as store:
                claim = store.conn.execute("SELECT active_writer_run_id FROM working_copies WHERE working_copy_id=?", (conversation["working_copy_id"],)).fetchone()[0]
                self.assertEqual(claim, live.run["run_id"])
            live.close()

    def test_conversation_recovery_reports_uncertain_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = self._registered(root, "uncertain")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = self._writer_conversation(store, project, "uncertain")
                prepared = self._prepare_writer(store, project, conversation, "uncertain-run")
                self._kill_run(store, prepared.run["run_id"])
                prepared.close()
                with WriterLock.acquire(store.state_root, conversation["working_copy_id"], 1):
                    outcome = client.recover_conversation(conversation_id=conversation["conversation_id"], actor_id="test-uncertain")
                    decisions = {item["runId"]: item["decision"] for item in outcome["runs"]}
                    self.assertEqual(decisions[prepared.run["run_id"]], "uncertain")
                    run = store.conn.execute("SELECT observed_state FROM runs WHERE run_id=?", (prepared.run["run_id"],)).fetchone()
                    self.assertEqual(run["observed_state"], "needs_attention")

    def _fail_interrupted_writer(self, store: PiStore, run_id: str, *, needs_attention: bool = True) -> None:
        fail_run(store, run_id=run_id, code="HOST_SUPERVISOR_INTERRUPTED", detail="host supervisor interrupted", needs_attention=needs_attention)

    def test_conversation_recovery_recovers_interrupted_terminal_writer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = self._registered(root, "terminal")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = self._writer_conversation(store, project, "terminal")
                prepared = self._prepare_writer(store, project, conversation, "terminal-run")
                run_id = prepared.run["run_id"]
                self._kill_run(store, run_id)
                self._fail_interrupted_writer(store, run_id)
                run = store.conn.execute("SELECT observed_state FROM runs WHERE run_id=?", (run_id,)).fetchone()
                self.assertEqual(run["observed_state"], "needs_attention")
                claim = store.conn.execute("SELECT active_writer_run_id FROM working_copies WHERE working_copy_id=?", (conversation["working_copy_id"],)).fetchone()[0]
                self.assertEqual(claim, run_id)
                prepared.close()
            outcome = client.recover_conversation(conversation_id=conversation["conversation_id"], actor_id="surface-repair")
            decisions = {item["runId"]: item["decision"] for item in outcome["runs"]}
            self.assertEqual(decisions[run_id], "recovered")
            self.assertEqual(outcome["runs"][0]["run"]["observed_state"], "lost")
            with PiStore(root / "state") as store:
                claim = store.conn.execute("SELECT active_writer_run_id FROM working_copies WHERE working_copy_id=?", (conversation["working_copy_id"],)).fetchone()[0]
                self.assertIsNone(claim)
                fresh = self._prepare_writer(store, project, conversation, "terminal-fresh")
                fresh.close()
                self.assertNotEqual(fresh.run["run_id"], run_id)

    def test_recover_run_accepts_interrupted_claiming_writer_and_preserves_code(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = self._registered(root, "failed-claim")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = self._writer_conversation(store, project, "failed-claim")
                prepared = self._prepare_writer(store, project, conversation, "failed-claim-run")
                run_id = prepared.run["run_id"]
                self._kill_run(store, run_id)
                self._fail_interrupted_writer(store, run_id)
                prepared.close()
            recovered = client.recover_run(run_id=run_id, actor_id="test-failed-claim")
            self.assertEqual(recovered["observed_state"], "lost")
            self.assertEqual(recovered["error_code"], "HOST_SUPERVISOR_INTERRUPTED")
            with PiStore(root / "state") as store:
                claim = store.conn.execute("SELECT active_writer_run_id FROM working_copies WHERE working_copy_id=?", (conversation["working_copy_id"],)).fetchone()[0]
                self.assertIsNone(claim)

    def test_conversation_recovery_releases_terminal_failed_writer_claim_without_state_transition(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = self._registered(root, "no-claim")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = self._writer_conversation(store, project, "no-claim")
                prepared = self._prepare_writer(store, project, conversation, "no-claim-run")
                run_id = prepared.run["run_id"]
                self._kill_run(store, run_id)
                self._fail_interrupted_writer(store, run_id, needs_attention=False)
                run = store.conn.execute("SELECT observed_state FROM runs WHERE run_id=?", (run_id,)).fetchone()
                self.assertEqual(run["observed_state"], "failed")
                prepared.close()
                outcome = client.recover_conversation(conversation_id=conversation["conversation_id"], actor_id="surface-repair")
                item = next(item for item in outcome["runs"] if item["runId"] == run_id)
                self.assertEqual(item["decision"], "recovered")
                recovered = item["run"]
                self.assertEqual(recovered["observed_state"], "failed")
                self.assertEqual(recovered["error_code"], "HOST_SUPERVISOR_INTERRUPTED")
                claim = store.conn.execute("SELECT active_writer_run_id FROM working_copies WHERE working_copy_id=?", (conversation["working_copy_id"],)).fetchone()[0]
                self.assertIsNone(claim)
                event = store.conn.execute("SELECT event_kind FROM control_events WHERE resource_id=? ORDER BY sequence DESC LIMIT 1", (run_id,)).fetchone()
                self.assertEqual(event["event_kind"], "run.writer-claim-recovered")
                retried = client.recover_run(run_id=run_id, actor_id="test-terminal-claim")
                self.assertEqual(retried["observed_state"], "failed")
                fresh = self._prepare_writer(store, project, conversation, "terminal-claim-fresh")
                fresh.close()

    def test_conversation_recovery_refuses_unproved_container_for_interrupted_writer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            project = self._registered(root, "unproved")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = self._writer_conversation(store, project, "unproved")
                prepared = self._prepare_writer(store, project, conversation, "unproved-run")
                run_id = prepared.run["run_id"]
                self._kill_run(store, run_id)
                self._fail_interrupted_writer(store, run_id, needs_attention=False)
                store.conn.execute("UPDATE runs SET container_id=? WHERE run_id=?", ("c" * 64, run_id))
                prepared.close()
                with mock.patch("scripts.pi_control.docker_runtime.cleanup_run_container", return_value={"absent": False, "errors": ["probe-refused"]}):
                    outcome = client.recover_conversation(conversation_id=conversation["conversation_id"], actor_id="surface-repair")
                decisions = {item["runId"]: item["decision"] for item in outcome["runs"]}
                self.assertEqual(decisions[run_id], "uncertain")
            with PiStore(root / "state") as store:
                claim = store.conn.execute("SELECT active_writer_run_id FROM working_copies WHERE working_copy_id=?", (conversation["working_copy_id"],)).fetchone()[0]
                self.assertEqual(claim, run_id)
                run = store.conn.execute("SELECT observed_state FROM runs WHERE run_id=?", (run_id,)).fetchone()
                self.assertEqual(run["observed_state"], "failed")

    def test_launch_error_preserves_specific_writer_message(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = PiControllerClient(root / "state").register_project(str(_repo(root, "message")), "message")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = self._writer_conversation(store, project, "message")
                prepared = self._prepare_writer(store, project, conversation, "message-run")
                run_id = prepared.run["run_id"]
                self._kill_run(store, run_id)
                self._fail_interrupted_writer(store, run_id)
                prepared.close()
                with self.assertRaises(LaunchError) as caught:
                    self._prepare_writer(store, project, conversation, "message-retry")
                self.assertIn("writer state changed before acquisition", str(caught.exception))
                self.assertNotIn("violated a database invariant", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
