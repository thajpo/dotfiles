import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = (ROOT / "pi/extensions/auto-continue/index.ts").resolve()


class PiCompactionTests(unittest.TestCase):
    def test_compaction_queues_hidden_continuation(self):
        script = f"""
import register from {json.dumps(EXTENSION.as_uri())};
const handlers = new Map();
const sent = [];
const pi = {{
  on(name, handler) {{ handlers.set(name, handler); }},
  sendMessage(message, options) {{ sent.push({{ message, options }}); }},
}};
register(pi);
handlers.get("session_compact")({{ reason: "threshold", willRetry: false }});
await new Promise((resolve) => setTimeout(resolve, 10));
process.stdout.write(JSON.stringify(sent));
"""
        result = subprocess.run(
            ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = json.loads(result.stdout)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["message"]["customType"], "pi-auto-continue")
        self.assertFalse(sent[0]["message"]["display"])
        self.assertTrue(sent[0]["options"]["triggerTurn"])
        self.assertEqual(sent[0]["options"]["deliverAs"], "followUp")

    def test_resume_after_saved_compaction_also_restarts_task(self):
        script = f"""
import register from {json.dumps(EXTENSION.as_uri())};
const handlers = new Map();
const sent = [];
const pi = {{
  on(name, handler) {{ handlers.set(name, handler); }},
  sendMessage(message, options) {{ sent.push({{ message, options }}); }},
}};
register(pi);
handlers.get("session_start")({{}}, {{ sessionManager: {{ getBranch: () => [{{ type: "compaction" }}] }} }});
await new Promise((resolve) => setTimeout(resolve, 10));
process.stdout.write(JSON.stringify(sent));
"""
        result = subprocess.run(
            ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = json.loads(result.stdout)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["message"]["details"]["reason"], "resume")

    def test_overflow_retry_is_not_duplicated(self):
        script = f"""
import register from {json.dumps(EXTENSION.as_uri())};
const handlers = new Map();
const sent = [];
const pi = {{
  on(name, handler) {{ handlers.set(name, handler); }},
  sendMessage(message, options) {{ sent.push({{ message, options }}); }},
}};
register(pi);
handlers.get("session_compact")({{ reason: "overflow", willRetry: true }});
await new Promise((resolve) => setTimeout(resolve, 10));
process.stdout.write(JSON.stringify(sent));
"""
        result = subprocess.run(
            ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])


if __name__ == "__main__":
    unittest.main()
