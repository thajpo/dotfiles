from pathlib import Path
import json
import os
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "omp" / "extensions" / "pisec.ts"
SECRETARY_TOOLS = [
    "pisec_project_activity",
    "pisec_report_secretary_issue",
    "pisec_project_status",
    "pisec_git_status",
    "pisec_push_branch",
    "pisec_inspect_workstream_changes",
    "pisec_prepare_workstream_merge",
    "pisec_merge_workstream",
    "pisec_list_workstreams",
    "pisec_inspect_workstream",
    "pisec_prepare_workstream",
    "pisec_create_workstream",
    "pisec_send_workstream",
    "pisec_focus_workstream",
    "pisec_complete_workstream",
    "pisec_retire_workstream",
    "pisec_list_decisions",
    "pisec_record_decision",
    "pisec_list_coordination_requests",
    "pisec_inspect_coordination_request",
    "pisec_answer_coordination_request",
    "pisec_resolve_decision",
    "pisec_list_worker_research_requests",
    "pisec_inspect_worker_research",
    "pisec_claim_worker_research",
    "pisec_request_worker_research_context",
    "pisec_answer_worker_research",
    "pisec_decline_worker_research",
]
WORKER_TOOLS = [
    "pisec_request_coordination",
    "pisec_list_coordination",
    "pisec_inspect_coordination",
    "pisec_acknowledge_coordination",
    "pisec_show_task_packet",
    "pisec_request_secretary_research",
    "pisec_check_secretary_research",
    "pisec_inspect_secretary_research",
    "pisec_add_secretary_research_context",
    "pisec_acknowledge_secretary_research",
]


class OmpExtensionTests(unittest.TestCase):
    def test_extension_is_harness_neutral_and_has_no_retired_transport(self):
        source = EXTENSION.read_text()
        self.assertNotIn("AoE", source)
        self.assertNotIn("agent-of-empires", source)
        self.assertNotIn("aoeRequest", source)
        self.assertNotIn("runAoe", source)
        for name in SECRETARY_TOOLS:
            self.assertIn(name, source)
        self.assertIn('policy: "prompt"', source)
        self.assertIn("ctx.hasUI", source)
        self.assertIn("session_shutdown", source)

    def test_bun_load_registers_nothing_without_pisec_role(self):
        script = f"""
const records = {{tools: [], events: []}};
const pi = {{
  zod: {{}},
  setLabel() {{}},
  registerTool(value) {{ records.tools.push(value.name); }},
  on(value) {{ records.events.push(value); }},
  setActiveTools() {{ throw new Error('must not activate tools'); }},
}};
await import({json.dumps(EXTENSION.as_uri())});
const module = await import({json.dumps(EXTENSION.as_uri())});
module.default(pi);
console.log(JSON.stringify(records));
"""
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("PISEC_"):
                env.pop(key)
        result = subprocess.run(["bun", "-e", script], cwd=ROOT, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"tools": [], "events": []})

    def test_bun_load_registers_exact_secretary_surface_and_dynamic_scope(self):
        script = f"""
const records = {{tools: [], events: [], labels: []}};
const chain = () => ({{min: chain, max: chain, optional: chain, int: chain, url: chain}});
const zod = {{string: chain, enum: chain, any: chain, object: value => value, literal: chain, array: chain, number: chain, boolean: chain}};
const pi = {{
  zod,
  setLabel(value) {{ records.labels.push(value); }},
  registerTool(value) {{ records.tools.push(value); }},
  on(value) {{ records.events.push(value); }},
  setActiveTools() {{ return Promise.resolve(); }},
}};
process.env.PISEC_ROLE = 'secretary';
process.env.PISEC_RUNTIME_SOCKET = '/tmp/runtime.sock';
process.env.PISEC_SECRETARY_SOCKET = '/tmp/secretary.sock';
process.env.PISEC_RUNTIME_TOKEN = 't'.repeat(48);
process.env.PISEC_WORKSTREAM_ID = 'ws_' + 'a'.repeat(32);
process.env.PISEC_RUNTIME_INSTANCE_ID = 'instance';
process.env.PISEC_SURFACE_ID = 'w1:p1';
const module = await import({json.dumps(EXTENSION.as_uri())} + '?secretary=' + Date.now());
module.default(pi);
const create = records.tools.find(value => value.name === 'pisec_create_workstream');
const merge = records.tools.find(value => value.name === 'pisec_merge_workstream');
const scope = {{operationId:'op_'+'a'.repeat(32), projectId:'prj_'+'b'.repeat(32), workstreamId:'ws_'+'a'.repeat(32), title:'Title', purpose:'Purpose', brief:'Full brief', harnessId:'omp', workspaceAdapterId:'herdr', executionProfile:'worker-default', targetRef:'main', baseCommitOid:'a'.repeat(40), branchName:'pisec/ws_'+'a'.repeat(32)+'/work', worktreePath:'/tmp/work', privateGitObjectDir:'/tmp/objects', gitCommonObjectDir:'/tmp/common/objects', agentName:'pisec-agent', externalDomains:['html.duckduckgo.com'], effects:['create'], nonEffects:['push']}};
const refused = await create.execute('id', {{approval_scope: scope}}, undefined, undefined, {{hasUI: false}});
const mergeScope = {{kind:'git.merge.ff-only', projectId:'prj_'+'b'.repeat(32), workstreamId:'ws_'+'a'.repeat(32), targetBranch:'main', targetCommitOid:'a'.repeat(40), sourceBranch:'pisec/ws_'+'a'.repeat(32)+'/work', sourceCommitOid:'b'.repeat(40), strategy:'ff-only', effects:['advance main'], nonEffects:['no push']}};
const mergeRefused = await merge.execute('id', {{approval_scope: mergeScope}}, undefined, undefined, {{hasUI: false}});
console.log(JSON.stringify({{tools: records.tools.map(value => value.name), events: records.events, label: records.labels[0], approval: create.approval(scope), refused, mergeApproval: merge.approval(mergeScope), mergeRefused}}));
"""
        result = subprocess.run(["bun", "-e", script], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["tools"], SECRETARY_TOOLS)
        self.assertEqual(output["label"], "Pisec Secretary")
        self.assertIn("full brief: Full brief", output["approval"]["reason"])
        self.assertIn("agent name: pisec-agent", output["approval"]["reason"])
        self.assertIn("exact external domains: html.duckduckgo.com", output["approval"]["reason"])
        self.assertIn("workspace adapter: herdr", output["approval"]["reason"])
        self.assertEqual(output["approval"]["policy"], "prompt")
        self.assertEqual(output["approval"]["tier"], "exec")
        self.assertTrue(output["refused"]["isError"])
        self.assertIn("interactive approval UI", output["refused"]["content"][0]["text"])
        self.assertIn("target branch: main", output["mergeApproval"]["reason"])
        self.assertIn("source commit OID: " + "b" * 40, output["mergeApproval"]["reason"])
        self.assertEqual(output["mergeApproval"]["policy"], "prompt")
        self.assertTrue(output["mergeRefused"]["isError"])
        self.assertIn("interactive approval UI", output["mergeRefused"]["content"][0]["text"])
        self.assertIn("session_shutdown", output["events"])

    def test_bun_worker_registers_runtime_only_without_secretary_tools(self):
        script = f"""
const records = {{tools: [], events: [], labels: []}};
const chain = () => ({{min: chain, max: chain, optional: chain, int: chain, url: chain}});
const zod = {{string: chain, enum: chain, any: chain, object: value => value, literal: chain, array: chain, number: chain, boolean: chain}};
const pi = {{
  zod,
  setLabel(value) {{ records.labels.push(value); }},
  registerTool(value) {{ records.tools.push(value.name); }},
  on(value) {{ records.events.push(value); }},
  setActiveTools() {{ return Promise.resolve(); }},
}};
process.env.PISEC_ROLE = 'worker';
process.env.PISEC_RUNTIME_SOCKET = '/tmp/runtime.sock';
process.env.PISEC_RUNTIME_TOKEN = 't'.repeat(48);
process.env.PISEC_WORKSTREAM_ID = 'ws_' + 'a'.repeat(32);
process.env.PISEC_RUNTIME_INSTANCE_ID = 'instance';
process.env.PISEC_SURFACE_ID = 'w1:p1';
const module = await import({json.dumps(EXTENSION.as_uri())} + '?worker=' + Date.now());
module.default(pi);
console.log(JSON.stringify({{tools: records.tools, events: records.events, label: records.labels[0]}}));
"""
        result = subprocess.run(["bun", "-e", script], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["tools"], WORKER_TOOLS)
        self.assertEqual(output["label"], "Pisec Worker")
        self.assertIn("session_start", output["events"])

    def test_bun_build_succeeds(self):
        result = subprocess.run(["bun", "build", str(EXTENSION), "--target", "bun", "--outdir", "/tmp/pisec-extension-check"], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
