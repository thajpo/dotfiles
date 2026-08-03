import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import {
  Key,
  matchesKey,
  truncateToWidth,
  visibleWidth,
  wrapTextWithAnsi,
  type Component,
} from "@earendil-works/pi-tui";
import {
  ASYNC_DIR,
  RESULTS_DIR,
  SUBAGENT_ASYNC_COMPLETE_EVENT,
  SUBAGENT_ASYNC_STARTED_EVENT,
  SUBAGENT_CONTROL_EVENT,
  SUBAGENT_FOREGROUND_COMPLETE_EVENT,
  SUBAGENT_FOREGROUND_STARTED_EVENT,
  SUBAGENT_RESULT_INTERCOM_EVENT,
  SUBAGENT_STEERING_NOTICE_EVENT,
} from "../../npm/node_modules/pi-subagents/src/shared/types.ts";
import { listAsyncRuns } from "../../npm/node_modules/pi-subagents/src/runs/background/async-status.ts";
import { workflowArtifactsDirForSession } from "../workflow-state/core.mjs";
import {
  boundedText,
  cycleAgentIndex,
  explicitSessionMessage,
  extractActivePacket,
  formatTaskPacket,
  projectInspectorState,
  reduceInspectorKey,
  statusGlyph,
} from "./core.mjs";

const REFRESH_MS = 750;
const MAX_STATUS_BYTES = 1024 * 1024;
const MAX_EVENT_BYTES = 256 * 1024;
const MAX_ARTIFACT_BYTES = 64 * 1024;
const MAX_OUTPUT_BYTES = 128 * 1024;
const MAX_TRANSCRIPT_BYTES = 256 * 1024;
const MAX_DISPLAY_MESSAGES = 800;
const MAX_RUNS = 64;
const OBSERVE_COMMAND = "observe";

type Theme = ExtensionContext["ui"]["theme"];
type InspectorSnapshot = ReturnType<typeof projectInspectorState>;
type RuntimeAgent = {
  key: string;
  source: string;
  runId?: string;
  index?: number;
  agent: string;
  status: string;
  activityState?: string;
  currentTool?: string;
  currentPath?: string;
  task?: string;
  startedAt: number;
  updatedAt: number;
  error?: string;
};

type RuntimeMessage = {
  id: string;
  kind: string;
  source: string;
  agent?: string;
  runId?: string;
  index?: number;
  ts: number;
  text: string;
};

function isObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function pathWithin(root: string, candidate: string): boolean {
  const base = path.resolve(root);
  const target = path.resolve(candidate);
  return target === base || target.startsWith(`${base}${path.sep}`);
}

function currentUid(): number | undefined {
  return typeof process.getuid === "function" ? process.getuid() : undefined;
}

function ownedRegularFile(filePath: string): boolean {
  try {
    const stat = fs.lstatSync(filePath);
    if (!stat.isFile() || stat.isSymbolicLink()) return false;
    const uid = currentUid();
    return uid === undefined || stat.uid === uid;
  } catch {
    return false;
  }
}

function canonicalContained(filePath: string, roots: string[]): boolean {
  try {
    const realFile = fs.realpathSync(filePath);
    return roots.some((root) => {
      if (!fs.existsSync(root)) return false;
      return pathWithin(fs.realpathSync(root), realFile);
    });
  } catch {
    return false;
  }
}

function readJsonObject(filePath: string, maxBytes: number, roots: string[]): Record<string, unknown> | null {
  const text = readTail(filePath, maxBytes, roots);
  if (text === null) return null;
  try {
    const parsed: unknown = JSON.parse(text);
    return isObject(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function readTail(filePath: string, maxBytes: number, roots: string[]): string | null {
  const resolved = path.resolve(filePath);
  if (!roots.some((root) => pathWithin(root, resolved)) || !ownedRegularFile(resolved) || !canonicalContained(resolved, roots)) return null;
  let fd: number | undefined;
  try {
    const noFollow = fs.constants.O_NOFOLLOW ?? 0;
    fd = fs.openSync(resolved, fs.constants.O_RDONLY | noFollow);
    const stat = fs.fstatSync(fd);
    const uid = currentUid();
    if (!stat.isFile() || (uid !== undefined && stat.uid !== uid)) return null;
    const length = Math.min(stat.size, maxBytes);
    const buffer = Buffer.alloc(length);
    const read = fs.readSync(fd, buffer, 0, length, Math.max(0, stat.size - length));
    return buffer.subarray(0, read).toString("utf8");
  } catch {
    return null;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function readContainedText(filePath: string, maxBytes: number, roots: string[]): string | null {
  return readTail(filePath, maxBytes, roots);
}

function safeArtifactRoot(candidate: unknown, roots: string[]): string | undefined {
  if (typeof candidate !== "string" || !path.isAbsolute(candidate)) return undefined;
  const resolved = path.resolve(candidate);
  if (!roots.some((root) => pathWithin(root, resolved))) return undefined;
  try {
    const stat = fs.lstatSync(resolved);
    if (!stat.isDirectory() || stat.isSymbolicLink() || !canonicalContained(resolved, roots)) return undefined;
    return resolved;
  } catch {
    return undefined;
  }
}

function trustedSessionRoots(status: Record<string, unknown>, runDir: string): string[] {
  const roots = [runDir];
  for (const field of ["sessionDir", "sessionFile"]) {
    const value = status[field];
    if (typeof value !== "string" || !path.isAbsolute(value)) continue;
    roots.push(field === "sessionFile" ? path.dirname(value) : value);
  }
  return [...new Set(roots.map((root) => path.resolve(root)))];
}

function trustedArtifactRoots(status: Record<string, unknown>, runDir: string, ctx: ExtensionContext): string[] {
  const roots = [path.join(path.dirname(ASYNC_DIR), "artifacts")];
  const sessionFile = ctx.sessionManager.getSessionFile();
  if (sessionFile) roots.push(path.join(path.dirname(sessionFile), "subagent-artifacts"));
  for (const field of ["sessionDir", "sessionFile"]) {
    const value = status[field];
    if (typeof value !== "string" || !path.isAbsolute(value)) continue;
    const directory = field === "sessionFile" ? path.dirname(value) : value;
    roots.push(path.join(directory, "subagent-artifacts"));
  }
  roots.push(runDir);
  return [...new Set(roots.map((root) => path.resolve(root)))];
}

function safeAgentName(agent: string): string {
  return agent.replace(/[^\w.-]/g, "_");
}

function artifactPath(root: string, runId: string, agent: string, index: number | undefined, count: number, suffix: "input.md" | "output.md" | "meta.json"): string {
  const flatSuffix = index !== undefined && count > 1 ? `_${index}` : "";
  return path.join(root, `${runId}_${safeAgentName(agent)}${flatSuffix}_${suffix}`);
}

function parseTaskArtifact(text: string | null): string | undefined {
  if (!text) return undefined;
  const withoutHeader = text.replace(/^# Task for [^\n]+\n\n?/, "");
  const result = boundedText(withoutHeader.trim());
  return result || undefined;
}

function sessionTimestamp(value: unknown, fallback: number): number {
  if (!isObject(value)) return fallback;
  const message = isObject(value.message) ? value.message : value;
  const candidate = value.timestamp ?? value.ts ?? message.timestamp;
  if (typeof candidate === "number" && Number.isFinite(candidate)) return candidate;
  if (typeof candidate === "string") {
    const parsed = Date.parse(candidate);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function readSessionMessages(sessionFile: string | undefined, roots: string[], runId?: string, agent?: string, index?: number): RuntimeMessage[] {
  if (!sessionFile) return [];
  const text = readTail(sessionFile, MAX_OUTPUT_BYTES, roots);
  if (!text) return [];
  const result: RuntimeMessage[] = [];
  const fallbackBase = Date.now();
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const parsed: unknown = JSON.parse(line);
      const message = explicitSessionMessage(parsed);
      if (!message) continue;
      result.push({
        id: `session:${sessionFile}:${result.length}`,
        kind: message.role === "user" ? "instruction" : message.role === "tool" ? "progress" : "message",
        source: "session",
        agent,
        runId,
        index,
        ts: sessionTimestamp(parsed, fallbackBase + result.length),
        text: message.text,
      });
    } catch {
      // A child can be writing a partial JSONL line. Ignore it until the next refresh.
    }
  }
  return result.slice(-MAX_DISPLAY_MESSAGES);
}

function readTranscriptMessages(transcriptPath: string | undefined, roots: string[], runId: string, agent: string, index: number): RuntimeMessage[] {
  if (!transcriptPath || !path.isAbsolute(transcriptPath)) return [];
  const text = readTail(transcriptPath, MAX_TRANSCRIPT_BYTES, roots);
  if (!text) return [];
  const result: RuntimeMessage[] = [];
  const fallbackBase = Date.now();
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const parsed: unknown = JSON.parse(line);
      const message = explicitSessionMessage(parsed);
      if (!message) continue;
      result.push({
        id: `transcript:${transcriptPath}:${result.length}`,
        kind: message.role === "tool" ? "progress" : message.role === "assistant" ? "message" : "instruction",
        source: "transcript",
        agent,
        runId,
        index,
        ts: sessionTimestamp(parsed, fallbackBase + result.length),
        text: message.text,
      });
    } catch {
      // Ignore incomplete JSONL records while the child is writing.
    }
  }
  return result.slice(-MAX_DISPLAY_MESSAGES);
}

function parseEvents(eventsText: string | null, runId: string, agentByIndex: string[]): RuntimeMessage[] {
  if (!eventsText) return [];
  const result: RuntimeMessage[] = [];
  for (const line of eventsText.split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const parsed: unknown = JSON.parse(line);
      if (!isObject(parsed)) continue;
      const type = typeof parsed.type === "string" ? parsed.type : "";
      const event = isObject(parsed.event) ? parsed.event : parsed;
      const ts = typeof parsed.ts === "number" ? parsed.ts : Date.now();
      const index = typeof event.index === "number" ? event.index : typeof parsed.index === "number" ? parsed.index : undefined;
      const agent = index !== undefined ? agentByIndex[index] : typeof event.agent === "string" ? event.agent : undefined;
      if (type === "subagent.control" || type === "subagent.steering.notice" || type.includes("failed") || type.includes("attention")) {
        const text = boundedText(parsed.noticeText ?? parsed.message ?? event.message ?? event.error ?? type);
        if (text) result.push({ id: `event:${runId}:${ts}:${result.length}`, kind: type.includes("failed") || type.includes("attention") ? "failure" : "control", source: "events", agent, runId, index, ts, text });
      }
    } catch {
      // Ignore malformed or incomplete event lines.
    }
  }
  return result.slice(-MAX_DISPLAY_MESSAGES);
}

function readContextManifest(sessionFile: string | undefined, roots: string[], runId: string, agent: string | undefined, index: number | undefined): RuntimeMessage[] {
  if (!sessionFile) return [];
  const directory = workflowArtifactsDirForSession(sessionFile);
  if (!roots.some((root) => pathWithin(root, directory))) return [];
  let files: string[];
  try {
    files = fs.readdirSync(directory).filter((name) => name.startsWith("context-") && name.endsWith(".json")).sort().slice(-1);
  } catch {
    return [];
  }
  const manifestPath = files.length ? path.join(directory, files[0]!) : undefined;
  const manifest = manifestPath && canonicalContained(manifestPath, roots)
    ? readJsonObject(manifestPath, MAX_STATUS_BYTES, roots)
    : null;
  if (!manifest) return [];
  const contextFiles = Array.isArray(manifest.context_files) ? manifest.context_files : [];
  const tools = Array.isArray(manifest.selected_tools) ? manifest.selected_tools : [];
  const skills = Array.isArray(manifest.skill_names) ? manifest.skill_names : [];
  const parts = [
    contextFiles.length ? `context files: ${contextFiles.slice(0, 16).map((item) => isObject(item) ? item.path : item).filter(Boolean).join(", ")}` : "",
    tools.length ? `tools: ${tools.slice(0, 24).join(", ")}` : "",
    skills.length ? `skills: ${skills.slice(0, 24).join(", ")}` : "",
    isObject(manifest.task_packet) && manifest.task_packet.present === true ? "task packet: present" : "",
  ].filter(Boolean);
  if (parts.length === 0) return [];
  return [{
    id: `context:${runId}:${index ?? ""}:${files[0]}`,
    kind: "status",
    source: "context-audit",
    agent,
    runId,
    index,
    ts: typeof manifest.created_at === "number" ? manifest.created_at : Date.now(),
    text: parts.join(" · "),
  }];
}

function readRunData(status: Record<string, unknown>, runDir: string, ctx: ExtensionContext, warnings: string[]): { run: Record<string, unknown>; messages: RuntimeMessage[] } {
  const artifactRoots = trustedArtifactRoots(status, runDir, ctx);
  const sessionRoots = trustedSessionRoots(status, runDir);
  const runId = typeof status.runId === "string" ? status.runId : path.basename(runDir);
  const rawSteps = Array.isArray(status.steps) ? status.steps : [];
  const steps: Record<string, unknown>[] = [];
  const messages: RuntimeMessage[] = [];
  const artifactRoot = safeArtifactRoot(status.artifactsDir, artifactRoots);
  const agentNames = rawSteps.map((step) => isObject(step) && typeof step.agent === "string" ? step.agent : "unknown");
  for (let index = 0; index < rawSteps.length; index += 1) {
    const raw = rawSteps[index];
    if (!isObject(raw)) continue;
    const step = { ...raw };
    const agent = typeof step.agent === "string" ? step.agent : "unknown";
    const input = artifactRoot ? parseTaskArtifact(readContainedText(artifactPath(artifactRoot, runId, agent, index, rawSteps.length, "input.md"), MAX_ARTIFACT_BYTES, artifactRoots)) : undefined;
    const output = artifactRoot ? boundedText(readTail(artifactPath(artifactRoot, runId, agent, index, rawSteps.length, "output.md"), MAX_OUTPUT_BYTES, artifactRoots) ?? "") : "";
    if (input) {
      step.task = input;
      messages.push({ id: `instruction:async:${runId}:${index}`, kind: "instruction", source: "async", agent, runId, index, ts: typeof step.startedAt === "number" ? step.startedAt : Date.now(), text: input });
    }
    if (output && (step.status === "complete" || step.status === "completed" || step.status === "failed" || step.status === "paused" || step.status === "stopped")) {
      messages.push({ id: `result:async:${runId}:${index}`, kind: step.status === "failed" ? "failure" : "result", source: "artifact", agent, runId, index, ts: typeof step.endedAt === "number" ? step.endedAt : Date.now(), text: output });
    }
    if (typeof step.error === "string" && step.error) {
      messages.push({ id: `failure:async:${runId}:${index}`, kind: "failure", source: "status", agent, runId, index, ts: typeof step.endedAt === "number" ? step.endedAt : Date.now(), text: step.error });
    }
    const sessionFile = typeof step.sessionFile === "string" ? step.sessionFile : undefined;
    messages.push(...readSessionMessages(sessionFile, sessionRoots, runId, agent, index));
    messages.push(...readTranscriptMessages(typeof step.transcriptPath === "string" ? step.transcriptPath : undefined, artifactRoots, runId, agent, index));
    messages.push(...readContextManifest(sessionFile, sessionRoots, runId, agent, index));
    steps.push(step);
  }
  const events = readTail(path.join(runDir, "events.jsonl"), MAX_EVENT_BYTES, [runDir]);
  messages.push(...parseEvents(events, runId, agentNames));
  if (typeof status.error === "string" && status.error) {
    messages.push({ id: `failure:async:${runId}`, kind: "failure", source: "status", runId, ts: typeof status.lastUpdate === "number" ? status.lastUpdate : Date.now(), text: status.error });
  }
  const run = { ...status, id: runId, steps };
  return { run, messages };
}

function readNestedRunData(
  value: unknown,
  ctx: ExtensionContext,
  warnings: string[],
  messages: RuntimeMessage[],
  seen: Set<string>,
): void {
  if (!isObject(value)) return;
  const run = value;
  const runId = typeof run.id === "string" ? run.id : undefined;
  const asyncDir = typeof run.asyncDir === "string" ? path.resolve(run.asyncDir) : undefined;
  if (asyncDir && !seen.has(asyncDir)) {
    seen.add(asyncDir);
    const tempRoot = path.dirname(ASYNC_DIR);
    const statusPath = path.join(asyncDir, "status.json");
    if (!pathWithin(tempRoot, asyncDir) || !canonicalContained(statusPath, [tempRoot])) {
      warnings.push(`Skipped nested run outside the Pi subagent root: ${boundedText(asyncDir, 240)}.`);
    } else {
      const status = readJsonObject(statusPath, MAX_STATUS_BYTES, [tempRoot]);
      if (status) {
        const inspected = readRunData(status, asyncDir, ctx, warnings);
        messages.push(...inspected.messages);
      }
    }
  }
  if (Array.isArray(run.children)) {
    for (const child of run.children) readNestedRunData(child, ctx, warnings, messages, seen);
  }
}

function scanRuns(ctx: ExtensionContext): { runs: Record<string, unknown>[]; messages: RuntimeMessage[]; warnings: string[] } {
  const runs: Record<string, unknown>[] = [];
  const messages: RuntimeMessage[] = [];
  const warnings: string[] = [];
  const sessionId = ctx.sessionManager.getSessionId();
  let entries: fs.Dirent[];
  let summaries: Record<string, unknown>[] = [];
  try {
    summaries = listAsyncRuns(ASYNC_DIR, {
      ...(sessionId ? { sessionId } : {}),
      resultsDir: RESULTS_DIR,
      limit: MAX_RUNS,
    }) as unknown as Record<string, unknown>[];
  } catch (error) {
    warnings.push(`Nested fleet projection unavailable: ${boundedText(error instanceof Error ? error.message : String(error), 500)}`);
  }
  const summaryById = new Map<string, Record<string, unknown>>();
  for (const summary of summaries) {
    if (typeof summary.id === "string") summaryById.set(summary.id, summary);
  }
  try {
    entries = fs.readdirSync(ASYNC_DIR, { withFileTypes: true });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") warnings.push(`Fleet scan unavailable: ${boundedText(error instanceof Error ? error.message : String(error), 500)}`);
    return { runs, messages, warnings };
  }
  const runEntries = entries.filter((entry) => entry.isDirectory() && !entry.isSymbolicLink()).slice(0, MAX_RUNS);
  if (entries.length > runEntries.length) warnings.push(`Fleet scan limited to ${MAX_RUNS} run directories.`);
  const nestedSeen = new Set<string>();
  for (const entry of runEntries) {
    const runDir = path.join(ASYNC_DIR, entry.name);
    const statusPath = path.join(runDir, "status.json");
    if (!canonicalContained(statusPath, [ASYNC_DIR])) {
      warnings.push(`Skipped status outside the async run root for ${boundedText(entry.name, 120)}.`);
      continue;
    }
    const status = readJsonObject(statusPath, MAX_STATUS_BYTES, [ASYNC_DIR]);
    if (!status) {
      if (ownedRegularFile(statusPath)) warnings.push(`Skipped malformed status for ${boundedText(entry.name, 120)}.`);
      continue;
    }
    if (sessionId && status.sessionId !== sessionId) continue;
    const inspected = readRunData(status, runDir, ctx, warnings);
    const runId = typeof status.runId === "string" ? status.runId : entry.name;
    const summary = summaryById.get(runId);
    if (summary) {
      const summarySteps = Array.isArray(summary.steps) ? summary.steps : [];
      const rawSteps = Array.isArray(inspected.run.steps) ? inspected.run.steps : [];
      const steps = summarySteps.map((step, index) => ({
        ...(isObject(step) ? step : {}),
        ...(isObject(rawSteps[index]) ? rawSteps[index] : {}),
      }));
      runs.push({ ...summary, ...inspected.run, id: runId, steps, nestedChildren: summary.nestedChildren ?? inspected.run.nestedChildren });
      if (Array.isArray(summary.nestedChildren)) {
        for (const child of summary.nestedChildren) readNestedRunData(child, ctx, warnings, messages, nestedSeen);
      }
    } else {
      runs.push(inspected.run);
    }
    messages.push(...inspected.messages);
  }
  runs.sort((left, right) => Number(right.lastUpdate ?? right.startedAt ?? 0) - Number(left.lastUpdate ?? left.startedAt ?? 0));
  return { runs, messages, warnings };
}

function eventSessionMatches(value: unknown, sessionId: string | null, knownRunIds: Set<string>, runId?: string): boolean {
  if (!sessionId) return true;
  if (!isObject(value)) return false;
  const candidate = value.sessionId;
  if (typeof candidate === "string") return candidate === sessionId;
  return typeof runId === "string" && knownRunIds.has(runId);
}

function runtimeAgentFrom(value: Record<string, unknown>, index: number | undefined, source: string): RuntimeAgent {
  const runId = typeof value.runId === "string" ? value.runId : typeof value.id === "string" ? value.id : undefined;
  const agent = typeof value.agent === "string" ? value.agent : "agent";
  const key = `${source}:${runId ?? "unknown"}:${index ?? 0}`;
  const now = Date.now();
  const task = typeof value.task === "string" ? value.task : typeof value.goal === "string" ? value.goal : undefined;
  const error = typeof value.error === "string" ? value.error : undefined;
  const activityState = typeof value.activityState === "string" ? value.activityState : undefined;
  const currentTool = typeof value.currentTool === "string" ? value.currentTool : undefined;
  const currentPath = typeof value.currentPath === "string" ? value.currentPath : undefined;
  return {
    key,
    source,
    ...(runId ? { runId } : {}),
    ...(index !== undefined ? { index } : {}),
    agent,
    status: typeof value.status === "string" ? value.status : typeof value.state === "string" ? value.state : "running",
    ...(activityState !== undefined ? { activityState } : {}),
    ...(currentTool !== undefined ? { currentTool } : {}),
    ...(currentPath !== undefined ? { currentPath } : {}),
    ...(task !== undefined ? { task } : {}),
    startedAt: typeof value.ts === "number" ? value.ts : typeof value.startedAt === "number" ? value.startedAt : now,
    updatedAt: now,
    ...(error !== undefined ? { error } : {}),
  };
}

class InspectorRuntime {
  private readonly agents = new Map<string, RuntimeAgent>();
  private readonly messages = new Map<string, RuntimeMessage>();
  private readonly knownRunIds = new Set<string>();
  private sessionId: string | null = null;

  setSession(sessionId: string | null): void {
    if (this.sessionId === sessionId) return;
    this.sessionId = sessionId;
    this.agents.clear();
    this.messages.clear();
    this.knownRunIds.clear();
  }

  private addMessage(message: RuntimeMessage): void {
    if (!message.text) return;
    this.messages.set(message.id, { ...message, text: boundedText(message.text) });
    while (this.messages.size > MAX_DISPLAY_MESSAGES) this.messages.delete(this.messages.keys().next().value!);
  }

  private addAgent(agent: RuntimeAgent): void {
    const previous = this.agents.get(agent.key);
    this.agents.set(agent.key, { ...previous, ...agent, updatedAt: Date.now() });
  }

  onStarted(value: unknown): void {
    const runId = isObject(value) && typeof value.id === "string" ? value.id : undefined;
    if (!eventSessionMatches(value, this.sessionId, this.knownRunIds, runId) || !isObject(value)) return;
    if (runId) this.knownRunIds.add(runId);
    const rawAgents = Array.isArray(value.agents) ? value.agents : Array.isArray(value.chain) ? value.chain : typeof value.agent === "string" ? [value.agent] : [];
    rawAgents.forEach((name, index) => {
      const agent = typeof name === "string" ? name : `step-${index}`;
      const record = runtimeAgentFrom({ ...value, runId, agent, status: "queued", task: index === 0 ? value.task ?? value.goal : undefined }, index, "async");
      this.addAgent(record);
      if (record.task) this.addMessage({ id: `runtime-instruction:${record.key}`, kind: "instruction", source: "runtime", agent: record.agent, runId, index, ts: record.startedAt, text: record.task });
    });
  }

  onForegroundStarted(value: unknown): void {
    const runId = isObject(value) && (typeof value.runId === "string" ? value.runId : typeof value.id === "string" ? value.id : undefined);
    if (!eventSessionMatches(value, this.sessionId, this.knownRunIds, runId) || !isObject(value)) return;
    if (runId) this.knownRunIds.add(runId);
    const tasks = Array.isArray(value.tasks) ? value.tasks : [];
    const agents = tasks.length > 0
      ? tasks
      : Array.isArray(value.agents) ? value.agents.map((agent) => ({ agent })) : [];
    agents.forEach((candidate, index) => {
      const task = isObject(candidate) ? candidate : { agent: candidate };
      const agent = typeof task.agent === "string" ? task.agent : `step-${index}`;
      const record = runtimeAgentFrom({ ...value, runId, agent, status: "running", task: task.task }, index, "foreground");
      this.addAgent(record);
      if (record.task) this.addMessage({ id: `runtime-instruction:${record.key}`, kind: "instruction", source: "runtime", agent: record.agent, runId, index, ts: record.startedAt, text: record.task });
    });
  }

  onComplete(value: unknown): void {
    const runId = isObject(value) && (typeof value.id === "string" ? value.id : typeof value.runId === "string" ? value.runId : undefined);
    if (!eventSessionMatches(value, this.sessionId, this.knownRunIds, runId) || !isObject(value)) return;
    if (runId) this.knownRunIds.add(runId);
    const index = typeof value.index === "number" ? value.index : typeof value.taskIndex === "number" ? value.taskIndex : undefined;
    const agent = typeof value.agent === "string" ? value.agent : "agent";
    const success = value.success === true || value.state === "complete" || value.state === "completed";
    const status = success ? "complete" : value.state === "paused" ? "paused" : "failed";
    const record = runtimeAgentFrom({ ...value, runId, agent, status }, index, value.source === "foreground" ? "foreground" : "async");
    this.addAgent(record);
    const summary = typeof value.summary === "string" ? value.summary : typeof value.message === "string" ? value.message : "No result summary available.";
    this.addMessage({ id: `runtime-result:${record.key}:${record.updatedAt}`, kind: success ? "result" : "failure", source: record.source, agent, runId, index, ts: record.updatedAt, text: summary });
  }

  onControl(value: unknown): void {
    const outer = isObject(value) ? value : undefined;
    const event = outer && isObject(outer.event) ? outer.event : outer;
    const runId = event && typeof event.runId === "string" ? event.runId : undefined;
    if (!eventSessionMatches(value, this.sessionId, this.knownRunIds, runId) || !outer || !event) return;
    const index = typeof event.index === "number" ? event.index : undefined;
    const previous = runId ? [...this.agents.values()].find((candidate) => candidate.runId === runId && candidate.index === index) : undefined;
    const source = outer.source === "foreground" || previous?.source === "foreground" ? "foreground" : previous?.source ?? "async";
    const agent = typeof event.agent === "string" ? event.agent : previous?.agent;
    const activityState = event.to === "needs_attention" || event.to === "active_long_running" ? event.to : undefined;
    const currentTool = typeof event.currentTool === "string" ? event.currentTool : previous?.currentTool;
    const currentPath = typeof event.currentPath === "string" ? event.currentPath : previous?.currentPath;
    const updated = runId ? {
      ...(previous ?? { key: `${source}:${runId}:${index ?? 0}`, source, runId, index, agent: agent ?? "agent", status: "running", startedAt: Date.now(), updatedAt: Date.now() }),
      ...(agent ? { agent } : {}),
      ...(activityState ? { activityState } : {}),
      ...(currentTool ? { currentTool } : {}),
      ...(currentPath ? { currentPath } : {}),
      ...(typeof event.recentFailureSummary === "string" ? { error: event.recentFailureSummary } : {}),
    } : undefined;
    if (updated) this.addAgent(updated);
    const text = typeof outer.noticeText === "string" ? outer.noticeText : typeof event.message === "string" ? event.message : "Subagent control event";
    this.addMessage({ id: `runtime-control:${runId ?? "unknown"}:${index ?? ""}:${Date.now()}`, kind: activityState === "needs_attention" ? "warning" : "control", source: "runtime", agent, runId, index, ts: typeof event.ts === "number" ? event.ts : Date.now(), text });
  }

  onIntercom(value: unknown): void {
    const runId = isObject(value) && typeof value.runId === "string" ? value.runId : undefined;
    if (!eventSessionMatches(value, this.sessionId, this.knownRunIds, runId) || !isObject(value)) return;
    if (runId) this.knownRunIds.add(runId);
    const index = typeof value.index === "number" ? value.index : undefined;
    const agent = typeof value.agent === "string" ? value.agent : undefined;
    const text = typeof value.summary === "string" ? value.summary : typeof value.message === "string" ? value.message : "Subagent message";
    const failed = value.status === "failed" || value.status === "paused" || value.status === "stopped";
    this.addMessage({ id: `runtime-intercom:${runId ?? "unknown"}:${index ?? ""}:${Date.now()}`, kind: failed ? "failure" : "result", source: "intercom", agent, runId, index, ts: Date.now(), text });
  }

  snapshot(ctx: ExtensionContext): InspectorSnapshot {
    const packet = extractActivePacket(ctx.sessionManager.getBranch());
    const scanned = scanRuns(ctx);
    for (const run of scanned.runs) {
      if (typeof run.id === "string") this.knownRunIds.add(run.id);
    }
    return projectInspectorState({
      packet,
      runs: scanned.runs,
      runtimeAgents: [...this.agents.values()],
      messages: [...scanned.messages, ...this.messages.values()],
      warnings: scanned.warnings,
      now: Date.now(),
    });
  }
}

function fit(text: string, width: number): string {
  const clipped = truncateToWidth(text, Math.max(0, width), "");
  return clipped + " ".repeat(Math.max(0, width - visibleWidth(clipped)));
}

function tabLabel(tab: string, active: boolean, theme: Theme): string {
  const label = `[${tab}]`;
  return active ? theme.fg("accent", theme.bold(label)) : theme.fg("dim", label);
}

function detailLabel(label: string, value: unknown): string | undefined {
  if (value === undefined || value === null || value === "") return undefined;
  return `${label}: ${boundedText(value, 700)}`;
}

function elapsedLabel(startedAt: number, updatedAt: number, status: string): string {
  const end = status === "running" || status === "queued" || status === "pending" ? Date.now() : updatedAt;
  const seconds = Math.floor(Math.max(0, end - startedAt) / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s`;
}

class InspectorComponent implements Component {
  private snapshot: InspectorSnapshot;
  private selected = 0;
  private selectedKey: string | undefined;
  private tab: "task" | "fleet" | "messages" = "fleet";
  private allMessages = false;
  private transcript = false;
  private bodyScroll = 0;
  private disposed = false;
  private readonly timer: ReturnType<typeof setInterval>;
  private readonly tui: { requestRender: () => void; terminal?: { rows?: number } };
  private readonly theme: Theme;
  private readonly done: (result: undefined) => void;
  private readonly getSnapshot: () => InspectorSnapshot;

  constructor(
    tui: { requestRender: () => void; terminal?: { rows?: number } },
    theme: Theme,
    done: (result: undefined) => void,
    getSnapshot: () => InspectorSnapshot,
  ) {
    this.tui = tui;
    this.theme = theme;
    this.done = done;
    this.getSnapshot = getSnapshot;
    this.snapshot = getSnapshot();
    this.preserveSelection();
    this.timer = setInterval(() => {
      if (this.disposed) return;
      this.refresh();
      this.tui.requestRender();
    }, REFRESH_MS);
    this.timer.unref?.();
  }

  private preserveSelection(): void {
    if (this.selectedKey) {
      const index = this.snapshot.agents.findIndex((agent) => agent.key === this.selectedKey);
      if (index >= 0) this.selected = index;
    }
    this.selected = Math.max(0, Math.min(this.selected, Math.max(0, this.snapshot.agents.length - 1)));
    this.selectedKey = this.snapshot.agents[this.selected]?.key;
  }

  private refresh(): void {
    this.snapshot = this.getSnapshot();
    this.preserveSelection();
  }

  private move(delta: number): void {
    this.selected = cycleAgentIndex(this.snapshot.agents, this.selected, delta);
    this.selectedKey = this.snapshot.agents[this.selected]?.key;
    this.tui.requestRender();
  }

  handleInput(data: string): void {
    const action = reduceInspectorKey(data);
    if (matchesKey(data, "escape") || matchesKey(data, "ctrl+c")) return this.done(undefined);
    if (matchesKey(data, "tab") || data === "\u001b[Z") return this.move(data === "\u001b[Z" ? -1 : 1);
    if (matchesKey(data, "up") || matchesKey(data, "k")) return this.move(-1);
    if (matchesKey(data, "down") || matchesKey(data, "j")) return this.move(1);
    if (action.type === "close") return this.done(undefined);
    if (action.type === "next-agent") return this.move(1);
    if (action.type === "previous-agent") return this.move(-1);
    if (action.type === "tab") {
      this.tab = action.tab as "task" | "fleet" | "messages";
      this.bodyScroll = 0;
      this.tui.requestRender();
      return;
    }
    if (action.type === "all-messages") {
      this.tab = "messages";
      this.bodyScroll = 0;
      this.allMessages = !this.allMessages;
      this.tui.requestRender();
      return;
    }
    if (action.type === "transcript") {
      this.tab = "messages";
      this.bodyScroll = 0;
      this.transcript = !this.transcript;
      this.tui.requestRender();
      return;
    }
    if (matchesKey(data, "pageUp")) {
      this.bodyScroll = Math.max(0, this.bodyScroll - 8);
      this.tui.requestRender();
      return;
    }
    if (matchesKey(data, "pageDown")) {
      this.bodyScroll += 8;
      this.tui.requestRender();
      return;
    }
    if (action.type === "refresh") {
      this.refresh();
      this.bodyScroll = 0;
      this.tui.requestRender();
    }
  }

  private selectedAgent() {
    return this.snapshot.agents[this.selected];
  }

  private taskLines(): string[] {
    const lines = formatTaskPacket(this.snapshot.packet);
    if (this.snapshot.warnings.length) {
      lines.push("", this.theme.fg("warning", "Warnings:"));
      lines.push(...this.snapshot.warnings.map((warning) => `  • ${warning}`));
    }
    return lines;
  }

  private fleetLines(width: number): string[] {
    if (this.snapshot.agents.length === 0) return ["No tracked agents.", "", "Agents appear here when a delegated run starts or has persisted status."];
    const selected = this.selectedAgent();
    const detail = selected ? [
      this.theme.fg("accent", "Selected agent"),
      detailLabel("Agent", selected.agent),
      detailLabel("Source", selected.source),
      detailLabel("Run", selected.runId),
      detailLabel("Parent run", (selected as { parentRunId?: string }).parentRunId),
      selected.index !== undefined ? `Child: ${selected.index + 1}` : undefined,
      detailLabel("Status", selected.status),
      detailLabel("Elapsed", elapsedLabel(selected.startedAt, selected.updatedAt, selected.status)),
      detailLabel("Activity", selected.activityState),
      detailLabel("Current tool", selected.currentTool),
      detailLabel("Path", selected.currentPath),
      detailLabel("Model", (selected as { model?: string }).model),
      selected.task ? "Instruction: " : "Instruction: unavailable (no explicit artifact captured)",
      selected.task ? `  ${selected.task}` : undefined,
      selected.error ? `Error: ${selected.error}` : undefined,
    ].filter((line): line is string => Boolean(line)) : [];
    const roster = [`Agents: ${this.snapshot.agents.length}`, ""];
    this.snapshot.agents.forEach((agent, index) => {
      const marker = index === this.selected ? this.theme.fg("accent", "›") : " ";
      const source = agent.source ? `${agent.source} ` : "";
      const run = agent.runId ? ` ${agent.runId.slice(0, 12)}${agent.index !== undefined ? `:${agent.index + 1}` : ""}` : "";
      const activity = agent.currentTool ? ` · ${agent.currentTool}` : agent.activityState ? ` · ${agent.activityState}` : "";
      roster.push(`${marker} ${statusGlyph(agent.status)} ${source}${agent.agent}${run} · ${agent.status} · ${elapsedLabel(agent.startedAt, agent.updatedAt, agent.status)}${activity}`);
    });
    return [...detail, ...(detail.length ? [""] : []), ...roster].map((line) => truncateToWidth(line, width, ""));
  }

  private messagesLines(width: number): string[] {
    const selected = this.selectedAgent();
    const messages = this.snapshot.messages.filter((message) => {
      if (this.allMessages) return true;
      if (!selected) return true;
      if (message.runId === selected.runId) return message.index === undefined || selected.index === undefined || message.index === selected.index;
      return message.runId === undefined && message.agent === selected.agent;
    });
    const visible = messages.slice(-(this.transcript ? 800 : 48));
    const lines = [
      this.allMessages ? "All-agent stream" : selected ? `Messages for ${selected.agent}` : "Messages",
      this.transcript ? "Transcript view" : "Compact view",
      "",
    ];
    for (const message of visible) {
      const label = `${new Date(message.ts).toISOString().slice(11, 19)} ${message.kind} ${message.agent ?? ""}`.trim();
      const wrapped = wrapTextWithAnsi(`${this.theme.fg("muted", label)} · ${message.text}`, Math.max(1, width));
      lines.push(...wrapped);
    }
    if (visible.length === 0) lines.push("No explicit messages recorded for this selection.");
    return lines;
  }

  private bodyLines(width: number): string[] {
    if (this.tab === "task") return this.taskLines();
    if (this.tab === "messages") return this.messagesLines(width);
    return this.fleetLines(width);
  }

  render(width: number): string[] {
    if (width < 44) return [truncateToWidth("Inspector needs at least 44 columns. Esc closes.", width, "")];
    const innerWidth = width - 2;
    const rows = this.tui.terminal?.rows ?? 32;
    const bodyHeight = Math.max(4, Math.min(34, Math.floor(rows * 0.82) - 6));
    const body = this.bodyLines(innerWidth);
    const maxScroll = Math.max(0, body.length - bodyHeight);
    if (this.tab === "fleet" && this.snapshot.agents.length > 0) {
      const selectedLine = Math.min(body.length - 1, body.length - this.snapshot.agents.length + this.selected);
      if (selectedLine < this.bodyScroll) this.bodyScroll = selectedLine;
      if (selectedLine >= this.bodyScroll + bodyHeight) this.bodyScroll = selectedLine - bodyHeight + 1;
    }
    this.bodyScroll = Math.max(0, Math.min(this.bodyScroll, maxScroll));
    const visible = body.slice(this.bodyScroll, this.bodyScroll + bodyHeight);
    const tabs = `${tabLabel("Task", this.tab === "task", this.theme)}  ${tabLabel("Fleet", this.tab === "fleet", this.theme)}  ${tabLabel("Messages", this.tab === "messages", this.theme)}`;
    const footer = "1/2/3 tabs · j/k or Tab agents · PgUp/PgDn scroll · a all · v transcript · Esc close";
    const lines = [
      this.theme.fg("border", `╭${"─".repeat(innerWidth)}╮`),
      this.theme.fg("border", "│") + fit(` ${this.theme.bold("Pi Inspector")} ${this.theme.fg("dim", "· read-only")}`, innerWidth) + this.theme.fg("border", "│"),
      this.theme.fg("border", "│") + fit(` ${tabs}`, innerWidth) + this.theme.fg("border", "│"),
      this.theme.fg("border", `├${"─".repeat(innerWidth)}┤`),
      ...Array.from({ length: bodyHeight }, (_, index) => this.theme.fg("border", "│") + fit(visible[index] ?? "", innerWidth) + this.theme.fg("border", "│")),
      this.theme.fg("border", `├${"─".repeat(innerWidth)}┤`),
      this.theme.fg("border", "│") + fit(`${this.theme.fg("dim", footer)}${maxScroll > 0 ? ` · ${body.length} lines` : ""}`, innerWidth) + this.theme.fg("border", "│"),
      this.theme.fg("border", `╰${"─".repeat(innerWidth)}╯`),
    ];
    return lines.map((line) => truncateToWidth(line, width, ""));
  }

  invalidate(): void {
    this.refresh();
  }

  dispose(): void {
    this.disposed = true;
    clearInterval(this.timer);
  }
}

export default function observabilityExtension(pi: ExtensionAPI): void {
  const runtime = new InspectorRuntime();
  let open = false;
  const unsubscribers: Array<() => void> = [];

  const show = async (ctx: ExtensionContext): Promise<void> => {
    runtime.setSession(ctx.sessionManager.getSessionId() ?? null);
    if (!ctx.hasUI) {
      ctx.ui.notify("Inspector requires an interactive Pi UI; use /observe from an interactive session.", "warning");
      return;
    }
    if (open) {
      ctx.ui.notify("Pi Inspector is already open.", "info");
      return;
    }
    open = true;
    try {
      await ctx.ui.custom<undefined>(
        (tui, theme, _keybindings, done) => new InspectorComponent(tui, theme, done, () => runtime.snapshot(ctx)),
        { overlay: true, overlayOptions: { anchor: "center", width: "95%", minWidth: 60, maxHeight: "85%", margin: 1 } },
      );
    } finally {
      open = false;
    }
  };

  pi.registerCommand(OBSERVE_COMMAND, {
    description: "Open the read-only Task/Fleet/Messages Pi Inspector",
    handler: async (_args, ctx) => show(ctx),
  });
  pi.registerShortcut(Key.ctrl("i"), {
    description: "Open Pi Inspector",
    handler: async (ctx) => show(ctx),
  });

  const on = (name: string, handler: (value: unknown) => void) => {
    const unsubscribe = pi.events.on(name, handler);
    if (typeof unsubscribe === "function") unsubscribers.push(unsubscribe);
  };
  on(SUBAGENT_ASYNC_STARTED_EVENT, (value) => runtime.onStarted(value));
  on(SUBAGENT_ASYNC_COMPLETE_EVENT, (value) => runtime.onComplete(value));
  on(SUBAGENT_FOREGROUND_STARTED_EVENT, (value) => runtime.onForegroundStarted(value));
  on(SUBAGENT_FOREGROUND_COMPLETE_EVENT, (value) => runtime.onComplete(value));
  on(SUBAGENT_CONTROL_EVENT, (value) => runtime.onControl(value));
  on(SUBAGENT_RESULT_INTERCOM_EVENT, (value) => runtime.onIntercom(value));
  on(SUBAGENT_STEERING_NOTICE_EVENT, (value) => runtime.onControl(value));

  pi.on("session_start", (_event, ctx) => {
    runtime.setSession(ctx.sessionManager.getSessionId() ?? null);
  });
  pi.on("session_shutdown", () => {
    for (const unsubscribe of unsubscribers.splice(0)) unsubscribe();
  });
  pi.on("tool_result", () => {
    // A task packet can be replaced by a tool call; the next render reads the
    // branch again rather than caching the packet.
  });
}
