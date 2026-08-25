from pathlib import Path
import tempfile
import unittest

from scripts.pisec.git_runner import git_text, run_git
from scripts.pisec.models import NeedsAttentionError
from scripts.pisec.worker_repo import create_worker_repository, project_target_state, validate_worker_repository
from tests.pisec_fixture import make_repo


class WorkerRepositoryTests(unittest.TestCase):
    def test_independent_repository_is_complete_and_fixed_identity_commits_without_host_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "primary"
            worker = root / "worker"
            make_repo(primary)
            branch, target_ref, base_oid = project_target_state(primary, "main")
            create_worker_repository(
                primary=primary,
                worker=worker,
                project_id="prj_" + "a" * 32,
                workstream_id="ws_" + "b" * 32,
                target_branch_ref=target_ref,
                base_oid=base_oid,
                target_branch=branch,
            )
            run_git(worker, ("config", "--local", "--unset-all", "user.name"), accepted=range(256))
            run_git(worker, ("config", "--local", "--unset-all", "user.email"), accepted=range(256))
            (worker / "worker.txt").write_text("worker\n")
            run_git(worker, ("add", "worker.txt"), role="worker")
            run_git(worker, ("commit", "-qm", "worker change"), role="worker")
            head = validate_worker_repository(
                worker,
                branch_name="pisec/ws_" + "b" * 32 + "/work",
                base_oid=base_oid,
                target_branch=branch,
            )
            self.assertEqual(head, git_text(worker, "rev-parse", "HEAD"))
            self.assertEqual(git_text(worker, "remote"), "")
            self.assertFalse((worker / ".git" / "objects" / "info" / "alternates").exists())
            self.assertEqual(git_text(primary, "branch", "--format=%(refname:short)"), "main")
            refs = set(git_text(worker, "for-each-ref", "--format=%(refname)").splitlines())
            self.assertEqual(refs, {
                "refs/heads/pisec/ws_" + "b" * 32 + "/work",
                "refs/remotes/origin/HEAD",
                "refs/remotes/origin/main",
            })

    def test_dirty_detached_and_wrong_identity_reject_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "primary"
            worker = root / "worker"
            make_repo(primary)
            branch, target_ref, base_oid = project_target_state(primary, "main")
            create_worker_repository(
                primary=primary,
                worker=worker,
                project_id="prj_" + "c" * 32,
                workstream_id="ws_" + "d" * 32,
                target_branch_ref=target_ref,
                base_oid=base_oid,
                target_branch=branch,
            )
            (worker / "dirty.txt").write_text("dirty\n")
            with self.assertRaises(NeedsAttentionError):
                validate_worker_repository(worker, branch_name="pisec/ws_" + "d" * 32 + "/work", base_oid=base_oid, target_branch=branch)
