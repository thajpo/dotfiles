import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
STATS_SCRIPT = ROOT / "scripts/pi-secretary-stats.py"
STATS_MODULE = (ROOT / "pi/extensions/secretary-subagents/stats.ts").resolve()


class SecretaryStatsTests(unittest.TestCase):
    def test_summary_groups_session_runs_and_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = Path(tmp) / "stats.jsonl"
            stats.write_text("\n".join([
                json.dumps({
                    "kind": "session", "durationMs": 100, "turns": 2,
                    "tokens": {"totalTokens": 10}, "projectAlias": "demo",
                }),
                json.dumps({
                    "kind": "subagent_run", "durationMs": 300, "turns": 3,
                    "toolCalls": 4, "tokens": {"totalTokens": 20}, "projectAlias": "demo",
                    "failure": {"kind": "acceptance"},
                    "steps": [{
                        "agent": "scout", "model": "model-a", "durationMs": 300,
                        "turns": 3, "toolCalls": 4, "acceptanceLevel": "checked", "tokens": {"totalTokens": 20},
                    }],
                }),
            ]) + "\n")
            result = subprocess.run(
                ["python3", str(STATS_SCRIPT), "--file", str(stats), "--json"],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(value["sessions"]["totalTokens"], 10)
            self.assertEqual(value["subagentRuns"]["totalTokens"], 20)
            self.assertEqual(value["stepsByAgent"]["scout"]["totalTokens"], 20)
            self.assertEqual(value["stepsByModel"]["model-a"]["durationMs"], 300)
            self.assertEqual(value["failures"], {"total": 1, "byKind": {"acceptance": 1}, "stepsByAcceptanceLevel": {"checked": 1}})

    def test_typescript_writer_records_failure_and_acceptance_diagnostics_without_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PI_CODING_AGENT_DIR"] = str(Path(tmp) / "agent")
            script = f"""
import {{ recordSecretarySubagentStats }} from {json.dumps(STATS_MODULE.as_uri())};
const statusPath = {json.dumps(str(Path(tmp) / "run" / "status.json"))};
const fs = await import("node:fs");
fs.mkdirSync({json.dumps(str(Path(tmp) / "run"))}, {{ recursive: true }});
fs.writeFileSync(statusPath, JSON.stringify({{
  runId: "run-failed", sessionId: "session-1", mode: "parallel", state: "failed",
  startedAt: 100, endedAt: 250, error: "Acceptance rejected: required criterion failed",
  timedOut: false, steps: [{{
    agent: "scout", status: "failed", error: "Acceptance rejected: required criterion failed",
    acceptance: {{ status: "rejected", effectiveAcceptance: {{ level: "checked" }}, runtimeChecks: [{{ status: "failed", message: "criterion-1 was not satisfied" }}] }},
    tokens: {{ input: 2, output: 3, total: 5 }},
  }}], totalTokens: {{ input: 2, output: 3, total: 5 }}
}}));
recordSecretarySubagentStats({{
  projectAlias: "demo",
  data: {{ id: "run-failed", sessionId: "session-1", mode: "parallel", success: false,
    timestamp: 250, durationMs: 150, asyncDir: {json.dumps(str(Path(tmp) / "run"))},
    cwd: "/secret/path", summary: "do not persist this prompt/output" }},
}});
"""
            result = subprocess.run(
                ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
                cwd=ROOT, env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            line = (Path(env["PI_CODING_AGENT_DIR"]) / "secretary-stats.jsonl").read_text()
            value = json.loads(line)
            self.assertEqual(value["state"], "failed")
            self.assertEqual(value["failure"]["kind"], "acceptance")
            self.assertIn("criterion-1 was not satisfied", value["failure"]["acceptanceFailedChecks"])
            self.assertEqual(value["steps"][0]["acceptanceLevel"], "checked")
            self.assertNotIn("do not persist", line)
            self.assertNotIn("/secret/path", line)

    def test_typescript_writer_omits_prompt_and_path_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PI_CODING_AGENT_DIR"] = str(Path(tmp) / "agent")
            script = f"""
import {{ recordSecretarySubagentStats }} from {json.dumps(STATS_MODULE.as_uri())};
recordSecretarySubagentStats({{
  projectAlias: "demo",
  data: {{
    id: "run-1", sessionId: "session-1", mode: "single", success: true,
    timestamp: 200, durationMs: 100, summary: "do not persist this",
    cwd: "/secret/path", results: [], totalTokens: {{ input: 2, output: 3, total: 5 }},
  }},
}});
"""
            result = subprocess.run(
                ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
                cwd=ROOT, env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            line = (Path(env["PI_CODING_AGENT_DIR"]) / "secretary-stats.jsonl").read_text()
            self.assertIn('"totalTokens":5', line)
            self.assertNotIn("do not persist this", line)
            self.assertNotIn("/secret/path", line)


if __name__ == "__main__":
    unittest.main()
