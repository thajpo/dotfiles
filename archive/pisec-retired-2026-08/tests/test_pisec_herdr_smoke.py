import os
import time
import unittest

from scripts.pisec.adapters import HarnessManifest
from scripts.pisec.workspaces.herdr import HerdrWorkspaceAdapter


@unittest.skipUnless(os.environ.get("PISEC_REAL_HERDR_SMOKE") == "1", "set PISEC_REAL_HERDR_SMOKE=1 for installed Herdr protocol smoke")
class RealHerdrLifecycleSmoke(unittest.TestCase):
    def test_real_protocol_projects_pisec_states_and_ignores_official_release(self):
        pane = os.environ["PISEC_REAL_HERDR_PANE"]
        adapter = HerdrWorkspaceAdapter()
        harness = HarnessManifest("omp", "omp", "17.3.4")
        instance = f"real-herdr-smoke-{time.time_ns()}"

        def state() -> str:
            snapshot = adapter.snapshot()
            agent = next(item for item in snapshot["agents"] if item.get("pane_id") == pane)
            return str(agent["agent_status"])

        adapter.report_state(pane, "working", None, 1, instance, harness)
        self.assertEqual(state(), "working")
        release = adapter._request("pane.release_agent", {"pane_id": pane, "source": "herdr:omp", "agent": "omp", "seq": time.time_ns() // 1_000})
        self.assertEqual(release, {"type": "ok"})
        self.assertEqual(state(), "working")
        adapter.report_state(pane, "blocked", "smoke", 2, instance, harness)
        self.assertEqual(state(), "blocked")
        adapter.report_state(pane, "idle", None, 3, instance, harness)
        self.assertEqual(state(), "idle")


if __name__ == "__main__":
    unittest.main()
