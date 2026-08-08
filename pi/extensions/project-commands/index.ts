import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import path from "node:path";

function controller(): string {
  const value = process.env.PI_SYSTEM_CONTROL;
  if (!value || !path.isAbsolute(value) || path.basename(value) !== "pi-control") throw new Error("Pi system controller path is missing");
  return value;
}

function id(name: string, prefix: string): string {
  const value = process.env[name] ?? "";
  if (!new RegExp(`^${prefix}_[0-9a-f]{32}$`).test(value)) throw new Error(`missing ${name}`);
  return value;
}

async function invoke(pi: ExtensionAPI, command: string, request: Record<string, unknown>, signal: AbortSignal): Promise<string> {
  const state = process.env.PI_SYSTEM_STATE_ROOT;
  const args = state ? ["--state-root", state, "command", command] : ["command", command];
  const result = await pi.exec("python3", [controller(), ...args, "--request-json", JSON.stringify(request)], { signal, timeout: 30_000 });
  if (result.code !== 0) throw new Error((result.stderr || result.stdout || "command operation failed").trim().slice(0, 1024));
  return result.stdout.trim();
}

export default function projectCommands(pi: ExtensionAPI): void {
  pi.registerTool({
    name: "request_project_command",
    label: "Request project command",
    description: "Create an exact, bounded host or container-network command request. Approval and one-use consumption remain controller/user operations.",
    parameters: Type.Object({
      executionPlace: Type.Union([Type.Literal("container-network"), Type.Literal("host")]),
      command: Type.Array(Type.String({ minLength: 1, maxLength: 4096 }), { minItems: 1, maxItems: 128 }),
      workingDirectory: Type.String({ minLength: 1, maxLength: 4096 }),
      requiredResource: Type.String({ minLength: 1, maxLength: 512 }),
      purpose: Type.String({ minLength: 1, maxLength: 2048 }),
      expectedEffect: Type.String({ minLength: 1, maxLength: 4096 }),
      changeScope: Type.Record(Type.String({ maxLength: 128 }), Type.Unknown()),
      expectedOutput: Type.Optional(Type.String({ maxLength: 4096 })),
      sensitiveOutput: Type.Optional(Type.Boolean()),
      expectedDurationMs: Type.Optional(Type.Integer({ minimum: 1, maximum: 3600000 })),
    }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      const projectId = id("PI_SYSTEM_PROJECT_ID", "prj");
      const conversationId = id("PI_SYSTEM_CONVERSATION_ID", "conv");
      const runId = id("PI_SYSTEM_RUN_ID", "run");
      const writerGeneration = Number(process.env.PI_SYSTEM_WRITER_GENERATION ?? "");
      if (!Number.isSafeInteger(writerGeneration) || writerGeneration < 1) throw new Error("project command requires a writer generation");
      const value = await invoke(pi, "request", { projectId, conversationId, runId, writerGeneration, ...params }, signal);
      return { content: [{ type: "text", text: value }], details: {} };
    },
  });

  pi.registerTool({
    name: "execute_project_command",
    label: "Execute approved project command",
    description: "Consume one exact user approval and execute the recorded host or container-network command within its assigned working-copy boundary.",
    parameters: Type.Object({
      commandRequestId: Type.String({ pattern: "^cmd_[0-9a-f]{32}$" }),
      requestDigest: Type.String({ minLength: 64, maxLength: 64 }),
    }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      const projectId = id("PI_SYSTEM_PROJECT_ID", "prj");
      const value = await invoke(pi, "execute", { projectId, ...params }, signal);
      return { content: [{ type: "text", text: value }], details: {} };
    },
  });
}
