from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from scripts.pisec.models import IdempotencyConflictError, InvalidRequestError, NeedsAttentionError, ScopeMismatchError
from scripts.pisec.pi_store import PiStore
from scripts.pisec.secretary import ensure_secretary
from scripts.pisec.projects import register_project
from scripts.pisec.workstreams import authorize_apply_workstream, complete_workstream, prepare_workstream, retire_workstream
from tests.pisec_fixture import DelayedFixtureWorkspace, FixtureGitObjects, FixtureHarness, FixtureWorkspace, UnattestedFixtureWorkspace, make_repo


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

class AttentionGitObjects(FixtureGitObjects):
    def materialize(self, scope):
        raise NeedsAttentionError("private Git object store is unavailable")


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
        git_objects = FixtureGitObjects()
        return temp, root, repo, store, project, harness, workspace, git_objects

    def prepare(self, root, store, project, harness, workspace, key="create-1", failpoint=None):
        task_packet = {"schemaVersion": 1, "outcome": "Parser behavior is implemented and verified.", "boundaries": ["Change the parser only."], "acceptance": ["Parser tests pass."], "openQuestions": [], "evidence": ["Test output."]}
        return prepare_workstream(store, project_id=project["project_id"], title="Implement parser", purpose="Ship exact behavior", brief="Implement and verify the parser without unrelated changes.", task_packet=task_packet, idempotency_key=key, harness=harness, workspace=workspace, work_root=root / "worktrees", object_root=root / "objects", failpoint=failpoint)

    def apply(self, prepared, store, harness, workspace, git_objects, failpoint=None):
        return authorize_apply_workstream(store, scope=prepared["approvalScope"], harness=harness, workspace=workspace, git_objects=git_objects, failpoint=failpoint)

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
            prepare_workstream(store, project_id=project["project_id"], title="Different", purpose="Ship exact behavior", brief="Implement and verify the parser without unrelated changes.", task_packet={"schemaVersion": 1, "outcome": "Parser behavior is implemented and verified.", "boundaries": ["Change the parser only."], "acceptance": ["Parser tests pass."], "openQuestions": [], "evidence": ["Test output."]}, idempotency_key="create-1", harness=harness, workspace=workspace, work_root=root / "worktrees", object_root=root / "objects")

    def test_prepare_carries_python_env_into_scope(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        env_dir = root / "venv"
        env_dir.mkdir()
        task_packet = {"schemaVersion": 1, "outcome": "Parser behavior is implemented and verified.", "boundaries": ["Change the parser only."], "acceptance": ["Parser tests pass."], "openQuestions": [], "evidence": ["Test output."]}
        prepared = prepare_workstream(store, project_id=project["project_id"], title="Implement parser", purpose="Ship exact behavior", brief="Implement and verify the parser without unrelated changes.", task_packet=task_packet, idempotency_key="create-env", harness=harness, workspace=workspace, work_root=root / "worktrees", object_root=root / "objects", python_env=str(env_dir))
        self.assertEqual(prepared["approvalScope"]["pythonEnv"], str(env_dir.resolve()))
        plain = self.prepare(root, store, project, harness, workspace, key="create-plain")
        self.assertIn("pythonEnv", plain["approvalScope"])
        self.assertIsNone(plain["approvalScope"]["pythonEnv"])

    def test_prepare_carries_project_store_dirs_into_scope(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        prepared = self.prepare(root, store, project, harness, workspace)
        self.assertEqual(prepared["approvalScope"]["projectWorktreesDir"], str((root / "worktrees" / project["project_id"]).resolve()))
        self.assertEqual(prepared["approvalScope"]["projectGitObjectsDir"], str((root / "objects" / project["project_id"]).resolve()))

    def test_stored_legacy_proposal_replays_without_project_store_dirs(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        prepared = self.prepare(root, store, project, harness, workspace)
        row = store.conn.execute("SELECT result_json FROM operations WHERE kind='workstream.create' AND workstream_id=?", (prepared["approvalScope"]["workstreamId"],)).fetchone()
        legacy = json.loads(row["result_json"])
        legacy.pop("projectWorktreesDir")
        legacy.pop("projectGitObjectsDir")
        store.conn.execute("UPDATE operations SET result_json=? WHERE kind='workstream.create' AND workstream_id=?", (json.dumps(legacy), prepared["approvalScope"]["workstreamId"]))
        replayed = self.prepare(root, store, project, harness, workspace)
        self.assertEqual(replayed["approvalScope"]["workstreamId"], prepared["approvalScope"]["workstreamId"])
        self.assertNotIn("projectWorktreesDir", replayed["approvalScope"])

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
        explicit = prepare_workstream(store, project_id=project["project_id"], title="Implement parser", purpose="Ship exact behavior", brief="Implement and verify the parser without unrelated changes.", task_packet=task_packet, idempotency_key="create-explicit", harness=harness, workspace=workspace, work_root=root / "worktrees", object_root=root / "objects", python_env=str(other))
        self.assertEqual(explicit["approvalScope"]["pythonEnv"], str(other.resolve()))

    def test_prepare_rejects_invalid_python_env(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        task_packet = {"schemaVersion": 1, "outcome": "Parser behavior is implemented and verified.", "boundaries": ["Change the parser only."], "acceptance": ["Parser tests pass."], "openQuestions": [], "evidence": ["Test output."]}
        with self.assertRaises(InvalidRequestError):
            prepare_workstream(store, project_id=project["project_id"], title="Implement parser", purpose="Ship exact behavior", brief="Implement and verify the parser without unrelated changes.", task_packet=task_packet, idempotency_key="create-rel", harness=harness, workspace=workspace, work_root=root / "worktrees", object_root=root / "objects", python_env="relative/venv")
        real_env = root / "real-venv"
        real_env.mkdir()
        env_link = root / "venv-link"
        env_link.symlink_to(real_env, target_is_directory=True)
        with self.assertRaises(NeedsAttentionError):
            prepare_workstream(store, project_id=project["project_id"], title="Implement parser", purpose="Ship exact behavior", brief="Implement and verify the parser without unrelated changes.", task_packet=task_packet, idempotency_key="create-link", harness=harness, workspace=workspace, work_root=root / "worktrees", object_root=root / "objects", python_env=str(env_link))

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
        with patch("scripts.pisec.workstreams.time.monotonic", side_effect=[0.0, 6.0]):
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
            authorize_apply_workstream(store, scope=bad, harness=harness, workspace=workspace, git_objects=git_objects)
        self.assertEqual(len(workspace.worktrees), 1)
        self.assertEqual(store.conn.execute("SELECT count(*) FROM authorizations").fetchone()[0], 0)

    def test_replay_converges_after_every_checkpoint(self):
        failpoints = ["after_authorization_consume", "after_workspace_creation", "after_binding_persistence", "after_policy_map_materialization", "after_agent_start", "after_brief_delivery", "before_final_event_commit", "after_final_event_commit"]
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

    def test_completion_and_retirement_retain_git_names(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        prepared = self.prepare(root, store, project, harness, workspace)
        result = self.apply(prepared, store, harness, workspace, git_objects)
        workstream = result["workstream"]
        branch = workstream["branch_name"]
        checkout = workstream["worktree_path"]
        complete_workstream(store, project["project_id"], workstream["workstream_id"])
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
            self.apply(prepared, store, harness, workspace, git_objects, failpoint=CrashOnce("after_workspace_creation"))
        workspace.project_workspace_id = "fixture-other-workspace"
        with self.assertRaises(NeedsAttentionError):
            self.apply(prepared, store, harness, workspace, git_objects)
        row = store.conn.execute("SELECT provisioning_state FROM workstreams WHERE kind='worker'").fetchone()
        self.assertEqual(row[0], "needs_attention")

    def test_adapter_attention_after_workspace_effect_is_durable(self):
        temp, root, repo, store, project, harness, workspace, git_objects = self.fixture()
        self.addCleanup(temp.cleanup)
        self.addCleanup(store.close)
        prepared = self.prepare(root, store, project, harness, workspace, key="attention-after-workspace")
        with self.assertRaises(NeedsAttentionError):
            self.apply(prepared, store, harness, workspace, AttentionGitObjects())
        operation = store.conn.execute("SELECT state,step FROM operations WHERE workstream_id=?", (prepared["workstream"]["workstream_id"],)).fetchone()
        workstream = store.conn.execute("SELECT provisioning_state,attention_reason FROM workstreams WHERE workstream_id=?", (prepared["workstream"]["workstream_id"],)).fetchone()
        self.assertEqual(tuple(operation), ("needs_attention", "workspace_tab_observed_or_created"))
        self.assertEqual(workstream[0], "needs_attention")
        self.assertIn("private Git object store", workstream[1])


if __name__ == "__main__":
    unittest.main()
