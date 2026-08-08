from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.client import ControllerClient, protocol_request
from scripts.pi_control.errors import ActivationMismatchError
from scripts.pi_control.models import new_id
from scripts.pi_control.store import ControllerStore
from scripts.pi_control.workstreams import ensure_project_activation, transition_activation


class LauncherResolutionTests(unittest.TestCase):
    def test_controller_launch_resolves_exact_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / "session.jsonl"
            session.write_text('{"id":"pi-session"}\n')
            with ControllerStore(root / "state") as store:
                project = "prj_" + "1" * 32
                wc = new_id("wc")
                conv = new_id("conv")
                store.conn.execute("INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (project, "p", "/p/.git", 1, 1, "/p", "sha1", "trusted", "p", "active", "unknown", 1, "t", "t"))
                store.conn.execute("INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,effective_mode,desired_state,observed_state,resource_version,controller_owned,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (wc, project, "p", "primary", "personal", "/p", "trusted-live", "present", "ready", 1, 1, "t", "t"))
                store.conn.execute("INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (conv, project, wc, "personal", "p", "pi-session", str(session), "active", "ready", 1, "t", "t"))
                store.register_build("build", source_tree_hash="t", artifact_manifest_hash="a", pi_version="p", package_lock_hash="l", status="staged")
                op = store.create_operation(idempotency_key="mig", kind="migration", resource_type="migration", resource_id="migration-resource", actor_type="controller", request={})
                migration = new_id("mig")
                store.conn.execute("INSERT INTO migration_runs(migration_id,operation_id,idempotency_key,mode,controller_build_id,request_digest,source_manifest_digest,state,step,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (migration, op.operation_id, "mig", "shadow-import", "build", "r", "s", "succeeded", "done", 1, "t", "t"))
                activation = ensure_project_activation(store, project_id=project)
                shadow = transition_activation(store, project_id=project, mode="shadow", expected_resource_version=activation["resource_version"], controller_build_id="build", migration_id=migration)
                store.conn.execute("UPDATE installed_builds SET status='active' WHERE build_id='build'")
                controller = transition_activation(store, project_id=project, mode="controller", expected_resource_version=shadow["resource_version"], controller_build_id="build", migration_id=migration)
                result = protocol_request(ControllerClient(root / "state", read_only=True), {"protocolVersion": 2, "operation": "launch.resolve", "request": {"projectId": project, "conversationId": conv, "workingCopyId": wc, "sessionFile": str(session), "expectedActivationResourceVersion": controller["resource_version"]}})
                self.assertEqual(result["value"]["authority"], "controller")
                with self.assertRaises(ActivationMismatchError):
                    protocol_request(ControllerClient(root / "state", read_only=True), {"protocolVersion": 2, "operation": "launch.resolve", "request": {"projectId": project, "conversationId": conv, "workingCopyId": wc, "sessionFile": str(session), "expectedActivationResourceVersion": 1}})


if __name__ == "__main__": unittest.main()
