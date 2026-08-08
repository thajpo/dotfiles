"""C1b project activation state transitions."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.errors import ActivationMismatchError, ResourceStaleError
from scripts.pi_control.models import new_id
from scripts.pi_control.store import ControllerStore
from scripts.pi_control.workstreams import ensure_project_activation, transition_activation


def _project(store: ControllerStore, project_id: str = "prj_" + "1" * 32) -> str:
    store.conn.execute("INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (project_id, "p", f"/{project_id}/git", 1, 1, f"/{project_id}", "sha1", "trusted", "policy", "active", "unknown", 1, "t", "t"))
    return project_id


def _migration(store: ControllerStore, build_id: str = "build") -> str:
    store.register_build(build_id, source_tree_hash="tree", artifact_manifest_hash="manifest", pi_version="pi", package_lock_hash="lock", status="staged")
    operation = store.create_operation(idempotency_key="migration-op", kind="migration", resource_type="migration", resource_id="migration-resource", actor_type="controller", request={})
    migration_id = new_id("mig")
    store.conn.execute("INSERT INTO migration_runs(migration_id,operation_id,idempotency_key,mode,controller_build_id,request_digest,source_manifest_digest,state,step,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (migration_id, operation.operation_id, "migration-op", "shadow-import", build_id, "request", "source", "succeeded", "complete", 1, "t", "t"))
    return migration_id


class ActivationTests(unittest.TestCase):
    def test_legal_shadow_controller_rollback_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            project_id = _project(store)
            activation = ensure_project_activation(store, project_id=project_id)
            migration_id = _migration(store)
            shadow = transition_activation(store, project_id=project_id, mode="shadow", expected_resource_version=activation["resource_version"], controller_build_id="build", migration_id=migration_id)
            self.assertEqual(shadow["mode"], "shadow")
            store.conn.execute("UPDATE installed_builds SET status='active', activated_at='t' WHERE build_id='build'")
            controller = transition_activation(store, project_id=project_id, mode="controller", expected_resource_version=shadow["resource_version"], controller_build_id="build", migration_id=migration_id)
            self.assertEqual(controller["mode"], "controller")
            legacy = transition_activation(store, project_id=project_id, mode="legacy", expected_resource_version=controller["resource_version"], rollback=True)
            self.assertEqual(legacy["mode"], "legacy")
            self.assertEqual(store.conn.execute("SELECT count(*) FROM control_events WHERE resource_type='project_activation'").fetchone()[0], 4)

    def test_direct_controller_and_bad_predicates_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            project_id = _project(store)
            activation = ensure_project_activation(store, project_id=project_id)
            migration_id = _migration(store)
            with self.assertRaises(ActivationMismatchError):
                transition_activation(store, project_id=project_id, mode="controller", expected_resource_version=activation["resource_version"], controller_build_id="build", migration_id=migration_id)
            shadow = transition_activation(store, project_id=project_id, mode="shadow", expected_resource_version=activation["resource_version"], controller_build_id="build", migration_id=migration_id)
            with self.assertRaises(ActivationMismatchError):
                transition_activation(store, project_id=project_id, mode="controller", expected_resource_version=shadow["resource_version"], controller_build_id="build", migration_id=migration_id, expected_project_version=9)
            store.conn.execute("UPDATE installed_builds SET status='active', activated_at='t' WHERE build_id='build'")
            with self.assertRaises(ResourceStaleError):
                transition_activation(store, project_id=project_id, mode="controller", expected_resource_version=1, controller_build_id="build", migration_id=migration_id)


if __name__ == "__main__":
    unittest.main()
