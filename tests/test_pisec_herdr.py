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
                    "agents": [{"name": "pisec-agent", "pane_id": "w1:p1", "tab_id": "t1", "workspace_id": "w1", "agent_status": "working"}],
                }
            return {"type": "session_snapshot", "snapshot": snapshot}
        if method == "worktree.create":
            self.created = True
            return {"type": "worktree_created", "workspace": {"workspace_id": "w1"}, "tab": {"tab_id": "t1"}, "root_pane": {"pane_id": "w1:p1"}, "worktree": {"path": params["path"], "branch": params["branch"]}}
        if method == "workspace.create":
            return {"type": "workspace_created", "workspace": {"workspace_id": "w2"}, "tab": {"tab_id": "t2"}, "root_pane": {"pane_id": "w2:p1"}}
        if method == "agent.start":
            return {"type": "agent_started", "agent": {}, "argv": ["omp"]}
        if method == "agent.prompt":
            return {"type": "agent_prompted", "agent": {}}
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

    def test_create_observe_start_prompt_and_focus(self):
        created = self.adapter.create_worktree(cwd="/tmp/repo", branch="pisec/ws/work", base="a" * 40, path="/tmp/work", label="Work", focus=False)
        self.assertEqual(created.surface_id, "w1:p1")
        observed = self.adapter.observe_workstream(path="/tmp/work", agent_name="pisec-agent")
        self.assertEqual(observed.workspace_id, "w1")
        self.adapter.start_agent("w1:p1", "pisec-agent", "omp")
        self.adapter.prompt_agent("w1:p1", "full brief", ("working", "blocked", "idle"), 30000)
        self.adapter.focus_agent("w1:p1")
        methods = [item[0] for item in self.state.requests]
        self.assertIn("worktree.create", methods)
        self.assertIn("agent.start", methods)
        prompt = next(params for method, params in self.state.requests if method == "agent.prompt")
        self.assertEqual(prompt["text"], "full brief")
        self.assertEqual(prompt["wait"]["until"], ["working", "blocked", "idle"])

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

    def test_runtime_reports_use_protocol_sources_and_sequences(self):
        self.adapter.report_state("w1:p1", "blocked", "approval", 7, "instance-1", self.harness)
        self.adapter.report_session("w1:p1", ("path", "/sessions/one.jsonl"), 8, "resume", "instance-1", self.harness)
        self.adapter.release_agent("w1:p1", 9, "instance-1", self.harness)
        reports = {method: params for method, params in self.state.requests if method.startswith("pane.")}
        self.assertEqual(reports["pane.report_agent_session"]["source"], "herdr:omp")
        self.assertEqual(reports["pane.report_agent"]["source"], reports["pane.release_agent"]["source"])
        self.assertRegex(reports["pane.report_agent"]["source"], r"^pisec:omp:[0-9a-f]{32}$")
        self.assertEqual(reports["pane.report_agent"]["seq"], 7)
        self.assertEqual(reports["pane.report_agent_session"]["session_start_source"], "resume")
        with self.assertRaises(ValueError):
            self.adapter.report_state("w1:p1", "done", None, 10, "instance-1", self.harness)

    def test_socket_permissions_are_enforced(self):
        os.chmod(self.path, 0o660)
        with self.assertRaises(Exception):
            self.adapter.snapshot()

    def test_reconcile_counts_updates_and_marks_missing_runtime(self):
        class Store:
            def __init__(self):
                self.conn = sqlite3.connect(":memory:")
                self.conn.row_factory = sqlite3.Row
                self.conn.executescript(
                    "CREATE TABLE workstreams (workstream_id TEXT PRIMARY KEY, kind TEXT, worktree_path TEXT, desired_state TEXT, provisioning_state TEXT, attention_reason TEXT, updated_at TEXT);"
                    "CREATE TABLE runtime_bindings (workstream_id TEXT PRIMARY KEY, agent_name TEXT, workspace_id TEXT, workspace_view_id TEXT, workspace_surface_id TEXT, observed_state TEXT, last_observed_at TEXT, updated_at TEXT);"
                    "CREATE TABLE operations (operation_id TEXT, workstream_id TEXT, state TEXT, created_at TEXT);"
                )
                self.conn.execute("INSERT INTO workstreams(workstream_id,kind,worktree_path,desired_state,provisioning_state) VALUES ('ws-1','worker','/tmp/work','active','bound')")
                self.conn.execute("INSERT INTO runtime_bindings VALUES ('ws-1','pisec-agent','w1','t1','w1:p1','unknown',NULL,'now')")

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
        self.assertEqual(store.conn.execute("SELECT observed_state FROM runtime_bindings").fetchone()[0], "working")
        self.state.created = False
        result = self.adapter.reconcile(store)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["missing"], 1)
        self.assertEqual(store.conn.execute("SELECT provisioning_state FROM workstreams").fetchone()[0], "needs_attention")


if __name__ == "__main__":
    unittest.main()
