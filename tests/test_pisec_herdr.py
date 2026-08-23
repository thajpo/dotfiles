from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import socketserver
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts.pisec.adapters import HarnessManifest
from scripts.pisec.models import PisecError
from scripts.pisec.workspaces.herdr import HerdrWorkspaceAdapter


class FakeHerdrState:
    def __init__(self):
        self.requests = []
        self.created = False
        self.agent_status = "idle"
        self.official_authority = True

    def result(self, method, params):
        self.requests.append((method, params))
        if method == "ping":
            return {"type": "pong", "version": "0.8.0", "protocol": 19}
        if method == "session.snapshot":
            if not self.created:
                snapshot = {"version": "0.8.0", "protocol": 19, "workspaces": [], "tabs": [], "panes": [], "layouts": [], "agents": []}
            else:
                snapshot = {
                    "version": "0.8.0",
                    "protocol": 19,
                    "workspaces": [{"workspace_id": "w1", "worktree": {"checkout_path": "/tmp/work", "branch": "pisec/ws/work"}}],
                    "tabs": [{"tab_id": "t1", "workspace_id": "w1"}],
                    "panes": [{"pane_id": "w1:p1", "tab_id": "t1", "workspace_id": "w1"}],
                    "layouts": [],
                    "agents": [{"name": "pisec-agent", "pane_id": "w1:p1", "tab_id": "t1", "workspace_id": "w1", "agent_status": self.agent_status}],
                }
            return {"type": "session_snapshot", "snapshot": snapshot}
        if method == "pane.process_info":
            return {
                "type": "pane_process_info",
                "process_info": {
                    "pane_id": params["pane_id"],
                    "shell_pid": 10,
                    "foreground_processes": [
                        {
                            "pid": 11,
                            "argv": ["/usr/bin/fence", "--settings", "/tmp/policy", "--", "/usr/bin/omp"],
                        }
                    ],
                },
            }
        if method == "workspace.create":
            self.created = True
            return {"type": "workspace_created", "workspace": {"workspace_id": "w2"}, "tab": {"tab_id": "t2"}, "root_pane": {"pane_id": "w2:p1"}}
        if method == "tab.create":
            self.created = True
            return {"type": "tab_created", "workspace": {"workspace_id": params["workspace_id"]}, "tab": {"tab_id": "t3"}, "root_pane": {"pane_id": f"{params['workspace_id']}:p3"}}
        if method == "pane.send_input":
            return {"type": "pane_input_sent"}
        if method == "pane.focus":
            return {"type": "pane_focused"}
        if method == "pane.move":
            workspace_id = params["destination"]["workspace_id"]
            return {
                "type": "pane_move",
                "move_result": {
                    "changed": True,
                    "previous_pane_id": params["pane_id"],
                    "previous_tab_id": "t1",
                    "previous_workspace_id": "w1",
                    "pane": {"pane_id": f"{workspace_id}:p4", "tab_id": f"{workspace_id}:t4", "workspace_id": workspace_id, "cwd": "/tmp/work"},
                },
            }
        if method == "tab.close":
            return {"type": "tab_closed", "tab_id": params["tab_id"]}
        if method == "agent.prompt":
            return {"type": "agent_prompted", "agent": {}}
        if method == "pane.report_agent":
            if not self.official_authority or params["source"] == "herdr:omp":
                self.agent_status = params["state"]
            return {"type": "ok"}
        if method == "pane.release_agent":
            if params["source"] != "herdr:omp":
                self.agent_status = "unknown"
            return {"type": "ok"}
        return {"type": "ok"}


class FakeServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class HerdrTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "herdr.sock"
        self.state = FakeHerdrState()
        state = self.state

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                request = json.loads(self.rfile.readline())
                response = {"id": request["id"], "result": state.result(request["method"], request["params"])}
                self.wfile.write((json.dumps(response) + "\n").encode())

        self.server = FakeServer(str(self.path), Handler)
        os.chmod(self.path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.adapter = HerdrWorkspaceAdapter(self.path)
        self.harness = HarnessManifest("omp", "omp", "17.3.4")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def test_create_observe_run_prompt_focus_and_close(self):
        created = self.adapter.create_workspace(cwd="/tmp/repo", label="Project: repo", focus=False)
        self.assertEqual(created.workspace_id, "w2")
        tab = self.adapter.create_tab(workspace_id="w2", cwd="/tmp/work", label="Task: Work", focus=False)
        self.assertEqual(tab.view_id, "t3")
        moved = self.adapter.move_surface_to_tab(surface_id="w1:p1", workspace_id="w2", label="Project: repo")
        self.assertEqual((moved.workspace_id, moved.view_id, moved.surface_id), ("w2", "w2:t4", "w2:p4"))
        self.adapter.rename_tab("t2", "Project chat")
        self.adapter.run_command(
            "w2:p1",
            ["/tmp/launcher with spaces", "--resume=abc", "quoted value"],
            {"HERDR_SESSION": "main", "HERDR_PANE_ID": "w2:p1"},
        )
        self.adapter.prompt_agent("w2:p1", "full brief", ("working", "blocked", "idle"), 30000)
        self.adapter.focus_pane("w2:p1")
        self.adapter.close_tab("t3")
        methods = [item[0] for item in self.state.requests]
        self.assertIn("workspace.create", methods)
        self.assertIn("tab.create", methods)
        self.assertIn("tab.rename", methods)
        self.assertIn("pane.send_input", methods)
        self.assertNotIn("worktree.create", methods)
        self.assertNotIn("agent.start", methods)
        self.assertIn("pane.focus", methods)
        self.assertIn("pane.move", methods)
        self.assertIn("tab.close", methods)
        command = next(params for method, params in self.state.requests if method == "pane.send_input")
        self.assertEqual(command["text"], "HERDR_SESSION=main HERDR_PANE_ID=w2:p1 '/tmp/launcher with spaces' --resume=abc 'quoted value'")
        self.assertEqual(command["keys"], ["Enter"])
        prompt = next(params for method, params in self.state.requests if method == "agent.prompt")
        self.assertEqual(prompt["text"], "full brief")
        self.assertEqual(prompt["wait"]["until"], ["working", "blocked", "idle"])

    def test_observe_tab_requires_exact_workspace_and_cwd(self):
        self.adapter.snapshot = lambda: {
            "version": "0.8.0",
            "protocol": 19,
            "workspaces": [{"workspace_id": "w2"}],
            "tabs": [{"tab_id": "t2", "workspace_id": "w2"}, {"tab_id": "t3", "workspace_id": "w2"}],
            "panes": [
                {"pane_id": "w2:p1", "tab_id": "t2", "workspace_id": "w2", "cwd": "/tmp/repo"},
                {"pane_id": "w2:p3", "tab_id": "t3", "workspace_id": "w2", "cwd": "/tmp/work"},
            ],
            "layouts": [],
            "agents": [],
        }
        observed = self.adapter.observe_tab(workspace_id="w2", cwd="/tmp/work")
        self.assertEqual(observed.view_id, "t3")
        self.assertEqual(observed.surface_id, "w2:p3")
        self.assertEqual(observed.worktree_path, "/tmp/work")
    def test_close_workspace_is_idempotent_when_already_missing(self):
        with patch.object(self.adapter, "_request", side_effect=PisecError("workspace w2 not found")):
            result = self.adapter.close_workspace("w2")
        self.assertEqual(result, {"type": "workspace_closed", "workspace_id": "w2", "already_closed": True})

    def test_primary_workspace_observation_uses_pane_cwd_before_agent_start(self):
        self.adapter.snapshot = lambda: {
            "version": "0.8.0",
            "protocol": 19,
            "workspaces": [{"workspace_id": "w2"}],
            "tabs": [{"tab_id": "t2", "workspace_id": "w2"}],
            "panes": [{"pane_id": "w2:p1", "tab_id": "t2", "workspace_id": "w2", "cwd": "/tmp/repo"}],
            "layouts": [],
            "agents": [],
        }
        observed = self.adapter.observe_workstream(path="/tmp/repo", agent_name="pisec-secretary")
        self.assertEqual(observed.workspace_id, "w2")
        self.assertEqual(observed.view_id, "t2")
        self.assertEqual(observed.surface_id, "w2:p1")
        self.assertIsNone(observed.worktree_path)
        self.assertIsNone(observed.agent)

    def test_stock_snapshot_agent_kind_is_correlated_by_pane(self):
        self.adapter.snapshot = lambda: {
            "version": "0.8.0",
            "protocol": 19,
            "workspaces": [{"workspace_id": "w2"}],
            "tabs": [{"tab_id": "t2", "workspace_id": "w2"}],
            "panes": [{"pane_id": "w2:p1", "tab_id": "t2", "workspace_id": "w2", "cwd": "/tmp/repo"}],
            "layouts": [],
            "agents": [{"agent": "omp", "agent_status": "idle", "workspace_id": "w2", "tab_id": "t2", "pane_id": "w2:p1"}],
        }
        observed = self.adapter.observe_workstream(path="/tmp/repo", agent_name="pisec-secretary")
        self.assertEqual(observed.agent.name, "omp")
        self.assertEqual(observed.agent.surface_id, "w2:p1")
        self.assertTrue(observed.agent.interactive_ready)

    def test_runtime_reports_use_only_the_pisec_source(self):
        self.adapter.report_state("w1:p1", "blocked", "approval", 7, "instance-1", self.harness)
        self.adapter.report_state("w1:p1", "working", None, 8, "instance-1", self.harness)
        self.adapter.report_session("w1:p1", ("path", "/sessions/one.jsonl"), 9, "resume", "instance-1", self.harness)
        self.adapter.release_agent("w1:p1", 10, "instance-1", self.harness)
        reports = [(method, params) for method, params in self.state.requests if method.startswith("pane.")]
        state_reports = [params for method, params in reports if method == "pane.report_agent"]
        session_report = next(params for method, params in reports if method == "pane.report_agent_session")
        releases = [params for method, params in reports if method == "pane.release_agent"]
        source = state_reports[-1]["source"]
        self.assertEqual(session_report["source"], source)
        self.assertEqual(releases[-1]["source"], source)
        self.assertRegex(source, r"^pisec:omp:[0-9a-f]{32}$")
        self.assertEqual([item["source"] for item in releases], [source])
        self.assertEqual([item["seq"] for item in state_reports], [7, 8])
        self.assertEqual(session_report["session_start_source"], "resume")
        with self.assertRaises(ValueError):
            self.adapter.report_state("w1:p1", "done", None, 11, "instance-1", self.harness)
        self.assertEqual(len([params for method, params in self.state.requests if method == "pane.release_agent"]), 1)

    def test_official_release_returns_ok_without_mutating_real_protocol_semantics(self):
        self.state.created = True
        self.state.agent_status = "working"
        result = self.adapter._request("pane.release_agent", {"pane_id": "w1:p1", "source": "herdr:omp", "agent": "omp", "seq": 100})
        self.assertEqual(result, {"type": "ok"})
        self.assertEqual(self.adapter.snapshot()["agents"][0]["agent_status"], "working")
        self.adapter.report_state("w1:p1", "blocked", None, 1, "instance-1", self.harness)
        self.assertEqual(self.adapter.snapshot()["agents"][0]["agent_status"], "working")

    def test_socket_permissions_are_enforced(self):
        os.chmod(self.path, 0o660)
        with self.assertRaises(Exception):
            self.adapter.snapshot()

    def test_idle_runtime_stop_uses_pane_preserving_eof(self):
        self.assertEqual(self.adapter.stop_runtime("w1:p1"), {"type": "ok"})
        method, params = self.state.requests[-1]
        self.assertEqual(method, "pane.send_keys")
        self.assertEqual(params, {"pane_id": "w1:p1", "keys": ["ctrl+d"]})

    def test_runtime_liveness_uses_process_identity_not_agent_metadata(self):
        live = self.adapter.observe_runtime("w1:p1", "/tmp/policy")
        self.assertEqual(live.state, "live")
        with patch.object(
            self.adapter,
            "_request",
            return_value={
                "type": "pane_process_info",
                "process_info": {
                    "pane_id": "w1:p1",
                    "shell_pid": 10,
                    "foreground_processes": [{"pid": 10, "argv": ["/bin/bash"]}],
                },
            },
        ):
            stopped = self.adapter.observe_runtime("w1:p1", "/tmp/policy")
        self.assertEqual(stopped.state, "stopped")
        with patch.object(
            self.adapter,
            "_request",
            return_value={
                "type": "pane_process_info",
                "process_info": {
                    "pane_id": "w1:p1",
                    "shell_pid": 10,
                    "foreground_processes": [{"pid": 12, "argv": ["/usr/bin/vim"]}],
                },
            },
        ):
            unknown = self.adapter.observe_runtime("w1:p1", "/tmp/policy")
        self.assertEqual(unknown.state, "unknown")
        with patch.object(
            self.adapter,
            "_request",
            return_value={
                "type": "pane_process_info",
                "process_info": {
                    "pane_id": "w1:p1",
                    "shell_pid": 10,
                    "foreground_processes": [{"pid": 12, "argv": None}],
                },
            },
        ):
            transient = self.adapter.observe_runtime("w1:p1", "/tmp/policy")
        self.assertEqual(transient.state, "unknown")

    def test_reconcile_counts_updates_and_marks_missing_runtime(self):
        class Store:
            def __init__(self):
                self.conn = sqlite3.connect(":memory:")
                self.conn.row_factory = sqlite3.Row
                self.conn.executescript(
                    "CREATE TABLE workstreams (workstream_id TEXT PRIMARY KEY, kind TEXT, worktree_path TEXT, desired_state TEXT, provisioning_state TEXT, attention_reason TEXT, updated_at TEXT);"
                    "CREATE TABLE runtime_bindings (workstream_id TEXT PRIMARY KEY, agent_name TEXT, workspace_id TEXT, workspace_view_id TEXT, workspace_surface_id TEXT, policy_path TEXT, runtime_instance_id TEXT, workspace_report_seq INTEGER, observed_state TEXT, last_observed_at TEXT, updated_at TEXT);"
                    "CREATE TABLE operations (operation_id TEXT, workstream_id TEXT, state TEXT, created_at TEXT);"
                )
                self.conn.execute("INSERT INTO workstreams(workstream_id,kind,worktree_path,desired_state,provisioning_state) VALUES ('ws-1','worker','/tmp/work','active','bound')")
                self.conn.execute("INSERT INTO runtime_bindings VALUES ('ws-1','pisec-agent','w1','t1','w1:p1','/tmp/policy','instance-1',4,'idle',NULL,'now')")

            @contextmanager
            def transaction(self):
                try:
                    yield self.conn
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise

        store = Store()
        self.state.created = True
        result = self.adapter.reconcile(store, event={"kind": "runtime"})
        self.assertEqual(result, {"reconciled": True, "updated": 1, "missing": 0, "eventAccepted": True})
        self.assertEqual(store.conn.execute("SELECT observed_state FROM runtime_bindings").fetchone()[0], "idle")
        claims = [params for method, params in self.state.requests if method == "pane.release_agent" and params["source"] == "herdr:omp"]
        self.assertEqual(claims, [])
        self.state.created = False
        result = self.adapter.reconcile(store)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["missing"], 1)
        self.assertEqual(store.conn.execute("SELECT provisioning_state FROM workstreams").fetchone()[0], "needs_attention")
    def test_reconcile_uses_pane_cwd_before_shared_workspace_worktree(self):
        class Store:
            def __init__(self):
                self.conn = sqlite3.connect(":memory:")
                self.conn.row_factory = sqlite3.Row
                self.conn.executescript(
                    "CREATE TABLE workstreams (workstream_id TEXT PRIMARY KEY, kind TEXT, worktree_path TEXT, desired_state TEXT, provisioning_state TEXT, attention_reason TEXT, updated_at TEXT);"
                    "CREATE TABLE runtime_bindings (workstream_id TEXT PRIMARY KEY, agent_name TEXT, workspace_id TEXT, workspace_view_id TEXT, workspace_surface_id TEXT, policy_path TEXT, runtime_instance_id TEXT, workspace_report_seq INTEGER, observed_state TEXT, last_observed_at TEXT, updated_at TEXT);"
                    "CREATE TABLE operations (operation_id TEXT, workstream_id TEXT, state TEXT, created_at TEXT);"
                )
                self.conn.execute("INSERT INTO workstreams(workstream_id,kind,worktree_path,desired_state,provisioning_state) VALUES ('ws-1','worker','/tmp/work','active','bound')")
                self.conn.execute("INSERT INTO runtime_bindings VALUES ('ws-1','pisec-agent','w1','t1','w1:p1','/tmp/policy','instance-1',4,'idle',NULL,'now')")

            @contextmanager
            def transaction(self):
                try:
                    yield self.conn
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise

        self.adapter.snapshot = lambda: {
            "version": "0.8.0",
            "protocol": 19,
            "workspaces": [{"workspace_id": "w1", "worktree": {"checkout_path": "/tmp/first-mate", "branch": "main"}}],
            "tabs": [{"tab_id": "t1", "workspace_id": "w1"}],
            "panes": [{"pane_id": "w1:p1", "tab_id": "t1", "workspace_id": "w1", "cwd": "/tmp/work"}],
            "layouts": [],
            "agents": [{"agent": "omp", "agent_status": "working", "workspace_id": "w1", "tab_id": "t1", "pane_id": "w1:p1"}],
        }
        store = Store()
        result = self.adapter.reconcile(store)
        self.assertEqual(result, {"reconciled": True, "updated": 1, "missing": 0, "eventAccepted": False})
        self.assertEqual(store.conn.execute("SELECT observed_state FROM runtime_bindings").fetchone()[0], "idle")


if __name__ == "__main__":
    unittest.main()
