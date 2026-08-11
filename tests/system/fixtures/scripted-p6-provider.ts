/** Credential-free deterministic provider for the installed P6 journey. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai";

const EXPECTED = ["acknowledge_project_message", "bash", "edit", "inventory_dependency_changes", "list_project_messages", "package_operation_status", "post_project_message", "project_command_status", "read", "reply_project_message", "request_package_operation", "request_project_command", "subagent", "write"];
const commands = [
  { operation: "host.fixture-success", purpose: "p6 installed success", state: "succeeded" },
  { operation: "host.fixture-failure", purpose: "p6 installed failure", state: "failed" },
  { operation: "host.fixture-timeout", purpose: "p6 installed timeout", state: "failed", timedOut: true },
  { operation: "network.namespace-probe", purpose: "p6 installed no-contact network namespace", state: "succeeded", networkMode: "bridge" },
  { operation: "host.controller-status", purpose: "p6 installed explicit reject", state: "rejected" },
];

function last(context: any): any { return context.messages.at(-1); }
function text(message: any): string { return message?.content?.map((part: any) => part.type === "text" ? part.text : "").join("\n") ?? ""; }
function value(context: any): any {
  const result = last(context);
  if (result?.role !== "toolResult" || result.isError) throw new Error(`P6 tool failed: ${text(result)}`);
  return JSON.parse(text(result));
}
function expectedError(context: any, fragment: string): void {
  const result = last(context);
  if (result?.role !== "toolResult" || result.isError !== true || !text(result).toLowerCase().includes(fragment.toLowerCase())) throw new Error(`expected refusal containing ${fragment}: ${text(result)}`);
}
function promptText(context: any): string {
  const prompt = context.messages.find((item: any) => item.role === "user");
  if (!prompt) return "{}";
  return typeof prompt.content === "string" ? prompt.content : prompt.content.map((part: any) => part.type === "text" ? part.text : "").join("\n");
}

export default function scriptedP6Provider(pi: ExtensionAPI): void {
  const faux = fauxProvider({ api: "scripted", provider: "scripted", models: [{ id: "scripted-1", name: "Installed P6 Script" }], tokenSize: { min: 4096, max: 4096 } });
  let messageId = "";
  let packageRequestId = "";
  let commandRequestId = "";
  const responses: Array<(context: any) => any> = [];
  responses.push((context) => {
    const tools = (context.tools ?? []).map((tool: any) => tool.name).sort();
    if (JSON.stringify(tools) !== JSON.stringify(EXPECTED)) throw new Error(`unexpected P6 tools: ${tools.join(",")}`);
    return fauxAssistantMessage(fauxToolCall("post_project_message", { kind: "progress", payload: { phase: "P6" }, idempotencyKey: "p6-installed-message" }, { id: "p6-message-post" }), { stopReason: "toolUse" });
  });
  responses.push((context) => {
    messageId = value(context).message_id;
    return fauxAssistantMessage(fauxToolCall("list_project_messages", { states: ["pending"] }, { id: "p6-message-list" }), { stopReason: "toolUse" });
  });
  responses.push((context) => {
    if (!value(context).some((item: any) => item.message_id === messageId)) throw new Error("posted message was not listed");
    return fauxAssistantMessage(fauxToolCall("acknowledge_project_message", { messageId }, { id: "p6-message-ack" }), { stopReason: "toolUse" });
  });
  responses.push((context) => {
    if (value(context).state !== "acknowledged") throw new Error("message acknowledgement failed");
    return fauxAssistantMessage(fauxToolCall("reply_project_message", { targetMessageId: messageId, payload: { answer: "recorded" }, idempotencyKey: "p6-installed-reply" }, { id: "p6-message-reply" }), { stopReason: "toolUse" });
  });
  responses.push((context) => {
    if (value(context).reply_to_message_id !== messageId) throw new Error("message reply binding failed");
    const fixture = JSON.parse(promptText(context));
    return fauxAssistantMessage(fauxToolCall("inventory_dependency_changes", { changeId: fixture.changeId, revision: fixture.revision }, { id: "p6-dependency-inventory" }), { stopReason: "toolUse" });
  });
  responses.push((context) => {
    const inventory = value(context);
    const exact = new Set(inventory.differences?.map((item: any) => `${item.ecosystem}:${item.packageName}:${item.exactVersion}`));
    if (!exact.has("npm:p6-tiny-npm:1.0.0") || !exact.has("python:p6-tiny-python:1.0.0")) throw new Error("immutable dependency inventory failed");
    return fauxAssistantMessage(fauxToolCall("request_package_operation", { changeId: inventory.changeId, revision: inventory.revision, ecosystem: "npm", action: "add", packageName: "p6-tiny-npm", exactVersion: "1.0.0" }, { id: "p6-package-request-npm" }), { stopReason: "toolUse" });
  });
  responses.push((context) => {
    packageRequestId = value(context).package_request_id;
    return fauxAssistantMessage(fauxToolCall("bash", { argv: ["python3", "-c", "import time;time.sleep(2)"] }, { id: "p6-package-wait" }), { stopReason: "toolUse" });
  });
  responses.push((context) => {
    value(context);
    return fauxAssistantMessage(fauxToolCall("package_operation_status", { packageRequestId }, { id: "p6-package-status-npm" }), { stopReason: "toolUse" });
  });
  responses.push((context) => {
    const packageStatus = value(context);
    const result = packageStatus.result_json ? JSON.parse(packageStatus.result_json) : {};
    if (packageStatus.state !== "succeeded" || result.materialized !== true || result.installedPackages?.[0]?.name !== "p6-tiny-npm") throw new Error("npm package materialization evidence is wrong");
    const prompt = JSON.parse(promptText(context));
    return fauxAssistantMessage(fauxToolCall("request_package_operation", { changeId: prompt.changeId, revision: prompt.revision, ecosystem: "python", action: "add", packageName: "p6-tiny-python", exactVersion: "1.0.0" }, { id: "p6-package-request-python" }), { stopReason: "toolUse" });
  });
  responses.push((context) => {
    packageRequestId = value(context).package_request_id;
    return fauxAssistantMessage(fauxToolCall("bash", { argv: ["python3", "-c", "import time;time.sleep(2)"] }, { id: "p6-package-wait-python" }), { stopReason: "toolUse" });
  });
  responses.push((context) => {
    value(context);
    return fauxAssistantMessage(fauxToolCall("package_operation_status", { packageRequestId }, { id: "p6-package-status-python" }), { stopReason: "toolUse" });
  });
  responses.push((context) => {
    const packageStatus = value(context);
    const packageResult = packageStatus.result_json ? JSON.parse(packageStatus.result_json) : {};
    if (packageStatus.state !== "succeeded" || packageResult.materialized !== true || packageResult.installedPackages?.[0]?.name !== "p6-tiny-python") throw new Error("Python package materialization evidence is wrong");
    const prompt = JSON.parse(promptText(context));
    return fauxAssistantMessage(fauxToolCall("inventory_dependency_changes", { changeId: prompt.changeId, revision: prompt.unsupportedRevision }, { id: "p6-unsupported-manager" }), { stopReason: "toolUse" });
  });
  responses.push((context) => {
    expectedError(context, "unsupported package manager");
    const prompt = JSON.parse(promptText(context));
    return fauxAssistantMessage(fauxToolCall("inventory_dependency_changes", { changeId: prompt.changeId, revision: prompt.unlockedRevision }, { id: "p6-unlocked-input" }), { stopReason: "toolUse" });
  });
  responses.push((context) => {
    expectedError(context, "unlocked");
    const prompt = JSON.parse(promptText(context));
    return fauxAssistantMessage(fauxToolCall("inventory_dependency_changes", { changeId: prompt.changeId, revision: prompt.rangeRevision }, { id: "p6-range-input" }), { stopReason: "toolUse" });
  });
  responses.push((context) => {
    expectedError(context, "exact and hash-pinned");
    return fauxAssistantMessage(fauxToolCall("request_project_command", { operation: commands[0].operation, purpose: commands[0].purpose }, { id: "p6-command-request-0" }), { stopReason: "toolUse" });
  });
  commands.forEach((command, index) => {
    if (index > 0) responses.push((context) => {
        const status = value(context);
        if (status.state !== commands[index - 1].state) throw new Error(`command state differs: ${status.state}`);
        const result = status.result_json ? JSON.parse(status.result_json) : {};
        if (commands[index - 1].timedOut && result.timedOut !== true) throw new Error("timeout was not recorded");
        if (commands[index - 1].networkMode && result.networkMode !== commands[index - 1].networkMode) throw new Error("network namespace mode was not recorded");
      return fauxAssistantMessage(fauxToolCall("request_project_command", { operation: command.operation, purpose: command.purpose }, { id: `p6-command-request-${index}` }), { stopReason: "toolUse" });
    });
    responses.push((context) => {
      commandRequestId = value(context).command_request_id;
      return fauxAssistantMessage(fauxToolCall("bash", { argv: ["python3", "-c", "import time;time.sleep(2)"] }, { id: `p6-command-wait-${index}` }), { stopReason: "toolUse" });
    });
    responses.push((context) => {
      value(context);
      return fauxAssistantMessage(fauxToolCall("project_command_status", { commandRequestId }, { id: `p6-command-status-${index}` }), { stopReason: "toolUse" });
    });
  });
  responses.push((context) => {
    const status = value(context);
    if (status.state !== commands.at(-1)?.state) throw new Error("rejected command status is wrong");
    return fauxAssistantMessage(fauxToolCall("request_project_command", { operation: "host.controller-status", purpose: "p6 installed stale after run" }, { id: "p6-command-stale" }), { stopReason: "toolUse" });
  });
  responses.push((context) => {
    if (!value(context).command_request_id) throw new Error("stale command request was not created");
    return fauxAssistantMessage("P6_FINAL", { stopReason: "stop" });
  });
  faux.setResponses(responses);
  pi.registerProvider(faux.provider);
}
