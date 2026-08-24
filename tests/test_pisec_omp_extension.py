from pathlib import Path
import json
import os
import subprocess
import unittest

from scripts.pisec.operation_contracts import SOCKET_OPERATIONS

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "omp" / "extensions" / "pisec.ts"
SECRETARY_TOOL_OPERATIONS = [
    ("pisec_project_activity", "project.activity"),
    ("pisec_refresh_project", "project.refresh"),
    ("pisec_install_release", "runtime.release.install"),
    ("pisec_report_secretary_issue", "issue.report"),
    ("pisec_list_issues", "issue.list"),
    ("pisec_inspect_issue", "issue.inspect"),
    ("pisec_add_issue_context", "issue.add_context"),
    ("pisec_verify_issue", "issue.verify"),
    ("pisec_project_status", "project.status"),
    ("pisec_git_status", "git.status"),
    ("pisec_push_branch", "git.push"),
    ("pisec_inspect_workstream_changes", "git.workstream_changes"),
    ("pisec_prepare_workstream_acceptance", "workstream.accept.prepare"),
    ("pisec_accept_workstream", "workstream.accept.apply"),
    ("pisec_list_workstreams", "workstream.list"),
    ("pisec_inspect_workstream", "workstream.inspect"),
    ("pisec_list_integrations", "integration.list"),
    ("pisec_inspect_integration", "integration.inspect"),
    ("pisec_prepare_workstream", "workstream.prepare"),
    ("pisec_create_workstream", "workstream.authorize_apply"),
    ("pisec_send_workstream", "workstream.send"),
    ("pisec_focus_workstream", "workstream.focus"),
    ("pisec_retire_workstream", "workstream.retire"),
    ("pisec_list_decisions", "decision.list"),
    ("pisec_record_decision", "decision.record"),
    ("pisec_list_coordination_requests", "coordination.list"),
    ("pisec_inspect_coordination_request", "coordination.inspect"),
    ("pisec_answer_coordination_request", "coordination.answer"),
    ("pisec_resolve_decision", "decision.resolve"),
    ("pisec_list_worker_research_requests", "research.list"),
    ("pisec_inspect_worker_research", "research.inspect"),
    ("pisec_claim_worker_research", "research.claim"),
    ("pisec_request_worker_research_context", "research.request_context"),
    ("pisec_answer_worker_research", "research.answer"),
    ("pisec_decline_worker_research", "research.decline"),
]
FLEET_TOOL_OPERATIONS = [
    ("pisec_fleet_list_access_grants", "fleet.access.list"),
    ("pisec_fleet_inspect_access_grant", "fleet.access.inspect"),
    ("pisec_fleet_prepare_access_grant", "fleet.access.grant.prepare"),
    ("pisec_fleet_apply_access_grant", "fleet.access.grant.apply"),
    ("pisec_fleet_prepare_access_revoke", "fleet.access.revoke.prepare"),
    ("pisec_fleet_apply_access_revoke", "fleet.access.revoke.apply"),
    ("pisec_fleet_list_issues", "fleet.issue.list"),
    ("pisec_fleet_inspect_issue", "fleet.issue.inspect"),
    ("pisec_fleet_add_issue_context", "fleet.issue.add_context"),
    ("pisec_fleet_acknowledge_issue", "fleet.issue.acknowledge"),
    ("pisec_fleet_resolve_issue", "fleet.issue.resolve"),
    ("pisec_fleet_status", "fleet.status"),
    ("pisec_fleet_events", "fleet.events"),
    ("pisec_fleet_send_secretary", "fleet.secretary.send"),
    ("pisec_fleet_list_workstreams", "fleet.workstream.list"),
    ("pisec_fleet_inspect_workstream", "fleet.workstream.inspect"),
    ("pisec_fleet_list_integrations", "fleet.integration.list"),
    ("pisec_fleet_inspect_integration", "fleet.integration.inspect"),
    ("pisec_fleet_git_changes", "fleet.git.workstream_changes"),
    ("pisec_fleet_prepare_workstream", "fleet.workstream.prepare"),
    ("pisec_fleet_create_worker", "fleet.workstream.authorize_apply"),
    ("pisec_fleet_prepare_acceptance", "fleet.workstream.accept.prepare"),
    ("pisec_fleet_accept_workstream", "fleet.workstream.accept.apply"),
]
WORKER_TOOL_OPERATIONS = [
    ("pisec_checkpoint_workstream", "workstream.checkpoint"),
    ("pisec_submit_completion", "workstream.completion.submit"),
    ("pisec_request_help", "help.request"),
    ("pisec_request_coordination", "coordination.request"),
    ("pisec_list_coordination", "coordination.list"),
    ("pisec_inspect_coordination", "coordination.inspect"),
    ("pisec_report_issue", "issue.report"),
    ("pisec_list_issues", "issue.list"),
    ("pisec_inspect_issue", "issue.inspect"),
    ("pisec_add_issue_context", "issue.add_context"),
    ("pisec_verify_issue", "issue.verify"),
    ("pisec_show_task_packet", "task.get"),
    ("pisec_request_secretary_research", "research.request"),
    ("pisec_check_secretary_research", "research.list"),
    ("pisec_inspect_secretary_research", "research.inspect"),
    ("pisec_add_secretary_research_context", "research.add_context"),
    ("pisec_acknowledge_secretary_research", "research.acknowledge"),
]
SECRETARY_TOOLS = [name for name, _ in SECRETARY_TOOL_OPERATIONS]
FLEET_TOOLS = [name for name, _ in FLEET_TOOL_OPERATIONS]
WORKER_TOOLS = [name for name, _ in WORKER_TOOL_OPERATIONS]


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
const zod = {{string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain}};
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
const accept = records.tools.find(value => value.name === 'pisec_accept_workstream');
const scope = {{operationId:'op_'+'a'.repeat(32), projectId:'prj_'+'b'.repeat(32), workstreamId:'ws_'+'a'.repeat(32), title:'Title', purpose:'Purpose', brief:'Full brief', harnessId:'omp', workspaceAdapterId:'herdr', executionProfile:'worker-default', targetRef:'main', baseCommitOid:'a'.repeat(40), branchName:'pisec/ws_'+'a'.repeat(32)+'/work', worktreePath:'/tmp/work', privateGitObjectDir:'/tmp/objects', gitCommonObjectDir:'/tmp/common/objects', agentName:'pisec-agent', externalDomains:['html.duckduckgo.com'], effects:['create'], nonEffects:['push']}};
const refused = await create.execute('id', {{approval_scope: scope}}, undefined, undefined, {{hasUI: false}});
const acceptanceScope = {{kind:'workstream.accept', projectId:'prj_'+'b'.repeat(32), workstreamId:'ws_'+'a'.repeat(32), targetBranch:'main', completionPacketSha256:'c'.repeat(64), taskPacketSha256:'d'.repeat(64), candidatePatchSha256:'e'.repeat(64), changedPaths:['src/main.ts'], acceptance:[{{criterion:'passed'}}], verification:[{{command:'bun test', result:'passed'}}], conflictPolicy:'bounded-worker-reconciliation', mergePolicy:{{}}, effects:['advance main'], nonEffects:['no push']}};
const acceptRefused = await accept.execute('id', {{approval_scope: acceptanceScope}}, undefined, undefined, {{hasUI: false}});
console.log(JSON.stringify({{tools: records.tools.map(value => value.name), events: records.events, label: records.labels[0], approval: create.approval(scope), refused, acceptanceApproval: accept.approval(acceptanceScope), acceptRefused}}));
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
        self.assertIn("target branch: main", output["acceptanceApproval"]["reason"])
        self.assertIn("candidate patch digest: " + "e" * 64, output["acceptanceApproval"]["reason"])
        self.assertEqual(output["acceptanceApproval"]["policy"], "prompt")
        self.assertTrue(output["acceptRefused"]["isError"])
        self.assertIn("interactive approval UI", output["acceptRefused"]["content"][0]["text"])
        self.assertIn("session_shutdown", output["events"])

    def test_bun_worker_registers_runtime_only_without_secretary_tools(self):
        script = f"""
const records = {{tools: [], events: [], labels: []}};
const chain = () => ({{min: chain, max: chain, optional: chain, int: chain, url: chain}});
const zod = {{string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain}};
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

    def test_exposed_tools_only_use_operations_allowed_by_their_socket(self):
        for socket, tools in (
            ("secretary", SECRETARY_TOOL_OPERATIONS),
            ("fleet", FLEET_TOOL_OPERATIONS),
            ("runtime", WORKER_TOOL_OPERATIONS),
        ):
            with self.subTest(socket=socket):
                exposed = {operation for _, operation in tools}
                self.assertEqual(exposed - SOCKET_OPERATIONS[socket], set())

    def test_bun_build_succeeds(self):
        result = subprocess.run(["bun", "build", str(EXTENSION), "--target", "bun", "--outdir", "/tmp/pisec-extension-check"], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
