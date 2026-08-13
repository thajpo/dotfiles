/** Credential-free deterministic provider for repair-journey child runs. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai";

const EXPECTED = {
  investigator: ["acknowledge_project_message", "git_read", "grep", "harness_feedback", "list_project_messages", "ls", "post_project_message", "read", "record_package_security_review", "reply_project_message"],
  reviewer: ["acknowledge_project_message", "check_package_review_gate", "git_read", "grep", "harness_feedback", "list_project_messages", "ls", "post_project_message", "read", "reply_project_message"],
  workstream: ["acknowledge_project_message", "bash", "edit", "harness_feedback", "inventory_dependency_changes", "list_project_messages", "observe_change_queue", "observe_fleet", "observe_messages", "observe_tasks", "package_operation_status", "post_project_message", "project_command_status", "read", "reply_project_message", "request_package_operation", "request_project_command", "subagent", "subagent_interrupt", "subagent_list", "subagent_resume", "subagent_start", "subagent_status", "subagent_steer", "subagent_stop", "subagent_wait", "submit_change", "worker_start", "write"],
};

function promptText(context: any): string {
  const prompt = context.messages.findLast((message: any) => message.role === "user");
  return prompt?.role === "user"
    ? (typeof prompt.content === "string" ? prompt.content : prompt.content.map((part: any) => part.type === "text" ? part.text : "").join("\n"))
    : "";
}

function resultText(context: any): { result: any; text: string } {
  const result = context.messages.at(-1);
  const text = result?.role === "toolResult"
    ? result.content.map((part: any) => part.type === "text" ? part.text : "").join("\n")
    : "";
  return { result, text };
}

export default function scriptedRepairChildProvider(pi: ExtensionAPI): void {
  const faux = fauxProvider({
    api: "scripted",
    provider: "scripted",
    models: [{ id: "scripted-1", name: "Installed Repair Child" }],
    tokenSize: { min: 4096, max: 4096 },
  });
  faux.setResponses([
    (context) => {
      const prompt = promptText(context);
      const tools = (context.tools ?? []).map((tool: any) => tool.name).sort();
      const role = prompt.includes("worker") ? "workstream" : prompt.includes("reviewer") ? "reviewer" : "investigator";
      if (JSON.stringify(tools) !== JSON.stringify(EXPECTED[role])) throw new Error(`unexpected repair child tools: ${tools.join(",")}`);
      const task = prompt.split("\n\n").at(-1) ?? "";
      if (task.includes("worker probe")) {
        return fauxAssistantMessage(fauxToolCall("read", { path: "README" }, { id: "repair-worker-read" }), { stopReason: "toolUse" });
      }
      return fauxAssistantMessage(fauxToolCall("read", { path: "README" }, { id: "repair-child-read-1" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error(`expected a readable file result: ${text.slice(0, 200)}`);
      const prompt = promptText(context);
      const task = prompt.split("\n\n").at(-1) ?? "";
      if (task.includes("worker probe")) {
        return fauxAssistantMessage(fauxToolCall("write", { path: "worker.txt", content: "worker work\n" }, { id: "repair-worker-write" }), { stopReason: "toolUse" });
      }
      if (task.includes("async completion probe")) {
        return fauxAssistantMessage(fauxToolCall("post_project_message", { kind: "progress", payload: { childStage: "started", childRequestId: "completion-probe" }, idempotencyKey: "repair-child-progress-1" }, { id: "repair-child-post-1" }), { stopReason: "toolUse" });
      }
      return fauxAssistantMessage(fauxToolCall("post_project_message", { kind: "progress", payload: { childStage: "started", childRequestId: "escalation-probe" }, idempotencyKey: "repair-child-progress-2" }, { id: "repair-child-post-2" }), { stopReason: "toolUse" });
    },
    async (context) => {
      const { result } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("child progress message failed");
      const prompt = promptText(context);
      const task = prompt.split("\n\n").at(-1) ?? "";
      if (task.includes("worker probe")) {
        // Hold the worker claim briefly so the journey can prove one-writer
        // enforcement with a second writer while the worker still runs.
        await new Promise((resolve) => setTimeout(resolve, 60000));
        return fauxAssistantMessage("REPAIR_WORKER_FINAL", { stopReason: "stop" });
      }
      if (task.includes("async completion probe")) {
        // Keep the child running long enough that the parent's immediate
        // continuation proves the start did not block the parent turn.
        await new Promise((resolve) => setTimeout(resolve, 60000));
        return fauxAssistantMessage(fauxToolCall("post_project_message", { kind: "progress", payload: { childCompletion: true }, idempotencyKey: "repair-child-complete-1" }, { id: "repair-child-complete" }), { stopReason: "toolUse" });
      }
      return fauxAssistantMessage(fauxToolCall("post_project_message", { kind: "needs-user", payload: { escalation: "decision-required", decision: "approve the child plan?" }, idempotencyKey: "repair-child-escalation-1" }, { id: "repair-child-escalate" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("child final message failed");
      const prompt = promptText(context);
      const task = prompt.split("\n\n").at(-1) ?? "";
      if (task.includes("escalation probe")) {
        return fauxAssistantMessage("REPAIR_ESCALATION_FINAL", { stopReason: "stop" });
      }
      return fauxAssistantMessage("REPAIR_CHILD_FINAL", { stopReason: "stop" });
    },
  ]);
  pi.registerProvider(faux.provider);
}
