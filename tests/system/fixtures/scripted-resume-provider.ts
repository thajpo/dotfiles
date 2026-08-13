/** Credential-free deterministic provider for the installed coding-resume journey.
 * Day one: read -> edit -> write -> test -> isolation. Day two (resume) tolerates
 * the already-edited worktree and completes the same cycle idempotently. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai";

const EXPECTED = ["acknowledge_project_message", "bash", "edit", "harness_feedback", "inventory_dependency_changes", "list_project_messages", "observe_change_queue", "observe_fleet", "observe_messages", "observe_tasks", "package_operation_status", "post_project_message", "project_command_status", "read", "reply_project_message", "request_package_operation", "request_project_command", "subagent", "subagent_interrupt", "subagent_list", "subagent_resume", "subagent_start", "subagent_status", "subagent_steer", "subagent_stop", "subagent_wait", "submit_change", "worker_start", "write"];

function last(context: any): any { return context.messages.at(-1); }
function text(message: any): string { return message?.content?.map((part: any) => part.type === "text" ? part.text : "").join("\n") ?? ""; }

export default function scriptedResumeProvider(pi: ExtensionAPI): void {
  const faux = fauxProvider({ api: "scripted", provider: "scripted", models: [{ id: "scripted-1", name: "Installed Resume Script" }], tokenSize: { min: 4096, max: 4096 } });
  faux.setResponses([
    (context) => {
      const tools = (context.tools ?? []).map((tool: any) => tool.name).sort();
      if (JSON.stringify(tools) !== JSON.stringify(EXPECTED)) throw new Error(`unexpected writer tools: ${tools.join(",")}`);
      return fauxAssistantMessage(fauxToolCall("read", { path: "tracked.txt" }, { id: "u-read" }), { stopReason: "toolUse" });
    },
    (context) => {
      if (last(context)?.isError) throw new Error("resume writer read failed");
      const value = JSON.parse(text(last(context)));
      // Day one sees "base"; day two (resume) sees "edited". Both are valid.
      if (value.lines?.[0] !== "base" && value.lines?.[0] !== "edited") throw new Error(`resume read unexpected: ${text(last(context))}`);
      const newText = value.lines?.[0] === "base" ? "edited" : "edited-again";
      return fauxAssistantMessage(fauxToolCall("edit", { path: "tracked.txt", oldText: value.lines[0], newText }, { id: "u-edit" }), { stopReason: "toolUse" });
    },
    (context) => {
      if (last(context)?.isError) throw new Error("resume writer edit failed");
      return fauxAssistantMessage(fauxToolCall("write", { path: "created.txt", content: "created\n" }, { id: "u-write" }), { stopReason: "toolUse" });
    },
    (context) => {
      if (last(context)?.isError) throw new Error("resume writer write failed");
      return fauxAssistantMessage(fauxToolCall("bash", { argv: ["python3", "-c", "assert open('created.txt').read() == 'created\\n'; print('RESUME_TEST_OK')"] }, { id: "u-test" }), { stopReason: "toolUse" });
    },
    (context) => {
      if (last(context)?.isError || !text(last(context)).includes("RESUME_TEST_OK")) throw new Error("resume writer shell test failed");
      return fauxAssistantMessage("RESUME_FINAL", { stopReason: "stop" });
    },
  ]);
  pi.registerProvider(faux.provider);
}
