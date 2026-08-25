from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from scripts.pisec.models import ConflictError, IdempotencyConflictError, InvalidRequestError, NeedsAttentionError, ScopeMismatchError
from scripts.pisec.pi_store import PiStore
from scripts.pisec.secretary import ensure_secretary
from scripts.pisec.projects import _git, register_project
from scripts.pisec.workflow import checkpoint, submit_completion
from scripts.pisec.integration import apply_workstream_acceptance, prepare_workstream_acceptance
from scripts.pisec.workstreams import authorize_apply_workstream, complete_workstream, prepare_workstream, retire_workstream
from tests.pisec_fixture import DelayedFixtureWorkspace, FixtureHarness, FixtureWorkspace, UnattestedFixtureWorkspace, make_repo


class AmbiguousWorkspace(FixtureWorkspace):
    def observe_workstream(self, *, path: str, agent_name: str):
        return None


class CrashOnce:
    def __init__(self, target):
        self.target = target
        self.hit_target = False

    def hit(self, name, context):
        if name == self.target and not self.hit_target:
            self.hit_target = True
            raise RuntimeError(f"crash at {name}")

class WorkstreamTests(unittest.TestCase):
    def fixture(self, workspace_type=FixtureWorkspace):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        repo = root / "repo"
        make_repo(repo)
        store = PiStore(root / "state")
        project = register_project(store, repo, default_ref="main")
        harness = FixtureHarness(root)
        workspace = workspace_type(root, store)
        ensure_secretary(store, project["project_id"], harness, workspace)
        return temp, root, repo, store, project, harness, workspace, None

    def prepare(self, root, store, project, harness, workspace, key="create-1", failpoint=None):
        task_packet = {"schemaVersion": 1, "outcome": "Parser behavior is implemented and verified.", "boundaries": ["Change the parser only."], "acceptance": ["Parser tests pass."], "openQuestions": [], "evidence": ["Test output."]}
        return prepare_workstream(store, project_id=project["project_id"], title="Implement parser", purpose="Ship exact behavior", brief="Implement and verify the parser without unrelated changes.", task_packet=task_packet, idempotency_key=key, harness=harness, workspace=workspace, work_root=root / "worktrees", failpoint=failpoint)
    def apply(self, prepared, store, harness, workspace, git_objects, failpoint=None):
        del git_objects
        return authorize_apply_workstream(store, scope=prepared["approvalScope"], harness=harness, workspace=workspace, failpoint=failpoint)

    def submit_completion(self, store, workstream):
        binding = store.conn.execute("SELECT runtime_instance_id FROM runtime_bindings WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()
        task = store.conn.execute("SELECT packet_sha256 FROM task_packets WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()
        source = _git(Path(workstream["worktree_path"]), "rev-parse", "HEAD").lower()
        packet = {
            "acceptance": [{"criterion": "Parser tests pass.", "status": "passed", "evidence": ["Test output."]}],
            "verification": [{"command": "fixture verification", "result": "passed"}],
            "sourceCommit": source,
            "taskPacketSha256": task["packet_sha256"],
            "changedSurfaces": ["fixture"],
            "residualRisk": "none",
        }
        return submit_completion(store, workstream_id=workstream["workstream_id"], runtime_instance_id=binding["runtime_instance_id"], packet=packet)

    def test_prepare_is_pure_and_idempotent(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        first = self.prepare(root, store, project, harness, workspace)
        second = self.prepare(root, store, project, harness, workspace)
        self.assertEqual(first["approvalScope"], second["approvalScope"])
        self.assertFalse(Path(first["approvalScope"]["worktreePath"]).exists())
        self.assertEqual(store.conn.execute("SELECT count(*) FROM workstreams").fetchone()[0], 2)
        with self.assertRaises(IdempotencyConflictError):
            prepare_workstream(store, project_id=project["project_id"], title="Different", purpose="Ship exact behavior", brief="Implement and verify the parser without unrelated changes.", task_packet={"schemaVersion": 1, "outcome": "Parser behavior is implemented and verified.", "boundaries": ["Change the parser only."], "acceptance": ["Parser tests pass."], "openQuestions": [], "evidence": ["Test output."]}, idempotency_key="create-1", harness=harness, workspace=workspace, work_root=root / "worktrees")

    def test_prepare_carries_python_env_into_scope(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        env_dir = root / "venv"
        env_dir.mkdir()
        task_packet = {"schemaVersion": 1, "outcome": "Parser behavior is implemented and verified.", "boundaries": ["Change the parser only."], "acceptance": ["Parser tests pass."], "openQuestions": [], "evidence": ["Test output."]}
        prepared = prepare_workstream(store, project_id=project["project_id"], title="Implement parser", purpose="Ship exact behavior", brief="Implement and verify the parser without unrelated changes.", task_packet=task_packet, idempotency_key="create-env", harness=harness, workspace=workspace, work_root=root / "worktrees", python_env=str(env_dir))
        self.assertEqual(prepared["approvalScope"]["pythonEnv"], str(env_dir.resolve()))
        plain = self.prepare(root, store, project, harness, workspace, key="create-plain")
        self.assertIn("pythonEnv", plain["approvalScope"])
        self.assertIsNone(plain["approvalScope"]["pythonEnv"])

    def test_prepare_carries_project_store_dirs_into_scope(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        prepared = self.prepare(root, store, project, harness, workspace)
        self.assertNotIn("projectWorktreesDir", prepared["approvalScope"])
        self.assertNotIn("projectGitObjectsDir", prepared["approvalScope"])

    def test_stored_legacy_proposal_replays_without_project_store_dirs(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        prepared = self.prepare(root, store, project, harness, workspace)
        row = store.conn.execute("SELECT result_json FROM operations WHERE kind='workstream.create' AND workstream_id=?", (prepared["approvalScope"]["workstreamId"],)).fetchone()
        legacy = json.loads(row["result_json"])
        legacy.pop("targetBranchRef")
        store.conn.execute("UPDATE operations SET result_json=? WHERE kind='workstream.create' AND workstream_id=?", (json.dumps(legacy), prepared["approvalScope"]["workstreamId"]))
        with self.assertRaises(ScopeMismatchError):
            self.prepare(root, store, project, harness, workspace)

    def test_prepare_auto_discovers_repo_venv(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        venv = repo / ".venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        prepared = self.prepare(root, store, project, harness, workspace, key="create-auto")
        self.assertEqual(prepared["approvalScope"]["pythonEnv"], str(venv.resolve()))
        other = root / "other-venv"
        other.mkdir()
        task_packet = {"schemaVersion": 1, "outcome": "Parser behavior is implemented and verified.", "boundaries": ["Change the parser only."], "acceptance": ["Parser tests pass."], "openQuestions": [], "evidence": ["Test output."]}
        explicit = prepare_workstream(store, project_id=project["project_id"], title="Implement parser", purpose="Ship exact behavior", brief="Implement and verify the parser without unrelated changes.", task_packet=task_packet, idempotency_key="create-explicit", harness=harness, workspace=workspace, work_root=root / "worktrees", python_env=str(other))
        self.assertEqual(explicit["approvalScope"]["pythonEnv"], str(other.resolve()))

    def test_prepare_rejects_invalid_python_env(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        task_packet = {"schemaVersion": 1, "outcome": "Parser behavior is implemented and verified.", "boundaries": ["Change the parser only."], "acceptance": ["Parser tests pass."], "openQuestions": [], "evidence": ["Test output."]}
        with self.assertRaises(InvalidRequestError):
            prepare_workstream(store, project_id=project["project_id"], title="Implement parser", purpose="Ship exact behavior", brief="Implement and verify the parser without unrelated changes.", task_packet=task_packet, idempotency_key="create-rel", harness=harness, workspace=workspace, work_root=root / "worktrees", python_env="relative/venv")
        real_env = root / "real-venv"
        real_env.mkdir()
        env_link = root / "venv-link"
        env_link.symlink_to(real_env, target_is_directory=True)
        with self.assertRaises(NeedsAttentionError):
            prepare_workstream(store, project_id=project["project_id"], title="Implement parser", purpose="Ship exact behavior", brief="Implement and verify the parser without unrelated changes.", task_packet=task_packet, idempotency_key="create-link", harness=harness, workspace=workspace, work_root=root / "worktrees", python_env=str(env_link))

    def test_proposal_commit_replays_after_crash(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        with self.assertRaises(RuntimeError):
            self.prepare(root, store, project, harness, workspace, failpoint=CrashOnce("after_proposal_commit"))
        replay = self.prepare(root, store, project, harness, workspace)
        self.assertEqual(replay["operation"]["state"], "planned")
        self.assertEqual(store.conn.execute("SELECT count(*) FROM workstreams").fetchone()[0], 2)

    def test_runtime_attestation_allows_launch_pending_surface(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        prepared = self.prepare(root, store, project, harness, workspace, key="launch-pending-agent")
        result = self.apply(prepared, store, harness, workspace, git_objects)
        self.assertEqual(result["operation"]["state"], "succeeded")
        self.assertEqual(len(workspace.prompts), 2)
        self.assertEqual(workspace.prompts[1][0], "fixture-surface-2")

    def test_secretary_recovery_uses_binding_when_repository_path_is_ambiguous(self):
        temp, _root, _repo, store, _project, _harness, _workspace, _git_objects = self.fixture(AmbiguousWorkspace)
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        row = store.conn.execute("SELECT provisioning_state FROM workstreams WHERE kind='secretary'").fetchone()
        self.assertEqual(row["provisioning_state"], "bound")

    def test_waits_for_interactive_readiness_before_prompting(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture(DelayedFixtureWorkspace)
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        prepared = self.prepare(root, store, project, harness, workspace, key="delayed-interactive-ready")
        result = self.apply(prepared, store, harness, workspace, git_objects)
        self.assertEqual(result["operation"]["state"], "succeeded")
        self.assertGreaterEqual(workspace.observations, 2)
        self.assertEqual(len(workspace.prompts), 2)

    def test_rejects_agent_without_pisec_runtime_attestation(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture(UnattestedFixtureWorkspace)
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        prepared = self.prepare(root, store, project, harness, workspace, key="unattested-agent")
        with patch("scripts.pisec.workstreams._wait_for_agent", side_effect=NeedsAttentionError("agent started without Pisec runtime attestation")):
            with self.assertRaisesRegex(NeedsAttentionError, "runtime attestation"):
                self.apply(prepared, store, harness, workspace, git_objects)
        self.assertEqual(len(workspace.prompts), 1)

    def test_scope_mismatch_creates_no_effect(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        prepared = self.prepare(root, store, project, harness, workspace)
        bad = dict(prepared["approvalScope"])
        bad["brief"] += " changed"
        with self.assertRaises(ScopeMismatchError):
            authorize_apply_workstream(store, scope=bad, harness=harness, workspace=workspace)
        self.assertEqual(len(workspace.worktrees), 1)
        self.assertEqual(store.conn.execute("SELECT count(*) FROM authorizations").fetchone()[0], 0)

    def test_replay_converges_after_every_checkpoint(self):
        failpoints = ["after_authorization_consume", "after_worker_repo_creation", "after_worker_repo_verification", "after_policy_map_materialization", "after_agent_start", "after_brief_delivery", "before_final_event_commit", "after_final_event_commit"]
        for index, point in enumerate(failpoints):
            with self.subTest(point=point):
                temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
                try:
                    prepared = self.prepare(root, store, project, harness, workspace, key=f"create-{index}")
                    with self.assertRaises(RuntimeError):
                        self.apply(prepared, store, harness, workspace, git_objects, failpoint=CrashOnce(point))
                    result = self.apply(prepared, store, harness, workspace, git_objects)
                    self.assertEqual(result["operation"]["state"], "succeeded")
                    self.assertEqual(len(workspace.worktrees), 2)
                    self.assertEqual(len(workspace.agents), 2)
                    self.assertEqual(len(workspace.prompts), 2)
                    self.assertEqual(store.conn.execute("SELECT count(*) FROM authorizations").fetchone()[0], 1)
                    self.assertEqual(store.conn.execute("SELECT count(*) FROM events WHERE kind='workstream.created'").fetchone()[0], 1)
                finally:
                    store.close()
                    temp.cleanup()

    def test_worker_replay_reuses_persisted_surface_snapshot(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        prepared = self.prepare(root, store, project, harness, workspace, key="surface-replay")
        with self.assertRaises(RuntimeError):
            self.apply(prepared, store, harness, workspace, git_objects, failpoint=CrashOnce("after_worker_repo_creation"))
        self.assertEqual(harness.surface_calls, 2)
        (root / "runtime-surface" / "changed.txt").write_text("changed\n")
        with self.assertRaises(NeedsAttentionError):
            self.apply(prepared, store, harness, workspace, git_objects)
        self.assertEqual(harness.surface_calls, 2)

    def test_ready_checkpoint_submits_completion_automatically(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        prepared = self.prepare(root, store, project, harness, workspace)
        result = self.apply(prepared, store, harness, workspace, git_objects)
        workstream = result["workstream"]
        binding = store.conn.execute("SELECT runtime_instance_id FROM runtime_bindings WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()
        task = store.conn.execute("SELECT packet_sha256 FROM task_packets WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()
        source = _git(Path(workstream["worktree_path"]), "rev-parse", "HEAD").lower()
        packet = {
            "acceptance": [{"criterion": "Parser tests pass.", "status": "passed", "evidence": ["Test output."]}],
            "verification": [{"command": "fixture verification", "result": "passed"}],
            "sourceCommit": source,
            "taskPacketSha256": task["packet_sha256"],
            "changedSurfaces": ["fixture"],
            "residualRisk": "none",
        }
        submit_completion(store, workstream_id=workstream["workstream_id"], runtime_instance_id=binding["runtime_instance_id"], packet=packet)
        self.assertEqual(store.conn.execute("SELECT count(*) FROM completion_packets WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()[0], 1)
        self.assertEqual(store.conn.execute("SELECT phase FROM workstream_checkpoints WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()["phase"], "ready_review")
        submit_completion(store, workstream_id=workstream["workstream_id"], runtime_instance_id=binding["runtime_instance_id"], packet=packet)
        self.assertEqual(store.conn.execute("SELECT count(*) FROM completion_packets WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()[0], 1)
        changed_packet = dict(packet)
        changed_packet["residualRisk"] = "changed"
        submit_completion(store, workstream_id=workstream["workstream_id"], runtime_instance_id=binding["runtime_instance_id"], packet=changed_packet)
        self.assertEqual(store.conn.execute("SELECT count(*) FROM completion_packets WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()[0], 2)

    def test_ready_checkpoint_rolls_back_when_completion_submission_fails(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        prepared = self.prepare(root, store, project, harness, workspace)
        result = self.apply(prepared, store, harness, workspace, git_objects)
        workstream = result["workstream"]
        binding = store.conn.execute("SELECT runtime_instance_id FROM runtime_bindings WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()
        task = store.conn.execute("SELECT packet_sha256 FROM task_packets WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()
        packet = {
            "acceptance": [{"criterion": "Parser tests pass.", "status": "passed", "evidence": ["Test output."]}],
            "verification": [{"command": "fixture verification", "result": "passed"}],
            "sourceCommit": "0" * 40,
            "taskPacketSha256": task["packet_sha256"],
            "changedSurfaces": ["fixture"],
            "residualRisk": "none",
        }
        with self.assertRaises(ConflictError):
            submit_completion(store, workstream_id=workstream["workstream_id"], runtime_instance_id=binding["runtime_instance_id"], packet=packet)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM completion_packets WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()[0], 0)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM workstream_checkpoints WHERE workstream_id=?", (workstream["workstream_id"],)).fetchone()[0], 0)

    def test_completion_and_retirement_retain_git_names(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        prepared = self.prepare(root, store, project, harness, workspace)
        result = self.apply(prepared, store, harness, workspace, git_objects)
        workstream = result["workstream"]
        branch = workstream["branch_name"]
        checkout = workstream["worktree_path"]
        completion = self.submit_completion(store, workstream)
        acceptance = prepare_workstream_acceptance(store, project["project_id"], workstream["workstream_id"])
        apply_workstream_acceptance(store, project["project_id"], acceptance["approvalScope"])
        complete_workstream(store, project["project_id"], workstream["workstream_id"], completion["packet_sha256"], workspace)
        store.conn.execute("UPDATE runtime_bindings SET observed_state='idle' WHERE workstream_id=?", (workstream["workstream_id"],))
        retired = retire_workstream(store, project["project_id"], workstream["workstream_id"], workspace)
        self.assertEqual(retired["desired_state"], "retired")
        self.assertEqual(retired["branch_name"], branch)
        self.assertEqual(retired["worktree_path"], checkout)
        self.assertEqual(len(workspace.closed), 1)

    def test_mismatched_observation_stops_at_attention(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        prepared = self.prepare(root, store, project, harness, workspace)
        with self.assertRaises(RuntimeError):
            self.apply(prepared, store, harness, workspace, git_objects, failpoint=CrashOnce("after_worker_repo_creation"))
        workspace.project_workspace_id = "fixture-other-workspace"
        with self.assertRaises(NeedsAttentionError):
            self.apply(prepared, store, harness, workspace, git_objects)
        row = store.conn.execute("SELECT provisioning_state FROM workstreams WHERE kind='worker'").fetchone()
        self.assertEqual(row[0], "needs_attention")

if __name__ == "__main__":
    unittest.main()
