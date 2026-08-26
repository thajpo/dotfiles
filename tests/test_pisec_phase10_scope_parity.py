from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.pisec.access import effective_runtime_scope
from scripts.pisec.models import NeedsAttentionError
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import register_project
from scripts.pisec.refresh import mark_stale_bindings
from scripts.pisec.secretary import ensure_secretary
from scripts.pisec.workstreams import prepare_workstream
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace, make_repo


class Phase10ScopeParityTests(unittest.TestCase):
    def test_fixture_generation_hashes_every_production_scope_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = FixtureHarness(Path(tmp))
            base = {
                "executionProfile": "worker-default",
                "worktreePath": str(Path(tmp) / "worktree"),
                "externalDomains": ["fixture.test"],
                "dataDirs": [str(Path(tmp) / "data")],
                "implementationModel": "model-a",
                "harnessModel": "harness-a",
                "reasoningEffort": "high",
                "pythonEnv": str(Path(tmp) / "venv"),
                "runtimeSurfaceSha256": "a" * 64,
            }
            for field, changed in (
                ("externalDomains", ["example.test"]),
                ("dataDirs", [str(Path(tmp) / "other-data")]),
                ("implementationModel", "model-b"),
                ("harnessModel", "harness-b"),
                ("reasoningEffort", "xhigh"),
                ("pythonEnv", str(Path(tmp) / "other-venv")),
                ("runtimeSurfaceSha256", "b" * 64),
            ):
                mutated = {**base, field: changed}
                self.assertNotEqual(
                    harness.desired_generation(base),
                    harness.desired_generation(mutated),
                    field,
                )

    def test_effective_secretary_scope_preserves_profile_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                ensured = ensure_secretary(store, project["project_id"], harness, workspace)
                binding = dict(
                    store.conn.execute(
                        "SELECT r.*,w.kind,w.project_id FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=?",
                        (ensured["workstream"]["workstream_id"],),
                    ).fetchone()
                )

                scope = effective_runtime_scope(store, binding, harness=harness)

                self.assertEqual(
                    scope["externalDomains"],
                    list(harness.profile_domains("secretary-project", ())),
                )

    def test_worker_approval_scope_contains_profile_composed_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                ensure_secretary(store, project["project_id"], harness, workspace)
                prepared = prepare_workstream(
                    store,
                    project_id=project["project_id"],
                    title="Scope parity worker",
                    purpose="Prove the immutable worker scope",
                    brief="Use only the approved scope.",
                    task_packet={
                        "schemaVersion": 1,
                        "outcome": "Scope parity is proven.",
                        "boundaries": ["Tests only."],
                        "acceptance": ["The approved domain scope is exact."],
                        "openQuestions": [],
                        "evidence": ["Fail-first test output."],
                    },
                    idempotency_key="phase10-worker-scope",
                    harness=harness,
                    workspace=workspace,
                    work_root=root / "worktrees",
                )

                self.assertIn("externalDomains", prepared["approvalScope"])
                self.assertEqual(
                    prepared["approvalScope"]["externalDomains"],
                    list(harness.profile_domains("worker-default", ())),
                )

    def test_secretary_materialization_and_reconcile_keep_one_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                ensured = ensure_secretary(store, project["project_id"], harness, workspace)
                workstream_id = ensured["workstream"]["workstream_id"]

                result = mark_stale_bindings(store, harness, workstream_ids=(workstream_id,))
                binding = store.conn.execute(
                    "SELECT desired_generation_sha256,applied_generation_sha256 FROM runtime_bindings WHERE workstream_id=?",
                    (workstream_id,),
                ).fetchone()

                self.assertEqual(result["stale"], [])
                self.assertEqual(result["failed"], [])
                self.assertEqual(binding["desired_generation_sha256"], binding["applied_generation_sha256"])

    def test_secretary_finalization_cannot_commit_after_generation_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)

                def reconcile_during_finalization(_failpoint, name, context):
                    if name != "before_secretary_final_event_commit":
                        return
                    with store.transaction():
                        store.conn.execute(
                            "UPDATE runtime_bindings SET desired_generation_sha256=? WHERE workstream_id=?",
                            ("b" * 64, context["workstreamId"]),
                        )

                with patch("scripts.pisec.secretary._hit", side_effect=reconcile_during_finalization):
                    with self.assertRaises(NeedsAttentionError):
                        ensure_secretary(store, project["project_id"], harness, workspace)

                project_row = store.conn.execute(
                    "SELECT active FROM projects WHERE project_id=?",
                    (project["project_id"],),
                ).fetchone()
                workstream_row = store.conn.execute(
                    "SELECT provisioning_state FROM workstreams WHERE project_id=? AND kind='secretary'",
                    (project["project_id"],),
                ).fetchone()
                operation_row = store.conn.execute(
                    "SELECT state FROM operations WHERE project_id=? AND kind='secretary.ensure'",
                    (project["project_id"],),
                ).fetchone()
                binding_row = store.conn.execute(
                    "SELECT desired_generation_sha256,applied_generation_sha256 FROM runtime_bindings WHERE workstream_id=(SELECT secretary_workstream_id FROM projects WHERE project_id=?)",
                    (project["project_id"],),
                ).fetchone()

                self.assertFalse(
                    project_row["active"]
                    and workstream_row["provisioning_state"] == "bound"
                    and operation_row["state"] == "succeeded"
                    and binding_row["desired_generation_sha256"] != binding_row["applied_generation_sha256"]
                )


if __name__ == "__main__":
    unittest.main()
