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

export default function secretaryWork(_pi: ExtensionAPI): void {
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
    description: "Propose a named, durable implementation workstream for the project. This posts a durable needs-user proposal; it does not create the workstream. The user approves on the surface, and only then does the controller create the worktree, conversation, run, and runtime.",
    parameters: Type.Object({ title: Type.String({ minLength: 1, maxLength: 200 }), purpose: Type.String({ minLength: 1, maxLength: 4096 }), targetRef: Type.Optional(Type.String({ maxLength: 256 })), knownOverlap: Type.Optional(Type.String({ maxLength: 1024 })), idempotencyKey: Type.String({ minLength: 1, maxLength: 256 }) }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      return result(await controller.request("message.post", {
        kind: "needs-user",
        payload: { proposal: "workstream", title: params.title, purpose: params.purpose, targetRef: params.targetRef ?? "", knownOverlap: params.knownOverlap ?? "" },
        idempotencyKey: params.idempotencyKey,
      }, signal));
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
