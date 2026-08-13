/** Credential-free deterministic provider for the personal-primary writer. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai";

const EXPECTED = ["acknowledge_project_message", "bash", "edit", "harness_feedback", "inventory_dependency_changes", "list_project_messages", "observe_change_queue", "observe_fleet", "observe_messages", "observe_tasks", "package_operation_status", "post_project_message", "project_command_status", "read", "reply_project_message", "request_package_operation", "request_project_command", "subagent", "subagent_interrupt", "subagent_list", "subagent_resume", "subagent_start", "subagent_status", "subagent_steer", "subagent_stop", "subagent_wait", "submit_change", "worker_start", "write"];

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

export default function scriptedRepairWriterProvider(pi: ExtensionAPI): void {
  const faux = fauxProvider({
    api: "scripted",
    provider: "scripted",
    models: [{ id: "scripted-1", name: "Installed Repair Writer" }],
    tokenSize: { min: 4096, max: 4096 },
  });
  faux.setResponses([
    (context) => {
      const tools = (context.tools ?? []).map((tool: any) => tool.name).sort();
      if (JSON.stringify(tools) !== JSON.stringify(EXPECTED)) throw new Error(`unexpected repair writer tools: ${tools.join(",")}`);
      const prompt = promptText(context);
      const parsed = JSON.parse(prompt);
      if (parsed.role !== "writer") throw new Error("unexpected repair writer prompt");
      return fauxAssistantMessage(fauxToolCall("read", { path: "README" }, { id: "repair-writer-read" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError || !text.includes("baseline")) throw new Error(`expected the pre-existing baseline README: ${text.slice(0, 200)}`);
      return fauxAssistantMessage(fauxToolCall("write", { path: "task.txt", content: "task work\n" }, { id: "repair-writer-write" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("writer write failed");
      return fauxAssistantMessage(fauxToolCall("submit_change", {
        title: "personal primary task", summary: "task delta from the primary checkout",
        targetRef: "refs/heads/main", captureMode: "dirty",
        selectedPaths: ["task.txt"], excludedPaths: [], idempotencyKey: "repair-writer-submit",
      }, { id: "repair-writer-submit" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("submit_change failed");
      const value = JSON.parse(text);
      if (value.revision !== 1 || JSON.stringify(value.changedPaths) !== JSON.stringify(["task.txt"])) throw new Error(`task delta submission is wrong: ${text.slice(0, 300)}`);
      return fauxAssistantMessage(fauxToolCall("worker_start", { task: "worker probe", title: "repair worker", idempotencyKey: "repair-worker-1" }, { id: "repair-worker-start" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("worker_start failed");
      const value = JSON.parse(text);
      if (!value.launched || !value.childRequest?.child_request_id) throw new Error(`worker start is wrong: ${text.slice(0, 300)}`);
      if (!value.workstream?.working_copy_id || value.workstream.working_copy_id === value.childRequest.parent_working_copy_id) throw new Error("worker did not get its own controller-owned working copy");
      return fauxAssistantMessage("REPAIR_WRITER_FINAL", { stopReason: "stop" });
    },
  ]);
  pi.registerProvider(faux.provider);
}
