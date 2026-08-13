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

export default function changeFlow(_pi: ExtensionAPI): void {
  const controller = channel();

  controller.registerTool("change.submit", {
    name: "submit_change",
    label: "Submit change",
    description: "Submit the authenticated working copy's current state as one immutable change revision for the project queue. The controller captures the exact task delta; submission never integrates, pushes, or cleans anything. Ambiguous personal changes require explicit selection.",
    parameters: Type.Object({
      title: Type.String({ minLength: 1, maxLength: 200 }),
      summary: Type.String({ minLength: 1, maxLength: 4096 }),
      targetRef: Type.String({ minLength: 1, maxLength: 256 }),
      captureMode: Type.Optional(Type.Union([Type.Literal("clean"), Type.Literal("dirty"), Type.Literal("branch-tip"), Type.Literal("temporary-index")])),
      selectedPaths: Type.Optional(Type.Array(Type.String({ minLength: 1, maxLength: 1024 }), { maxItems: 512 })),
      excludedPaths: Type.Optional(Type.Array(Type.String({ minLength: 1, maxLength: 1024 }), { maxItems: 512 })),
      idempotencyKey: Type.String({ minLength: 1, maxLength: 256 }),
    }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      return result(await controller.request("change.submit", { title: params.title, summary: params.summary, targetRef: params.targetRef, captureMode: params.captureMode ?? "dirty", selectedPaths: params.selectedPaths ?? [], excludedPaths: params.excludedPaths ?? [], idempotencyKey: params.idempotencyKey }, signal));
    },
  });

  controller.registerTool("change.list", {
    name: "list_changes",
    label: "List changes",
    description: "List the authenticated project's change queue: drafts, open revisions awaiting review or integration, merged, and closed changes.",
    parameters: Type.Object({}, { additionalProperties: false }),
    async execute(_id, _params, signal) {
      return result(await controller.request("change.list", {}, signal));
    },
  });

  controller.registerTool("review.request", {
    name: "request_review",
    label: "Request review",
    description: "Create one exact-revision review assignment and launch the read-only reviewer on its detached snapshot. The review binds the exact tip and tree; a later revision makes the receipt stale. Requesting a review grants no integration authority.",
    parameters: Type.Object({
      changeId: Type.String({ minLength: 1, maxLength: 200 }),
      revision: Type.Integer({ minimum: 1 }),
    }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      return result(await controller.request("review.request", { changeId: params.changeId, revision: params.revision }, signal));
    },
  });

  controller.registerTool("integration.analyze", {
    name: "analyze_integration",
    label: "Analyze integration",
    description: "Analyze one exact change revision against its current target: ancestry, textual conflicts, overlap with other open changes, and the recommended strategy. Analysis is read-only and never moves the target; integration always requires a separate exact user authorization.",
    parameters: Type.Object({
      changeId: Type.String({ minLength: 1, maxLength: 200 }),
      revision: Type.Integer({ minimum: 1 }),
      targetRef: Type.String({ minLength: 1, maxLength: 256 }),
    }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      return result(await controller.request("integration.analyze", { changeId: params.changeId, revision: params.revision, targetRef: params.targetRef }, signal));
    },
  });
}
