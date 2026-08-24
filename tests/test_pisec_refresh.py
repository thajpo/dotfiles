from pathlib import Path
import tempfile
import unittest

from scripts.pisec.adapters import AdapterRegistry
from scripts.pisec.broker import BrokerDispatcher
from scripts.pisec.pi_store import PiStore
from scripts.pisec.refresh import _binding_scope
from tests.pisec_fixture import FixtureGitObjects, FixtureHarness, FixtureWorkspace, make_repo


class RuntimeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        make_repo(self.repo)
        self.harness = FixtureHarness(self.root)
        self.workspace = FixtureWorkspace(self.root)
        registry = AdapterRegistry()
        registry.register_harness(self.harness)
        registry.register_workspace(self.workspace)
        self.dispatcher = BrokerDispatcher(
            lambda: PiStore(self.root / "state"),
            registry=registry,
            harness=self.harness,
            workspace=self.workspace,
            git_objects=FixtureGitObjects(),
        )
        project = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "defaultRef": "main"})
        self.project_id = project["project_id"]
        opened = self.dispatcher.dispatch("admin", "project.open", {"project": self.project_id})
        self.workstream_id = opened["workstream"]["workstream_id"]
        with PiStore(self.root / "state") as store:
            binding = dict(store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone())
            self.secretary_token = Path(binding["launch_secret_path"]).read_text().strip()
            session = Path(binding["harness_home"]) / "sessions" / "retained.jsonl"
            session.write_text("retained session\n")
            session.chmod(0o600)
            store.conn.execute(
                "UPDATE runtime_bindings SET native_session_kind='path',native_session_value=?,applied_generation_sha256=NULL,observed_state='idle' WHERE workstream_id=?",
                (str(session), self.workstream_id),
            )
            self.identity = (binding["workspace_id"], binding["workspace_view_id"], binding["workspace_surface_id"], str(session))

    def tearDown(self):
        self.dispatcher.stop_background()
        self.temp.cleanup()

    def test_refresh_preserves_identity_and_session_then_is_idempotent(self):
        first = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})
        self.assertTrue(first["ok"])
        self.assertEqual(len(first["upgraded"]), 1)
        upgraded = first["upgraded"][0]
        self.assertEqual((upgraded["workspaceId"], upgraded["viewId"], upgraded["surfaceId"], upgraded["nativeSessionValue"]), self.identity)
        self.assertEqual(Path(self.identity[3]).read_text(), "retained session\n")
        stop_count = len([call for call in self.workspace.calls if call[0] == "stop"])
        second = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})
        self.assertEqual(second["upgraded"], [])
        self.assertEqual(len(second["skipped"]), 1)
        self.assertEqual(len([call for call in self.workspace.calls if call[0] == "stop"]), stop_count)

    def test_secretary_refresh_is_limited_to_its_project(self):
        other_repo = self.root / "other-repo"
        make_repo(other_repo)
        other_project = self.dispatcher.dispatch("admin", "project.register", {"path": str(other_repo), "defaultRef": "main"})
        other_opened = self.dispatcher.dispatch("admin", "project.open", {"project": other_project["project_id"]})
        other_workstream_id = other_opened["workstream"]["workstream_id"]
        with PiStore(self.root / "state") as store:
            store.conn.execute(
                "UPDATE runtime_bindings SET applied_generation_sha256=?,observed_state='idle' WHERE workstream_id=?",
                ("a" * 64, other_workstream_id),
            )
        result = self.dispatcher.dispatch(
            "secretary",
            "project.refresh",
            {"authToken": self.secretary_token, "waitSeconds": 0},
        )
        self.assertTrue(result["ok"])
        self.assertEqual([item["workstreamId"] for item in result["upgraded"]], [self.workstream_id])
        with PiStore(self.root / "state") as store:
            other_binding = store.conn.execute(
                "SELECT applied_generation_sha256 FROM runtime_bindings WHERE workstream_id=?",
                (other_workstream_id,),
            ).fetchone()
            self.assertEqual(other_binding["applied_generation_sha256"], "a" * 64)

    def test_refresh_recovers_stopped_stale_needs_attention_binding(self):
        with PiStore(self.root / "state") as store:
            row = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            store.conn.execute(
                "UPDATE runtime_bindings SET observed_state='stopped',applied_generation_sha256=? WHERE workstream_id=?",
                ("a" * 64, self.workstream_id),
            )
            store.conn.execute(
                "UPDATE workstreams SET provisioning_state='needs_attention',attention_reason='workspace runtime is missing' WHERE workstream_id=?",
                (self.workstream_id,),
            )
            self.assertNotEqual("a" * 64, row["desired_generation_sha256"])
        self.workspace.stop_runtime(self.identity[2])
        first = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})
        self.assertTrue(first["ok"])
        self.assertEqual(len(first["upgraded"]), 1)
        upgraded = first["upgraded"][0]
        self.assertEqual(
            (upgraded["workspaceId"], upgraded["viewId"], upgraded["surfaceId"], upgraded["nativeSessionValue"]),
            self.identity,
        )
        with PiStore(self.root / "state") as store:
            binding = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            workstream = store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (self.workstream_id,)).fetchone()
            self.assertEqual(binding["applied_generation_sha256"], binding["desired_generation_sha256"])
            self.assertEqual(workstream["provisioning_state"], "bound")

    def test_binding_scope_injects_project_data_dirs(self):
        data = self.repo / "data"
        data.mkdir()
        self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "dataDirs": [str(data)]})
        with PiStore(self.root / "state") as store:
            binding = store.conn.execute("SELECT r.*,w.kind,w.project_id FROM runtime_bindings r JOIN workstreams w USING(workstream_id) WHERE r.workstream_id=?", (self.workstream_id,)).fetchone()
            scope = _binding_scope(store, binding)
            self.assertIn(str(data.resolve()), scope["dataDirs"])

    def test_working_runtime_is_pending_and_not_interrupted(self):
        with PiStore(self.root / "state") as store:
            store.conn.execute("UPDATE runtime_bindings SET observed_state='working' WHERE workstream_id=?", (self.workstream_id,))
        result = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})
        self.assertEqual(result["upgraded"], [])
        self.assertEqual(result["pending"][0]["state"], "working")
        self.assertFalse(any(call[0] == "stop" for call in self.workspace.calls))


if __name__ == "__main__":
    unittest.main()
