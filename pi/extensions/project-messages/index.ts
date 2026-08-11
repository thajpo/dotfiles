import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const CHANNEL_SYMBOL = Symbol.for("pi.controllerChannel.v1");
type Channel = { registerTool(operation: string, definition: Parameters<ExtensionAPI["registerTool"]>[0]): void; request(operation: string, payload: Record<string, unknown>, signal?: AbortSignal): Promise<unknown> };
const ID = (prefix: string) => `^${prefix}_[0-9a-f]{32}$`;

function channel(): Channel {
  const value = (globalThis as unknown as Record<symbol, Channel>)[CHANNEL_SYMBOL];
  if (!value) throw new Error("authenticated controller channel is unavailable");
  return value;
}

function result(value: unknown) { return { content: [{ type: "text" as const, text: JSON.stringify(value) }], details: {} }; }

export default function projectMessages(_pi: ExtensionAPI): void {
  const controller = channel();
  controller.registerTool("message.post", {
    name: "post_project_message", label: "Post project message", description: "Post a durable message in the controller-authenticated project and run.",
    parameters: Type.Object({ kind: Type.Union([Type.Literal("progress"), Type.Literal("needs-user"), Type.Literal("decision-reply"), Type.Literal("review-requested"), Type.Literal("failure"), Type.Literal("interrupted"), Type.Literal("submitted-change"), Type.Literal("package-review-required"), Type.Literal("package-review-complete")]), payload: Type.Record(Type.String({ maxLength: 128 }), Type.Unknown()), idempotencyKey: Type.String({ minLength: 1, maxLength: 256 }), requestId: Type.Optional(Type.String({ maxLength: 256 })), replyToMessageId: Type.Optional(Type.String({ pattern: ID("msg") })) }, { additionalProperties: false }),
    async execute(_id, params, signal) { return result(await controller.request("message.post", params, signal)); },
  });
  controller.registerTool("message.list", {
    name: "list_project_messages", label: "List project messages", description: "List messages in the controller-authenticated project.",
    parameters: Type.Object({ states: Type.Optional(Type.Array(Type.Union([Type.Literal("pending"), Type.Literal("delivered"), Type.Literal("acknowledged"), Type.Literal("resolved")]), { maxItems: 4 })), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 256 })) }, { additionalProperties: false }),
    async execute(_id, params, signal) { return result(await controller.request("message.list", params, signal)); },
  });
  controller.registerTool("message.acknowledge", {
    name: "acknowledge_project_message", label: "Acknowledge project message", description: "Acknowledge or resolve one message in the controller-authenticated project.",
    parameters: Type.Object({ messageId: Type.String({ pattern: ID("msg") }), resolve: Type.Optional(Type.Boolean()) }, { additionalProperties: false }),
    async execute(_id, params, signal) { return result(await controller.request("message.acknowledge", params, signal)); },
  });
  controller.registerTool("message.reply", {
    name: "reply_project_message", label: "Reply to project message", description: "Reply to one message using the authenticated conversation and run.",
    parameters: Type.Object({ targetMessageId: Type.String({ pattern: ID("msg") }), payload: Type.Record(Type.String({ maxLength: 128 }), Type.Unknown()), idempotencyKey: Type.String({ minLength: 1, maxLength: 256 }) }, { additionalProperties: false }),
    async execute(_id, params, signal) { return result(await controller.request("message.reply", params, signal)); },
  });
}
