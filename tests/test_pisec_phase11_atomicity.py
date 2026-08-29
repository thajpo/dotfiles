from __future__ import annotations

from multiprocessing import get_context
import fcntl
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Any
from unittest.mock import patch

from scripts.pisec.control_plane import control_plane_lock
from scripts.pisec.effects import EFFECT_STEPS, effect_state, journal_compensate, journal_confirm, journal_entries, journal_intent
from scripts.pisec.models import NeedsAttentionError, PisecError
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import register_project
from scripts.pisec.workstreams import authorize_apply_workstream, prepare_workstream
from scripts.pisec.secretary import ensure_secretary
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace, make_repo


def _hold_lock(root: str, label: str, ready, release, result) -> None:
    with control_plane_lock(Path(root), timeout=5):
        result.put((label, "entered"))
        ready.set()
        release.wait(5)
        result.put((label, "released"))


def _take_lock(root: str, label: str, result) -> None:
    started = time.monotonic()
    def announce_wait() -> None:
        result.put((label, "waiting", "Pisec update is waiting for one worker creation to finish."))

    with control_plane_lock(Path(root), timeout=5, on_wait=announce_wait):
        result.put((label, "entered", time.monotonic() - started))


def _crash_with_lock(root: str, ready) -> None:
    with control_plane_lock(Path(root), timeout=5):
        ready.set()
        time.sleep(0.1)
        import os
        os._exit(17)


class CrashOnce:
    def __init__(self, target: str):
        self.target = target
        self.hit_target = False

    def hit(self, name: str, _context) -> None:
        if name == self.target and not self.hit_target:
            self.hit_target = True
            raise RuntimeError(f"crash at {name}")


class HarnessNamedWorkspace(FixtureWorkspace):
    def observe_workstream(self, *, path: str, agent_name: str):
        observed = super().observe_workstream(path=path, agent_name=agent_name)
        if observed is None:
            return None
        agent = next((item for item in self.agents.values() if item.surface_id == observed.surface_id), None)
        return type(observed)(observed.workspace_id, observed.view_id, observed.surface_id, observed.worktree_path, observed.branch_name, agent)

    def start_agent(self, surface_id: str, name: str, agent_kind: str):
        row = self.store.conn.execute(
            "SELECT w.kind FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workspace_surface_id=?",
            (surface_id,),
        ).fetchone()
        if row is not None and row["kind"] == "secretary":
            return super().start_agent(surface_id, name, agent_kind)
        return super().start_agent(surface_id, "fixture-agent", agent_kind)


class SettlingHarnessNamedWorkspace(HarnessNamedWorkspace):
    def __init__(self, root: Path, store: Any):
        super().__init__(root, store)
        self.runtime_observations = 0

    def observe_runtime(self, surface_id: str, process_identity: str):
        row = self.store.conn.execute(
            "SELECT w.kind FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workspace_surface_id=?",
            (surface_id,),
        ).fetchone()
        if row is not None and row["kind"] == "worker" and self.runtime_observations < 2:
            self.runtime_observations += 1
            observed = super().observe_runtime(surface_id, process_identity)
            return type(observed)("unknown", "pane process information is still settling")
        return super().observe_runtime(surface_id, process_identity)


class Phase11AtomicityTests(unittest.TestCase):
    def test_updater_lock_descriptor_survives_exec_without_releasing_exclusivity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            locks = root / "locks"
            locks.mkdir(mode=0o700, parents=True)
            path = locks / "control-plane.lock"
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                environment = dict(os.environ)
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
                environment["PISEC_CONTROL_PLANE_LOCK_FD"] = str(descriptor)
                source = (
                    "from pathlib import Path; import sys; "
                    "from scripts.pisec.control_plane import control_plane_lock; "
                    "\nwith control_plane_lock(Path(sys.argv[1]), timeout=0): print('entered')"
                )
                child = subprocess.run(
                    [sys.executable, "-c", source, str(root)],
                    env=environment,
                    pass_fds=(descriptor,),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertEqual(child.stdout.strip(), "entered")
                contender = os.open(path, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(contender)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def test_inherited_control_plane_descriptor_must_match_the_lock_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            wrong = root / "wrong.lock"
            root.mkdir(mode=0o700)
            descriptor = os.open(wrong, os.O_RDWR | os.O_CREAT, 0o600)
            os.fchmod(descriptor, 0o600)
            try:
                with patch.dict(os.environ, {"PISEC_CONTROL_PLANE_LOCK_FD": str(descriptor)}):
                    with self.assertRaisesRegex(PisecError, "inherited Pisec control-plane lock is invalid"):
                        with control_plane_lock(root, timeout=0):
                            self.fail("a mismatched inherited descriptor must not enter")
            finally:
                os.close(descriptor)

    def test_provisioning_journal_confirms_every_external_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo, default_ref="main")
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                ensure_secretary(store, project["project_id"], harness, workspace)
                packet = {
                    "schemaVersion": 1,
                    "outcome": "Complete the bounded fixture engineering task.",
                    "boundaries": ["Change only the approved fixture paths."],
                    "acceptance": ["The fixture verification passes."],
                    "openQuestions": [],
                    "evidence": ["The focused test output."],
                }
                prepared = prepare_workstream(
                    store,
                    project_id=project["project_id"],
                    title="Complete fixture task",
                    purpose="Exercise durable provisioning recovery",
                    brief="Inspect, implement, verify, and report the bounded engineering task.",
                    task_packet=packet,
                    idempotency_key="phase11-worker",
                    harness=harness,
                    workspace=workspace,
                    work_root=root / "worktrees",
                )
                applied = authorize_apply_workstream(store, scope=prepared["approvalScope"], harness=harness, workspace=workspace)
                entries = journal_entries(store, applied["operation"]["operation_id"])
                self.assertEqual([entry["step"] for entry in entries], list(EFFECT_STEPS))
                self.assertTrue(all(entry["state"] == "confirmed" for entry in entries))
                self.assertFalse((root / "state" / "profile-staging" / applied["operation"]["operation_id"]).exists())
                confirmed_steps = [
                    json.loads(row["payload_json"])["step"]
                    for row in store.conn.execute(
                        "SELECT payload_json FROM events WHERE operation_id=? AND kind='provisioning.effect.confirmed' ORDER BY sequence",
                        (applied["operation"]["operation_id"],),
                    )
                ]
                self.assertEqual(confirmed_steps, list(EFFECT_STEPS))
                binding_entry = next(entry for entry in entries if entry["step"] == "runtime_binding")
                self.assertTrue(binding_entry["identity"].get("runtimeInstanceId"))
                self.assertEqual(len({entry["identitySha256"] for entry in entries}), len(EFFECT_STEPS))
                self.assertEqual(
                    store.conn.execute(
                        "SELECT COUNT(*) FROM events WHERE operation_id=? AND kind='provisioning.effect.confirmed'",
                        (applied["operation"]["operation_id"],),
                    ).fetchone()[0],
                    len(EFFECT_STEPS),
                )

    def test_journal_accepts_adapter_agent_name_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo, default_ref="main")
                harness = FixtureHarness(root)
                workspace = SettlingHarnessNamedWorkspace(root, store)
                ensure_secretary(store, project["project_id"], harness, workspace)
                packet = {
                    "schemaVersion": 1,
                    "outcome": "Complete the bounded fixture engineering task.",
                    "boundaries": ["Change only the approved fixture paths."],
                    "acceptance": ["The fixture verification passes."],
                    "openQuestions": [],
                    "evidence": ["The focused test output."],
                }
                prepared = prepare_workstream(
                    store,
                    project_id=project["project_id"],
                    title="Accept adapter agent alias",
                    purpose="Verify harness agent identity compatibility",
                    brief="Inspect, implement, verify, and report the bounded engineering task.",
                    task_packet=packet,
                    idempotency_key="phase11-agent-alias",
                    harness=harness,
                    workspace=workspace,
                    work_root=root / "worktrees",
                )
                applied = authorize_apply_workstream(
                    store,
                    scope=prepared["approvalScope"],
                    harness=harness,
                    workspace=workspace,
                )
                self.assertEqual(applied["operation"]["state"], "succeeded")
                agent_entry = next(
                    entry for entry in journal_entries(store, applied["operation"]["operation_id"])
                    if entry["step"] == "agent_started"
                )
                self.assertEqual(agent_entry["identity"]["agentName"], "fixture-agent")

    def test_restart_after_every_journal_step_has_one_bound_worker(self):
        for step in EFFECT_STEPS:
            with self.subTest(step=step), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / "repo"
                make_repo(repo)
                with PiStore(root / "state") as store:
                    project = register_project(store, repo, default_ref="main")
                    harness = FixtureHarness(root)
                    workspace = FixtureWorkspace(root, store)
                    ensure_secretary(store, project["project_id"], harness, workspace)
                    packet = {
                        "schemaVersion": 1,
                        "outcome": "Complete the bounded fixture engineering task.",
                        "boundaries": ["Change only the approved fixture paths."],
                        "acceptance": ["The fixture verification passes."],
                        "openQuestions": [],
                        "evidence": ["The focused test output."],
                    }
                    prepared = prepare_workstream(
                        store,
                        project_id=project["project_id"],
                        title="Replay fixture task",
                        purpose="Exercise restart-safe provisioning",
                        brief="Inspect, implement, verify, and report the bounded engineering task.",
                        task_packet=packet,
                        idempotency_key=f"phase11-replay-{step}",
                        harness=harness,
                        workspace=workspace,
                        work_root=root / "worktrees",
                    )
                    with self.assertRaises(RuntimeError):
                        authorize_apply_workstream(
                            store,
                            scope=prepared["approvalScope"],
                            harness=harness,
                            workspace=workspace,
                            failpoint=CrashOnce(f"after_journal_{step}"),
                        )
                    result = authorize_apply_workstream(
                        store,
                        scope=prepared["approvalScope"],
                        harness=harness,
                        workspace=workspace,
                    )
                    self.assertEqual(result["operation"]["state"], "succeeded")
                    entries = journal_entries(store, result["operation"]["operation_id"])
                    self.assertEqual([entry["step"] for entry in entries], list(EFFECT_STEPS))
                    self.assertTrue(all(entry["state"] == "confirmed" for entry in entries))
                    self.assertEqual(len(workspace.worktrees), 2)
                    self.assertEqual(len(workspace.agents), 2)
                    self.assertEqual(
                        store.conn.execute(
                            "SELECT COUNT(*) FROM events WHERE workstream_id=? AND kind='runtime.bootstrap'",
                            (result["workstream"]["workstream_id"],),
                        ).fetchone()[0],
                        1,
                    )

    def test_journal_compensation_is_idempotent_and_identity_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo)
                harness = FixtureHarness(root)
                workspace = FixtureWorkspace(root, store)
                ensure_secretary(store, project["project_id"], harness, workspace)
                packet = {
                    "schemaVersion": 1,
                    "outcome": "Complete the bounded fixture engineering task.",
                    "boundaries": ["Change only the approved fixture paths."],
                    "acceptance": ["The fixture verification passes."],
                    "openQuestions": [],
                    "evidence": ["The focused test output."],
                }
                prepared = prepare_workstream(
                    store,
                    project_id=project["project_id"],
                    title="Prepare compensation fixture",
                    purpose="Exercise durable journal compensation",
                    brief="Prepare the bounded fixture and verify the journal behavior.",
                    task_packet=packet,
                    idempotency_key="phase11-compensation",
                    harness=harness,
                    workspace=workspace,
                    work_root=root / "worktrees",
                )
                operation_id = prepared["approvalScope"]["operationId"]
                workstream_id = prepared["approvalScope"]["workstreamId"]
                identity = {"path": str(root / "owned"), "branch": "pisec/ws_test/work", "baseCommitOid": "0" * 40}
                journal_intent(store, operation_id=operation_id, project_id=project["project_id"], workstream_id=workstream_id, step="worker_repository", identity=identity)
                journal_confirm(store, operation_id=operation_id, project_id=project["project_id"], workstream_id=workstream_id, step="worker_repository", identity={**identity, "observed": True})
                with self.assertRaises(NeedsAttentionError):
                    journal_compensate(store, operation_id=operation_id, project_id=project["project_id"], workstream_id=workstream_id, step="worker_repository", identity={**identity, "path": str(root / "not-owned")}, reason="wrong identity")
                first = journal_compensate(store, operation_id=operation_id, project_id=project["project_id"], workstream_id=workstream_id, step="worker_repository", identity=identity, reason="fixture rollback")
                second = journal_compensate(store, operation_id=operation_id, project_id=project["project_id"], workstream_id=workstream_id, step="worker_repository", identity=identity, reason="fixture rollback")
                self.assertEqual(first["state"], "compensated")
                self.assertEqual(second["state"], "compensated")
                self.assertEqual(effect_state(store, operation_id, "worker_repository")["state"], "compensated")

    def test_update_waits_for_provisioning_and_crashed_owner_releases_lock(self):
        context = get_context("fork")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            result = context.Queue()
            ready = context.Event()
            release = context.Event()
            creator = context.Process(target=_hold_lock, args=(str(root), "create", ready, release, result))
            creator.start()
            self.assertTrue(ready.wait(5))
            updater = context.Process(target=_take_lock, args=(str(root), "update", result))
            updater.start()
            time.sleep(0.2)
            self.assertTrue(updater.is_alive(), "update must wait for provisioning")
            release.set()
            creator.join(5)
            updater.join(5)
            self.assertEqual(creator.exitcode, 0)
            self.assertEqual(updater.exitcode, 0)
            messages = []
            while True:
                try:
                    messages.append(result.get_nowait())
                except queue.Empty:
                    break
            self.assertIn(("create", "entered"), messages)
            self.assertIn(("create", "released"), messages)
            self.assertIn(("update", "waiting", "Pisec update is waiting for one worker creation to finish."), messages)
            update_entry = next(item for item in messages if item[0] == "update" and item[1] == "entered")
            self.assertGreaterEqual(update_entry[2], 0.15)

            crashed_ready = context.Event()
            crashed = context.Process(target=_crash_with_lock, args=(str(root), crashed_ready))
            crashed.start()
            self.assertTrue(crashed_ready.wait(5))
            crashed.join(5)
            self.assertNotEqual(crashed.exitcode, 0)
            recovered = context.Process(target=_take_lock, args=(str(root), "recovered", result))
            recovered.start()
            recovered.join(5)
            self.assertEqual(recovered.exitcode, 0)


if __name__ == "__main__":
    unittest.main()
