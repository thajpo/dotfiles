from pathlib import Path
import json
import tempfile
import unittest

from scripts.pisec.git_runner import git_text, run_git
from scripts.pisec.models import InvalidRequestError, NeedsAttentionError
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import register_project
from scripts.pisec.secretary import ensure_secretary
from scripts.pisec.source_import import inspect_import_source, materialize_import
from scripts.pisec.worker_repo import create_worker_repository, project_target_state, validate_worker_repository
from scripts.pisec.workstreams import authorize_apply_workstream, prepare_workstream
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace, make_repo


class SourceImportTests(unittest.TestCase):
    def source_worktree(self, primary: Path, *, branch: str = "external", filename: str = "feature.txt", content: str = "external\n") -> tuple[Path, str]:
        base_oid = git_text(primary, "rev-parse", "HEAD")
        run_git(primary, ("branch", branch, base_oid))
        source = primary.parent / f"{branch}-worktree"
        run_git(primary, ("worktree", "add", "--quiet", str(source), branch))
        def remove_source_worktree() -> None:
            if primary.exists():
                run_git(primary, ("worktree", "remove", "--force", str(source)), accepted=(0, 1, 128))
        self.addCleanup(remove_source_worktree)
        (source / filename).write_text(content)
        run_git(source, ("add", filename), role="worker")
        run_git(source, ("commit", "-qm", "external feature"), role="worker")
        return source, base_oid

    def worker(self, primary: Path, worker: Path, workstream_id: str, base_oid: str) -> None:
        branch, target_ref, _ = project_target_state(primary, "main")
        create_worker_repository(
            primary=primary,
            worker=worker,
            project_id="prj_" + "a" * 32,
            workstream_id=workstream_id,
            target_branch_ref=target_ref,
            base_oid=base_oid,
            target_branch=branch,
        )

    def test_clean_committed_worktree_is_pinned_and_normalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "primary"
            worker = root / "worker"
            make_repo(primary)
            source, base_oid = self.source_worktree(primary)

            document = inspect_import_source(primary, base_oid, {"path": str(source)})
            self.assertEqual(document["kind"], "git_worktree")
            self.assertEqual(document["changedPaths"], ["feature.txt"])
            self.assertNotIn("dirty", document)
            self.assertNotIn("manifestSha256", document)
            self.assertNotIn("privateRef", document)

            self.worker(primary, worker, "ws_" + "b" * 32, base_oid)
            result = materialize_import(
                project_root=primary,
                target_oid=base_oid,
                worker=worker,
                source=document,
            )
            self.assertTrue(result["normalized"])
            self.assertEqual(git_text(worker, "rev-parse", "HEAD^{tree}"), document["sourceTreeOid"])
            validate_worker_repository(
                worker,
                branch_name="pisec/ws_" + "b" * 32 + "/work",
                base_oid=base_oid,
                target_branch="main",
            )

    def test_dirty_source_is_rejected_and_must_be_committed_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "primary"
            make_repo(primary)
            source, base_oid = self.source_worktree(primary)
            (source / "uncommitted.txt").write_text("commit me\n")

            with self.assertRaisesRegex(InvalidRequestError, "source checkout must be clean"):
                inspect_import_source(primary, base_oid, {"path": str(source)})

    def test_source_drift_after_approval_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "primary"
            worker = root / "worker"
            make_repo(primary)
            source, base_oid = self.source_worktree(primary)
            approved = inspect_import_source(primary, base_oid, {"path": str(source)})
            (source / "feature.txt").write_text("v2\n")
            run_git(source, ("commit", "-qam", "external v2"), role="worker")
            self.worker(primary, worker, "ws_" + "c" * 32, base_oid)

            with self.assertRaisesRegex(NeedsAttentionError, "source moved"):
                materialize_import(project_root=primary, target_oid=base_oid, worker=worker, source=approved)

    def test_conflicting_source_stops_creation_without_dirtying_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "primary"
            worker = root / "worker"
            make_repo(primary)
            source, source_base_oid = self.source_worktree(primary, filename="README", content="external\n")
            target_file = primary / "README"
            target_file.write_text("target\n")
            run_git(primary, ("add", "README"), role="worker")
            run_git(primary, ("commit", "-qm", "target change"), role="worker")
            target_oid = git_text(primary, "rev-parse", "HEAD")
            document = inspect_import_source(primary, target_oid, {"path": str(source)})
            self.assertEqual(document["mergeBaseOid"], source_base_oid)
            self.worker(primary, worker, "ws_" + "d" * 32, target_oid)

            with self.assertRaisesRegex(NeedsAttentionError, "does not apply cleanly"):
                materialize_import(project_root=primary, target_oid=target_oid, worker=worker, source=document)
            self.assertEqual(git_text(worker, "rev-parse", "HEAD"), target_oid)
            self.assertEqual(run_git(worker, ("show-ref", "--verify", "--quiet", "refs/pisec/import-candidate"), accepted=(0, 1)).returncode, 1)

    def test_invalid_source_checkout_and_source_selector_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "primary"
            unrelated = root / "unrelated"
            make_repo(primary)
            make_repo(unrelated)
            base_oid = git_text(primary, "rev-parse", "HEAD")
            with self.assertRaises(InvalidRequestError):
                inspect_import_source(primary, base_oid, {"path": str(unrelated)})
            with self.assertRaises(InvalidRequestError):
                inspect_import_source(primary, base_oid, {"ref": "main", "path": str(primary)})

    def test_prepare_and_authorize_persist_and_bind_imported_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "primary"
            make_repo(primary)
            source, _base_oid = self.source_worktree(primary)
            store = PiStore(root / "state")
            self.addCleanup(store.close)
            project = register_project(store, primary, default_ref="main")
            harness = FixtureHarness(root)
            workspace = FixtureWorkspace(root, store)
            ensure_secretary(store, project["project_id"], harness, workspace)
            task_packet = {
                "schemaVersion": 1,
                "outcome": "Imported work is available for review.",
                "boundaries": ["Review the imported change."],
                "acceptance": ["The imported file is present."],
                "openQuestions": [],
                "evidence": ["Worker tree inspection."],
            }
            prepared = prepare_workstream(
                store,
                project_id=project["project_id"],
                title="Review imported work",
                purpose="Load committed external work into a project worker",
                brief="Review the imported change.",
                task_packet=task_packet,
                idempotency_key="imported-work",
                harness=harness,
                workspace=workspace,
                source={"path": str(source)},
                work_root=root / "worktrees",
            )
            scope = prepared["approvalScope"]
            self.assertEqual(scope["importSource"]["kind"], "git_worktree")
            self.assertNotIn("privateRef", scope["importSource"])
            applied = authorize_apply_workstream(store, scope=scope, harness=harness, workspace=workspace)
            packet = store.conn.execute("SELECT packet_json FROM task_packets WHERE workstream_id=?", (scope["workstreamId"],)).fetchone()
            packet_value = json.loads(packet["packet_json"])
            self.assertEqual(packet_value["execution"]["importSource"]["sourceCommitOid"], scope["importSource"]["sourceCommitOid"])
            self.assertNotIn("dirty", packet_value["execution"]["importSource"])
            worktree = Path(applied["workstream"]["worktree_path"])
            self.assertEqual(git_text(worktree, "rev-parse", "HEAD^{tree}"), scope["importSource"]["sourceTreeOid"])
            self.assertEqual((worktree / "feature.txt").read_text(), "external\n")

    def test_registered_project_ref_can_be_imported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "primary"
            make_repo(primary)
            base_oid = git_text(primary, "rev-parse", "HEAD")
            run_git(primary, ("checkout", "-qb", "external"))
            (primary / "feature.txt").write_text("ref work\n")
            run_git(primary, ("add", "feature.txt"), role="worker")
            run_git(primary, ("commit", "-qm", "ref work"), role="worker")
            run_git(primary, ("checkout", "-q", "main"))
            document = inspect_import_source(primary, base_oid, {"ref": "external"})
            self.assertEqual(document["kind"], "project_ref")
            self.assertEqual(document["ref"], "external")
            self.assertEqual(document["changedPaths"], ["feature.txt"])
