/** Credential-free deterministic provider for the installed P7 journey. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai";

const EXPECTED_TOOLS = ["acknowledge_project_message", "bash", "edit", "harness_feedback", "inventory_dependency_changes", "list_project_messages", "observe_change_queue", "observe_fleet", "observe_messages", "observe_tasks", "package_operation_status", "post_project_message", "project_command_status", "read", "reply_project_message", "request_package_operation", "request_project_command", "subagent", "subagent_interrupt", "subagent_list", "subagent_resume", "subagent_start", "subagent_status", "subagent_steer", "subagent_stop", "subagent_wait", "submit_change", "worker_start", "write"];

function last(context: any): any { return context.messages.at(-1); }
function text(message: any): string { return message?.content?.map((part: any) => part.type === "text" ? part.text : "").join("\n") ?? ""; }
function value(context: any): any {
  const result = last(context);
  if (result?.role !== "toolResult" || result.isError) throw new Error(`P7 tool failed: ${text(result)}`);
  return JSON.parse(text(result));
}
function promptText(context: any): string {
  const prompt = context.messages.findLast((message: any) => message.role === "user");
  if (!prompt) return "{}";
  return typeof prompt.content === "string" ? prompt.content : prompt.content.map((part: any) => part.type === "text" ? part.text : "").join("\n");
}
function roleOf(context: any): string {
  const role = JSON.parse(promptText(context) || "{}").role;
  if (role !== "personal" && role !== "workstream") throw new Error(`unexpected P7 role: ${role}`);
  return role;
}

function settleChild(context: any, expectedChild: "inv" | "rev"): any {
  const role = roleOf(context);
  const sub = value(context);
  const childRequest = sub.childRequest ?? {};
  const terminal = sub.terminal ?? {};
  const expectedRole = expectedChild === "inv" ? "investigator" : "reviewer";
  if (childRequest.semantic_role !== expectedRole) throw new Error(`P7 child semantic role mismatch: ${JSON.stringify(sub)}`);
  if (childRequest.state !== "success" || terminal.terminal_class !== "success") throw new Error(`P7 child terminal is not success: ${JSON.stringify(sub)}`);
  if (!childRequest.snapshot_commit_oid || !childRequest.snapshot_tree_oid || !childRequest.snapshot_ref || !childRequest.child_run_id) throw new Error(`P7 child identity is incomplete: ${JSON.stringify(sub)}`);
  return fauxAssistantMessage(fauxToolCall("write", { path: `P7_${role}.md`, content: "P7 installed process\n" }, { id: `p7-write-${role}-${expectedChild}` }), { stopReason: "toolUse" });
}

export default function scriptedP7Provider(pi: ExtensionAPI): void {
  const faux = fauxProvider({ api: "scripted", provider: "scripted", models: [{ id: "scripted-1", name: "Installed P7 Script" }], tokenSize: { min: 4096, max: 4096 } });
  const steps: Array<(context: any, options: any, state: { callCount: number }) => any> = [
    (context) => {
      const tools = (context.tools ?? []).map((tool: any) => tool.name).sort();
      if (JSON.stringify(tools) !== JSON.stringify([...EXPECTED_TOOLS].sort())) throw new Error(`unexpected P7 tools: ${tools.join(",")}`);
      const role = roleOf(context);
      return fauxAssistantMessage(fauxToolCall("post_project_message", { kind: "progress", payload: { phase: "P7", role }, idempotencyKey: `p7-msg-${role}` }, { id: `p7-msg-${role}` }), { stopReason: "toolUse" });
    },
    (context) => {
      const role = roleOf(context);
      value(context);
      return fauxAssistantMessage(fauxToolCall("subagent", { role: "investigator", task: "inspect as investigator", idempotencyKey: `p7-child-inv-${role}` }, { id: `p7-sub-inv-${role}` }), { stopReason: "toolUse" });
    },
    (context) => settleChild(context, "inv"),
    (context) => {
      const role = roleOf(context);
      if (role === "personal") {
        return fauxAssistantMessage("P7_PERSONAL_FINAL", { stopReason: "stop" });
      }
      value(context);
      return fauxAssistantMessage(fauxToolCall("subagent", { role: "reviewer", task: "inspect exact review", idempotencyKey: `p7-child-rev-${role}` }, { id: `p7-sub-rev-${role}` }), { stopReason: "toolUse" });
    },
    (context) => settleChild(context, "rev"),
    (context) => {
      const role = roleOf(context);
      value(context);
      return fauxAssistantMessage(`P7_WORKSTREAM_FINAL`, { stopReason: "stop" });
    },
  ];
  faux.setResponses(steps);
  pi.registerProvider(faux.provider);
}