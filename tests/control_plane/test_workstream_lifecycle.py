from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.errors import WorkstreamConflictError
from scripts.pi_control.models import new_id
from scripts.pi_control.store import ControllerStore
from scripts.pi_control.workstreams import create_workstream, focus_workstream, relaunch_workstream, retire_workstream


class WorkstreamLifecycleTests(unittest.TestCase):
    def _setup(self, store: ControllerStore):
        project = "prj_" + "1" * 32
        wc, conv, ws = new_id("wc"), new_id("conv"), new_id("ws")
        store.conn.execute("INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (project, "p", "/p/.git", 1, 1, "/p", "sha1", "trusted", "p", "active", "unknown", 1, "t", "t"))
        store.conn.execute("INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,branch_ref,effective_mode,desired_state,observed_state,resource_version,controller_owned,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (wc, project, "ws", "worktree", "workstream", "/p/ws", "refs/heads/ws", "trusted-live", "present", "ready", 1, 1, "t", "t"))
        store.conn.execute("INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (conv, project, wc, "workstream", "ws", "pi-ws", "/p/ws.jsonl", "active", "ready", 1, "t", "t"))
        result = create_workstream(store, project_id=project, working_copy_id=wc, conversation_id=conv, title="ws", brief={}, target_ref="refs/heads/main", starting_oid="a" * 40, workstream_id=ws)
        return result

    def test_focus_is_read_only_and_retire_relaunch_cas(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            row = self._setup(store)
            self.assertTrue(focus_workstream(store, row["workstream_id"])["presentationOnly"])
            retired = retire_workstream(store, row["workstream_id"], expected_resource_version=1)
            self.assertEqual(retired["desired_state"], "retired")
            relaunched = relaunch_workstream(store, row["workstream_id"], expected_resource_version=2)
            self.assertEqual(relaunched["observed_state"], "creating")

    def test_live_run_blocks_retirement(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            row = self._setup(store)
            store.register_build("build", source_tree_hash="t", artifact_manifest_hash="a", pi_version="p", package_lock_hash="l", status="active")
            store.conn.execute("INSERT INTO runs(run_id,conversation_id,project_id,working_copy_id,authority,desired_state,observed_state,runtime_spec_hash,build_id,capability_hash,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (new_id("run"), row["conversation_id"], row["project_id"], row["working_copy_id"], "read-only", "running", "running", "spec", "build", "cap", 1, "t", "t"))
            with self.assertRaises(WorkstreamConflictError):
                retire_workstream(store, row["workstream_id"], expected_resource_version=1)


if __name__ == "__main__": unittest.main()
