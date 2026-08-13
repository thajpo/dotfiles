"""Exact workstream retirement: guards, git cleanup order, and idempotency."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.conversations import create_conversation
from scripts.pi_control.launch import prepare_run
from scripts.pi_control.pi_client import PiControllerClient
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.pi_workstreams import WorkstreamRetireError, create_workstream, retire_workstream
from scripts.pi_control.writer_lock import WriterLock
from tests.control_plane.test_p2_contract import tool_runtime
from tests.pi_test_build import allow_test_only_registered_build_rows
from tests.test_pi_core import _BUILD_ID, _host, _register_build, _repo


def _git(repository: Path, args: list[str]) -> str:
    import subprocess
    result = subprocess.run(["git", "-C", str(repository), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"git {args}: {result.stderr}")
    return result.stdout.strip()


def _ref_exists(repository: Path, ref: str) -> bool:
    import subprocess
    result = subprocess.run(["git", "-C", str(repository), "show-ref", "--verify", "--hash", ref], capture_output=True, text=True)
    return result.returncode == 0 and bool(result.stdout.strip())


class WorkstreamRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = allow_test_only_registered_build_rows()
        patcher.start()
        self.addCleanup(patcher.stop)

    def _setup(self, root: Path, name: str):
        client = PiControllerClient(root / "state")
        project = client.register_project(str(_repo(root, name)), name)
        with PiStore(root / "state") as store:
            _register_build(store, root)
            workstream = create_workstream(store, project_id=project["project_id"], title="retire me", idempotency_key=f"retire-{name}")
            return client, project, dict(workstream)

    def test_retire_removes_worktree_branch_and_records_durable_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client, project, workstream = self._setup(root, "happy")
            worktree = Path(workstream["worktree_path"])
            self.assertTrue(worktree.is_dir())
            self.assertTrue(_ref_exists(root / "happy", workstream["branch_ref"]))
            retired = client.retire_workstream(workstream_id=workstream["workstream_id"], actor_id="test-retire")
            self.assertEqual(retired["desired_state"], "retired")
            self.assertFalse(worktree.exists())
            with PiStore(root / "state") as store:
                wc = store.conn.execute("SELECT desired_state,observed_state FROM working_copies WHERE working_copy_id=?", (workstream["working_copy_id"],)).fetchone()
                conv = store.conn.execute("SELECT desired_state FROM conversations WHERE conversation_id=?", (workstream["conversation_id"],)).fetchone()
                pa = store.conn.execute("SELECT desired_state FROM presentation_assignments WHERE conversation_id=?", (workstream["conversation_id"],)).fetchone()
                event = store.conn.execute("SELECT 1 FROM control_events WHERE event_kind='workstream.retired' AND resource_id=?", (workstream["workstream_id"],)).fetchone()
                self.assertEqual((wc["desired_state"], wc["observed_state"]), ("absent", "missing"))
                self.assertEqual(conv["desired_state"], "archived")
                self.assertEqual(pa["desired_state"], "absent")
                self.assertIsNotNone(event)
            self.assertFalse(_ref_exists(root / "happy", workstream["branch_ref"]))

    def test_retire_refuses_dirty_working_copy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client, _project, workstream = self._setup(root, "dirty")
            Path(workstream["worktree_path"], "uncommitted.txt").write_text("work\n", encoding="utf-8")
            with self.assertRaises(WorkstreamRetireError):
                client.retire_workstream(workstream_id=workstream["workstream_id"], actor_id="test-dirty")
            with PiStore(root / "state") as store:
                state = store.conn.execute("SELECT desired_state FROM workstreams WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()
                self.assertEqual(state["desired_state"], "active")
            self.assertTrue(Path(workstream["worktree_path"]).is_dir())

    def test_retire_refuses_live_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client, project, workstream = self._setup(root, "liverun")
            with PiStore(root / "state") as store:
                working = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (workstream["working_copy_id"],)).fetchone()
                run_id = "run_" + "1" * 32
                prepared = prepare_run(store, conversation_id=workstream["conversation_id"], build_id=_BUILD_ID, host_process=_host("workstream"), tool_runtime=tool_runtime(run_id, {"project_id": project["project_id"]}, working), run_id=run_id)
                prepared.close()
            with self.assertRaises(WorkstreamRetireError):
                client.retire_workstream(workstream_id=workstream["workstream_id"], actor_id="test-live-run")
            self.assertTrue(Path(workstream["worktree_path"]).is_dir())

    def test_retire_refuses_held_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client, _project, workstream = self._setup(root, "locked")
            with PiStore(root / "state") as store:
                with WriterLock.acquire(store.state_root, workstream["working_copy_id"], 1):
                    with self.assertRaises(WorkstreamRetireError):
                        client.retire_workstream(workstream_id=workstream["workstream_id"], actor_id="test-lock")
            self.assertTrue(Path(workstream["worktree_path"]).is_dir())

    def test_retire_refuses_open_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client, project, workstream = self._setup(root, "change")
            with PiStore(root / "state") as store:
                now = "2026-08-13T00:00:00.000000Z"
                store.conn.execute(
                    "INSERT INTO changes(change_id,project_id,source_working_copy_id,title,summary,target_ref,baseline_oid,baseline_tree_oid,baseline_state_json,state,current_revision,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("chg_" + "c" * 28, project["project_id"], workstream["working_copy_id"], "open change", "", workstream["branch_ref"], workstream["starting_oid"], workstream["starting_oid"], "{}", "open", 1, 1, now, now),
                )
            with self.assertRaises(WorkstreamRetireError):
                client.retire_workstream(workstream_id=workstream["workstream_id"], actor_id="test-change")
            self.assertTrue(Path(workstream["worktree_path"]).is_dir())

    def test_retire_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client, _project, workstream = self._setup(root, "idem")
            first = client.retire_workstream(workstream_id=workstream["workstream_id"], actor_id="test-idem")
            second = client.retire_workstream(workstream_id=workstream["workstream_id"], actor_id="test-idem")
            self.assertEqual(first["desired_state"], "retired")
            self.assertEqual(second["desired_state"], "retired")

    def test_retire_refuses_branch_moved_from_expected_oid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client, _project, workstream = self._setup(root, "moved")
            worktree = Path(workstream["worktree_path"])
            _git(worktree, ["commit", "-qm", "move", "--allow-empty"])
            moved = _git(worktree, ["rev-parse", "HEAD"])
            with self.assertRaises(WorkstreamRetireError):
                client.retire_workstream(workstream_id=workstream["workstream_id"], actor_id="test-moved", expected_head_oid=workstream["starting_oid"])
            self.assertTrue(worktree.is_dir())


if __name__ == "__main__":
    unittest.main()
