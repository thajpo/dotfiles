import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const CHANNEL_SYMBOL = Symbol.for("pi.controllerChannel.v1");
type Channel = { registerTool(operation: string, definition: Parameters<ExtensionAPI["registerTool"]>[0]): void; request(operation: string, payload: Record<string, unknown>, signal?: AbortSignal): Promise<unknown> };

function channel(): Channel {
  const value = (globalThis as unknown as Record<symbol, Channel>)[CHANNEL_SYMBOL];
  if (!value) throw new Error("authenticated controller channel is unavailable");
  return value;
}

function result(value: unknown) { return { content: [{ type: "text" as const, text: JSON.stringify(value) }], details: {} }; }

export default function secretaryWork(_pi: ExtensionAPI, ctx: ExtensionContext): void {
  const controller = channel();

  controller.registerTool("project.work-index", {
    name: "project_work_index",
    label: "Project work index",
    description: "Inspect the authenticated project's work index: working conversations, investigations, changes awaiting review or merge, attention, recently integrated work, and unmanaged Git work. Refreshes controller and Git observations.",
    parameters: Type.Object({}, { additionalProperties: false }),
    async execute(_id, _params, signal) { return result(await controller.request("project.work-index", {}, signal)); },
  });

  controller.registerTool("investigation.start", {
    name: "start_investigation",
    label: "Start investigation",
    description: "Start one bounded read-only investigation on an immutable snapshot of the authenticated project and return its durable result. The investigation cannot write; completion, failure, interruption, and needs-user outcomes are durable controller state.",
    parameters: Type.Object({ purpose: Type.String({ minLength: 1, maxLength: 4096 }), idempotencyKey: Type.String({ minLength: 1, maxLength: 256 }) }, { additionalProperties: false }),
    async execute(_id, params, signal) { return result(await controller.request("subagent.spawn", { role: "investigator", task: params.purpose, idempotencyKey: params.idempotencyKey }, signal)); },
  });

  controller.registerTool("workstream.propose", {
    name: "propose_workstream",
    label: "Propose workstream",
    description: "Propose a named, durable implementation workstream for the project. This records the exact creation intent and posts a needs-user proposal; no worktree exists until the user approves the exact request interactively.",
    parameters: Type.Object({ title: Type.String({ minLength: 1, maxLength: 200 }), purpose: Type.String({ minLength: 1, maxLength: 4096 }), targetRef: Type.Optional(Type.String({ maxLength: 256 })), knownOverlap: Type.Optional(Type.String({ maxLength: 1024 })), idempotencyKey: Type.String({ minLength: 1, maxLength: 256 }) }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      return result(await controller.request("workstream.propose", {
        title: params.title, purpose: params.purpose,
        targetRef: params.targetRef ?? "", knownOverlap: params.knownOverlap ?? "",
        idempotencyKey: params.idempotencyKey,
      }, signal));
    },
  });

  controller.registerTool("workstream.approve", {
    name: "approve_workstream",
    label: "Approve workstream",
    description: "Approve one pending workstream proposal after the interactive confirmation card. The user must approve the exact proposal in the interactive UI; a generic yes or message acknowledgement is never approval. Approval issues a one-use authorization; the workstream is created only after approval.",
    parameters: Type.Object({ messageId: Type.String({ minLength: 1, maxLength: 200 }) }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      if (!ctx.hasUI) throw new Error("Workstream approval requires an interactive secretary session.");
      const listing = await controller.request("message.list", { states: ["pending", "delivered"], limit: 64 }, signal) as { messages?: Array<{ message_id: string; kind: string; payload_json: string }> };
      const proposal = (listing.messages ?? []).find((item) => item.message_id === params.messageId && item.kind === "needs-user");
      if (!proposal) throw new Error("Workstream proposal message was not found.");
      const payload = JSON.parse(proposal.payload_json ?? "{}") as Record<string, string>;
      const approved = await ctx.ui.confirm(
        "Approve workstream creation?",
        [
          `Title: ${payload.title ?? ""}`,
          `Purpose: ${payload.purpose ?? ""}`,
          `Target: ${payload.targetRef ?? "current branch"}`,
          `Base commit: ${payload.baseOid ?? ""}`,
          payload.knownOverlap ? `Known overlap: ${payload.knownOverlap}` : "",
          "A separate controller-owned worktree and headful conversation will be created. Creation does not integrate anything.",
        ].filter(Boolean).join("\n"),
      );
      if (!approved) throw new Error("The user rejected the workstream proposal.");
      const authorized = await controller.request("workstream.approve", { messageId: params.messageId }, signal) as { authorizationId: string };
      const applied = await controller.request("workstream.apply", { messageId: params.messageId, authorizationId: authorized.authorizationId }, signal);
      return result(applied);
    },
  });

  controller.registerTool("review.propose", {
    name: "propose_review",
    label: "Propose review",
    description: "Propose an exact-revision review of one submitted change. This posts a durable needs-user proposal; it does not create the review. The user approves on the surface, and only then does the controller create the exact-revision review assignment.",
    parameters: Type.Object({ changeId: Type.String({ minLength: 1, maxLength: 200 }), revision: Type.Integer({ minimum: 1 }), reason: Type.Optional(Type.String({ maxLength: 1024 })), idempotencyKey: Type.String({ minLength: 1, maxLength: 256 }) }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      return result(await controller.request("message.post", {
        kind: "needs-user",
        payload: { proposal: "review", changeId: params.changeId, revision: params.revision, reason: params.reason ?? "" },
        idempotencyKey: params.idempotencyKey,
      }, signal));
    },
  });

  controller.registerTool("integration.propose", {
    name: "propose_integration",
    label: "Propose integration",
    description: "Propose integrating one exact submitted change revision into its target. This posts a durable needs-user proposal; it does not integrate anything. Integration always requires a separate exact user authorization and revalidates the target immediately before compare-and-swap.",
    parameters: Type.Object({ changeId: Type.String({ minLength: 1, maxLength: 200 }), revision: Type.Integer({ minimum: 1 }), strategy: Type.Optional(Type.String({ maxLength: 64 })), idempotencyKey: Type.String({ minLength: 1, maxLength: 256 }) }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      return result(await controller.request("message.post", {
        kind: "needs-user",
        payload: { proposal: "integration", changeId: params.changeId, revision: params.revision, strategy: params.strategy ?? "" },
        idempotencyKey: params.idempotencyKey,
      }, signal));
    },
  });
}
