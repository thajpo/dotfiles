// @ts-check

import { validatePacket } from "../workflow-state/core.mjs";

/**
 * Dependency-light projection helpers for the read-only Pi Inspector.
 *
 * The UI deliberately consumes a bounded, sanitized projection rather than
 * rendering raw session JSONL or provider messages. Keep this module usable by
 * Node tests without loading the Pi runtime.
 */

export const PACKET_ENTRY_TYPE = "workflow-task-packet";
export const PACKET_CLEAR_TYPE = "workflow-task-packet-clear";
export const PACKET_SCHEMA_VERSION = 1;
export const MAX_TEXT_CHARS = 4000;
export const MAX_PACKET_ITEMS = 32;
export const MAX_AGENTS = 128;
export const MAX_MESSAGES = 800;

const SECRET_PATTERNS = [
  /\b(?:sk|rk|pk|ghp|gho|ghs|github_pat|glpat|npm|xoxb|xoxp)_[A-Za-z0-9_-]{8,}\b/g,
  /\b(?:sk|rk|pk|ghp|gho|ghs|github_pat|glpat|xoxb|xoxp)-[A-Za-z0-9_-]{8,}\b/g,
  /\bBearer\s+[A-Za-z0-9._~+/=-]{8,}/gi,
  /-----BEGIN [^-]+ PRIVATE KEY-----[\s\S]*?-----END [^-]+ PRIVATE KEY-----/g,
  /(\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key)\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;]+)/gi,
];

/** @param {unknown} value @param {number} [max] */
export function boundedText(value, max = MAX_TEXT_CHARS) {
  if (value === undefined || value === null) return "";
  let text;
  if (typeof value === "string") text = value;
  else {
    try { text = JSON.stringify(value); } catch { text = String(value); }
  }
  text = String(text).replaceAll("\u0000", "");
  for (const pattern of SECRET_PATTERNS) {
    pattern.lastIndex = 0;
    text = text.replace(pattern, (_match, prefix) => `${prefix ?? ""}[redacted]`);
  }
  if (text.length <= max) return text;
  return `${text.slice(0, Math.max(0, max - 1))}…`;
}

/** @param {unknown} value @param {number} [depth] */
export function sanitizeValue(value, depth = 0) {
  if (depth > 3) return "[depth limited]";
  if (typeof value === "string") return boundedText(value);
  if (typeof value === "number" || typeof value === "boolean") return value;
  if (value === null || value === undefined) return value;
  if (Array.isArray(value)) return value.slice(0, MAX_PACKET_ITEMS).map((item) => sanitizeValue(item, depth + 1));
  if (typeof value === "object") {
    const result = {};
    for (const [key, item] of Object.entries(value).slice(0, MAX_PACKET_ITEMS)) {
      result[boundedText(key, 120)] = sanitizeValue(item, depth + 1);
    }
    return result;
  }
  return boundedText(value);
}

/**
 * Reconstruct the newest packet-related entry. Invalid newest state fails
 * closed instead of falling back to an older packet.
 * @param {unknown} entries
 */
export function extractActivePacket(entries) {
  if (!Array.isArray(entries)) return null;
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (!entry || typeof entry !== "object") continue;
    const item = /** @type {Record<string, unknown>} */ (entry);
    if (item.type !== "custom") continue;
    if (item.customType === PACKET_CLEAR_TYPE) return null;
    if (item.customType !== PACKET_ENTRY_TYPE) continue;
    const data = item.data;
    if (!data || typeof data !== "object" || Array.isArray(data)) return null;
    const envelope = /** @type {Record<string, unknown>} */ (data);
    if (envelope.schema_version !== PACKET_SCHEMA_VERSION || !envelope.packet || typeof envelope.packet !== "object" || Array.isArray(envelope.packet)) return null;
    const packet = /** @type {Record<string, unknown>} */ (envelope.packet);
    if (validatePacket(packet).length !== 0) return null;
    return /** @type {Record<string, unknown>} */ (sanitizeValue(packet));
  }
  return null;
}

/** @param {unknown} packet */
export function formatTaskPacket(packet) {
  if (!packet || typeof packet !== "object") return ["No active task packet.", "", "Use task_packet to set one for this session."];
  const value = /** @type {Record<string, unknown>} */ (packet);
  const lines = [];
  const scalar = [
    ["Task ID", value.task_id],
    ["Mode", value.mode],
    ["Learning", value.learning],
    ["Goal", value.goal],
    ["Intended behavior", value.intended_behavior],
    ["Unchanged behavior", value.unchanged_behavior],
    ["Affected surfaces", value.affected_surfaces],
    ["Boundaries", value.boundaries],
    ["Current interpretation", value.current_interpretation],
    ["Current slice", value.current_slice],
    ["Question", value.question],
    ["Stop condition", value.stop_condition],
    ["Desired end state", value.desired_end_state],
    ["Unresolved decisions", value.open_decisions],
  ];
  for (const [label, item] of scalar) {
    if (item === undefined || item === null || item === "") continue;
    if (Array.isArray(item)) {
      lines.push(`${label}:`);
      for (const entry of item.slice(0, MAX_PACKET_ITEMS)) lines.push(`  • ${boundedText(entry)}`);
    } else {
      lines.push(`${label}: ${boundedText(item)}`);
    }
  }
  const listFields = [
    ["Constraints", value.constraints],
    ["Acceptance", value.acceptance],
    ["Decisions", value.decisions],
    ["Useful evidence", value.useful_evidence],
    ["Work areas", value.work_areas],
    ["Invariants", value.invariants],
    ["Results", value.results],
    ["Evidence", value.evidence],
  ];
  for (const [label, item] of listFields) {
    if (!Array.isArray(item) || item.length === 0) continue;
    lines.push(`${label}:`);
    for (const entry of item.slice(0, MAX_PACKET_ITEMS)) lines.push(`  • ${boundedText(entry)}`);
  }
  if (value.program && typeof value.program === "object" && !Array.isArray(value.program)) {
    const program = /** @type {Record<string, unknown>} */ (value.program);
    lines.push("Program:");
    for (const [label, field] of [
      ["Desired end state", program.desired_end_state],
      ["Work areas", program.work_areas],
      ["Dependency order", program.dependency_order],
      ["Current slice", program.current_slice],
    ]) {
      if (field === undefined || field === null || field === "") continue;
      lines.push(`  ${label}: ${boundedText(field)}`);
    }
  }
  return lines.length ? lines : ["Active task packet has no displayable fields."];
}

/** @param {unknown} value */
function statusValue(value) {
  const allowed = new Set(["queued", "pending", "running", "complete", "completed", "failed", "paused", "stopped", "detached"]);
  return typeof value === "string" && allowed.has(value) ? value : "unknown";
}

/** @param {unknown} value */
function messageKind(value) {
  const allowed = new Set(["instruction", "progress", "result", "failure", "control", "status", "message", "warning"]);
  return typeof value === "string" && allowed.has(value) ? value : "message";
}

/** @param {unknown} value */
function finiteTime(value, fallback) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/**
 * Extract only explicit user/assistant/tool text from a session record. System,
 * developer, thinking, and reasoning parts are intentionally excluded.
 * @param {unknown} value
 */
export function explicitSessionMessage(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = /** @type {Record<string, unknown>} */ (value);
  const message = record.message && typeof record.message === "object" && !Array.isArray(record.message)
    ? /** @type {Record<string, unknown>} */ (record.message)
    : record;
  const rawRole = typeof message.role === "string" ? message.role : "";
  if (rawRole !== "user" && rawRole !== "assistant" && rawRole !== "tool" && rawRole !== "toolResult") return null;
  const role = rawRole === "toolResult" ? "tool" : rawRole;
  if (rawRole === "toolResult") {
    const name = typeof message.toolName === "string" ? boundedText(message.toolName, 120) : typeof message.name === "string" ? boundedText(message.name, 120) : "tool";
    return { role, text: `[tool result: ${name}]` };
  }
  if (typeof message.content === "string") {
    const text = boundedText(message.content);
    return text ? { role, text } : null;
  }
  if (!Array.isArray(message.content)) return null;
  const parts = [];
  for (const part of message.content) {
    if (!part || typeof part !== "object" || Array.isArray(part)) continue;
    const item = /** @type {Record<string, unknown>} */ (part);
    const type = typeof item.type === "string" ? item.type.toLowerCase() : "";
    if (type.includes("think") || type.includes("reason")) continue;
    if (typeof item.text === "string") parts.push(item.text);
    else if (type === "toolcall" || type === "tool_call") {
      const name = typeof item.name === "string" ? item.name : typeof item.toolName === "string" ? item.toolName : "tool";
      parts.push(`[tool: ${boundedText(name, 120)}]`);
    }
  }
  const text = boundedText(parts.join("\n").trim());
  return text ? { role, text } : null;
}

/** @param {unknown} message @param {number} fallback */
export function normalizeMessage(message, fallback = Date.now()) {
  if (!message || typeof message !== "object") return null;
  const item = /** @type {Record<string, unknown>} */ (message);
  const text = boundedText(item.text ?? item.message ?? item.summary ?? "");
  if (!text) return null;
  const runId = typeof item.runId === "string" ? item.runId : undefined;
  const index = typeof item.index === "number" && Number.isInteger(item.index) ? item.index : undefined;
  return {
    id: typeof item.id === "string" && item.id ? item.id : `${item.kind ?? "message"}:${runId ?? "unknown"}:${index ?? ""}:${fallback}:${text.slice(0, 24)}`,
    kind: messageKind(item.kind),
    source: typeof item.source === "string" ? boundedText(item.source, 80) : "observed",
    agent: typeof item.agent === "string" ? boundedText(item.agent, 160) : undefined,
    runId,
    index,
    ts: finiteTime(item.ts, fallback),
    text,
  };
}

/** @param {unknown} values */
export function normalizeMessages(values) {
  if (!Array.isArray(values)) return [];
  const seen = new Set();
  const messages = [];
  for (const value of values) {
    const message = normalizeMessage(value);
    if (!message || seen.has(message.id)) continue;
    seen.add(message.id);
    messages.push(message);
  }
  messages.sort((left, right) => left.ts - right.ts || left.id.localeCompare(right.id));
  return messages.slice(-MAX_MESSAGES);
}

/** @param {unknown} record */
function stepAgent(record) {
  if (!record || typeof record !== "object") return "unknown";
  const value = /** @type {Record<string, unknown>} */ (record);
  return typeof value.agent === "string" && value.agent ? boundedText(value.agent, 160) : "unknown";
}

/** @param {Record<string, unknown>} run @param {Record<string, unknown>} step @param {number} index @param {Map<string, object>} instructions */
function projectStep(run, step, index, instructions) {
  const runId = typeof run.id === "string" ? run.id : typeof run.runId === "string" ? run.runId : "unknown";
  const key = `async:${runId}:${index}`;
  const instruction = instructions.get(key);
  const singleRunTask = Array.isArray(run.steps) && run.steps.length === 1 && typeof run.task === "string" ? boundedText(run.task) : undefined;
  const task = instruction?.text ?? (typeof step.task === "string" ? boundedText(step.task) : singleRunTask);
  const status = statusValue(step.status ?? run.state);
  const agent = stepAgent(step);
  const event = {
    key,
    source: "async",
    runId,
    index,
    agent,
    status,
    activityState: typeof step.activityState === "string" ? step.activityState : typeof run.activityState === "string" ? run.activityState : undefined,
    currentTool: typeof step.currentTool === "string" ? boundedText(step.currentTool, 120) : typeof run.currentTool === "string" ? boundedText(run.currentTool, 120) : undefined,
    currentPath: typeof step.currentPath === "string" ? boundedText(step.currentPath, 240) : typeof run.currentPath === "string" ? boundedText(run.currentPath, 240) : undefined,
    startedAt: finiteTime(step.startedAt, finiteTime(run.startedAt, Date.now())),
    updatedAt: finiteTime(step.lastActivityAt, finiteTime(run.lastUpdate, finiteTime(run.startedAt, Date.now()))),
    sessionFile: typeof step.sessionFile === "string" ? boundedText(step.sessionFile, 500) : undefined,
    transcriptPath: typeof step.transcriptPath === "string" ? boundedText(step.transcriptPath, 500) : undefined,
    task,
    model: typeof step.model === "string" ? boundedText(step.model, 180) : undefined,
    thinking: typeof step.thinking === "string" ? boundedText(step.thinking, 80) : undefined,
    error: typeof step.error === "string" ? boundedText(step.error) : typeof run.error === "string" ? boundedText(run.error) : undefined,
  };
  return event;
}

/** @param {unknown} value @param {string} [parentKey] @param {Array<object>} [out] */
function flattenNested(value, parentKey = "nested", out = []) {
  if (Array.isArray(value)) {
    for (let index = 0; index < value.length; index += 1) flattenNested(value[index], `${parentKey}:${index}`, out);
    return out;
  }
  if (!value || typeof value !== "object") return out;
  const run = /** @type {Record<string, unknown>} */ (value);
  const runId = typeof run.id === "string" ? run.id : parentKey;
  const steps = Array.isArray(run.steps) ? run.steps : [];
  if (steps.length === 0) {
    out.push({
      key: `nested:${runId}`,
      source: "nested",
      runId,
      index: undefined,
      agent: typeof run.agent === "string" ? boundedText(run.agent, 160) : typeof run.agents?.[0] === "string" ? boundedText(run.agents[0], 160) : "nested",
      parentRunId: typeof run.parentRunId === "string" ? boundedText(run.parentRunId, 160) : undefined,
      parentStepIndex: typeof run.parentStepIndex === "number" ? run.parentStepIndex : undefined,
      depth: typeof run.depth === "number" ? run.depth : undefined,
      task: typeof run.task === "string" ? boundedText(run.task) : undefined,
      status: statusValue(run.state),
      activityState: typeof run.activityState === "string" ? run.activityState : undefined,
      currentTool: typeof run.currentTool === "string" ? boundedText(run.currentTool, 120) : undefined,
      currentPath: typeof run.currentPath === "string" ? boundedText(run.currentPath, 240) : undefined,
      startedAt: finiteTime(run.startedAt, Date.now()),
      updatedAt: finiteTime(run.lastUpdate, finiteTime(run.startedAt, Date.now())),
      error: typeof run.error === "string" ? boundedText(run.error) : undefined,
    });
  } else {
    for (let index = 0; index < steps.length; index += 1) {
      const step = steps[index];
      if (!step || typeof step !== "object") continue;
      const stepRecord = /** @type {Record<string, unknown>} */ (step);
      out.push({
        key: `nested:${runId}:${index}`,
        source: "nested",
        runId,
        index,
        agent: stepAgent(stepRecord),
        parentRunId: typeof run.parentRunId === "string" ? boundedText(run.parentRunId, 160) : undefined,
        parentStepIndex: typeof run.parentStepIndex === "number" ? run.parentStepIndex : undefined,
        depth: typeof run.depth === "number" ? run.depth : undefined,
        task: typeof stepRecord.task === "string" ? boundedText(stepRecord.task) : undefined,
        status: statusValue(stepRecord.status ?? run.state),
        activityState: typeof stepRecord.activityState === "string" ? stepRecord.activityState : undefined,
        currentTool: typeof stepRecord.currentTool === "string" ? boundedText(stepRecord.currentTool, 120) : undefined,
        currentPath: typeof stepRecord.currentPath === "string" ? boundedText(stepRecord.currentPath, 240) : undefined,
        startedAt: finiteTime(stepRecord.startedAt, finiteTime(run.startedAt, Date.now())),
        updatedAt: finiteTime(stepRecord.lastActivityAt, finiteTime(run.lastUpdate, finiteTime(run.startedAt, Date.now()))),
        sessionFile: typeof stepRecord.sessionFile === "string" ? boundedText(stepRecord.sessionFile, 500) : undefined,
        error: typeof stepRecord.error === "string" ? boundedText(stepRecord.error) : typeof run.error === "string" ? boundedText(run.error) : undefined,
      });
      flattenNested(stepRecord.children, `nested:${runId}:${index}`, out);
    }
  }
  flattenNested(run.children, `nested:${runId}`, out);
  return out;
}

/** @param {unknown} runs @param {unknown} runtimeAgents @param {Map<string, object>} instructions */
function projectAgents(runs, runtimeAgents, instructions) {
  const agents = [];
  if (Array.isArray(runs)) {
    for (const candidate of runs) {
      if (!candidate || typeof candidate !== "object") continue;
      const run = /** @type {Record<string, unknown>} */ (candidate);
      const steps = Array.isArray(run.steps) ? run.steps : [];
      if (steps.length === 0) {
        const runId = typeof run.id === "string" ? run.id : "unknown";
        agents.push({
          key: `async:${runId}`,
          source: "async",
          runId,
          index: undefined,
          agent: typeof run.mode === "string" ? boundedText(run.mode, 160) : "run",
          status: statusValue(run.state),
          activityState: typeof run.activityState === "string" ? run.activityState : undefined,
          currentTool: typeof run.currentTool === "string" ? boundedText(run.currentTool, 120) : undefined,
          currentPath: typeof run.currentPath === "string" ? boundedText(run.currentPath, 240) : undefined,
          startedAt: finiteTime(run.startedAt, Date.now()),
          updatedAt: finiteTime(run.lastUpdate, finiteTime(run.startedAt, Date.now())),
          error: typeof run.error === "string" ? boundedText(run.error) : undefined,
        });
      } else {
        for (let index = 0; index < steps.length; index += 1) {
          const step = steps[index];
          if (!step || typeof step !== "object") continue;
          agents.push(projectStep(run, /** @type {Record<string, unknown>} */ (step), index, instructions));
        }
      }
      const nestedAgents = [];
      flattenNested(run.nestedChildren, `nested:${typeof run.id === "string" ? run.id : "run"}`, nestedAgents);
      for (const nestedAgent of nestedAgents) {
        const nestedInstruction = instructions.get(`nested:${nestedAgent.runId ?? "unknown"}:${nestedAgent.index ?? ""}`)
          ?? instructions.get(`async:${nestedAgent.runId ?? "unknown"}:${nestedAgent.index ?? ""}`);
        if (nestedInstruction && !nestedAgent.task) nestedAgent.task = nestedInstruction.text;
        agents.push(nestedAgent);
      }
    }
  }
  if (Array.isArray(runtimeAgents)) {
    for (const candidate of runtimeAgents) {
      if (!candidate || typeof candidate !== "object") continue;
      const agent = /** @type {Record<string, unknown>} */ (candidate);
      const key = typeof agent.key === "string" ? agent.key : undefined;
      if (!key || agents.some((item) => item.key === key)) continue;
      agents.push({
        key,
        source: typeof agent.source === "string" ? agent.source : "runtime",
        runId: typeof agent.runId === "string" ? agent.runId : undefined,
        index: typeof agent.index === "number" ? agent.index : undefined,
        agent: typeof agent.agent === "string" ? boundedText(agent.agent, 160) : "agent",
        status: statusValue(agent.status),
        activityState: typeof agent.activityState === "string" ? agent.activityState : undefined,
        currentTool: typeof agent.currentTool === "string" ? boundedText(agent.currentTool, 120) : undefined,
        currentPath: typeof agent.currentPath === "string" ? boundedText(agent.currentPath, 240) : undefined,
        startedAt: finiteTime(agent.startedAt, Date.now()),
        updatedAt: finiteTime(agent.updatedAt, Date.now()),
        task: typeof agent.task === "string" ? boundedText(agent.task) : undefined,
        error: typeof agent.error === "string" ? boundedText(agent.error) : undefined,
      });
    }
  }
  agents.sort((left, right) => {
    const active = (status) => status === "running" || status === "queued" || status === "pending";
    return Number(active(right.status)) - Number(active(left.status)) || right.updatedAt - left.updatedAt || left.key.localeCompare(right.key);
  });
  return agents.slice(0, MAX_AGENTS);
}

/**
 * Create the bounded UI projection from persisted run summaries and explicit
 * runtime messages. The function intentionally accepts plain objects so it is
 * easy to test with malformed fixtures.
 */
export function projectInspectorState(input = {}) {
  if (!input || typeof input !== "object") input = {};
  const now = finiteTime(input.now, Date.now());
  const instructions = new Map();
  for (const message of normalizeMessages(input.messages)) {
    if (message.kind !== "instruction") continue;
    const key = `${message.source === "nested" ? "nested" : "async"}:${message.runId ?? "unknown"}:${message.index ?? ""}`;
    instructions.set(key, message);
  }
  const agents = projectAgents(input.runs, input.runtimeAgents, instructions);
  const messages = normalizeMessages([
    ...(Array.isArray(input.messages) ? input.messages : []),
    ...agents.flatMap((agent) => {
      const result = [];
      if (agent.task) result.push({ kind: "instruction", source: agent.source, agent: agent.agent, runId: agent.runId, index: agent.index, ts: agent.startedAt, text: agent.task, id: `instruction:${agent.key}` });
      const statusText = [agent.status, agent.activityState, agent.currentTool ? `tool ${agent.currentTool}` : "", agent.currentPath ? agent.currentPath : ""].filter(Boolean).join(" · ");
      if (statusText) result.push({ kind: "status", source: agent.source, agent: agent.agent, runId: agent.runId, index: agent.index, ts: agent.updatedAt, text: statusText, id: `status:${agent.key}:${agent.updatedAt}` });
      if (agent.error) result.push({ kind: "failure", source: agent.source, agent: agent.agent, runId: agent.runId, index: agent.index, ts: agent.updatedAt, text: agent.error, id: `failure:${agent.key}:${agent.updatedAt}` });
      return result;
    }),
  ]);
  return {
    packet: input.packet && typeof input.packet === "object" ? sanitizeValue(input.packet) : null,
    agents,
    messages,
    warnings: Array.isArray(input.warnings) ? input.warnings.slice(0, 20).map((item) => boundedText(item, 500)) : [],
    updatedAt: now,
  };
}

/** @param {unknown} status */
export function statusGlyph(status) {
  switch (status) {
    case "running": return "●";
    case "queued": case "pending": return "◦";
    case "complete": case "completed": return "✓";
    case "paused": case "stopped": case "detached": return "■";
    case "failed": return "✗";
    default: return "?";
  }
}

/** @param {unknown} value */
export function reduceInspectorKey(value) {
  const key = typeof value === "string" ? value : "";
  if (key === "j" || key === "down" || key === "tab") return { type: "next-agent" };
  if (key === "k" || key === "up" || key === "shift+tab") return { type: "previous-agent" };
  if (key === "1" || key.toLowerCase() === "t") return { type: "tab", tab: "task" };
  if (key === "2" || key.toLowerCase() === "f") return { type: "tab", tab: "fleet" };
  if (key === "3" || key.toLowerCase() === "m") return { type: "tab", tab: "messages" };
  if (key.toLowerCase() === "a") return { type: "all-messages" };
  if (key.toLowerCase() === "v") return { type: "transcript" };
  if (key.toLowerCase() === "r") return { type: "refresh" };
  if (key === "escape" || key === "q" || key === "ctrl+c") return { type: "close" };
  return { type: "noop" };
}

/** @param {Array<object>} agents @param {number} selected @param {number} delta */
export function cycleAgentIndex(agents, selected, delta) {
  if (!Array.isArray(agents) || agents.length === 0) return 0;
  const next = (selected + delta) % agents.length;
  return next < 0 ? agents.length - 1 : next;
}
