import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import path from "node:path";

const MARKER = "workstream-brief-seeded-v1";
const PROJECT = /^[0-9a-f]{64}$/;
const WORKSTREAM = /^[a-z0-9][a-z0-9-]{0,62}$/;

type Identity = { projectId: string; workstreamId: string };

function identity(ctx: ExtensionContext): Identity {
  const entries = ctx.sessionManager.getBranch() as unknown as Array<Record<string, unknown>>;
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry.type !== "custom" || entry.customType !== MARKER) continue;
    const data = entry.data as Record<string, unknown> | undefined;
    const projectId = String(data?.projectId ?? "");
    const workstreamId = String(data?.workstreamId ?? "");
    if (PROJECT.test(projectId) && WORKSTREAM.test(workstreamId)) return { projectId, workstreamId };
  }
  throw new Error("this Pi session is not a host-assigned workstream");
}

function controlPath(): string {
  const configured = process.env.PI_WORKSTREAM_CONTROL;
  const agentDir = process.env.PI_CODING_AGENT_DIR ?? path.join(process.env.HOME ?? "", ".pi", "agent");
  const value = configured ?? path.join(agentDir, "share", "pi", "control", "pi-secretary-control.py");
  if (!value.startsWith("/") || path.basename(value) !== "pi-secretary-control.py") {
    throw new Error("invalid secretary control path");
  }
  return value;
}

export default function workstreamChannel(pi: ExtensionAPI): void {
  pi.registerTool({
    name: "notify_secretary", label: "Notify project secretary",
    description: "Post one bounded progress, needs-user, review-requested, or referral event. This is not general agent chat and does not create a user turn.",
    parameters: Type.Object({
      kind: Type.Union([Type.Literal("progress"), Type.Literal("needs-user"), Type.Literal("review-requested"), Type.Literal("referral")]),
      summary: Type.String({ maxLength: 500 }), details: Type.Optional(Type.String({ maxLength: 4096 })),
    }),
    async execute(_id, params, signal, _update, ctx) {
      const current = identity(ctx);
      const result = await pi.exec("python3", [controlPath(), "notify",
        "--project-id", current.projectId, "--workstream-id", current.workstreamId,
        "--kind", params.kind, "--summary", params.summary, "--details", params.details ?? ""],
        { signal, timeout: 30_000 });
      if (result.code !== 0) throw new Error((result.stderr || result.stdout || "notification failed").trim());
      return { content: [{ type: "text", text: result.stdout.trim() }], details: {} };
    },
  });
}
