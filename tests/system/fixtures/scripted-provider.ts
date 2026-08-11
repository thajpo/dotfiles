/** Credential-free deterministic provider for installed host-role tests. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai";

const EXPECTED_TOOLS = {
  secretary: ["acknowledge_project_message", "check_package_review_gate", "git_read", "grep", "list_project_messages", "ls", "post_project_message", "read", "record_dependency_disposition", "reply_project_message", "subagent"],
  investigator: ["git_read", "grep", "ls", "read", "record_package_security_review"],
  reviewer: ["check_package_review_gate", "git_read", "grep", "ls", "read"],
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

export default function scriptedProvider(pi: ExtensionAPI): void {
  const faux = fauxProvider({
    api: "scripted",
    provider: "scripted",
    models: [{ id: "scripted-1", name: "Installed Process Script" }],
    tokenSize: { min: 4096, max: 4096 },
  });

  faux.setResponses([
    (context) => {
      const tools = (context.tools ?? []).map((tool) => tool.name).sort();
      const prompt = promptText(context);
      if (prompt === "verify no controller scope") {
        if (tools.length !== 0) throw new Error(`unexpected unscoped tools: ${tools.join(",")}`);
        return fauxAssistantMessage("NO_SCOPE_FINAL", { stopReason: "stop" });
      }
       const role = prompt === "inspect as investigator" ? "investigator" : prompt === "inspect exact review" ? "reviewer" : "secretary";
       if (JSON.stringify(tools) !== JSON.stringify(EXPECTED_TOOLS[role])) throw new Error(`unexpected active tools: ${tools.join(",")}`);
      if (prompt === "inspect exact review") return fauxAssistantMessage(fauxToolCall("git_read", { query: "show", path: "README" }, { id: "scripted-review-read" }), { stopReason: "toolUse" });
      return fauxAssistantMessage(fauxToolCall("read", { path: "README", operation: "write" }, { id: prompt === "inspect as investigator" ? "scripted-investigator-read" : "scripted-read-1" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError || !text.includes("installed process")) throw new Error("expected the scoped README result");
      if (result.toolCallId === "scripted-review-read") return fauxAssistantMessage(fauxToolCall("edit", { path: "README", oldText: "installed process", newText: "forbidden" }, { id: "scripted-review-edit" }), { stopReason: "toolUse" });
      if (result.toolCallId === "scripted-investigator-read") return fauxAssistantMessage(fauxToolCall("bash", { command: "pwd" }, { id: "scripted-investigator-bash" }), { stopReason: "toolUse" });
      return fauxAssistantMessage(fauxToolCall("write", { path: "README", content: "forbidden\n" }, { id: "scripted-write-1" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      const expected = result?.toolCallId === "scripted-review-edit" ? "Tool edit not found" : result?.toolCallId === "scripted-investigator-bash" ? "Tool bash not found" : "Tool write not found";
      if (result?.role !== "toolResult" || !result.isError || !text.includes(expected)) throw new Error(`expected unavailable tool rejection: ${expected}`);
      const final = result.toolCallId === "scripted-review-edit" ? "REVIEWER_FINAL" : result.toolCallId === "scripted-investigator-bash" ? "INVESTIGATOR_FINAL" : "SCRIPTED_FINAL";
      return fauxAssistantMessage(final, { stopReason: "stop" });
    },
  ]);

  pi.registerProvider(faux.provider);
}
