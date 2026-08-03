import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE = (ROOT / "pi/extensions/secretary-subagents/lifecycle.ts").resolve()


class SecretaryLifecycleTests(unittest.TestCase):
    def test_lifecycle_authorization_is_bound_to_current_jobs(self):
        script = f"""
import {{ actionAuthorization, actionTargetIsAuthorized }} from {json.dumps(LIFECYCLE.as_uri())};
const local = new Map([
  ["01234567", {{ status: "running", agents: ["scout"] }}],
  ["89abcdef", {{ status: "running", agents: ["researcher"] }}],
]);
const one = new Map([["01234567", {{ status: "running", agents: ["scout"] }}]]);
const collision = new Map([
  ["01234567aa", {{ status: "running", agents: ["scout"] }}],
  ["01234567bb", {{ status: "running", agents: ["scout"] }}],
]);
const results = [
  actionTargetIsAuthorized("stop", {{ id: "01234567" }}, actionAuthorization("stop run 01234567"), {{ asyncJobs: one }}),
  actionTargetIsAuthorized("stop", {{ id: "fedcba98" }}, actionAuthorization("stop run fedcba98"), {{ asyncJobs: one }}),
  actionTargetIsAuthorized("stop", {{ id: "01234567" }}, actionAuthorization("stop run 01234567"), {{ asyncJobs: local }}),
  actionTargetIsAuthorized("stop", {{ id: "01234567" }}, actionAuthorization("stop the scout"), {{ asyncJobs: local }}),
  actionTargetIsAuthorized("stop", {{}}, actionAuthorization("stop the investigation"), {{ asyncJobs: one }}),
  actionTargetIsAuthorized("stop", {{}}, actionAuthorization("stop the investigation"), {{ asyncJobs: local }}),
  actionTargetIsAuthorized("stop", {{ id: "01234567aa" }}, actionAuthorization("stop run 01234567"), {{ asyncJobs: collision }}),
];
process.stdout.write(JSON.stringify(results));
"""
        result = subprocess.run(
            ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [True, False, True, True, True, False, False])


if __name__ == "__main__":
    unittest.main()
