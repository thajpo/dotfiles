/**
 * workflow-state extension
 *
 * Session-scoped current task packet and opt-in context measurement.
 * - Parent sessions replace/show/clear the packet through a `task_packet` custom tool.
 * - Child sessions (PI_SUBAGENT_CHILD=1) must not register the tool or inject packets.
 * - before_agent_start injects the latest packet into the system prompt (parent only).
 * - Opt-in context audit manifests via PI_WORKFLOW_CONTEXT_AUDIT=1.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import { Type } from "typebox";
import type {
  ExtensionAPI,
  ExtensionContext,
  ToolDefinition,
} from "@earendil-works/pi-coding-agent";
import {
  PACKET_ENTRY_TYPE,
  PACKET_SCHEMA_VERSION,
  PACKET_CLEAR_TYPE,
  validatePacket,
  renderCompact,
  latestActivePacket,
  workflowArtifactsDirForSession,
  buildContextManifest,
  computeSha256,
  byteLength,
  MAX_MANIFEST_SAMPLES,
} from "./core.mjs";

// ============================================================================
// Constants
// ============================================================================

const TOOL_NAME = "task_packet";

const CHILD_ENV = "PI_SUBAGENT_CHILD";
const CHILD_AGENT_ENV = "PI_SUBAGENT_CHILD_AGENT";
const CHILD_INDEX_ENV = "PI_SUBAGENT_CHILD_INDEX";
const AUDIT_ENV = "PI_WORKFLOW_CONTEXT_AUDIT";
const AUDIT_RAW_ENV = "PI_WORKFLOW_CONTEXT_AUDIT_RAW";

// ============================================================================
// TypeBox schema for task_packet parameters
// ============================================================================

const TaskPacketParams = Type.Object({
  action: Type.String({
    enum: ["replace", "show", "clear"],
    description: "Action to perform on the task packet",
  }),
  packet: Type.Optional(
    Type.Unsafe({
      type: "object",
      additionalProperties: true,
      description:
        "The task packet (required for replace). Must include task_id, mode (fast|rip|build|major), and learning (off|light|deep) plus mode-required fields.",
    }),
  ),
}, { additionalProperties: false });

// ============================================================================
// Manifest file helpers
// ============================================================================

function getWorkflowArtifactsDir(ctx: ExtensionContext): string | null {
  try {
    const sessionFile = ctx.sessionManager.getSessionFile();
    if (!sessionFile) return null;
    const sessionStat = fs.lstatSync(sessionFile);
    if (!sessionStat.isFile() || sessionStat.isSymbolicLink()) return null;

    const realSessionFile = fs.realpathSync(sessionFile);
    const realSessionDir = path.dirname(realSessionFile);
    const artifactsDir = workflowArtifactsDirForSession(realSessionFile);
    const sessionArtifactDir = path.dirname(artifactsDir);

    if (!fs.existsSync(sessionArtifactDir)) fs.mkdirSync(sessionArtifactDir, { mode: 0o700 });
    const sessionArtifactStat = fs.lstatSync(sessionArtifactDir);
    if (!sessionArtifactStat.isDirectory() || sessionArtifactStat.isSymbolicLink()) return null;
    if (typeof process.getuid === "function" && sessionArtifactStat.uid !== process.getuid()) return null;
    const realSessionArtifactDir = fs.realpathSync(sessionArtifactDir);
    const sessionRelative = path.relative(realSessionDir, realSessionArtifactDir);
    if (sessionRelative === "" || sessionRelative === ".." || sessionRelative.startsWith(`..${path.sep}`) || path.isAbsolute(sessionRelative)) return null;
    fs.chmodSync(realSessionArtifactDir, 0o700);

    if (!fs.existsSync(artifactsDir)) fs.mkdirSync(artifactsDir, { mode: 0o700 });
    const artifactStat = fs.lstatSync(artifactsDir);
    if (!artifactStat.isDirectory() || artifactStat.isSymbolicLink()) return null;
    if (typeof process.getuid === "function" && artifactStat.uid !== process.getuid()) return null;
    const realArtifactsDir = fs.realpathSync(artifactsDir);
    const relative = path.relative(realSessionArtifactDir, realArtifactsDir);
    if (relative === "" || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) return null;
    fs.chmodSync(realArtifactsDir, 0o700);

    if ((fs.statSync(realSessionArtifactDir).mode & 0o777) !== 0o700) return null;
    if ((fs.statSync(realArtifactsDir).mode & 0o777) !== 0o700) return null;
    return realArtifactsDir;
  } catch {
    return null;
  }
}

function writeManifestArtifact(
  artifactsDir: string,
  generationCounter: number,
  manifest: Record<string, unknown>,
): string | null {
  const counter = String(generationCounter).padStart(6, "0");
  const fileName = `context-${counter}-${Date.now()}.json`;
  const filePath = path.join(artifactsDir, fileName);
  const json = JSON.stringify(manifest, null, 2) + "\n";
  const tmpPath = filePath + ".tmp";
  try {
    fs.writeFileSync(tmpPath, json, { mode: 0o600, flag: "wx" });
    fs.chmodSync(tmpPath, 0o600);
    fs.renameSync(tmpPath, filePath);
    fs.chmodSync(filePath, 0o600);
    return filePath;
  } catch {
    try { fs.rmSync(tmpPath, { force: true }); } catch { /* best effort */ }
    return null;
  }
}

function listManifestFiles(artifactsDir: string): string[] {
  try {
    const entries = fs.readdirSync(artifactsDir, { withFileTypes: true });
    return entries
      .filter((e) => e.isFile() && e.name.startsWith("context-") && e.name.endsWith(".json"))
      .map((e) => {
        const file = path.join(artifactsDir, e.name);
        return { file, mtimeMs: fs.statSync(file).mtimeMs };
      })
      .sort((a, b) => a.mtimeMs - b.mtimeMs || a.file.localeCompare(b.file))
      .map((entry) => entry.file);
  } catch {
    return [];
  }
}

function pruneOldManifests(artifactsDir: string): void {
  const files = listManifestFiles(artifactsDir);
  if (files.length <= MAX_MANIFEST_SAMPLES) return;
  for (const f of files.slice(0, files.length - MAX_MANIFEST_SAMPLES)) {
    try { fs.rmSync(f, { force: true }); } catch { /* best effort */ }
  }
}

// ============================================================================
// Packet persistence
// ============================================================================

function persistPacket(pi: ExtensionAPI, packet: Record<string, unknown>): void {
  pi.appendEntry(PACKET_ENTRY_TYPE, { schema_version: PACKET_SCHEMA_VERSION, packet });
}

function persistClear(pi: ExtensionAPI): void {
  pi.appendEntry(PACKET_CLEAR_TYPE, { cleared: true, cleared_at: Date.now() });
}

function reconstructPacket(ctx: ExtensionContext): Record<string, unknown> | null {
  try {
    const entries = ctx.sessionManager.getBranch();
    // SessionEntry[] is not directly assignable to Record<string, unknown>[].
    // Convert through unknown since we only access type/customType/data fields.
    const packet = latestActivePacket(entries as unknown as Array<Record<string, unknown>>);
    return packet && validatePacket(packet).length === 0 ? packet : null;
  } catch {
    return null;
  }
}

// ============================================================================
// Context audit helpers
// ============================================================================

let globalGenerationCounter = 0;

function isContextAuditEnabled(): boolean {
  return process.env[AUDIT_ENV] === "1";
}

function isChildSession(): boolean {
  return process.env[CHILD_ENV] === "1";
}

interface PendingCapture {
  systemPrompt: string;
  messages: Array<{ role: string; content: string }>;
  contextFiles: Array<{ path: string; content: string }>;
  selectedTools: string[] | undefined;
  skillNames: string[] | undefined;
  submittedPrompt: string | undefined;
}

async function captureContextManifest(
  ctx: ExtensionContext,
  pc: PendingCapture,
): Promise<void> {
  const auditRaw = process.env[AUDIT_RAW_ENV] === "1";
  const artifactsDir = getWorkflowArtifactsDir(ctx);
  if (!artifactsDir) return;

  const sessionId = ctx.sessionManager.getSessionId() ?? "unknown";
  const sessionFile = ctx.sessionManager.getSessionFile() ?? undefined;
  const genId = `${Date.now()}-${process.pid}-${++globalGenerationCounter}`;

  const packet = isChildSession() ? null : reconstructPacket(ctx);
  const taskPacketJson = packet ? JSON.stringify(packet) : undefined;

  let contextTokens: number | undefined;
  let contextWindow: number | undefined;
  let contextPercent: number | undefined;
  try {
    const cu = ctx.getContextUsage();
    if (cu) {
      if (typeof cu.tokens === "number") contextTokens = cu.tokens;
      if (typeof cu.contextWindow === "number") contextWindow = cu.contextWindow;
      if (typeof cu.percent === "number") contextPercent = cu.percent;
    }
  } catch { /* best effort */ }

  const modelInfo = ctx.model
    ? { provider: ctx.model.provider, id: ctx.model.id }
    : undefined;

  let thinkingLevel: string | undefined;
  try {
    const tl = ctx.thinkingLevel;
    if (typeof tl === "string") thinkingLevel = tl;
  } catch { /* best effort */ }

  const childAgent = process.env[CHILD_AGENT_ENV] || undefined;
  const childIndexRaw = process.env[CHILD_INDEX_ENV];
  const childIndex = childIndexRaw ? parseInt(childIndexRaw, 10) : undefined;

  const manifest = await buildContextManifest({
    generationId: genId,
    systemPrompt: pc.systemPrompt,
    messages: pc.messages,
    sessionId,
    sessionFile,
    modelInfo,
    thinkingLevel,
    isChild: isChildSession(),
    childAgent,
    childIndex: Number.isFinite(childIndex) ? childIndex : undefined,
    contextUsagePercent: contextPercent,
    contextTokens,
    contextWindow,
    contextFiles: pc.contextFiles,
    selectedTools: pc.selectedTools,
    skillNames: pc.skillNames,
    taskPacketJson,
    taskPacketSize: taskPacketJson ? byteLength(taskPacketJson) : undefined,
    includeRaw: auditRaw,
    submittedPrompt: pc.submittedPrompt,
  });

  const filePath = writeManifestArtifact(artifactsDir, globalGenerationCounter, manifest);
  if (filePath) pruneOldManifests(artifactsDir);
}

// ============================================================================
// Extension entry point
// ============================================================================

export default function workflowStateExtension(pi: ExtensionAPI): void {
  const childSession = isChildSession();
  const contextAudit = isContextAuditEnabled();

  let currentPacket: Record<string, unknown> | null = null;

  // pendingCapture is built in before_agent_start, atomically consumed
  // in the first context event. null = no pending capture (either not
  // set yet, or already consumed).
  let pendingCapture: PendingCapture | null = null;

  // ================================================================
  // Tool: task_packet (parent sessions only)
  // ================================================================
  if (!childSession) {
    const tool: ToolDefinition<typeof TaskPacketParams, Record<string, unknown>> = {
      name: TOOL_NAME,
      label: "Task Packet",
      description:
        "Manage the current task packet. Actions: replace (set a new packet), show (display current packet), clear (remove current packet).",
      parameters: TaskPacketParams,

      async execute(_id, params, _signal, _onUpdate, ctx) {
        const { action } = params;

        if (action === "replace") {
          const pkt = params.packet;
          if (!pkt || typeof pkt !== "object") {
            throw new Error("'packet' parameter is required for replace.");
          }
          const errors = validatePacket(pkt as Record<string, unknown>);
          if (errors.length > 0) {
            throw new Error(
              `Packet validation failed:\n${errors.map((e) => `  - ${e}`).join("\n")}`,
            );
          }
          currentPacket = pkt as Record<string, unknown>;
          persistPacket(pi, currentPacket);
          return {
            content: [{ type: "text", text: "Task packet set successfully." }],
            details: { action: "replace", success: true },
          };
        }

        if (action === "show") {
          const pkt = currentPacket ?? reconstructPacket(ctx);
          if (!pkt) {
            return {
              content: [{ type: "text", text: "No task packet is currently set." }],
              details: { action: "show", present: false },
            };
          }
          return {
            content: [{ type: "text", text: renderCompact(pkt) }],
            details: { action: "show", present: true },
          };
        }

        if (action === "clear") {
          currentPacket = null;
          persistClear(pi);
          return {
            content: [{ type: "text", text: "Task packet cleared." }],
            details: { action: "clear", success: true },
          };
        }

        throw new Error(`Unknown action: ${action}. Use replace, show, or clear.`);
      },
    };

    pi.registerTool(tool);
  }

  // ================================================================
  // session lifecycle
  // ================================================================
  pi.on("session_start", (_event, ctx) => {
    currentPacket = reconstructPacket(ctx);
  });

  pi.on("session_tree", (_event, ctx) => {
    currentPacket = reconstructPacket(ctx);
  });

  // ================================================================
  // before_agent_start
  // 1. Compute effective system prompt (inject packet for parent).
  // 2. Collect audit metadata using known type fields.
  // 3. Return modified system prompt only if changed.
  // ================================================================
  pi.on("before_agent_start", (event, ctx) => {
    let effectiveSystemPrompt = event.systemPrompt;

    if (!childSession) {
      const packet = currentPacket ?? reconstructPacket(ctx);
      if (packet) {
        effectiveSystemPrompt = `${event.systemPrompt}\n\n${renderCompact(packet)}`;
      }
    }

    // Collect audit metadata (if audit enabled)
    if (contextAudit) {
      const sopts = event.systemPromptOptions;

      // Context files
      const rawCfs = sopts.contextFiles;
      const contextFiles: Array<{ path: string; content: string }> = [];
      if (Array.isArray(rawCfs)) {
        for (const cf of rawCfs) {
          if (cf && typeof cf.path === "string") {
            contextFiles.push({
              path: cf.path,
              content: typeof cf.content === "string" ? cf.content : "",
            });
          }
        }
      }

      // Selected tools (getActiveTools returns string[])
      let selectedTools: string[] | undefined;
      try {
        selectedTools = pi.getActiveTools();
      } catch { selectedTools = undefined; }

      // Skills from systemPromptOptions.skills (Skill[])
      let skillNames: string[] | undefined;
      const rawSkills = sopts.skills;
      if (Array.isArray(rawSkills)) {
        skillNames = rawSkills
          .map((s) => (typeof s === "object" && s !== null && "name" in s ? (s as { name: string }).name : typeof s === "string" ? s : ""))
          .filter(Boolean);
      }

      // Submitted user prompt from event.prompt
      const submittedPrompt = event.prompt;

      pendingCapture = {
        systemPrompt: effectiveSystemPrompt,
        messages: [],
        contextFiles,
        selectedTools,
        skillNames,
        submittedPrompt,
      };
    }

    if (effectiveSystemPrompt !== event.systemPrompt) {
      return { systemPrompt: effectiveSystemPrompt };
    }
  });

  // ================================================================
  // context: capture audit manifest exactly once per before_agent_start
  // ================================================================
  if (contextAudit) {
    pi.on("context", async (event, ctx) => {
      // Atomically take the pending capture. Null means this context
      // event is a subsequent tool-loop event — skip it.
      const pc = pendingCapture;
      if (!pc) return;
      pendingCapture = null;

      // AgentMessage is a union; some variants (e.g. BashExecutionMessage)
      // lack a `content` property. Use unrestrictive access via unknown.
      pc.messages = (event.messages ?? []).map((m: unknown) => {
        const msg = m as { role?: string; content?: unknown };
        const content = typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content ?? "");
        return {
          role: msg.role ?? "unknown",
          content,
        };
      });

      await captureContextManifest(ctx, pc);
    });
  }
}
