/** Credential-free deterministic provider for the repair-journey secretary. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai";

const EXPECTED = ["acknowledge_project_message", "analyze_integration", "check_package_review_gate", "git_read", "grep", "harness_feedback", "list_changes", "list_project_messages", "ls", "observe_change_queue", "observe_fleet", "observe_messages", "observe_tasks", "post_project_message", "project_work_index", "propose_integration", "propose_review", "propose_workstream", "approve_workstream", "read", "record_dependency_disposition", "reply_project_message", "request_review", "start_investigation", "subagent", "subagent_interrupt", "subagent_list", "subagent_resume", "subagent_start", "subagent_status", "subagent_steer", "subagent_stop", "subagent_wait"];

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

export default function scriptedRepairSecretaryProvider(pi: ExtensionAPI): void {
  const faux = fauxProvider({
    api: "scripted",
    provider: "scripted",
    models: [{ id: "scripted-1", name: "Installed Repair Secretary" }],
    tokenSize: { min: 4096, max: 4096 },
  });
  let childA = "";
  let childB = "";
  let childC = "";
  let childE = "";
  let childF = "";
  let interruptedConv = "";
  faux.setResponses([
    (context) => {
      const tools = (context.tools ?? []).map((tool: any) => tool.name).sort();
      if (JSON.stringify(tools) !== JSON.stringify(EXPECTED)) throw new Error(`unexpected repair secretary tools: ${tools.join(",")}`);
      const prompt = promptText(context);
      const parsed = JSON.parse(prompt);
      if (parsed.role !== "secretary" || !parsed.changeId || !parsed.revision || !parsed.targetRef) throw new Error("unexpected repair secretary prompt");
      return fauxAssistantMessage(fauxToolCall("observe_tasks", {}, { id: "repair-observe-tasks" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError || !text.includes("Working now")) throw new Error(`work index projection is wrong: ${text.slice(0, 200)}`);
      return fauxAssistantMessage(fauxToolCall("subagent_start", { role: "researcher", task: "map the api surface", idempotencyKey: "repair-child-a" }, { id: "repair-start-a" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("subagent_start A failed");
      childA = JSON.parse(text).childRequest.child_request_id;
      return fauxAssistantMessage(fauxToolCall("subagent_start", { role: "reviewer", task: "review the diff", idempotencyKey: "repair-child-b" }, { id: "repair-start-b" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("subagent_start B failed");
      childB = JSON.parse(text).childRequest.child_request_id;
      return fauxAssistantMessage(fauxToolCall("subagent_wait", { childRequestId: childA, timeoutSeconds: 180 }, { id: "repair-wait-a" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("subagent_wait A failed");
      const value = JSON.parse(text);
      if (!value.waited || value.terminal?.terminal_class !== "success") throw new Error(`child A did not succeed: ${text.slice(0, 300)}`);
      return fauxAssistantMessage(fauxToolCall("subagent_wait", { childRequestId: childB, timeoutSeconds: 180 }, { id: "repair-wait-b" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("subagent_wait B failed");
      const value = JSON.parse(text);
      if (!value.waited || value.terminal?.terminal_class !== "success") throw new Error(`child B did not succeed: ${text.slice(0, 300)}`);
      return fauxAssistantMessage(fauxToolCall("subagent_start", { role: "delegate", task: "interrupt me", idempotencyKey: "repair-child-c" }, { id: "repair-start-c" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("subagent_start C failed");
      childC = JSON.parse(text).childRequest.child_request_id;
      return fauxAssistantMessage(fauxToolCall("subagent_interrupt", { childRequestId: childC }, { id: "repair-interrupt-c" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("subagent_interrupt failed");
      const value = JSON.parse(text);
      if (!value.signaled) throw new Error("interrupt did not signal the launcher");
      return fauxAssistantMessage(fauxToolCall("subagent_wait", { childRequestId: childC, timeoutSeconds: 60 }, { id: "repair-wait-c" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("subagent_wait C failed");
      const value = JSON.parse(text);
      if (!value.waited || value.terminal?.terminal_class !== "interrupted") throw new Error(`child C was not interrupted: ${text.slice(0, 300)}`);
      interruptedConv = value.childRequest.child_conversation_id;
      return fauxAssistantMessage(fauxToolCall("subagent_resume", { childRequestId: childC }, { id: "repair-resume-c" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("subagent_resume failed");
      const value = JSON.parse(text);
      if (!value.launched) throw new Error("resume did not relaunch the child");
      return fauxAssistantMessage(fauxToolCall("subagent_wait", { childRequestId: childC, timeoutSeconds: 180 }, { id: "repair-wait-c2" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("subagent_wait C2 failed");
      const value = JSON.parse(text);
      if (!value.waited || value.terminal?.terminal_class !== "success") throw new Error(`resumed child C did not succeed: ${text.slice(0, 300)}`);
      if (value.childRequest.child_conversation_id !== interruptedConv) throw new Error("resumed child did not continue the same conversation");
      return fauxAssistantMessage(fauxToolCall("subagent_start", { role: "scout", task: "restart continuity probe", idempotencyKey: "repair-child-d" }, { id: "repair-start-d" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("subagent_start D failed");
      return fauxAssistantMessage(fauxToolCall("harness_feedback", { kind: "harness-improvement", title: "repair journey feedback", evidence: ["installed run"], recommendation: "keep the controller channel" }, { id: "repair-feedback" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("harness_feedback failed");
      return fauxAssistantMessage(fauxToolCall("propose_workstream", { title: "repair workstream", purpose: "verify the proposal surface", idempotencyKey: "repair-propose-ws" }, { id: "repair-propose-ws" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("propose_workstream failed");
      if (!text.includes("needs-user") || !text.includes("workstream")) throw new Error(`workstream proposal message is wrong: ${text.slice(0, 200)}`);
      const prompt = JSON.parse(promptText(context));
      return fauxAssistantMessage(fauxToolCall("request_review", { changeId: prompt.changeId, revision: prompt.revision }, { id: "repair-request-review" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("request_review failed");
      const value = JSON.parse(text);
      if (!value.launched || !value.conversationId) throw new Error(`review assignment is wrong: ${text.slice(0, 300)}`);
      const prompt = JSON.parse(promptText(context));
      return fauxAssistantMessage(fauxToolCall("analyze_integration", { changeId: prompt.changeId, revision: prompt.revision, targetRef: prompt.targetRef }, { id: "repair-analyze" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("integration.analyze failed");
      const value = JSON.parse(text);
      if (!value.strategy) throw new Error(`integration analysis is wrong: ${text.slice(0, 300)}`);
      return fauxAssistantMessage(fauxToolCall("subagent_start", { role: "investigator", task: "async completion probe", idempotencyKey: "repair-child-e" }, { id: "repair-start-e" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("subagent_start E failed");
      childE = JSON.parse(text).childRequest.child_request_id;
      // The parent continues immediately: the completion child still runs for
      // a minute, so this next start proves the turn was not blocked.
      return fauxAssistantMessage(fauxToolCall("subagent_start", { role: "investigator", task: "escalation probe", idempotencyKey: "repair-child-f" }, { id: "repair-start-f" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("subagent_start F failed");
      childF = JSON.parse(text).childRequest.child_request_id;
      return fauxAssistantMessage(fauxToolCall("subagent_wait", { childRequestId: childE, timeoutSeconds: 300 }, { id: "repair-wait-e" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("subagent_wait E failed");
      const value = JSON.parse(text);
      if (!value.waited || value.terminal?.terminal_class !== "success") throw new Error(`async completion child did not succeed: ${text.slice(0, 300)}`);
      return fauxAssistantMessage(fauxToolCall("subagent_wait", { childRequestId: childF, timeoutSeconds: 300 }, { id: "repair-wait-f" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("subagent_wait F failed");
      const value = JSON.parse(text);
      if (!value.waited || value.terminal?.terminal_class !== "success") throw new Error(`escalation child did not succeed: ${text.slice(0, 300)}`);
      return fauxAssistantMessage(fauxToolCall("observe_messages", {}, { id: "repair-observe-messages" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("message projections are wrong");
      const messages = Array.isArray(JSON.parse(text)) ? JSON.parse(text) : JSON.parse(JSON.parse(text).messages ?? "[]");
      const completion = messages.find((message: any) => message.kind === "progress" && JSON.stringify(message.payload_json ?? {}).includes("childCompletion"));
      const escalation = messages.find((message: any) => message.kind === "needs-user" && JSON.stringify(message.payload_json ?? {}).includes("escalation"));
      if (!completion || !escalation) throw new Error(`child completion or escalation message is missing: ${text.slice(0, 400)}`);
      return fauxAssistantMessage(fauxToolCall("reply_project_message", { targetMessageId: escalation.message_id, payload: { decision: "approved", childRequestId: childF }, idempotencyKey: "repair-reply-escalation" }, { id: "repair-reply" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("escalation reply failed");
      const reply = JSON.parse(text);
      if (!reply.message_id || reply.reply_to_message_id == null) throw new Error(`escalation reply is not durable: ${text.slice(0, 300)}`);
      return fauxAssistantMessage(fauxToolCall("observe_messages", {}, { id: "repair-observe-messages2" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError || !text.includes("needs-user")) throw new Error(`message projections are wrong: ${text.slice(0, 200)}`);
      return fauxAssistantMessage(fauxToolCall("observe_fleet", {}, { id: "repair-observe-fleet" }), { stopReason: "toolUse" });
    },
    (context) => {
      const { result, text } = resultText(context);
      if (result?.role !== "toolResult" || result.isError) throw new Error("observe_fleet failed");
      const value = JSON.parse(text);
      if (!Array.isArray(value) || value.length < 4) throw new Error(`fleet projection is wrong: ${text.slice(0, 300)}`);
      return fauxAssistantMessage("REPAIR_SECRETARY_FINAL", { stopReason: "stop" });
    },
  ]);
  pi.registerProvider(faux.provider);
}
