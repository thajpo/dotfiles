import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE = (ROOT / "pi/extensions/secretary-subagents/lifecycle.ts").resolve()


class SecretaryLifecycleTests(unittest.TestCase):
    def test_lifecycle_authorization_is_bound_to_current_jobs(self):
        script = f"""
import {{ actionAuthorization, actionTargetIsAuthorized, actionWasAuthorized }} from {json.dumps(LIFECYCLE.as_uri())};
const local = new Map([
  ["01234567", {{ status: "running", agents: ["scout"] }}],
  ["89abcdef", {{ status: "running", agents: ["researcher"] }}],
]);
const one = new Map([["01234567", {{ status: "running", agents: ["scout"] }}]]);
const collision = new Map([
  ["01234567aa", {{ status: "running", agents: ["scout"] }}],
  ["01234567bb", {{ status: "running", agents: ["scout"] }}],
]);
const named = new Map([
  ["run-1", {{ status: "running", agents: ["worker"] }}],
  ["nested-run-id", {{ status: "running", agents: ["worker"] }}],
]);
const resumable = new Map([
  ["complete-run-abcdef", {{ status: "completed", agents: ["planner"] }}],
  ["live-run-abcdef", {{ status: "running", agents: ["reviewer"] }}],
]);
const results = [
  actionTargetIsAuthorized("stop", {{ id: "01234567" }}, actionAuthorization("stop run 01234567"), {{ asyncJobs: one }}),
  actionTargetIsAuthorized("stop", {{ id: "fedcba98" }}, actionAuthorization("stop run fedcba98"), {{ asyncJobs: one }}),
  actionTargetIsAuthorized("stop", {{ id: "01234567" }}, actionAuthorization("stop run 01234567"), {{ asyncJobs: local }}),
  actionTargetIsAuthorized("stop", {{ id: "01234567" }}, actionAuthorization("stop the scout"), {{ asyncJobs: local }}),
  actionTargetIsAuthorized("stop", {{}}, actionAuthorization("stop the investigation"), {{ asyncJobs: one }}),
  actionTargetIsAuthorized("stop", {{}}, actionAuthorization("stop the investigation"), {{ asyncJobs: local }}),
  actionTargetIsAuthorized("stop", {{ id: "01234567aa" }}, actionAuthorization("stop run 01234567"), {{ asyncJobs: collision }}),
  actionTargetIsAuthorized("stop", {{ id: "run-1" }}, actionAuthorization("stop run-1"), {{ asyncJobs: named }}),
  actionTargetIsAuthorized("stop", {{ id: "nested-run-id" }}, actionAuthorization("stop nested-run-id"), {{ asyncJobs: named }}),
  actionTargetIsAuthorized("stop", {{ id: "run-1" }}, actionAuthorization("stop unrelated 01234567"), {{ asyncJobs: named }}),
  actionTargetIsAuthorized("stop", {{ id: "01234567aa" }}, actionAuthorization("stop run 01234567"), {{ asyncJobs: new Map([["01234567aa", {{ status: "running", agents: ["scout"] }}]]) }}),
  actionTargetIsAuthorized("stop", {{ id: "01234567" }}, actionAuthorization("stop run 01234567"), {{ asyncJobs: new Map([["01234567aa", {{ status: "running", agents: ["scout"] }}]]) }}),
  actionTargetIsAuthorized("resume", {{ id: "complete-run" }}, actionAuthorization("resume complete-run"), {{ fleetJobs: resumable }}),
  actionTargetIsAuthorized("resume", {{ id: "live-run-abcdef" }}, actionAuthorization("resume live-run-abcdef"), {{ fleetJobs: resumable }}),
  actionTargetIsAuthorized("resume", {{ id: "complete-run-abcdef" }}, actionAuthorization("resume the planner"), {{ fleetJobs: resumable }}),
  actionWasAuthorized("stop", "stop"),
  actionWasAuthorized("stop the investigator", "stop"),
  actionWasAuthorized("stop showing verbose explanations", "stop"),
  actionWasAuthorized("stop it from being verbose", "stop"),
  actionWasAuthorized("continue with the review", "resume"),
  actionWasAuthorized("resume the agent", "resume"),
  actionWasAuthorized("steer", "steer"),
  actionWasAuthorized("steer the child toward the failing test", "steer"),
  actionAuthorization("stop run-1 and resume run-2", "stop").ids.join(",") === "run-1",
  actionAuthorization("stop run-1 and resume run-2", "resume").ids.join(",") === "run-2",
];
process.stdout.write(JSON.stringify(results));
"""
        result = subprocess.run(
            ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [
            True, False, True, True, True, False, False, True, True, False,
            True, True, True, False, True,
            True, True, False, False, False, True, False, True,
            True, True,
        ])


if __name__ == "__main__":
    unittest.main()
