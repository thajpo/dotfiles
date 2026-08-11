import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const CHANNEL_SYMBOL = Symbol.for("pi.controllerChannel.v1");
type Channel = { registerTool(operation: string, definition: Parameters<ExtensionAPI["registerTool"]>[0]): void; request(operation: string, payload: Record<string, unknown>, signal?: AbortSignal): Promise<unknown> };
function channel(): Channel { const value = (globalThis as unknown as Record<symbol, Channel>)[CHANNEL_SYMBOL]; if (!value) throw new Error("authenticated controller channel is unavailable"); return value; }
function result(value: unknown) { return { content: [{ type: "text" as const, text: JSON.stringify(value) }], details: {} }; }

export default function projectCommands(_pi: ExtensionAPI): void {
  const controller = channel();
  controller.registerTool("command.request", {
    name: "request_project_command", label: "Request sensitive operation", description: "Request one structured host or network operation. This tool cannot approve or execute it.",
    parameters: Type.Object({ operation: Type.Union([Type.Literal("host.controller-status"), Type.Literal("host.fixture-success"), Type.Literal("host.fixture-failure"), Type.Literal("host.fixture-timeout"), Type.Literal("network.namespace-probe")]), purpose: Type.String({ minLength: 1, maxLength: 2048 }) }, { additionalProperties: false }),
    async execute(_id, params, signal) { return result(await controller.request("command.request", params, signal)); },
  });
  controller.registerTool("command.status", {
    name: "project_command_status", label: "Sensitive operation status", description: "Read status for one request in the authenticated project.",
    parameters: Type.Object({ commandRequestId: Type.String({ pattern: "^cmd_[0-9a-f]{32}$" }) }, { additionalProperties: false }),
    async execute(_id, params, signal) { return result(await controller.request("command.status", params, signal)); },
  });
}
