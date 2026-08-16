import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const CHANNEL_SYMBOL = Symbol.for("pi.controllerChannel.v1");
type Channel = { registerTool(operation: string, definition: Parameters<ExtensionAPI["registerTool"]>[0]): void; request(operation: string, payload: Record<string, unknown>, signal?: AbortSignal): Promise<unknown> };

function channel(): Channel {
  const value = (globalThis as unknown as Record<symbol, Channel>)[CHANNEL_SYMBOL];
  if (!value) throw new Error("authenticated controller channel is unavailable");
  return value;
}

function result(value: unknown) { return { content: [{ type: "text" as const, text: JSON.stringify(value) }], details: {} }; }

export default function observability(_pi: ExtensionAPI): void {
  const controller = channel();

  controller.registerTool("observe.tasks", {
    name: "observe_tasks",
    label: "Observe tasks",
    description: "Inspect the authenticated project's work index: working conversations, active runs, changes awaiting review or merge, attention, recently integrated work, and unmanaged Git work. Derived from controller records, never pane text or markers.",
    parameters: Type.Object({}, { additionalProperties: false }),
    async execute(_id, _params, signal) {
      return result(await controller.request("project.work-index", {}, signal));
    },
  });

  controller.registerTool("observe.fleet", {
    name: "observe_fleet",
    label: "Observe fleet",
    description: "Inspect the recent controller-created children of the authenticated run with their states and terminal records.",
    parameters: Type.Object({}, { additionalProperties: false }),
    async execute(_id, _params, signal) {
      return result(await controller.request("subagent.list", {}, signal));
    },
  });

  controller.registerTool("observe.messages", {
    name: "observe_messages",
    label: "Observe messages",
    description: "Inspect the authenticated project's durable message inbox (pending, delivered, acknowledged, resolved) including parent-child escalations.",
    parameters: Type.Object({ states: Type.Optional(Type.Array(Type.Union([Type.Literal("pending"), Type.Literal("delivered"), Type.Literal("acknowledged"), Type.Literal("resolved")]), { maxItems: 4 })), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 256 })) }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      const payload: Record<string, unknown> = { limit: params.limit ?? 64 };
      if (params.states !== undefined) payload.states = params.states;
      return result(await controller.request("message.list", payload, signal));
    },
  });

  controller.registerTool("observe.queue", {
    name: "observe_change_queue",
    label: "Observe change queue",
    description: "Inspect the authenticated project's change queue: drafts, open revisions, merged, and closed changes.",
    parameters: Type.Object({}, { additionalProperties: false }),
    async execute(_id, _params, signal) {
      return result(await controller.request("change.list", {}, signal));
    },
  });
}
