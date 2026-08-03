import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = ROOT / "pi/npm/node_modules/pi-subagents/src/runs/shared/acceptance.ts"
JITI = ROOT / "pi/npm/node_modules/jiti/lib/jiti.mjs"


class AcceptanceTests(unittest.TestCase):
    def test_not_applicable_is_allowed_at_attested_but_rejected_at_checked(self):
        script = f"""
import {{ createJiti }} from {json.dumps(JITI.as_uri())};
const jiti = createJiti(import.meta.url, {{ moduleCache: false }});
const {{ resolveEffectiveAcceptance, evaluateAcceptance }} = await jiti.import({json.dumps(ACCEPTANCE.as_uri())});
const report = (acceptance) => '```acceptance-report\\n' + JSON.stringify({{
  criteriaSatisfied: acceptance.criteria.map((criterion) => ({{ id: criterion.id, status: 'not-applicable', evidence: 'not applicable' }})),
  reviewFindings: ['not applicable'], residualRisks: ['none'],
}}) + '\\n```';
const base = {{ agentName: 'scout', acceptanceRole: 'read-only', task: 'Inspect and report findings without edits.' }};
const attested = resolveEffectiveAcceptance({{ ...base, explicit: 'attested' }});
const checked = resolveEffectiveAcceptance({{ ...base, explicit: 'checked' }});
const attestedResult = await evaluateAcceptance({{ acceptance: attested, output: report(attested), cwd: process.cwd() }});
const checkedResult = await evaluateAcceptance({{ acceptance: checked, output: report(checked), cwd: process.cwd() }});
process.stdout.write(JSON.stringify({{
  attested: {{ level: attested.level, status: attestedResult.status, checks: attestedResult.runtimeChecks }},
  checked: {{ level: checked.level, status: checkedResult.status, checks: checkedResult.runtimeChecks }},
}}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["attested"]["level"], "attested")
        self.assertEqual(value["attested"]["status"], "attested")
        self.assertEqual(value["attested"]["checks"], [])
        self.assertEqual(value["checked"]["level"], "checked")
        self.assertEqual(value["checked"]["status"], "rejected")
        self.assertIn("not-applicable", value["checked"]["checks"][0]["message"])


if __name__ == "__main__":
    unittest.main()
