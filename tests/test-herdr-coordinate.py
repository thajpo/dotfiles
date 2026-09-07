#!/usr/bin/env python3
"""Real Git/filesystem protocol tests. No live Herdr pane is ever prompted."""

import copy
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("herdr_coordinate", str(ROOT / "bin/herdr-coordinate"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
M = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(M)
REAL_API = M.api


def command(*argv, cwd=None):
    return subprocess.check_output(argv, cwd=cwd, text=True, stderr=subprocess.PIPE).strip()


class CoordinationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="herdr-coordinate-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo, self.worktree = self.root / "repo", self.root / "worker"
        self.repo.mkdir()
        command("git", "init", "-b", "main", cwd=self.repo)
        command("git", "config", "user.name", "Test", cwd=self.repo)
        command("git", "config", "user.email", "test@example.com", cwd=self.repo)
        (self.repo / "code.txt").write_text("base\n")
        command("git", "add", ".", cwd=self.repo)
        command("git", "commit", "-m", "base", cwd=self.repo)
        self.base = command("git", "rev-parse", "HEAD", cwd=self.repo)
        command("git", "worktree", "add", "-b", "worker", str(self.worktree), cwd=self.repo)
        self.commit()
        self.state = M.Store(self.root / "state")
        self.route = {"HERDR_SOCKET_PATH": str(self.root / "herdr.sock"), "HERDR_SESSION": "test"}
        self.owner = self.agent("w-owner:p1", "w-owner", "owner-session", self.repo)
        self.child = self.agent("w-child:p1", "w-child", "worker-session", self.worktree)
        self.agents = {self.owner["pane_id"]: self.owner, self.child["pane_id"]: self.child}
        self.prompts = []
        self.prompt_error = None
        self.during_prompt = None
        self.checks = self.root / "checks.json"
        self.checks.write_text(json.dumps({"tests": [sys.executable, "-c", "print('baseline checks')"]}))
        self.old_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.old_cwd)
        environ = {**os.environ, "HERDR_ENV": "1", **self.route}
        for key in ("HERDR_CONFIG_PATH", "HERDR_COORDINATION_DIR"):
            environ.pop(key, None)
        env_patch = patch.dict(os.environ, environ, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.api_patch = patch.object(M, "api", self.fake_api)
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)
        self.as_owner()

    @staticmethod
    def agent(pane, workspace, session, path):
        return {"pane_id": pane, "workspace_id": workspace, "agent": "codex",
                "agent_session": {"value": session, "source": "herdr:codex"},
                "agent_status": "idle", "cwd": str(path)}

    def fake_api(self, route, *args):
        self.assertEqual(route, self.route)
        if args[:2] == ("agent", "get"):
            if args[2] not in self.agents:
                raise M.CoordinationError("agent not found")
            return {"agent": copy.deepcopy(self.agents[args[2]])}
        if args[:2] == ("worktree", "list"):
            return {"source": {"source_workspace_id": "w-owner", "source_checkout_path": str(self.repo)},
                    "worktrees": [{"path": str(self.worktree), "open_workspace_id": "w-child",
                                   "is_linked_worktree": True, "is_prunable": False}]}
        if args[:2] == ("agent", "prompt"):
            self.prompts.append(args)
            if self.during_prompt:
                self.during_prompt()
            if self.prompt_error:
                raise M.CoordinationError(self.prompt_error)
            return {"agent": args[2]}
        self.fail(f"unexpected API operation: {args}")

    def as_agent(self, agent):
        os.environ["HERDR_PANE_ID"] = agent["pane_id"]
        os.environ["CODEX_THREAD_ID"] = agent["agent_session"]["value"]
        os.chdir(agent["cwd"])

    def as_owner(self):
        self.as_agent(self.owner)

    def as_worker(self):
        self.as_agent(self.child)

    def invoke(self, *argv):
        args = M.parse_args(["--state-dir", str(self.state.root), *argv])
        handler = {"init": M.init_project, "check": M.check_command, "ack": M.acknowledge}.get(args.action)
        return (handler or getattr(M, args.action))(self.state, args)

    def init(self, enabled=True):
        self.as_owner()
        return self.invoke("init", "--checks-file", str(self.checks), *( ["--enable"] if enabled else []))

    def register(self):
        self.as_owner()
        return self.invoke("register", "--pane", self.child["pane_id"], "--session", "worker-session",
                           "--owner-session", "owner-session", "--worktree", str(self.worktree),
                           "--base", self.base, "--task", "test task",
                           "--assignment", "Codex: implement the requested test behavior; add regression coverage")

    def setup_project(self, enabled=True):
        self.project = self.init(enabled)["project"]
        self.worker = self.register()["worker"]

    def submit(self):
        self.as_worker()
        self.invoke("check", "--label", "tests")
        return self.invoke("handoff", "--summary", "Worker: implemented and tested")

    def delivery(self, request, direction="review"):
        return M.identifier(request["id"], direction)

    def claim(self, request):
        self.as_owner()
        return self.invoke("ack", self.delivery(request))

    def approve(self, request):
        self.claim(request)
        self.invoke("check", "--request", request["id"], "--label", "tests")
        return self.invoke("review", request["id"], "--verdict", "ready",
                           "--summary", "Codex: reviewed", "--diff-reviewed",
                           "--acceptance", "Regression behavior and scope checked",
                           "--risks", "No known unresolved findings; live UI untested")

    def commit(self):
        path = self.worktree / "code.txt"
        path.write_text(path.read_text() + "change\n")
        command("git", "add", ".", cwd=self.worktree)
        command("git", "commit", "-m", "worker change", cwd=self.worktree)
        return command("git", "rev-parse", "HEAD", cwd=self.worktree)

    def test_unconfigured_spawn_does_not_create_state(self):
        with patch.dict(os.environ, {"HERDR_ENV": ""}):
            result = self.invoke("register", "--if-configured", "--pane", "x", "--session", "x",
                                 "--owner-session", "x", "--worktree", "x", "--base", "x",
                                 "--task", "x", "--assignment", "x")
        self.assertFalse(result["registered"])
        self.assertFalse(self.state.root.exists())

    def test_init_is_paused_and_worker_cannot_init_project(self):
        result = self.init(enabled=False)
        self.assertFalse(result["enabled"])
        self.as_worker()
        with self.assertRaisesRegex(M.CoordinationError, "project-owner"):
            self.invoke("init", "--checks-file", str(self.checks))

    def test_registration_is_immutable_and_requires_correct_sessions(self):
        self.setup_project()
        before = self.state.read()
        self.register()
        self.assertEqual(before, self.state.read())
        self.child["agent_session"]["value"] = "replacement-worker"
        with self.assertRaisesRegex(M.CoordinationError, "worker session changed"):
            self.register()

    def test_complete_review_requires_independent_checks_and_rubric(self):
        self.setup_project()
        request = self.submit()
        self.invoke("dispatch")
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.prompts[0][2], self.owner["pane_id"])
        self.assertTrue(self.prompts[0][3].startswith("Tool:"))
        self.claim(request)
        with self.assertRaisesRegex(M.CoordinationError, "diff-reviewed"):
            self.invoke("review", request["id"], "--verdict", "ready", "--summary", "looks good")
        with self.assertRaisesRegex(M.CoordinationError, "supervisor checks"):
            self.invoke("review", request["id"], "--verdict", "ready", "--summary", "reviewed",
                        "--diff-reviewed", "--acceptance", "done", "--risks", "none known")
        result = self.approve(request)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=self.repo), self.base)
        self.assertEqual(result["sha"], command("git", "rev-parse", "HEAD", cwd=self.worktree))

    def test_no_checks_no_handoff_and_no_overriding_baseline(self):
        self.setup_project()
        self.as_worker()
        with self.assertRaisesRegex(M.CoordinationError, "no recorded worker"):
            self.invoke("handoff", "--summary", "Worker: done")
        with self.assertRaisesRegex(M.CoordinationError, "cannot override"):
            self.invoke("check", "--label", "tests", "--", "true")
        self.invoke("check", "--label", "extra", "--", "true")
        with self.assertRaisesRegex(M.CoordinationError, "missing required"):
            self.invoke("handoff", "--summary", "Worker: done")

    def test_failed_supplemental_check_blocks_handoff_until_rerun(self):
        self.setup_project()
        self.as_worker()
        self.invoke("check", "--label", "tests")
        self.invoke("check", "--label", "regression", "--", "true")
        result = self.invoke("check", "--label", "regression", "--", "false")
        self.assertEqual(result["exit_code"], 1)
        with self.assertRaisesRegex(M.CoordinationError, "latest worker checks failed"):
            self.invoke("handoff", "--summary", "Worker: done")
        self.invoke("check", "--label", "regression", "--", "true")
        self.invoke("handoff", "--summary", "Worker: fixed test")

    def test_check_that_changes_candidate_is_not_valid_evidence(self):
        self.setup_project()
        self.as_worker()
        result = self.invoke("check", "--label", "mutating", "--", sys.executable,
                             "-c", "from pathlib import Path; Path('untracked').touch()")
        self.assertFalse(result["valid"])
        self.assertIn("dirty", result["detail"])

    def test_test_timeout_is_recorded_as_failure(self):
        self.setup_project()
        self.as_worker()
        result = self.invoke("check", "--label", "timeout", "--timeout", "1", "--", sys.executable,
                             "-c", "import time; time.sleep(30)")
        self.assertEqual(result["exit_code"], 124)
        self.assertTrue(Path(result["log"]).exists())

    def test_worker_cannot_ack_or_approve_its_own_work(self):
        self.setup_project()
        request = self.submit()
        with self.assertRaisesRegex(M.CoordinationError, "wrong session"):
            self.invoke("ack", self.delivery(request))
        with self.assertRaisesRegex(M.CoordinationError, "owning supervisor"):
            self.invoke("review", request["id"], "--verdict", "ready", "--summary", "self approved")

    def test_pause_busy_blocked_and_unknown_do_not_prompt(self):
        self.setup_project(enabled=False)
        self.submit()
        self.invoke("dispatch")
        self.assertFalse(self.prompts)
        self.as_owner()
        self.invoke("mode", "--enable")
        for state in ("working", "blocked", "unknown"):
            self.owner["agent_status"] = state
            self.invoke("dispatch")
            self.assertFalse(self.prompts)
        self.owner["agent_status"] = "idle"
        self.child["agent_status"] = "working"
        self.invoke("dispatch")
        self.assertFalse(self.prompts)
        self.child["agent_status"] = "done"
        self.invoke("dispatch")
        self.assertEqual(len(self.prompts), 1)

    def test_pane_reuse_never_inherits_pending_requests(self):
        self.setup_project()
        self.submit()
        self.owner["agent_session"]["value"] = "different-chat"
        self.invoke("dispatch")
        self.assertFalse(self.prompts)
        self.assertIn("identity changed", next(iter(self.state.read()["deliveries"].values()))["reason"])
        self.owner["agent_session"]["value"] = "owner-session"
        self.child["agent_session"]["value"] = "different-worker"
        self.invoke("dispatch")
        self.assertFalse(self.prompts)

    def test_calling_thread_must_match_native_session(self):
        self.setup_project()
        os.environ["CODEX_THREAD_ID"] = "other-chat"
        with self.assertRaisesRegex(M.CoordinationError, "calling Codex thread"):
            self.invoke("mode", "--enable")

    def test_dirty_or_moved_candidate_is_not_dispatched(self):
        self.setup_project()
        request = self.submit()
        (self.worktree / "untracked").touch()
        self.invoke("dispatch")
        self.assertFalse(self.prompts)
        (self.worktree / "untracked").unlink()
        self.commit()
        self.invoke("dispatch")
        self.assertFalse(self.prompts)
        self.assertEqual(self.invoke("status")["requests"][request["id"]]["effective_state"], "stale")

    def test_new_commit_requires_fresh_worker_evidence(self):
        self.setup_project()
        self.submit()
        self.commit()
        with self.assertRaisesRegex(M.CoordinationError, "no recorded worker"):
            self.invoke("handoff", "--summary", "Worker: new commit")

    def test_ready_is_invalidated_if_head_moves(self):
        self.setup_project()
        request = self.submit()
        self.approve(request)
        self.commit()
        report = self.invoke("status")
        self.assertEqual(report["requests"][request["id"]]["effective_state"], "stale")
        self.assertEqual(report["requests"][request["id"]]["state"], "ready")  # historical verdict retained

    def test_duplicate_handoff_and_restart_do_not_duplicate_prompts(self):
        self.setup_project()
        request = self.submit()
        self.assertEqual(self.invoke("handoff", "--summary", "duplicate"), request)
        self.invoke("dispatch")
        self.state = M.Store(self.state.root)
        self.invoke("dispatch")
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(len(self.state.read()["requests"]), 1)

    def test_uncertain_delivery_requires_inspection_and_explicit_retry(self):
        self.setup_project()
        request = self.submit()
        self.prompt_error = "response lost after submitting prompt"
        self.invoke("dispatch")
        self.invoke("dispatch")
        key = self.delivery(request)
        self.assertEqual(self.state.read()["deliveries"][key]["state"], "uncertain")
        self.assertEqual(len(self.prompts), 1)
        self.as_owner()
        self.invoke("retry", key, "--reason", "Codex: inspected pane; request was not submitted")
        self.prompt_error = None
        self.invoke("dispatch")
        self.assertEqual(len(self.prompts), 2)

    def test_crash_after_persisting_intent_does_not_resend(self):
        self.setup_project()
        request = self.submit()
        key = self.delivery(request)
        with self.state.edit() as data:
            data["deliveries"][key].update(state="sending", attempted_at=time.time())
        self.invoke("dispatch")
        self.assertFalse(self.prompts)
        self.assertEqual(self.state.read()["deliveries"][key]["state"], "uncertain")

    def test_acknowledgement_racing_prompt_response_is_preserved(self):
        self.setup_project()
        request = self.submit()
        self.during_prompt = lambda: self.claim(request)
        self.invoke("dispatch")
        self.assertEqual(self.state.read()["deliveries"][self.delivery(request)]["state"], "acknowledged")
        self.assertEqual(self.state.read()["requests"][request["id"]]["state"], "reviewing")

    def test_dispatcher_exclusion(self):
        self.setup_project()
        self.submit()
        with self.state.lock("dispatch.lock"):
            with self.assertRaisesRegex(M.CoordinationError, "another dispatcher"):
                self.invoke("dispatch")
        self.assertFalse(self.prompts)

    def test_acknowledgement_timeout_is_attention_not_retry(self):
        self.setup_project()
        request = self.submit()
        self.invoke("dispatch")
        with self.state.edit() as data:
            data["deliveries"][self.delivery(request)]["attempted_at"] -= 301
        report = self.invoke("status")
        self.assertIn("acknowledgement_overdue", report["deliveries"][self.delivery(request)]["attention"])
        self.invoke("dispatch")
        self.assertEqual(len(self.prompts), 1)

    def test_revision_loop_is_acknowledged_and_bounded(self):
        self.setup_project()
        for index in range(3):
            request = self.submit()
            self.claim(request)
            verdict = self.invoke("review", request["id"], "--verdict", "revise",
                                  "--summary", "Codex: add focused regression coverage")
            if index == 2:
                self.assertEqual(verdict["state"], "needs_user")
                self.assertTrue(verdict["review"]["revision_limit_reached"])
                self.as_worker()
                self.commit()
                self.invoke("check", "--label", "tests")
                with self.assertRaisesRegex(M.CoordinationError, "User decision"):
                    self.invoke("handoff", "--summary", "Worker: trying to continue")
                break
            self.assertEqual(verdict["state"], "changes_requested")
            self.invoke("dispatch")
            self.assertEqual(self.prompts[-1][2], self.child["pane_id"])
            self.as_worker()
            self.commit()
            self.invoke("check", "--label", "tests")
            with self.assertRaisesRegex(M.CoordinationError, "acknowledge the scoped revision"):
                self.invoke("handoff", "--summary", "Worker: forgot to acknowledge")
            self.invoke("ack", self.delivery(request, "revision"))

    def test_blocker_handoff_does_not_require_clean_checkout_or_passing_tests(self):
        self.setup_project()
        self.as_worker()
        (self.worktree / "unfinished").touch()
        request = self.invoke("handoff", "--kind", "blocked", "--summary", "Worker: requirement ambiguous")
        self.invoke("dispatch")
        self.assertEqual(len(self.prompts), 1)
        self.claim(request)
        with self.assertRaisesRegex(M.CoordinationError, "blocker report"):
            self.invoke("review", request["id"], "--verdict", "ready", "--summary", "done")
        self.invoke("review", request["id"], "--verdict", "needs_user", "--summary", "Codex: needs product decision")

    def test_rebind_is_explicit_and_pauses_background_delivery(self):
        self.setup_project()
        request = self.submit()
        self.claim(request)
        self.owner["agent_session"]["value"] = "resumed-supervisor"
        self.as_owner()
        with self.assertRaisesRegex(M.CoordinationError, "previous supervisor"):
            self.invoke("rebind", self.project, "--previous-session", "wrong")
        result = self.invoke("rebind", self.project, "--previous-session", "owner-session")
        self.assertFalse(result["enabled"])
        self.invoke("dispatch")
        self.assertFalse(self.prompts)
        self.invoke("mode", "--enable")
        self.invoke("dispatch")
        self.assertEqual(len(self.prompts), 1)
        self.claim(request)

    def test_status_is_read_only_and_defaults_to_exact_session(self):
        self.setup_project()
        self.submit()
        before = self.state.path.read_bytes()
        self.owner["agent_session"]["value"] = "other-conversation"
        self.as_owner()
        self.assertFalse(self.invoke("status")["configured"])
        self.assertTrue(self.invoke("status", "--all")["configured"])
        self.assertEqual(before, self.state.path.read_bytes())

    def test_corrupt_state_is_not_overwritten(self):
        self.setup_project()
        self.state.path.write_text("{broken")
        with self.assertRaisesRegex(M.CoordinationError, "cannot read"):
            self.invoke("dispatch")
        self.assertEqual(self.state.path.read_text(), "{broken")

    def test_check_all_runs_registered_argv_and_records_every_label(self):
        self.checks.write_text(json.dumps({"one": ["true"], "two": ["true"]}))
        self.setup_project()
        self.as_worker()
        result = self.invoke("check", "--all")
        self.assertEqual({c["label"] for c in result["checks"]}, {"one", "two"})
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["valid"])
        self.invoke("handoff", "--summary", "Worker: both baselines passed")

    def test_baseline_policy_cannot_silently_change(self):
        self.setup_project()
        self.checks.write_text(json.dumps({"tests": ["false"]}))
        with self.assertRaisesRegex(M.CoordinationError, "policy is immutable"):
            self.init()

    def test_readonly_show_rejects_another_conversation(self):
        self.setup_project()
        request = self.submit()
        self.owner["agent_session"]["value"] = "unrelated-conversation"
        self.as_owner()
        with self.assertRaisesRegex(M.CoordinationError, "another project/session"):
            self.invoke("show", request["id"])

    def test_failed_check_after_ready_invalidates_current_readiness(self):
        self.setup_project()
        request = self.submit()
        self.approve(request)
        self.as_worker()
        self.invoke("check", "--label", "late-regression", "--", "false")
        self.assertEqual(self.invoke("status")["requests"][request["id"]]["effective_state"], "stale")

    def test_stale_request_can_be_acknowledged_and_escalated(self):
        self.setup_project()
        request = self.submit()
        self.commit()
        self.claim(request)
        result = self.invoke("review", request["id"], "--verdict", "needs_user",
                             "--summary", "Codex: candidate changed during review")
        self.assertEqual(result["state"], "needs_user")
        self.assertIn("HEAD changed", result["candidate_issue_at_claim"])

    def test_resolve_requires_owner_and_consumes_explicit_extra_round(self):
        self.setup_project()
        request = self.submit()
        self.claim(request)
        with self.state.edit() as data:
            data["workers"][self.worker]["revisions"] = 2
        self.invoke("review", request["id"], "--verdict", "revise", "--summary", "Codex: unresolved findings")
        self.as_worker()
        with self.assertRaisesRegex(M.CoordinationError, "owning supervisor"):
            self.invoke("resolve", request["id"], "--user-decision", "not actually User",
                        "--additional-revisions", "1")
        self.as_owner()
        self.invoke("resolve", request["id"], "--user-decision", "User: allow one more round for the regression fix",
                    "--additional-revisions", "1")
        worker = self.state.read()["workers"][self.worker]
        self.assertEqual(worker["revision_limit"], 3)
        self.assertEqual(worker["revisions"], 3)
        self.as_worker()
        self.invoke("ack", self.delivery(request, "revision"))
        self.commit()
        revised = self.submit()
        self.claim(revised)
        result = self.invoke("review", revised["id"], "--verdict", "revise", "--summary", "Codex: still unresolved")
        self.assertEqual(result["state"], "needs_user")

    def test_second_review_is_held_until_first_verdict(self):
        self.setup_project()
        first = self.submit()
        self.as_worker()
        # Exercise serialization with two explicit requests. Normally handoff
        # supersedes this worker's prior request; independent workers queue both.
        self.invoke("handoff", "--kind", "blocked", "--summary", "Worker: supplemental blocker")
        with self.state.edit() as data:
            data["requests"][first["id"]]["state"] = "submitted"
            data["deliveries"][self.delivery(first)]["state"] = "pending"
        self.invoke("dispatch")
        self.assertEqual(len(self.prompts), 1)
        data = self.state.read()
        sent = next(d for d in data["deliveries"].values() if d["state"] == "delivered")
        pending = next(d for d in data["deliveries"].values() if d["state"] == "pending")
        self.assertEqual(pending["reason"], "supervisor_has_outstanding_review")
        self.as_owner()
        self.invoke("ack", sent["id"])
        self.invoke("dispatch")
        self.assertEqual(len(self.prompts), 1)
        self.invoke("review", sent["request"], "--verdict", "needs_user", "--summary", "Codex: human decision needed")
        self.invoke("dispatch")
        self.assertEqual(len(self.prompts), 2)

    def test_actual_transport_pins_route_and_does_not_interpret_prompt_text(self):
        with patch.object(M, "run", return_value='{"result":{"accepted":true}}') as execute:
            REAL_API(self.route, "agent", "prompt", "w-owner:p1", "Tool: literal $(do-not-run) `text`")
        argv = execute.call_args.args[0]
        env = execute.call_args.kwargs["env"]
        self.assertEqual(argv[-1], "Tool: literal $(do-not-run) `text`")
        self.assertEqual(env["HERDR_SOCKET_PATH"], self.route["HERDR_SOCKET_PATH"])
        self.assertEqual(env["HERDR_ENV"], "1")
        self.assertNotIn("HERDR_PANE_ID", env)
        self.assertNotIn("HERDR_CLIENT_SOCKET_PATH", env)

    def test_two_worker_sessions_cannot_share_the_same_checkout(self):
        self.setup_project()
        another = self.agent("w-child:p2", "w-child", "second-session", self.worktree)
        self.agents[another["pane_id"]] = another
        with self.assertRaisesRegex(M.CoordinationError, "another registered worker owns"):
            self.invoke("register", "--pane", another["pane_id"], "--session", "second-session",
                        "--owner-session", "owner-session", "--worktree", str(self.worktree),
                        "--base", self.base, "--task", "overlapping worker", "--assignment", "Codex: scope")

    def test_cli_unconfigured_status_never_contacts_herdr_or_creates_state(self):
        result = subprocess.run([sys.executable, str(ROOT / "bin/herdr-coordinate"),
                                 "--state-dir", str(self.state.root), "status", "--all"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["configured"])
        self.assertFalse(self.state.root.exists())


if __name__ == "__main__":
    unittest.main()
