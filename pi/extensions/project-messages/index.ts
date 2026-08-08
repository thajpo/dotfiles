import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import path from "node:path";

const ID = (prefix: string) => `^${prefix}_[0-9a-f]{32}$`;

function control(): string {
  const value = process.env.PI_SYSTEM_CONTROL;
  if (!value || !path.isAbsolute(value) || path.basename(value) !== "pi-control") throw new Error("Pi system controller path is missing");
  return value;
}

function stateArgs(): string[] {
  const value = process.env.PI_SYSTEM_STATE_ROOT;
  if (!value) return [];
  if (!path.isAbsolute(value) || value.includes("\0")) throw new Error("Pi system state root is invalid");
  return ["--state-root", value];
}

function binding(): { projectId: string; conversationId: string; runId: string; workstreamId?: string; writerGeneration?: number } {
  const projectId = process.env.PI_SYSTEM_PROJECT_ID ?? "";
  const conversationId = process.env.PI_SYSTEM_CONVERSATION_ID ?? "";
  const runId = process.env.PI_SYSTEM_RUN_ID ?? "";
  if (!new RegExp(ID("prj")).test(projectId) || !new RegExp(ID("conv")).test(conversationId) || !new RegExp(ID("run")).test(runId)) throw new Error("Pi system message binding is incomplete");
  const workstreamId = process.env.PI_SYSTEM_WORKSTREAM_ID;
  if (workstreamId && !new RegExp(ID("ws")).test(workstreamId)) throw new Error("Pi system workstream binding is invalid");
  const rawGeneration = process.env.PI_SYSTEM_WRITER_GENERATION;
  const writerGeneration = rawGeneration ? Number(rawGeneration) : undefined;
  if (rawGeneration && (!Number.isSafeInteger(writerGeneration) || writerGeneration! < 1)) throw new Error("Pi system writer generation is invalid");
  return { projectId, conversationId, runId, ...(workstreamId ? { workstreamId } : {}), ...(writerGeneration ? { writerGeneration } : {}) };
}

async function invoke(pi: ExtensionAPI, command: string, request: Record<string, unknown>, signal: AbortSignal): Promise<string> {
  const result = await pi.exec("python3", [control(), ...stateArgs(), "message", command, "--request-json", JSON.stringify(request)], { signal, timeout: 30_000 });
  if (result.code !== 0) throw new Error((result.stderr || result.stdout || "message operation failed").trim().slice(0, 1024));
  return result.stdout.trim();
}

export default function projectMessages(pi: ExtensionAPI): void {
  pi.registerTool({
    name: "post_project_message",
    label: "Post project message",
    description: "Write one durable, idempotent project-scoped progress, decision, review, or attention message through the controller.",
    parameters: Type.Object({
      kind: Type.Union([Type.Literal("progress"), Type.Literal("needs-user"), Type.Literal("decision-reply"), Type.Literal("review-requested"), Type.Literal("failure"), Type.Literal("interrupted"), Type.Literal("submitted-change"), Type.Literal("package-review-required"), Type.Literal("package-review-complete")]),
      payload: Type.Record(Type.String({ maxLength: 128 }), Type.Unknown()),
      idempotencyKey: Type.String({ minLength: 1, maxLength: 256 }),
      requestId: Type.Optional(Type.String({ maxLength: 256 })),
      replyToMessageId: Type.Optional(Type.String({ pattern: ID("msg") })),
    }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      const current = binding();
      const value = await invoke(pi, "post", { ...current, kind: params.kind, payload: params.payload, idempotencyKey: params.idempotencyKey, ...(params.requestId ? { requestId: params.requestId } : {}), ...(params.replyToMessageId ? { replyToMessageId: params.replyToMessageId } : {}) }, signal);
      return { content: [{ type: "text", text: value }], details: {} };
    },
  });
}
