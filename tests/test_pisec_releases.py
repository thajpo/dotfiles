from pathlib import Path
import hashlib
import sqlite3
import tempfile
import unittest

from scripts.pisec.adapters import AdapterRegistry, RuntimeReleaseArtifacts
from scripts.pisec.broker import BrokerDispatcher
from scripts.pisec.models import canonical_json
from scripts.pisec.pi_store import PiStore
from tests.pisec_fixture import FixtureGitObjects, FixtureHarness, FixtureWorkspace, make_repo


class MutableReleaseHarness(FixtureHarness):
    def __init__(self, root: Path):
        super().__init__(root)
        self.release_version = 1

    def build_runtime_release(self) -> RuntimeReleaseArtifacts:
        manifest = {"schemaVersion": 1, "adapter": self.manifest.adapter_id, "version": self.release_version}
        digest = hashlib.sha256(canonical_json(manifest).encode()).hexdigest()
        return RuntimeReleaseArtifacts(digest, manifest)


class RuntimeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        make_repo(self.repo)
        self.harness = MutableReleaseHarness(self.root)
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

    def tearDown(self):
        self.dispatcher.stop_background()
        self.temp.cleanup()

    def test_build_is_immutable_and_activation_is_explicit(self):
        first = self.dispatcher.dispatch("admin", "runtime.release.build", {})
        repeated = self.dispatcher.dispatch("admin", "runtime.release.build", {})
        self.assertEqual(repeated["release_id"], first["release_id"])
        self.assertTrue(repeated["reused"])
        listed = self.dispatcher.dispatch("admin", "runtime.release.list", {})
        self.assertIsNone(listed["currentReleaseId"])

        activated = self.dispatcher.dispatch("admin", "runtime.release.activate", {"releaseId": first["release_id"]})
        self.assertTrue(activated["activated"])
        with PiStore(self.root / "state") as store:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                store.conn.execute("UPDATE runtime_releases SET adapter_version='changed' WHERE release_id=?", (first["release_id"],))

    def test_new_source_snapshot_does_not_change_runtimes_until_activation(self):
        project = self.dispatcher.dispatch("admin", "project.register", {"path": str(self.repo), "defaultRef": "main"})
        opened = self.dispatcher.dispatch("admin", "project.open", {"project": project["project_id"]})
        workstream_id = opened["workstream"]["workstream_id"]
        with PiStore(self.root / "state") as store:
            original = store.conn.execute("SELECT applied_release_id FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()[0]

        self.harness.release_version = 2
        built = self.dispatcher.dispatch("admin", "runtime.release.build", {})
        self.assertNotEqual(built["release_id"], original)
        unchanged = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})
        self.assertEqual(unchanged["upgraded"], [])

        self.dispatcher.dispatch("admin", "runtime.release.activate", {"releaseId": built["release_id"]})
        upgraded = self.dispatcher.dispatch("admin", "project.refresh", {"all": True, "waitSeconds": 0})
        self.assertEqual([item["workstreamId"] for item in upgraded["upgraded"]], [workstream_id])
        with PiStore(self.root / "state") as store:
            binding = store.conn.execute("SELECT desired_release_id,applied_release_id FROM runtime_bindings WHERE workstream_id=?", (workstream_id,)).fetchone()
            self.assertEqual(tuple(binding), (built["release_id"], built["release_id"]))


if __name__ == "__main__":
    unittest.main()
