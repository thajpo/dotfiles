// @ts-check
/**
 * workflow-state core module
 *
 * Dependency-free validation, rendering, and manifest helpers.
 * Importable by Node tests without Pi runtime packages.
 */

// ============================================================================
// Constants
// ============================================================================

/** Entry type and schema version used in session JSONL custom entries. */
export const PACKET_ENTRY_TYPE = "workflow-task-packet";
export const PACKET_SCHEMA_VERSION = 1;

/** Entry type for a clear/tombstone entry. */
export const PACKET_CLEAR_TYPE = "workflow-task-packet-clear";

/** Valid modes. */
export const MODES = /** @type {const} */ (["fast", "rip", "build", "major"]);

/**
 * FAST requires exactly: task_id, mode, learning, goal, constraints, acceptance.
 * No other fields are permitted.
 */
export const FAST_REQUIRED = /** @type {const} */ (["task_id", "mode", "learning", "goal", "constraints", "acceptance"]);

/**
 * RIP initially requires only task_id, mode, learning, question, environment,
 * useful_evidence, and stop_condition. Hypotheses/results are evolving fields.
 */
export const RIP_REQUIRED = /** @type {const} */ (["task_id", "mode", "learning", "question", "environment", "useful_evidence", "stop_condition"]);

/**
 * BUILD requires: task_id, mode, learning, intended_behavior,
 * unchanged_behavior, affected_surfaces, decisions, acceptance.
 */
export const BUILD_REQUIRED = /** @type {const} */ (["task_id", "mode", "learning", "intended_behavior", "unchanged_behavior", "affected_surfaces", "decisions", "acceptance"]);

/**
 * MAJOR requires: task_id, mode, learning, program.desired_end_state,
 * program.work_areas, program.dependency_order, program.current_slice,
 * decisions, open_decisions.
 */
export const MAJOR_REQUIRED = /** @type {const} */ ([
  "task_id", "mode", "learning",
  "program.desired_end_state",
  "program.work_areas",
  "program.dependency_order",
  "program.current_slice",
  "decisions", "open_decisions",
]);

/** Shared optional fields permitted across all modes (except FAST where extras are rejected). */
export const SHARED_OPTIONAL = /** @type {const} */ ([
  "original_request",
  "current_interpretation",
  "goal",
  "current_slice",
  "decisions",
  "boundaries",
  "acceptance",
  "open_decisions",
  "evidence",
  "program",
  "candidates",
  "remaining_uncertainty",
]);

/** Allowed top-level field sets per mode. */
const MODE_FIELD_SETS = {
  fast: new Set(FAST_REQUIRED),
  rip: new Set([...RIP_REQUIRED, ...SHARED_OPTIONAL, "current_hypotheses", "experiment_results"]),
  build: new Set([...BUILD_REQUIRED, ...SHARED_OPTIONAL]),
  major: new Set(["task_id", "mode", "learning", "decisions", "open_decisions", "program", ...SHARED_OPTIONAL]),
};

const MODE_REQUIRED = {
  fast: FAST_REQUIRED,
  rip: RIP_REQUIRED,
  build: BUILD_REQUIRED,
  major: MAJOR_REQUIRED,
};

/** Packet and context-manifest bounds. */
export const MAX_PACKET_BYTES = 32 * 1024;
export const MAX_ARRAY_ITEMS = 100;
export const MAX_STRING_CHARS = 8_000;
export const MAX_ARRAY_STRING_CHARS = 2_000;
export const MAX_MANIFEST_SAMPLES = 100;

// ============================================================================
// Mode helpers
// ============================================================================

/**
 * @param {unknown} value
 * @returns {value is "fast" | "rip" | "build" | "major"}
 */
export function isValidMode(value) {
  return typeof value === "string" && MODES.includes(/** @type {any} */ (value));
}

// ============================================================================
// Validation
// ============================================================================

const TEXT_FIELDS = new Set([
  "task_id", "original_request", "current_interpretation", "goal", "current_slice",
  "question", "environment", "stop_condition", "intended_behavior", "unchanged_behavior",
]);
const STRING_ARRAY_FIELDS = new Set([
  "constraints", "decisions", "boundaries", "acceptance", "open_decisions",
  "useful_evidence", "current_hypotheses", "experiment_results", "affected_surfaces",
  "remaining_uncertainty",
]);
const EVIDENCE_FIELDS = new Set(["summaries", "artifact_paths"]);
const PROGRAM_FIELDS = new Set([
  "desired_end_state", "work_areas", "dependency_order", "completed_slices", "current_slice", "future_slices",
]);
const CANDIDATE_FIELDS = new Set([
  "candidate_id", "hypothesis", "base_commit", "result", "decisive_evidence",
  "useful_pieces", "superseding_candidate",
]);

/**
 * @param {unknown} value
 * @returns {value is Record<string, unknown>}
 */
function isObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

/**
 * @param {unknown} value
 * @param {string} field
 * @param {string[]} errors
 * @param {boolean} [required]
 */
function validateString(value, field, errors, required = false) {
  if (value === undefined && !required) return;
  if (typeof value !== "string" || (required && value.trim().length === 0)) {
    errors.push(`${field}: must be ${required ? "a non-empty" : "a"} string`);
    return;
  }
  if (value.length > MAX_STRING_CHARS) errors.push(`${field}: exceeds ${MAX_STRING_CHARS} characters`);
}

/**
 * @param {unknown} value
 * @param {string} field
 * @param {string[]} errors
 * @param {boolean} [nonEmpty]
 */
function validateStringArray(value, field, errors, nonEmpty = false) {
  if (!Array.isArray(value)) {
    errors.push(`${field}: must be an array`);
    return;
  }
  if (nonEmpty && value.length === 0) errors.push(`${field}: must contain at least one item`);
  if (value.length > MAX_ARRAY_ITEMS) errors.push(`${field}: exceeds ${MAX_ARRAY_ITEMS} items`);
  value.forEach((item, index) => {
    if (typeof item !== "string") errors.push(`${field}[${index}]: must be a string`);
    else if (item.length > MAX_ARRAY_STRING_CHARS) errors.push(`${field}[${index}]: exceeds ${MAX_ARRAY_STRING_CHARS} characters`);
  });
}

/** @param {Record<string, unknown>} value @param {Set<string>} allowed @param {string} path @param {string[]} errors */
function rejectUnknownFields(value, allowed, path, errors) {
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) errors.push(`${path}${key}: unknown field`);
  }
}

/**
 * Validate a bounded task packet and return actionable errors.
 * @param {Record<string, unknown>} packet
 * @returns {string[]}
 */
export function validatePacket(packet) {
  /** @type {string[]} */
  const errors = [];
  if (!isObject(packet)) return ["packet must be a non-null object"];

  let serialized;
  try {
    serialized = JSON.stringify(packet);
  } catch {
    return ["packet: must be JSON-serializable"];
  }
  if (byteLength(serialized) > MAX_PACKET_BYTES) errors.push(`packet: exceeds ${MAX_PACKET_BYTES} bytes`);

  const mode = packet.mode;
  if (!isValidMode(mode)) {
    errors.push(`mode: must be one of ${MODES.join(", ")}`);
    return errors;
  }
  if (packet.learning !== "off" && packet.learning !== "light" && packet.learning !== "deep") {
    errors.push("learning: must be one of off, light, deep");
  }

  const allowed = MODE_FIELD_SETS[mode];
  rejectUnknownFields(packet, allowed, "", errors);

  const required = MODE_REQUIRED[mode];
  for (const field of required) {
    if (field.startsWith("program.")) {
      const subKey = field.slice("program.".length);
      if (!isObject(packet.program) || !(subKey in packet.program)) errors.push(`${field}: required`);
    } else if (!(field in packet)) errors.push(`${field}: required`);
  }

  for (const [field, value] of Object.entries(packet)) {
    if (TEXT_FIELDS.has(field)) validateString(value, field, errors, field === "task_id" || required.includes(/** @type {any} */ (field)));
    if (STRING_ARRAY_FIELDS.has(field)) {
      const nonEmpty = (field === "acceptance" && (mode === "fast" || mode === "build")) || field === "constraints";
      validateStringArray(value, field, errors, nonEmpty);
    }
  }

  if (packet.evidence !== undefined) {
    if (!isObject(packet.evidence)) errors.push("evidence: must be an object");
    else {
      rejectUnknownFields(packet.evidence, EVIDENCE_FIELDS, "evidence.", errors);
      validateStringArray(packet.evidence.summaries, "evidence.summaries", errors);
      validateStringArray(packet.evidence.artifact_paths, "evidence.artifact_paths", errors);
    }
  }

  if (packet.program !== undefined) {
    if (!isObject(packet.program)) errors.push("program: must be an object");
    else {
      rejectUnknownFields(packet.program, PROGRAM_FIELDS, "program.", errors);
      validateString(packet.program.desired_end_state, "program.desired_end_state", errors, mode === "major");
      validateString(packet.program.current_slice, "program.current_slice", errors, mode === "major");
      for (const field of ["work_areas", "dependency_order", "completed_slices", "future_slices"]) {
        const value = packet.program[field];
        if (value !== undefined || (mode === "major" && (field === "work_areas" || field === "dependency_order"))) {
          validateStringArray(value, `program.${field}`, errors, mode === "major" && (field === "work_areas" || field === "dependency_order"));
        }
      }
    }
  }

  if (packet.candidates !== undefined) {
    if (!Array.isArray(packet.candidates)) errors.push("candidates: must be an array");
    else {
      if (packet.candidates.length > MAX_ARRAY_ITEMS) errors.push(`candidates: exceeds ${MAX_ARRAY_ITEMS} items`);
      packet.candidates.forEach((candidate, index) => {
        const path = `candidates[${index}]`;
        if (typeof candidate === "string") validateString(candidate, path, errors);
        else if (!isObject(candidate)) errors.push(`${path}: must be a string or object`);
        else {
          rejectUnknownFields(candidate, CANDIDATE_FIELDS, `${path}.`, errors);
          for (const [field, value] of Object.entries(candidate)) {
            if (Array.isArray(value)) validateStringArray(value, `${path}.${field}`, errors);
            else validateString(value, `${path}.${field}`, errors);
          }
        }
      });
    }
  }

  return errors;
}

/**
 * Type guard: returns true if packet has no validation errors.
 * @param {Record<string, unknown>} packet
 * @returns {boolean}
 */
export function isValidPacket(packet) {
  return validatePacket(packet).length === 0;
}

// ============================================================================
// Rendering — lossless tagged JSON
// ============================================================================

/**
 * Render a packet as compact tagged JSON, lossless and parseable.
 * @param {Record<string, unknown> | null | undefined} packet
 * @returns {string}
 */
export function renderCompact(packet) {
  if (!packet || typeof packet !== "object") return "";
  return "TASK_PACKET " + JSON.stringify(packet);
}

/**
 * Parse a tagged task-packet JSON string back to an object.
 * Returns null if the string is not valid tagged JSON.
 * @param {string | null | undefined} text
 * @returns {Record<string, unknown> | null}
 */
export function parseTaggedPacket(text) {
  if (typeof text !== "string") return null;
  const json = text.startsWith("TASK_PACKET ") ? text.slice("TASK_PACKET ".length) : text;
  try {
    const parsed = JSON.parse(json);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return /** @type {Record<string, unknown>} */ (parsed);
    }
    return null;
  } catch {
    return null;
  }
}

// ============================================================================
// Session entry helpers
// ============================================================================

/**
 * Find the latest active task packet from a session entry array.
 * Walks backwards: if the most recent packet-related entries end with a
 * tombstone (clear), returns null. Returns the most recent non-cleared packet.
 * @param {Array<Record<string, unknown>> | null | undefined} entries
 * @returns {Record<string, unknown> | null}
 */
export function latestActivePacket(entries) {
  if (!Array.isArray(entries)) return null;

  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i];
    if (!entry || typeof entry !== "object") continue;

    const type = /** @type {string | undefined} */ (entry.type);
    const customType = /** @type {string | undefined} */ (entry.customType);
    if (type !== "custom") continue;

    // The newest packet-related entry is authoritative. Corrupt or unsupported
    // state fails closed instead of resurrecting an older packet.
    if (customType === PACKET_CLEAR_TYPE) return null;
    if (customType === PACKET_ENTRY_TYPE) {
      const data = /** @type {Record<string, unknown> | undefined} */ (entry.data);
      if (!isObject(data) || data.schema_version !== PACKET_SCHEMA_VERSION || !isObject(data.packet)) return null;
      return validatePacket(data.packet).length === 0 ? data.packet : null;
    }
  }

  return null;
}

// ============================================================================
// Path helper — session-scoped artifacts dir
// ============================================================================

/** Directory name for workflow context artifacts. */
export const ARTIFACTS_DIR_NAME = "workflow-artifacts";

/**
 * Resolve the session-scoped workflow-artifacts directory from a session file path.
 * @param {string} sessionFile - absolute path to session JSONL
 * @returns {string} artifacts directory path
 */
export function workflowArtifactsDirForSession(sessionFile) {
  const sessionDir = pathDirname(sessionFile);
  const sessionBase = pathBasename(sessionFile, ".jsonl");
  return pathJoin(sessionDir, sessionBase, ARTIFACTS_DIR_NAME);
}

/**
 * @param {string} p
 * @returns {string}
 */
function pathDirname(p) {
  const idx = p.lastIndexOf("/");
  if (idx === -1) return ".";
  return idx === 0 ? "/" : p.slice(0, idx);
}

/**
 * @param {string} p
 * @param {string} ext
 * @returns {string}
 */
function pathBasename(p, ext) {
  const idx = p.lastIndexOf("/");
  const name = idx === -1 ? p : p.slice(idx + 1);
  if (ext && name.endsWith(ext)) return name.slice(0, -ext.length);
  return name;
}

/**
 * @param {...string} parts
 * @returns {string}
 */
function pathJoin(...parts) {
  return parts.filter((p) => p.length > 0).join("/").replace(/\/+/g, "/");
}

// ============================================================================
// Context manifest helpers
// ============================================================================

/**
 * Compute SHA-256 hex digest of a string.
 * @param {string} str
 * @returns {Promise<string>}
 */
export async function computeSha256(str) {
  const bytes = new TextEncoder().encode(str);
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  const hex = Array.from(new Uint8Array(hash))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return hex;
}

/**
 * @param {string} str
 * @returns {number}
 */
export function byteLength(str) {
  return new TextEncoder().encode(str).length;
}

/**
 * Build a context audit manifest.
 * Raw prompt/messages included only when includeRaw is true.
 *
 * @param {object} options
 * @param {string} options.generationId
 * @param {string} options.systemPrompt
 * @param {Array<{role: string, content: string}>} options.messages
 * @param {string} [options.sessionId]
 * @param {string} [options.sessionFile]
 * @param {object} [options.modelInfo]
 * @param {string} [options.modelInfo.provider]
 * @param {string} [options.modelInfo.id]
 * @param {string} [options.thinkingLevel]
 * @param {boolean} [options.isChild]
 * @param {string} [options.childAgent]
 * @param {number} [options.childIndex]
 * @param {number|undefined} [options.contextUsagePercent]
 * @param {number|undefined} [options.contextTokens]
 * @param {number|undefined} [options.contextWindow]
 * @param {Array<{path: string, content: string}>} [options.contextFiles]
 * @param {string[]} [options.selectedTools]
 * @param {string[]} [options.skillNames]
 * @param {string|undefined} [options.taskPacketJson]
 * @param {number|undefined} [options.taskPacketSize]
 * @param {boolean} [options.includeRaw]
 * @param {string|undefined} [options.submittedPrompt]
 * @returns {Promise<Record<string, unknown>>}
 */
export async function buildContextManifest(options) {
  const {
    generationId,
    systemPrompt,
    messages,
    sessionId,
    sessionFile,
    modelInfo,
    thinkingLevel,
    isChild,
    childAgent,
    childIndex,
    contextUsagePercent,
    contextTokens,
    contextWindow,
    contextFiles,
    selectedTools,
    skillNames,
    taskPacketJson,
    taskPacketSize,
    includeRaw,
    submittedPrompt,
  } = options;

  const messageMeta = await Promise.all(
    (messages || []).map(async (m) => {
      const content = typeof m.content === "string" ? m.content : JSON.stringify(m.content ?? "");
      return {
        role: m.role,
        chars: content.length,
        bytes: byteLength(content),
        hash: await computeSha256(content),
        ...(includeRaw ? { content } : {}),
      };
    }),
  );

  const contextFileMeta = await Promise.all(
    (contextFiles || []).map(async (cf) => {
      const content = typeof cf.content === "string" ? cf.content : "";
      return {
        path: cf.path,
        bytes: byteLength(content),
        chars: content.length,
        hash: await computeSha256(content),
        ...(includeRaw ? { content } : {}),
      };
    }),
  );

  /** @type {Record<string, unknown>} */
  const manifest = {
    manifest_version: 1,
    generation_id: generationId,
    captured_at: Date.now(),
    session_id: sessionId ?? null,
    session_file: sessionFile ?? null,
    is_child: isChild ?? false,
    child_agent: childAgent ?? null,
    child_index: childIndex ?? null,
    model: modelInfo
      ? { provider: modelInfo.provider ?? null, id: modelInfo.id ?? null }
      : null,
    thinking_level: thinkingLevel ?? null,
    context_usage:
      contextTokens != null || contextWindow != null || contextUsagePercent != null
        ? {
            tokens: contextTokens ?? null,
            context_window: contextWindow ?? null,
            percent: contextUsagePercent ?? null,
          }
        : null,
    context_files: contextFileMeta.length > 0 ? contextFileMeta : null,
    selected_tools: selectedTools && selectedTools.length > 0 ? selectedTools : null,
    skill_names: skillNames && skillNames.length > 0 ? skillNames : null,
    task_packet: taskPacketJson
      ? { size: taskPacketSize ?? byteLength(taskPacketJson), present: true }
      : { present: false, size: 0 },
  };

  manifest.system_prompt = {
    chars: systemPrompt.length,
    bytes: byteLength(systemPrompt),
    hash: await computeSha256(systemPrompt),
    ...(includeRaw ? { text: systemPrompt } : {}),
  };

  manifest.messages = messageMeta;

  if (submittedPrompt != null && submittedPrompt.length > 0) {
    manifest.submitted_prompt = {
      chars: submittedPrompt.length,
      bytes: byteLength(submittedPrompt),
      hash: await computeSha256(submittedPrompt),
      ...(includeRaw ? { text: submittedPrompt } : {}),
    };
  }

  return manifest;
}

/**
 * Bound the sample array to MAX_MANIFEST_SAMPLES (drop oldest, keep newest).
 * @param {Array<Record<string, unknown>> | null | undefined} samples
 * @returns {Array<Record<string, unknown>>}
 */
export function boundManifestSamples(samples) {
  if (!Array.isArray(samples)) return [];
  if (samples.length <= MAX_MANIFEST_SAMPLES) return samples;
  return samples.slice(samples.length - MAX_MANIFEST_SAMPLES);
}
