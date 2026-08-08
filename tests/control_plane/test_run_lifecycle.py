from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.models import new_id
from scripts.pi_control.store import ControllerStore


class RunLifecycleTests(unittest.TestCase):
    def test_run_prepare_claims_exact_epoch_and_terminalizes(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            project = "prj_" + "1" * 32
            wc = new_id("wc")
            conv = new_id("conv")
            store.conn.execute("INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (project, "p", "/p/.git", 1, 1, "/p", "sha1", "trusted", "p", "active", "unknown", 1, "t", "t"))
            store.conn.execute("INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,branch_ref,effective_mode,desired_state,observed_state,resource_version,controller_owned,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (wc, project, "wc", "worktree", "personal", "/p/wc", "refs/heads/wc", "trusted-live", "present", "ready", 1, 1, "t", "t"))
            store.conn.execute("INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (conv, project, wc, "personal", "p", "pi-run", "/p/session.jsonl", "active", "ready", 1, "t", "t"))
            store.register_build("build", source_tree_hash="t", artifact_manifest_hash="a", pi_version="p", package_lock_hash="l", status="active")
            run = store.create_run(run_id=new_id("run"), conversation_id=conv, authority="writer", project_id=project, working_copy_id=wc, expected_working_copy_version=1, writer_epoch=1, runtime_spec_hash="sha256:" + "1" * 64, build_id="build", capability_hash="sha256:" + "2" * 64)
            self.assertEqual(run["writer_epoch"], 1)
            self.assertEqual(store.conn.execute("SELECT active_writer_run_id FROM working_copies WHERE working_copy_id=?", (wc,)).fetchone()[0], run["run_id"])
            store.terminalize_run(run["run_id"])
            self.assertIsNone(store.conn.execute("SELECT active_writer_run_id FROM working_copies WHERE working_copy_id=?", (wc,)).fetchone()[0])
            self.assertEqual(store.conn.execute("SELECT count(*) FROM control_events WHERE event_kind='writer.claim.cleared'").fetchone()[0], 1)


if __name__ == "__main__": unittest.main()
