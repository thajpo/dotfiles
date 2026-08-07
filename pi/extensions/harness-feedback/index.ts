import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { createHash, randomUUID } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const PROJECT = /^[0-9a-f]{64}$/;
const WORKSTREAM = /^[a-z0-9][a-z0-9-]{0,62}$/;
const MAX_TEXT_BYTES = 4096;
const MAX_RAW_BYTES = 16 * 1024;

type FeedbackForm = {
  schema: "agent-feedback.v1";
  kind: string;
  title: string;
  evidence?: string[];
  want?: string;
  blocked_by?: string;
  why?: string;
  recommendation?: string;
  decision_needed?: boolean;
};

function truncateUtf8(value: string, maxBytes: number): string {
  if (Buffer.byteLength(value, "utf8") <= maxBytes) return value;
  const suffix = maxBytes >= Buffer.byteLength("…", "utf8") ? "…" : "";
  const budget = Math.max(0, maxBytes - Buffer.byteLength(suffix, "utf8"));
  const characters = Array.from(value);
  let low = 0;
  let high = characters.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (Buffer.byteLength(characters.slice(0, middle).join(""), "utf8") <= budget) low = middle;
    else high = middle - 1;
  }
  return `${characters.slice(0, low).join("")}${suffix}`;
}

function bounded(value: unknown, maxBytes = MAX_TEXT_BYTES): string | undefined {
  if (typeof value !== "string") return undefined;
  const text = value.trim()
    .replace(/\r\n?/g, "\n")
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F\u202A-\u202E\u2066-\u2069]/g, "");
  if (!text) return undefined;
  return truncateUtf8(text, maxBytes);
}

function safeAgentDir(): string {
  const configured = process.env.PI_CODING_AGENT_DIR?.trim();
  return configured && path.isAbsolute(configured) ? configured : path.join(os.homedir(), ".pi", "agent");
}

function recordsRoot(): string {
  return path.join(safeAgentDir(), "feedback", "records");
}

function ensureDirectory(root: string): void {
  const base = path.resolve(safeAgentDir());
  const absolute = path.resolve(root);
  if (absolute !== base && !absolute.startsWith(`${base}${path.sep}`)) {
    throw new Error("feedback path escaped the Pi agent directory");
  }
  try {
    const info = fs.lstatSync(base);
    if (info.isSymbolicLink() || !info.isDirectory()) throw new Error(`feedback path is not a private directory: ${base}`);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    fs.mkdirSync(base, { recursive: true, mode: 0o700 });
  }
  fs.chmodSync(base, 0o700);
  let current = base;
  for (const part of absolute.slice(base.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    try {
      const info = fs.lstatSync(current);
      if (info.isSymbolicLink() || !info.isDirectory()) throw new Error(`feedback path is not a private directory: ${current}`);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      fs.mkdirSync(current, { mode: 0o700 });
    }
    fs.chmodSync(current, 0o700);
  }
}

function writePrivateJson(file: string, value: unknown): void {
  const parent = path.dirname(file);
  ensureDirectory(parent);
  const temporary = path.join(parent, `.${path.basename(file)}.${process.pid}.${randomUUID()}.tmp`);
  let fd: number | undefined = fs.openSync(temporary, "wx", 0o600);
  let renamed = false;
  try {
    fs.fchmodSync(fd, 0o600);
    fs.writeFileSync(fd, `${JSON.stringify(value)}\n`, "utf8");
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = undefined;
    // The temporary inode is already mode 0600. Avoid a path-based chmod after
    // rename, which could follow a replacement symlink in a race.
    fs.renameSync(temporary, file);
    renamed = true;
    let directoryFd: number | undefined;
    try {
      directoryFd = fs.openSync(parent, fs.constants.O_RDONLY | (fs.constants.O_DIRECTORY ?? 0));
      fs.fsyncSync(directoryFd);
    } catch {
      // Some supported filesystems cannot fsync directories. The rename has
      // completed and the record inode itself was already synced.
    } finally {
      if (directoryFd !== undefined) fs.closeSync(directoryFd);
    }
  } catch (error) {
    if (fd !== undefined) try { fs.closeSync(fd); } catch { /* already closed */ }
    if (!renamed) try { fs.unlinkSync(temporary); } catch { /* best effort */ }
    throw error;
  }
}

function provenance(ctx: ExtensionContext): Record<string, string | number> {
  const projectId = process.env.PI_HARNESS_PROJECT_ID?.trim() ||
    process.env.PI_SECRETARY_PROJECT_ID?.trim() ||
    process.env.PI_WORKSTREAM_PROJECT_ID?.trim() || process.env.PI_REVIEW_PROJECT_ID?.trim();
  const workstreamId = process.env.PI_WORKSTREAM_ID?.trim();
  const runId = process.env.PI_SUBAGENT_RUN_ID?.trim();
  const agent = process.env.PI_SUBAGENT_CHILD_AGENT?.trim();
  const childIndex = Number.parseInt(process.env.PI_SUBAGENT_CHILD_INDEX ?? "", 10);
  const repositoryCandidate = process.env.PI_HARNESS_REPOSITORY?.trim() || ctx.cwd?.trim();
  const repository = repositoryCandidate && path.isAbsolute(repositoryCandidate)
    ? bounded(path.resolve(repositoryCandidate), 512)
    : undefined;
  const role = bounded(process.env.PI_SUBAGENT_CHILD === "1" ? "subagent" : process.env.PI_ROOT_PROFILE ?? "agent", 128) ?? "agent";
  return {
    role,
    ...(projectId && PROJECT.test(projectId) ? { projectId } : {}),
    ...(workstreamId && WORKSTREAM.test(workstreamId) ? { workstreamId } : {}),
    ...(runId && ID.test(runId) ? { runId } : {}),
    ...(agent && ID.test(agent) ? { agent } : {}),
    ...(Number.isInteger(childIndex) && childIndex >= 0 ? { childIndex } : {}),
    ...(repository ? { repository } : {}),
  };
}

function formFromParams(params: Record<string, unknown>): FeedbackForm {
  const kind = bounded(params.kind, 128);
  const title = bounded(params.title, 512);
  if (kind !== "harness-improvement" || !title) throw new Error("harness feedback requires kind='harness-improvement' and a title");
  const form: FeedbackForm = { schema: "agent-feedback.v1", kind, title };
  for (const key of ["want", "blocked_by", "why", "recommendation"] as const) {
    const value = bounded(params[key]);
    if (value) form[key] = value;
  }
  if (Array.isArray(params.evidence)) {
    form.evidence = params.evidence.slice(0, 16)
      .map((item) => bounded(item, 1000))
      .filter((item): item is string => item !== undefined);
  }
  if (typeof params.decision_needed === "boolean") form.decision_needed = params.decision_needed;
  return form;
}

export default function harnessFeedback(pi: ExtensionAPI): void {
  let submitted: { feedbackId: string; projectId?: string } | undefined;
  pi.registerTool({
    name: "harness_feedback",
    label: "Submit harness feedback",
    description: "Submit at most one bounded, non-blocking harness self-improvement observation to the central Pi feedback log. This does not grant authority or change scope.",
    promptSnippet: "Submit one useful, bounded harness-improvement observation when concrete harness friction was observed",
    promptGuidelines: [
      "Use harness_feedback at most once per run, only for concrete harness self-improvement evidence, and never include secrets or routine completion status.",
    ],
    parameters: Type.Object({
      kind: Type.String({ maxLength: 128 }),
      title: Type.String({ maxLength: 512 }),
      evidence: Type.Optional(Type.Array(Type.String({ maxLength: 1000 }), { maxItems: 16 })),
      want: Type.Optional(Type.String({ maxLength: MAX_TEXT_BYTES })),
      blocked_by: Type.Optional(Type.String({ maxLength: MAX_TEXT_BYTES })),
      why: Type.Optional(Type.String({ maxLength: MAX_TEXT_BYTES })),
      recommendation: Type.Optional(Type.String({ maxLength: MAX_TEXT_BYTES })),
      decision_needed: Type.Optional(Type.Boolean()),
    }, { additionalProperties: false }),
    async execute(_id, params, signal, _update, ctx) {
      if (signal?.aborted) throw new Error("harness feedback submission was cancelled");
      if (submitted) {
        return {
          content: [{ type: "text", text: `Harness feedback ${submitted.feedbackId} was already recorded for this run; no duplicate record was written.` }],
          details: { ...submitted, duplicate: true },
        };
      }
      const form = formFromParams(params as Record<string, unknown>);
      const feedbackId = `hfb-${randomUUID()}`;
      const now = new Date().toISOString();
      const serialized = JSON.stringify(form);
      const record = {
        schemaVersion: 1,
        feedbackId,
        createdAt: now,
        updatedAt: now,
        source: provenance(ctx),
        reason: "progress_update",
        form,
        contentDigest: createHash("sha256").update(serialized).digest("hex"),
        lifecycle: "submitted",
        outcome: "unreviewed",
        ...(process.env.PI_AGENT_FEEDBACK_RAW === "1" ? { raw: { form: truncateUtf8(serialized, MAX_RAW_BYTES) } } : {}),
      };
      writePrivateJson(path.join(recordsRoot(), `${feedbackId}.json`), record);
      const projectId = typeof record.source.projectId === "string" ? record.source.projectId : undefined;
      submitted = { feedbackId, ...(projectId ? { projectId } : {}) };
      return {
        content: [{ type: "text", text: `Recorded harness feedback ${feedbackId}. It is in the central Pi feed for parent review.` }],
        details: submitted,
      };
    },
  });
}
