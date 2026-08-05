import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import fs from "node:fs";

const MARKER = "workstream-brief-seeded-v1";
const ID = /^[a-z0-9][a-z0-9-]{0,62}$/;
const PROJECT_ID = /^[0-9a-f]{64}$/;

function alreadySeeded(ctx: ExtensionContext, workstreamId: string): boolean {
  try {
    return (ctx.sessionManager.getBranch() as unknown as Array<Record<string, unknown>>).some((entry) =>
      entry.type === "custom" && entry.customType === MARKER &&
      (entry.data as Record<string, unknown> | undefined)?.workstreamId === workstreamId);
  } catch {
    return false;
  }
}

export default function workstreamBrief(pi: ExtensionAPI): void {
  pi.on("before_agent_start", (event) => {
    const workstreamId = process.env.PI_WORKSTREAM_ID ?? "";
    const projectId = process.env.PI_WORKSTREAM_PROJECT_ID ?? "";
    const briefPath = process.env.PI_WORKSTREAM_BRIEF_PATH ?? "";
    if (!ID.test(workstreamId) || !PROJECT_ID.test(projectId) || !briefPath.startsWith("/")) return;
    return {
      systemPrompt: event.systemPrompt + "\n\nYou are the headful implementation worker for the host-assigned workstream " + workstreamId + ". The user is speaking directly to you; implement the approved brief in this assigned worktree and do not wait for the secretary to relay ordinary user conversation. The project secretary remains a supervisory peer. Send concise milestone updates with notify_secretary({ kind: \"progress\", ... }), and use needs-user or review-requested when the secretary must act. Include bounded AGENT_FEEDBACK JSON in progress details for useful risks or improvement suggestions, but never silently promote feedback into memory or project ideas. Preserve the assigned repository boundary and do not commit, push, or modify another worktree unless the user and project workflow explicitly authorize it.\n",
    };
  });

  pi.on("session_start", async (_event, ctx) => {
    const workstreamId = process.env.PI_WORKSTREAM_ID ?? "";
    const projectId = process.env.PI_WORKSTREAM_PROJECT_ID ?? "";
    const briefPath = process.env.PI_WORKSTREAM_BRIEF_PATH ?? "";
    if (!workstreamId && !briefPath) return;
    if (!ID.test(workstreamId) || !PROJECT_ID.test(projectId) || !briefPath.startsWith("/") || alreadySeeded(ctx, workstreamId)) return;
    const info = fs.lstatSync(briefPath);
    if (!info.isFile() || info.isSymbolicLink() || info.size > 20 * 1024) {
      throw new Error("workstream brief is not a bounded regular file");
    }
    const brief = fs.readFileSync(briefPath, "utf8");
    pi.appendEntry(MARKER, { projectId, workstreamId, seededAt: new Date().toISOString() });
    pi.setSessionName(`workstream-${workstreamId}`);
    pi.sendUserMessage(
      `Host-assigned workstream brief (supplied once):\n\n${brief}\n\n` +
      "Begin by stating the received outcome, settled decisions and boundaries, open questions, and first inspection. Do not ask the user to restate the task.",
    );
  });
}
