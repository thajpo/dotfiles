import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import path from "node:path";

const PROJECT = /^[0-9a-f]{64}$/;
const ID = /^[a-z0-9][a-z0-9-]{0,62}$/;
const OID = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;
const REVIEW_ENVIRONMENT_KEYS = [
  "PI_REVIEW_PROJECT_ID",
  "PI_REVIEW_REQUEST_ID",
  "PI_REVIEW_SESSION_ID",
  "PI_REVIEW_CANDIDATE_OID",
  "PI_REVIEW_BASE_OID",
  "PI_REVIEW_CAPABILITY",
  "PI_REVIEW_CONTROL",
] as const;

function environment() {
  const value = { projectId: process.env.PI_REVIEW_PROJECT_ID ?? "", requestId: process.env.PI_REVIEW_REQUEST_ID ?? "",
    sessionId: process.env.PI_REVIEW_SESSION_ID ?? "", candidate: process.env.PI_REVIEW_CANDIDATE_OID ?? "",
    base: process.env.PI_REVIEW_BASE_OID ?? "", capability: process.env.PI_REVIEW_CAPABILITY ?? "",
    control: process.env.PI_REVIEW_CONTROL ?? "" };
  if (!PROJECT.test(value.projectId) || !ID.test(value.requestId) || !OID.test(value.candidate) || !OID.test(value.base) ||
      !value.sessionId || !value.capability || !value.control.startsWith("/") || path.basename(value.control) !== "pi-secretary-control.py") {
    throw new Error("invalid immutable review assignment");
  }
  return value;
}

function reviewAssignment() {
  if (!REVIEW_ENVIRONMENT_KEYS.some((key) => process.env[key] !== undefined)) return undefined;
  return environment();
}

export default function reviewReceipt(pi: ExtensionAPI): void {
  // This extension is auto-discovered globally but only has meaning in the
  // explicitly assigned review process. Ordinary Pi sessions must not try to
  // manufacture review metadata or expose a receipt tool.
  const assigned = reviewAssignment();
  if (!assigned) return;

  pi.on("before_agent_start", (event) => {
    return { systemPrompt: event.systemPrompt + `\n\nYou are an independent mechanically read-only reviewer assigned to exact commit ${assigned.candidate} against base ${assigned.base}. ` +
      "Inspect only this checkout. Do not implement or modify anything. Report correctness, regressions, security boundaries, test evidence, and scope. " +
      "Submit exactly one receipt when finished; acceptance means no blocker/high issue remains for this exact commit. Later feature commits make the receipt stale." };
  });
  pi.registerTool({
    name: "submit_review_receipt", label: "Submit exact-OID review receipt",
    description: "Submit the final accept/reject receipt bound by the host to this immutable checkout.",
    parameters: Type.Object({ verdict: Type.Union([Type.Literal("accept"), Type.Literal("reject")]),
      summary: Type.String({ maxLength: 1000 }), findings: Type.String({ maxLength: 16384 }) }),
    async execute(_id, params, signal) {
      const assigned = environment();
      const result = await pi.exec("python3", [assigned.control, "review-submit", "--project-id", assigned.projectId,
        "--request-id", assigned.requestId, "--verdict", params.verdict, "--summary", params.summary, "--findings", params.findings],
        { signal, timeout: 30_000 });
      if (result.code !== 0) throw new Error((result.stderr || result.stdout || "receipt rejected").trim());
      return { content: [{ type: "text", text: result.stdout.trim() }], details: {} };
    },
  });
}
