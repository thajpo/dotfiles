from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.pi_control.child_runs import (
    ChildExecutor,
    ChildLineageError,
    ChildPermissionError,
    ChildWorkspaceError,
    plan_read_only_child,
    plan_writer_child,
    validate_child_plan,
)
from scripts.pi_control.errors import ConstraintError, InvalidRequestError
from scripts.pi_control.models import ChildSource
from scripts.pi_control.snapshot import SnapshotPolicy, capture_snapshot
from scripts.pi_control.store import ControllerStore


class ChildRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.state = self.root / "state"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "Child Test")
        self._git("config", "user.email", "child@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-qm", "base")
        self.snapshot = capture_snapshot(self.repo, self.state, snapshot_id="snap_" + "1" * 32)
        self.parent_run = "run_" + "2" * 32
        self.parent_conversation = "conv_" + "3" * 32
        self.parent_wc = "wc_" + "4" * 32

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Child Test",
            "GIT_AUTHOR_EMAIL": "child@example.invalid",
            "GIT_COMMITTER_NAME": "Child Test",
            "GIT_COMMITTER_EMAIL": "child@example.invalid",
        }
        return subprocess.run(["git", *args], cwd=self.repo, env=environment, text=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_read_only_plan_requires_exact_parent_and_snapshot_lineage(self) -> None:
        plan = plan_read_only_child(
            self.snapshot,
            child_id="child_" + "5" * 32,
            parent_run_id=self.parent_run,
            parent_conversation_id=self.parent_conversation,
            parent_working_copy_id=self.parent_wc,
            expected_parent_head_oid=self.snapshot.source_head_oid,
        )
        self.assertEqual(plan.snapshot_ref, "refs/pi/snapshots/" + self.snapshot.snapshot_id)
        self.assertIsNone(plan.child_working_copy_id)
        self.assertEqual(validate_child_plan(plan), plan)
        with self.assertRaises(ChildLineageError):
            plan_read_only_child(
                self.snapshot,
                child_id="child_" + "6" * 32,
                parent_run_id=self.parent_run,
                parent_conversation_id=self.parent_conversation,
                expected_parent_head_oid="0" * len(self.snapshot.source_head_oid or "0"),
            )
        with self.assertRaises(ChildLineageError):
            plan_read_only_child(
                self.snapshot,
                child_id="child_" + "7" * 32,
                parent_run_id=self.parent_run,
                parent_conversation_id="not-a-conversation",
            )
        forged = replace(plan, parent_working_copy_id="not-a-working-copy", plan_digest="")
        forged = replace(forged, plan_digest=forged.as_dict()["planDigest"])
        with self.assertRaises(ChildLineageError):
            validate_child_plan(forged)

    def test_read_only_child_has_mechanical_tool_and_filesystem_boundary(self) -> None:
        plan = plan_read_only_child(
            self.snapshot,
            child_id="child_" + "8" * 32,
            parent_run_id=self.parent_run,
            parent_conversation_id=self.parent_conversation,
            parent_working_copy_id=self.parent_wc,
        )
        executor = ChildExecutor(self.state)
        workspace = executor.prepare(plan)
        try:
            self.assertFalse(workspace.writable)
            self.assertEqual(workspace.assert_tool("read"), None)
            with self.assertRaises(ChildPermissionError):
                workspace.assert_tool("write")
            result = executor.run_command(workspace, ["python3", "-c", "from pathlib import Path; Path('new.txt').write_text('no')"])
            self.assertEqual(result.state, "failed")
            self.assertFalse((Path(workspace.path) / "new.txt").exists())
        finally:
            executor.release(workspace)

    def test_parent_can_advance_without_rebinding_read_only_child(self) -> None:
        plan = plan_read_only_child(
            self.snapshot,
            child_id="child_" + "9" * 32,
            parent_run_id=self.parent_run,
            parent_conversation_id=self.parent_conversation,
        )
        (self.repo / "tracked.txt").write_text("parent advanced\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-qm", "advance")
        executor = ChildExecutor(self.state)
        workspace = executor.prepare(plan)
        try:
            result = executor.run_command(workspace, ["cat", "tracked.txt"])
            self.assertEqual(result.state, "succeeded")
            self.assertEqual(result.stdout, "base\n")
        finally:
            executor.release(workspace)

    def test_writer_child_requires_distinct_working_copy_and_is_writable(self) -> None:
        with self.assertRaises(ChildLineageError):
            plan_writer_child(
                self.snapshot,
                child_id="child_" + "a" * 32,
                parent_run_id=self.parent_run,
                parent_conversation_id=self.parent_conversation,
                parent_working_copy_id=self.parent_wc,
                child_working_copy_id=self.parent_wc,
            )
        plan = plan_writer_child(
            self.snapshot,
            child_id="child_" + "b" * 32,
            parent_run_id=self.parent_run,
            parent_conversation_id=self.parent_conversation,
            parent_working_copy_id=self.parent_wc,
            child_working_copy_id="wc_" + "c" * 32,
        )
        executor = ChildExecutor(self.state)
        workspace = executor.prepare(plan)
        try:
            self.assertTrue(workspace.writable)
            result = executor.run_command(workspace, ["python3", "-c", "from pathlib import Path; Path('writer.txt').write_text('child')"])
            self.assertEqual(result.state, "succeeded")
            self.assertEqual((Path(workspace.path) / "writer.txt").read_text(encoding="utf-8"), "child")
            (Path(workspace.path) / "writer.txt").unlink()
        finally:
            executor.release(workspace)

    def test_missing_or_rebound_snapshot_fails_before_workspace(self) -> None:
        plan = plan_read_only_child(
            self.snapshot,
            child_id="child_" + "d" * 32,
            parent_run_id=self.parent_run,
            parent_conversation_id=self.parent_conversation,
        )
        manifest = self.state / "snapshots" / self.snapshot.snapshot_id / "manifest.json"
        original = manifest.read_text(encoding="utf-8")
        manifest.write_text(original.replace(self.snapshot.snapshot_commit_oid, "0" * len(self.snapshot.snapshot_commit_oid)), encoding="utf-8")
        with self.assertRaises(Exception):
            ChildExecutor(self.state).prepare(plan)

    def test_child_source_is_durable_and_parent_bound_in_sqlite(self) -> None:
        with ControllerStore(self.root / "controller") as store:
            project_id = "prj_" + "1" * 32
            parent_wc = "wc_" + "2" * 32
            parent_conv = "conv_" + "3" * 32
            child_conv = "conv_" + "4" * 32
            parent_run = "run_" + "5" * 32
            child_run = "run_" + "6" * 32
            store.register_build("build", source_tree_hash="tree", artifact_manifest_hash="manifest", pi_version="pi", package_lock_hash="lock", status="active")
            store.conn.execute("INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (project_id, "p", str(self.repo / ".git"), 1, 1, str(self.repo), "sha1", "trusted", "policy", "active", "ready", 1, "t", "t"))
            store.conn.execute("INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,effective_mode,desired_state,observed_state,writer_epoch,resource_version,controller_owned,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (parent_wc, project_id, "primary", "primary", "personal", str(self.repo), "trusted-live", "present", "ready", 0, 1, 1, "t", "t"))
            store.conn.execute("INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (parent_conv, project_id, parent_wc, "personal", "parent", "parent-session", str(self.root / "parent.jsonl"), "active", "ready", 1, "t", "t"))
            store.conn.execute("INSERT INTO conversations(conversation_id,project_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (child_conv, project_id, "personal", "child", "child-session", str(self.root / "child.jsonl"), "active", "ready", 1, "t", "t"))
            store.create_run(run_id=parent_run, conversation_id=parent_conv, project_id=project_id, working_copy_id=parent_wc, authority="read-only", runtime_spec_hash="runtime", build_id="build", capability_hash="cap")
            plan = plan_read_only_child(self.snapshot, child_id="child_" + "f" * 32, parent_run_id=parent_run, parent_conversation_id=parent_conv, parent_working_copy_id=parent_wc)
            row = store.create_run(run_id=child_run, conversation_id=child_conv, project_id=project_id, authority="read-only", parent_run_id=parent_run, parent_conversation_id=parent_conv, child_source=plan.source_dict(), runtime_spec_hash="runtime", build_id="build", capability_hash="cap")
            self.assertEqual(json.loads(row["child_source_json"]), plan.source_dict())
            with self.assertRaises(ConstraintError):
                store.create_run(run_id="run_" + "7" * 32, conversation_id=child_conv, project_id=project_id, authority="read-only", parent_run_id=parent_run, parent_conversation_id=parent_conv, child_source={**plan.source_dict(), "authority": "writer"}, runtime_spec_hash="runtime", build_id="build", capability_hash="cap")
            with self.assertRaises(ConstraintError):
                store.create_run(run_id="run_" + "8" * 32, conversation_id=child_conv, project_id=project_id, authority="read-only", parent_run_id=parent_run, parent_conversation_id="conv_" + "9" * 32, child_source=plan.source_dict(), runtime_spec_hash="runtime", build_id="build", capability_hash="cap")

    def test_child_source_value_object_rejects_forged_or_noncanonical_bindings(self) -> None:
        source = plan_read_only_child(self.snapshot, child_id="child_" + "f" * 32, parent_run_id=self.parent_run, parent_conversation_id=self.parent_conversation, parent_working_copy_id=self.parent_wc).source_dict()
        self.assertEqual(ChildSource.from_mapping(source).as_dict(), source)
        with self.assertRaises(InvalidRequestError):
            ChildSource.from_mapping({**source, "snapshotRef": "refs/heads/main"})
        with self.assertRaises(InvalidRequestError):
            ChildSource.from_mapping({**source, "unexpected": True})
        with self.assertRaises(InvalidRequestError):
            ChildSource(snapshot_id=source["snapshotId"], snapshot_ref=source["snapshotRef"], snapshot_commit_oid="not-an-oid", snapshot_tree_oid=source["snapshotTreeOid"], source_head_oid=source["sourceHeadOid"], source_tree_oid=source["sourceTreeOid"], authority=source["authority"])

    def test_unsupported_child_authority_does_not_get_a_workspace(self) -> None:
        plan = plan_read_only_child(
            self.snapshot,
            child_id="child_" + "e" * 32,
            parent_run_id=self.parent_run,
            parent_conversation_id=self.parent_conversation,
        )
        invalid = plan.__class__(**{**plan.__dict__, "authority": "root", "plan_digest": ""})
        with self.assertRaises(ChildLineageError):
            validate_child_plan(invalid)


if __name__ == "__main__":
    unittest.main()
