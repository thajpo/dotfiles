import { lstatSync, realpathSync } from "node:fs";
import { isAbsolute, relative, resolve, sep } from "node:path";
import { createHash, randomBytes } from "node:crypto";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { PISEC_OPERATION_CATALOGUE } from "./pisec-operation-catalogue.generated";
import { FIRST_MATE_PROMPT, SECRETARY_PROMPT, WORKER_PROMPT } from "./pisec-prompts";

type JsonObject = Record<string, unknown>;
type RuntimeState = "starting" | "working" | "blocked" | "idle" | "stopped" | "missing" | "error" | "unknown";
type SessionSwitchReason = "new" | "resume" | "fork" | "handoff";
type SessionReference = { kind: "path" | "id" | null; value: string | null };
type RuntimeContext = { hasUI: boolean; sessionManager: { getSessionFile?: () => string | undefined } };
type PreparedTurn = { taskPacket?: unknown; bootstrap?: unknown; attention?: unknown };
type PisecMessageAPI = {
  sendMessage?: (message: JsonObject, options?: { triggerTurn?: boolean }) => void;
};
type RuntimeGate = "unattested" | "ready" | "blocked";

const ROLE = process.env.PISEC_ROLE;
const RUNTIME_SOCKET = process.env.PISEC_RUNTIME_SOCKET;
const SECRETARY_SOCKET = process.env.PISEC_SECRETARY_SOCKET;
const FLEET_SOCKET = process.env.PISEC_FLEET_SOCKET;
const RUNTIME_TOKEN = process.env.PISEC_RUNTIME_TOKEN;
const RUNTIME_GENERATION = process.env.PISEC_RUNTIME_GENERATION;
const WORKSTREAM_ID = process.env.PISEC_WORKSTREAM_ID;
const INSTANCE_ID = process.env.PISEC_RUNTIME_INSTANCE_ID;
const START_SOURCE = process.env.PISEC_SESSION_START_SOURCE === "resume" ? "resume" : "startup";
const SURFACE_ID = process.env.PISEC_SURFACE_ID;
const HARNESS_HOME = process.env.PI_CODING_AGENT_DIR;
const CATALOGUE_OPERATIONS = new Set(PISEC_OPERATION_CATALOGUE.map(entry => entry.operation));
const IDEMPOTENT_OPERATIONS = new Set([
  "coordination.answer",
  "fleet.issue.add_context",
  "fleet.issue.request_remediation",
  "fleet.issue.request_verification",
  "help.request",
  "issue.add_context",
  "issue.report",
  "issue.request_verification",
  "issue.verify",
  "issue.link_remediation",
  "research.add_context",
  "research.answer",
  "research.decline",
  "research.request",
  "research.request_context",
  "workstream.checkpoint",
  "workstream.prepare",
]);
const WORKER_DIAGNOSTIC_TOOLS = [
  "pisec_list_attention",
  "pisec_inspect_attention",
  "pisec_show_task_packet",
  "pisec_list_coordination",
  "pisec_inspect_coordination",
  "pisec_list_issues",
  "pisec_inspect_issue",
  "pisec_check_secretary_research",
  "pisec_inspect_secretary_research",
  "pisec_request_help",
  "pisec_report_issue",
];
const WORKER_RECOVERY_OPERATIONS = new Set([
  "attention.inspect",
  "attention.list",
  "coordination.inspect",
  "coordination.list",
  "help.request",
  "issue.inspect",
  "issue.list",
  "issue.report",
  "research.inspect",
  "research.list",
  "task.get",
]);
let workerRuntimeGate: RuntimeGate = "unattested";
let savedWorkerTools: string[] | undefined;

function canonicalValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.keys(value as JsonObject).sort().map(key => [key, canonicalValue((value as JsonObject)[key])]));
}

function generatedIdempotencyKey(operation: string, payload: JsonObject, nativeToolId: string): string {
  const canonical = JSON.stringify(canonicalValue({ operation, nativeToolId, payload }));
  return `adapter:omp:${createHash("sha256").update(canonical).digest("hex")}`;
}

function adapterPayload(operation: string, params: JsonObject, nativeToolId: string, map?: (params: JsonObject) => JsonObject): JsonObject {
  const mapped = map ? map(params) : { ...params };
  const payload = { ...mapped };
  delete payload.idempotencyKey;
  delete payload.idempotency_key;
  if (IDEMPOTENT_OPERATIONS.has(operation) || (operation === "workstream.retire" && payload.remediationIssueId !== undefined)) {
    payload.idempotencyKey = generatedIdempotencyKey(operation, payload, nativeToolId);
  }
  return payload;
}

function workerCanUse(operation: string): boolean {
  return ROLE !== "worker" || workerRuntimeGate === "ready" || WORKER_RECOVERY_OPERATIONS.has(operation);
}

async function setWorkerRuntimeGate(pi: ExtensionAPI, next: RuntimeGate): Promise<void> {
  if (ROLE !== "worker" || workerRuntimeGate === next) return;
  if (next === "blocked") {
    const getter = (pi as unknown as { getActiveTools?: () => string[] }).getActiveTools;
    if (savedWorkerTools === undefined && typeof getter === "function") {
      try {
        savedWorkerTools = [...getter()];
      } catch {
        savedWorkerTools = undefined;
      }
    }
    workerRuntimeGate = next;
    try {
      await Promise.resolve(pi.setActiveTools(WORKER_DIAGNOSTIC_TOOLS));
    } catch {
      // The wrapper gate below remains authoritative if tool activation cannot
      // be changed by the host API.
    }
    return;
  }
  workerRuntimeGate = next;
  if (next === "ready" && savedWorkerTools !== undefined) {
    const restore = savedWorkerTools;
    savedWorkerTools = undefined;
    try {
      await Promise.resolve(pi.setActiveTools(restore));
    } catch {
      // A controlled restart can restore host tools if the current surface is
      // unable to accept the saved activation set.
    }
  }
}

function sendPisecMessage(pi: ExtensionAPI, content: string, details: JsonObject, triggerTurn: boolean): void {
  const sender = (pi as unknown as PisecMessageAPI).sendMessage;
  if (typeof sender !== "function") throw new Error("Pisec extension cannot deliver typed control messages");
  sender({ customType: "pisec", content, display: true, details: { source: "pisec", ...details } }, { triggerTurn });
}
function isPisecRole(value: string | undefined): value is "secretary" | "first_mate" | "worker" {
  return value === "secretary" || value === "first_mate" || value === "worker";
}

function textResult(value: unknown, isError = false) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return {
    content: [{ type: "text" as const, text }],
    details: value,
    ...(isError ? { isError: true } : {}),
  };
}

function randomHex(bytes = 16): string {
  return randomBytes(bytes).toString("hex");
}

function requestId(): string {
  return `req_${randomHex(16)}`;
}

function sessionReference(ctx: RuntimeContext): SessionReference {
  const value = ctx.sessionManager.getSessionFile?.();
  if (!value) return { kind: null, value: null };
  if (value.startsWith("/")) return ownerControlledSessionFile(value) ? { kind: "path", value } : { kind: null, value: null };
  return { kind: "id", value };
}
function ownerControlledSessionFile(value: unknown): value is string {
  if (!HARNESS_HOME || typeof value !== "string" || !isAbsolute(value) || !value.endsWith(".jsonl")) return false;
  if (typeof process.getuid !== "function") return false;
  const uid = process.getuid();
  const sessionRoot = resolve(HARNESS_HOME, "sessions");
  try {
    const rootInfo = lstatSync(sessionRoot);
    const targetInfo = lstatSync(value);
    if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink() || rootInfo.uid !== uid || (rootInfo.mode & 0o022) !== 0) return false;
    if (!targetInfo.isFile() || targetInfo.isSymbolicLink() || targetInfo.uid !== uid || (targetInfo.mode & 0o022) !== 0) return false;
    const resolvedRoot = realpathSync(sessionRoot);
    const resolvedTarget = realpathSync(value);
    if (resolvedTarget !== resolve(value)) return false;
    const withinRoot = relative(resolvedRoot, resolvedTarget);
    return withinRoot !== "" && withinRoot !== ".." && !withinRoot.startsWith(`..${sep}`) && !isAbsolute(withinRoot);
  } catch {
    return false;
  }
}


function runtimePayload(
  state: RuntimeState,
  event: "session_start" | "lifecycle" | "session_shutdown",
  reason: SessionSwitchReason | null,
  seq: number,
  ctx: RuntimeContext,
  reference?: SessionReference,
): JsonObject {
  const current = reference ?? sessionReference(ctx);
  return {
    workstreamId: WORKSTREAM_ID,
    runtimeInstanceId: INSTANCE_ID,
    seq,
    event,
    reason,
    state,
    nativeSessionKind: current.kind,
    nativeSessionValue: current.value,
    startSource: START_SOURCE,
    surfaceId: SURFACE_ID,
    token: RUNTIME_TOKEN,
    generation: RUNTIME_GENERATION,
  };
}

function runtimeAuth(): JsonObject {
  return {
    workstreamId: WORKSTREAM_ID,
    runtimeInstanceId: INSTANCE_ID,
    surfaceId: SURFACE_ID,
    token: RUNTIME_TOKEN,
    generation: RUNTIME_GENERATION,
  };
}

function socketRequest(socketPath: string, operation: string, payload: JsonObject, signal?: AbortSignal): Promise<unknown> {
  if (!CATALOGUE_OPERATIONS.has(operation)) return Promise.reject(new Error(`Pisec operation is not in the checked catalogue: ${operation}`));
  const { promise, resolve, reject } = Promise.withResolvers<unknown>();
  if (signal?.aborted) {
    reject(new Error("request aborted"));
    return promise;
  }
  const requestIdValue = requestId();
  const request = JSON.stringify({ protocolVersion: 1, requestId: requestIdValue, operation, payload }) + "\n";
  let body = "";
  let settled = false;
  let closeSocket: (() => void) | undefined;
  let timeout: Timer;
  const finish = (error?: Error, value?: unknown) => {
    if (settled) return;
    settled = true;
    clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
    closeSocket?.();
    if (error) reject(error);
    else resolve(value);
  };
  const abort = () => finish(new Error("request aborted"));
  signal?.addEventListener("abort", abort, { once: true });
  timeout = setTimeout(() => finish(new Error("broker request timed out")), 30_000);
  void Bun.connect({
    unix: socketPath,
    socket: {
      open(socket) {
        closeSocket = socket.end.bind(socket);
        socket.write(request);
      },
      data(_socket, chunk) {
        body += chunk.toString("utf8");
        if (Buffer.byteLength(body, "utf8") > 64 * 1024) {
          finish(new Error("broker response is too large"));
          return;
        }
        if (!body.includes("\n")) return;
        const line = body.slice(0, body.indexOf("\n"));
        try {
          const response = JSON.parse(line) as JsonObject;
          if (response.requestId !== requestIdValue) throw new Error("broker response id mismatch");
          if (response.ok !== true) {
            const error = response.error as JsonObject | undefined;
            throw new Error(typeof error?.message === "string" ? error.message : "broker request failed");
          }
          finish(undefined, response.result);
        } catch (error) {
          finish(error instanceof Error ? error : new Error(String(error)));
        }
      },
      close() {
        finish(new Error("broker closed without a complete response"));
      },
      error(_socket, error) {
        finish(error);
      },
    },
  }).catch(error => finish(error instanceof Error ? error : new Error(String(error))));
  return promise;
}

async function runtimeRequest(payload: JsonObject, signal?: AbortSignal): Promise<unknown> {
  if (!RUNTIME_SOCKET || !RUNTIME_TOKEN || !RUNTIME_GENERATION || !WORKSTREAM_ID || !INSTANCE_ID || !SURFACE_ID) throw new Error("Pisec runtime binding is incomplete");
  return socketRequest(RUNTIME_SOCKET, "runtime.report", payload, signal);
}

async function runtimeOperation(operation: string, payload: JsonObject = {}, signal?: AbortSignal): Promise<unknown> {
  if (!RUNTIME_SOCKET || !RUNTIME_TOKEN || !RUNTIME_GENERATION || !WORKSTREAM_ID || !INSTANCE_ID || !SURFACE_ID) throw new Error("Pisec runtime binding is incomplete");
  return socketRequest(RUNTIME_SOCKET, operation, { ...runtimeAuth(), ...payload }, signal);
}
async function semanticRequest(operation: string, payload: JsonObject, signal?: AbortSignal): Promise<unknown> {
  const socket = ROLE === "first_mate" ? FLEET_SOCKET : SECRETARY_SOCKET;
  if (!socket || !RUNTIME_TOKEN) throw new Error("Pisec control binding is incomplete");
  return socketRequest(socket, operation, { ...payload, authToken: RUNTIME_TOKEN }, signal);
}

function renderTaskPacket(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "(unrenderable task packet)";
  }
}

const WORKER_ROUTING_CONTRACT = "Project worker routing: when the user asks to spawn, open, start, make headful, or load a worker, create a Pisec workstream with pisec_prepare_workstream followed by the exact approved pisec_create_workstream transition. A project worker is the Pisec runtime bound to a Herdr tab in the recorded project workspace. Generic task agents, hub processes, shell PTYs, JavaScript agent() handles, and manual Git worktrees are helper sessions only; they do not create project worker tabs and must never be reported as such. For an existing Pisec workstream, list, inspect, and focus it. For an unregistered local Git branch or worktree, use the external source/import option and never attach directly to the source checkout. Only claim success after the Pisec result contains and corroborates workspace, view/tab, and surface identities.";

function renderExactScope(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "Pisec refused: approval scope is not an object";
  const scope = value as JsonObject;
  const packet = scope.taskPacket && typeof scope.taskPacket === "object" ? scope.taskPacket as JsonObject : {};
  const allowed = Array.isArray(packet.boundaries) ? packet.boundaries.join("; ") || "(none)" : "(invalid)";
  const acceptance = Array.isArray(packet.acceptance) ? packet.acceptance.join("; ") || "(none)" : "(invalid)";
  const nonEffects = Array.isArray(scope.nonEffects) ? scope.nonEffects.join("; ") : "(invalid)";
  const importSource = scope.importSource && typeof scope.importSource === "object" && !Array.isArray(scope.importSource)
    ? scope.importSource as JsonObject
    : null;
  const sourceDescription = importSource
    ? `${String(importSource.kind ?? "external Git")} ${String(importSource.path ?? importSource.ref ?? "(unspecified)")}; normalize the pinned clean commit into the worker`
    : "(none)";
  const readable = [
    ...(Array.isArray(scope.dataDirs) ? scope.dataDirs.map(String) : []),
    ...(typeof scope.pythonEnv === "string" ? [scope.pythonEnv] : []),
  ];
  return [
    "Pisec worker delegation approval",
    `intended outcome: ${String(packet.outcome ?? scope.purpose ?? scope.title ?? "")}`,
    `allowed paths and changes: ${allowed}`,
    `non-effects: ${nonEffects}`,
    `external source: ${sourceDescription}`,
    `acceptance tests: ${acceptance}`,
    `harness/model: ${String(scope.harnessId ?? "")} / ${String(scope.implementationModel ?? scope.harnessModel ?? "configured default")}`,
    `approved readable data: ${readable.join(", ") || "(none)"}`,
    "warning: approved readable data and Python paths are user data and are not proven secret-free",
  ].join("\n");
}

function renderAcceptanceScope(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "Pisec refused: acceptance scope is not an object";
  const scope = value as JsonObject;
  const changedPaths = Array.isArray(scope.changedPaths) ? scope.changedPaths.join(", ") || "(none)" : "(invalid)";
  const achieved = String(scope.achievedOutcome ?? scope.outcome ?? "(see completion evidence)");
  const acceptance = Array.isArray(scope.acceptance) ? JSON.stringify(scope.acceptance) : "(invalid)";
  const verification = Array.isArray(scope.verification) ? JSON.stringify(scope.verification) : "(invalid)";
  const effects = Array.isArray(scope.effects) ? scope.effects.join("; ") : "(invalid)";
  const nonEffects = Array.isArray(scope.nonEffects) ? scope.nonEffects.join("; ") : "(invalid)";
  return [
    "Pisec candidate acceptance",
    `achieved outcome: ${achieved}`,
    `changed paths: ${changedPaths}`,
    `acceptance criteria: ${acceptance}`,
    `verification evidence: ${verification}`,
    `residual risk: ${String(scope.residualRisk ?? "(none stated)")}`,
    `effects: ${effects}`,
    `non-effects: ${nonEffects}`,
  ].join("\n");
}

function registerRuntime(pi: ExtensionAPI): void {
  let sequence = 0;
  let rootSession = false;
  let agentActive = false;
  let reportQueue: Promise<void> = Promise.resolve();
  const report = (
    nextState: RuntimeState,
    event: "session_start" | "lifecycle" | "session_shutdown" = "lifecycle",
    ctx: RuntimeContext,
    reference?: SessionReference,
    reason: SessionSwitchReason | null = null,
  ) => {
    sequence += 1;
    const payload = runtimePayload(nextState, event, reason, sequence, ctx, reference);
    const task = reportQueue.catch(() => undefined).then(async () => {
      await runtimeRequest(payload);
    });
    reportQueue = task;
    return task;
  };
  const blockRuntime = async (error: unknown, ctx: RuntimeContext): Promise<void> => {
    await setWorkerRuntimeGate(pi, "blocked");
    const message = error instanceof Error ? error.message : String(error);
    try {
      sendPisecMessage(
        pi,
        "Pisec runtime-blocked notice: broker turn preparation failed. Do not change the project. Inspect diagnostics and wait for recovery.",
        {
          eventType: "runtime-blocked",
          sourceKind: "runtime.turn.prepare",
          sourceRecordId: `runtime.turn.prepare:${WORKSTREAM_ID ?? "unknown"}`,
          sourceRevision: sequence,
          workstreamId: WORKSTREAM_ID,
        },
        false,
      );
    } catch {
      // The mutation gate is still enforced when the host cannot render a
      // typed notice.
    }
    try {
      await report("blocked", "lifecycle", ctx);
    } catch {
      // A failed broker is the reason for this state; diagnostics remain local.
    }
    const ui = (ctx as RuntimeContext & { ui?: { notify?: (text: string, level?: string) => void } }).ui;
    ui?.notify?.(`Pisec runtime blocked: ${message}`, "error");
  };
  const deliverPreparedTurn = async (value: unknown, triggerBootstrap: boolean): Promise<void> => {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("broker returned no prepared runtime turn");
    const turn = value as PreparedTurn;
    if (Array.isArray(turn.attention)) {
      for (const item of turn.attention) {
        if (!item || typeof item !== "object" || Array.isArray(item)) continue;
        const attention = item as JsonObject;
        const attentionId = typeof attention.attentionId === "string" ? attention.attentionId : "";
        const sourceKind = typeof attention.sourceKind === "string" ? attention.sourceKind : "unknown";
        const sourceId = typeof attention.sourceId === "string" ? attention.sourceId : "unknown";
        const revision = typeof attention.revision === "number" ? attention.revision : 0;
        if (!attentionId || revision < 1) continue;
        sendPisecMessage(
          pi,
          `Pisec attention: ${sourceKind} ${sourceId} requires review. Inspect the authenticated Pisec source before acting.`,
          { eventType: "coordinator.attention", sourceKind, sourceRecordId: attentionId, sourceRevision: revision, sourceId, workstreamId: WORKSTREAM_ID },
          false,
        );
      }
    }
    if (!turn.bootstrap || typeof turn.bootstrap !== "object" || Array.isArray(turn.bootstrap)) return;
    const bootstrap = turn.bootstrap as JsonObject;
    const eventId = typeof bootstrap.sourceRecordId === "string" ? bootstrap.sourceRecordId : "";
    const revision = typeof bootstrap.sourceRevision === "number" ? bootstrap.sourceRevision : 0;
    if (!eventId || revision < 1) throw new Error("broker returned an incomplete runtime bootstrap event");
    sendPisecMessage(
      pi,
      "Pisec bootstrap event authorizes the first engineering turn. Begin the assigned engineering task now. Read the broker-authenticated task packet before acting.",
      {
        eventType: typeof bootstrap.eventType === "string" ? bootstrap.eventType : "worker.bootstrap",
        sourceKind: "runtime.bootstrap",
        sourceRecordId: eventId,
        sourceRevision: revision,
        workstreamId: WORKSTREAM_ID,
        role: bootstrap.role,
      },
      triggerBootstrap,
    );
    await runtimeOperation("runtime.bootstrap.ack", { bootstrapEventId: eventId, bootstrapRevision: revision });
  };
  const prepareAndDeliver = async (ctx: RuntimeContext, triggerBootstrap: boolean): Promise<void> => {
    let lastError: unknown = new Error("runtime turn preparation did not complete");
    for (let attempt = 0; attempt < 40; attempt += 1) {
      try {
        const turn = await runtimeOperation("runtime.turn.prepare");
        await setWorkerRuntimeGate(pi, "ready");
        await deliverPreparedTurn(turn, triggerBootstrap);
        return;
      } catch (error) {
        lastError = error;
        if (attempt < 39) await new Promise(resolve => setTimeout(resolve, 250));
      }
    }
    await blockRuntime(lastError, ctx);
  };
  pi.on("session_start", async (_event, ctx) => {
    rootSession = ctx.hasUI === true || ROLE === "worker";
    if (!rootSession) return;
    agentActive = false;
    try {
      await report("idle", "session_start", ctx, sessionReference(ctx));
      await prepareAndDeliver(ctx, true);
    } catch (error) {
      await blockRuntime(error, ctx);
    }
  });
  pi.on("session_before_switch", async (event, ctx) => {
    if (!rootSession) return { cancel: true };
    if (event.reason === "handoff") return { cancel: true };
    const targetSessionFile = event.reason === "resume" && ownerControlledSessionFile(event.targetSessionFile)
      ? event.targetSessionFile
      : null;
    if (event.reason === "resume" && targetSessionFile === null) return { cancel: true };
    try {
      await runtimeOperation("session.switch.prepare", { reason: event.reason, targetSessionFile });
      return;
    } catch (error) {
      ctx.ui.notify(`Pisec session switch refused: ${error instanceof Error ? error.message : String(error)}`, "error");
      return { cancel: true };
    }
  });
  pi.on("session_switch", async (event, ctx) => {
    if (!rootSession) return;
    agentActive = false;
    try {
      await report("idle", "lifecycle", ctx, sessionReference(ctx), event.reason);
    } catch (error) {
      const previous = event.previousSessionFile;
      const suffix = previous ? `; previous session was ${previous}` : "";
      ctx.ui.notify(`Pisec session switch report failed${suffix}: ${error instanceof Error ? error.message : String(error)}`, "error");
      throw error;
    }
  });
  pi.on("session_branch", async (_event, ctx) => {
    if (!rootSession) return;
    agentActive = false;
    return report("idle", "lifecycle", ctx, sessionReference(ctx));
  });
  pi.on("session_tree", async (_event, ctx) => {
    if (!rootSession) return;
    agentActive = false;
    return report("idle", "lifecycle", ctx, sessionReference(ctx));
  });
  pi.on("before_agent_start", async (event, ctx) => {
    try {
      const turn = await runtimeOperation("runtime.turn.prepare");
      await setWorkerRuntimeGate(pi, "ready");
      await deliverPreparedTurn(turn, false);
      if (ROLE === "first_mate") {
        return {
          systemPrompt: [
            ...event.systemPrompt,
            FIRST_MATE_PROMPT,
          ],
        };
      }
      if (ROLE === "secretary") {
        return {
          systemPrompt: [
            ...event.systemPrompt,
            WORKER_ROUTING_CONTRACT,
            SECRETARY_PROMPT,
          ],
        };
      }
      const fullPacket = turn && typeof turn === "object" ? (turn as JsonObject).taskPacket : null;
      if (!fullPacket || typeof fullPacket !== "object" || Array.isArray(fullPacket)) throw new Error("broker returned no immutable task packet");
      const taskPacket = (fullPacket as JsonObject).packet ?? fullPacket;
      const packetObject = taskPacket && typeof taskPacket === "object" && !Array.isArray(taskPacket) ? taskPacket as JsonObject : {};
      const execution = packetObject.execution && typeof packetObject.execution === "object" && !Array.isArray(packetObject.execution)
        ? packetObject.execution as JsonObject
        : {};
      const importSource = execution.importSource && typeof execution.importSource === "object" && !Array.isArray(execution.importSource)
        ? execution.importSource as JsonObject
        : null;
      const importContract = importSource
        ? `EXTERNAL_GIT_IMPORT\nThis worker owns the Pisec-normalized candidate. The approved source checkout is clean, committed, read-only, and was never attached or modified. Review the normalized import and continue from the Pisec-owned branch.`
        : "EXTERNAL_GIT_IMPORT\nNo external source snapshot is attached to this workstream.";
      const pythonEnv = turn && typeof turn === "object" && typeof (turn as JsonObject).pythonEnv === "string"
        ? String((turn as JsonObject).pythonEnv)
        : "";
      const pythonContract = pythonEnv
        ? `PYTHON_ENVIRONMENT\nAn approved python environment is exposed read-only inside this Fence at: ${pythonEnv}\nRun the project interpreter directly. Never run package-management writes inside the shared environment.`
        : "PYTHON_ENVIRONMENT\nNo python environment was approved for this workstream; the Fence exposes none.";
      return {
        systemPrompt: [
          ...event.systemPrompt,
          WORKER_PROMPT,
          importContract,
          `IMMUTABLE_TASK_PACKET\n${renderTaskPacket(taskPacket)}`,
          pythonContract,
        ],
      };
    } catch (error) {
      await blockRuntime(error, ctx);
      return { systemPrompt: [...event.systemPrompt, "PISEC_RUNTIME_BLOCKED\nThe authenticated Pisec runtime handshake failed. Perform diagnostics only and do not change the project."] };
    }
  });
  pi.on("agent_start", async (_event, ctx) => {
    if (!rootSession) return;
    agentActive = true;
    return report("working", "lifecycle", ctx, sessionReference(ctx));
  });
  pi.on("agent_end", async (_event, ctx) => {
    if (!rootSession || !agentActive) return;
    agentActive = false;
    return report("idle", "lifecycle", ctx);
  });
  pi.on("tool_execution_end", async (event, _ctx) => {
    if (!rootSession) return;
    const detail = event as unknown as JsonObject;
    const result = detail.result;
    const resultObject = result && typeof result === "object" && !Array.isArray(result) ? result as JsonObject : undefined;
    const failed = detail.isError === true || detail.error !== undefined || resultObject?.isError === true;
    const toolName = typeof detail.toolName === "string" ? detail.toolName : typeof detail.name === "string" ? detail.name : null;
    if (!failed || toolName === null || toolName.length === 0 || toolName.length > 128 || !/^[A-Za-z0-9_.:\/-]+$/.test(toolName)) return;
    try {
      await runtimeOperation("runtime.tool_failure", { toolName, failureCode: "tool_error" });
    } catch {
      // Failure telemetry must never turn the original tool failure into a second failure.
    }
  });
  pi.on("tool_approval_requested", async (_event, ctx) => {
    if (!rootSession || !agentActive) return;
    return report("blocked", "lifecycle", ctx);
  });
  pi.on("tool_approval_resolved", async (_event, ctx) => {
    if (!rootSession) return;
    return report(agentActive ? "working" : "idle", "lifecycle", ctx);
  });
  pi.on("auto_retry_start", async (_event, ctx) => {
    if (!rootSession || !agentActive) return;
    return report("blocked", "lifecycle", ctx);
  });
  pi.on("auto_retry_end", async (_event, ctx) => {
    if (!rootSession) return;
    return report(agentActive ? "working" : "idle", "lifecycle", ctx);
  });
  pi.on("session_shutdown", async (_event, ctx) => {
    if (!rootSession) return;
    agentActive = false;
    return report("stopped", "session_shutdown", ctx);
  });
}

function secretaryTools(pi: ExtensionAPI): void {
  const z = pi.zod;
  const semantic = (name: string, label: string, operation: string, parameters: unknown, approval: "read" | "exec", map?: (params: JsonObject) => JsonObject) => {
    pi.registerTool({
      name,
      label,
      description: `Use when the typed ${operation} transition is needed; inspect the result and follow its next action.`,
      approval,
      parameters,
      async execute(_id, params, signal) {
        try {
          return textResult(await semanticRequest(operation, adapterPayload(operation, params as JsonObject, String(_id ?? ""), map), signal));
        } catch (error) {
          return textResult(error instanceof Error ? error.message : String(error), true);
        }
      },
    });
  };
  const taskPacketSchema = z.object({ schemaVersion: z.literal(1), outcome: z.string().min(1).max(4096), boundaries: z.array(z.string().min(1).max(4096)).max(16), acceptance: z.array(z.string().min(1).max(4096)).max(16), openQuestions: z.array(z.string().min(1).max(4096)).max(16), evidence: z.array(z.string().min(1).max(4096)).max(16) });
  const importSourceSchema = z.object({ ref: z.string().min(1).max(512).optional(), path: z.string().min(1).max(4096).optional() });
  semantic("pisec_list_attention", "List attention", "attention.list", z.object({ limit: z.number().int().min(1).max(32).optional() }), "read", params => params.limit === undefined ? {} : { limit: params.limit });
  semantic("pisec_inspect_attention", "Inspect attention", "attention.inspect", z.object({ attention_id: z.string().min(1).max(128) }), "read", params => ({ attentionId: params.attention_id }));
  semantic("pisec_project_activity", "Pisec project activity", "project.activity", z.object({ after: z.number().int().min(0).optional() }), "read", params => params.after === undefined ? {} : { after: params.after });
  semantic("pisec_list_issues", "List issues", "issue.list", z.object({ state: z.enum(["open", "acknowledged", "remediating", "verifying", "resolved"]).optional(), limit: z.number().int().min(1).max(1000).optional() }), "read", params => ({ ...(params.state ? { state: params.state } : {}), ...(params.limit ? { limit: params.limit } : {}) }));
  semantic("pisec_inspect_issue", "Inspect issue", "issue.inspect", z.object({ issue_id: z.string().min(1).max(128) }), "read", params => ({ issueId: params.issue_id }));
  semantic("pisec_add_issue_context", "Add issue context", "issue.add_context", z.object({ issue_id: z.string().min(1).max(128), context: z.any() }), "exec", params => ({ issueId: params.issue_id, context: params.context }));
  semantic("pisec_verify_issue", "Verify issue", "issue.verify", z.object({ issue_id: z.string().min(1).max(128), status: z.enum(["fixed", "still_blocked"]), evidence: z.any() }), "exec", params => ({ issueId: params.issue_id, status: params.status, evidence: params.evidence }));
  semantic("pisec_acknowledge_issue", "Acknowledge issue", "issue.acknowledge", z.object({ issue_id: z.string().min(1).max(128) }), "exec", params => ({ issueId: params.issue_id }));
  semantic("pisec_link_issue_remediation", "Link issue remediation", "issue.link_remediation", z.object({ issue_id: z.string().min(1).max(128), workstream_id: z.string().min(1).max(128) }), "exec", params => ({ issueId: params.issue_id, workstreamId: params.workstream_id }));
  semantic("pisec_request_issue_verification", "Request issue verification", "issue.request_verification", z.object({ issue_id: z.string().min(1).max(128), evidence: z.any() }), "exec", params => ({ issueId: params.issue_id, evidence: params.evidence }));
  semantic("pisec_resolve_issue", "Resolve issue", "issue.resolve", z.object({ issue_id: z.string().min(1).max(128), disposition: z.enum(["declined", "duplicate", "not_reproducible"]), reason: z.string().min(1).max(4096), decision_id: z.string().min(1).max(128) }), "exec", params => ({ issueId: params.issue_id, disposition: params.disposition, reason: params.reason, decisionId: params.decision_id }));

  semantic("pisec_project_status", "Pisec project status", "project.status", z.object({}), "read");
  semantic("pisec_git_status", "Pisec Git status", "git.status", z.object({}), "read");
  semantic("pisec_push_branch", "Push project branch", "git.push", z.object({ branch: z.string().min(1).max(512), expected_local_oid: z.string().regex(/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/), expected_remote_oid: z.string().regex(/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/) }), "exec", params => ({ branch: params.branch, expectedLocalOid: params.expected_local_oid, expectedRemoteOid: params.expected_remote_oid }));
  semantic("pisec_inspect_workstream_changes", "Inspect workstream Git changes", "git.workstream_changes", z.object({ workstream_id: z.string().min(1).max(128) }), "read", params => ({ workstreamId: params.workstream_id }));
  semantic("pisec_prepare_workstream_acceptance", "Prepare workstream acceptance", "workstream.accept.prepare", z.object({ workstream_id: z.string().min(1).max(128) }), "read", params => ({ workstreamId: params.workstream_id }));
  pi.registerTool({
    name: "pisec_accept_workstream",
    label: "Accept Pisec workstream",
    description: "Accept one exact bounded workstream candidate. This is the only user approval; the secretary owns later integration and closeout.",
    approval: scope => ({ tier: "exec", policy: "prompt", reason: renderAcceptanceScope(scope) }),
    parameters: z.object({ approval_scope: z.any() }),
    async execute(_id, params, _signal, _onUpdate, ctx) {
      if (!ctx.hasUI) return textResult("Pisec refused workstream acceptance because an interactive approval UI is unavailable.", true);
      try {
        return textResult(await semanticRequest("workstream.accept.apply", { approvalScope: params.approval_scope as JsonObject }));
      } catch (error) {
        return textResult(error instanceof Error ? error.message : String(error), true);
      }
    },
  });
  semantic("pisec_list_workstreams", "List Pisec workstreams", "workstream.list", z.object({}), "read");
  semantic("pisec_inspect_workstream", "Inspect Pisec workstream", "workstream.inspect", z.object({ workstream_id: z.string().min(1).max(128) }), "read", params => ({ workstreamId: params.workstream_id }));
  semantic("pisec_list_integrations", "List Pisec integrations", "integration.list", z.object({ state: z.enum(["queued", "refreshing", "awaiting_worker", "verifying", "applying", "integrated", "needs_attention"]).optional() }), "read", params => params.state ? { state: params.state } : {});
  semantic("pisec_inspect_integration", "Inspect Pisec integration", "integration.inspect", z.object({ integration_id: z.string().min(1).max(128) }), "read", params => ({ integrationId: params.integration_id }));
  semantic("pisec_prepare_workstream", "Prepare Pisec workstream", "workstream.prepare", z.object({ title: z.string().min(1).max(512), purpose: z.string().min(1).max(4096), brief: z.string().min(1).max(4096), task_packet: taskPacketSchema, target_ref: z.string().min(1).max(512).optional(), source: importSourceSchema.optional(), implementation_model: z.string().min(1).max(256).optional(), execution_profile: z.literal("worker-default").optional(), work_mode: z.enum(["FAST", "RIP", "BUILD", "MAJOR"]).optional(), learning_overlay: z.enum(["OFF", "LIGHT", "DEEP"]).optional(), learning_seam: z.string().min(1).max(1024).optional(), decision_ids: z.array(z.string().min(1).max(128)).max(16).optional(), python_env: z.string().min(1).max(4096).optional() }), "read", params => ({ title: params.title, purpose: params.purpose, brief: params.brief, taskPacket: params.task_packet, ...(params.target_ref ? { targetRef: params.target_ref } : {}), ...(params.source ? { source: params.source } : {}), ...(params.implementation_model ? { implementationModel: params.implementation_model } : {}), ...(params.execution_profile ? { executionProfile: params.execution_profile } : {}), ...(params.work_mode ? { workMode: params.work_mode } : {}), ...(params.learning_overlay ? { learningOverlay: params.learning_overlay } : {}), ...(params.learning_seam ? { learningSeam: params.learning_seam } : {}), ...(params.decision_ids ? { decisionIds: params.decision_ids } : {}), ...(params.python_env ? { pythonEnv: params.python_env } : {}) }));
  pi.registerTool({
    name: "pisec_create_workstream",
    label: "Create Pisec workstream",
    description: "Apply one previously prepared immutable Pisec workstream scope. This is the only semantic tool that creates external resources and always requires exact user approval. Imported work is snapshotted into the Pisec worker; the original checkout is never attached or modified.",
    approval: scope => ({ tier: "exec", policy: "prompt", reason: renderExactScope(scope) }),
    parameters: z.object({ approval_scope: z.any() }),
    async execute(_id, params, _signal, _onUpdate, ctx) {
      if (!ctx.hasUI) return textResult("Pisec refused workstream creation because an interactive approval UI is unavailable.", true);
      try {
        return textResult(await semanticRequest("workstream.authorize_apply", { approvalScope: params.approval_scope as JsonObject }));
      } catch (error) {
        return textResult(error instanceof Error ? error.message : String(error), true);
      }
    },
  });
  semantic("pisec_focus_workstream", "Focus Pisec workstream", "workstream.focus", z.object({ workstream_id: z.string().min(1).max(128) }), "exec", params => ({ workstreamId: params.workstream_id }));
  semantic("pisec_retire_workstream", "Retire Pisec workstream", "workstream.retire", z.object({ workstream_id: z.string().min(1).max(128), remediation_issue_id: z.string().min(1).max(128).optional(), failure_reason: z.string().min(1).max(4096).optional() }), "exec", params => ({ workstreamId: params.workstream_id, ...(params.remediation_issue_id ? { remediationIssueId: params.remediation_issue_id, failureReason: params.failure_reason } : {}) }));
  semantic("pisec_list_decisions", "List Pisec decisions", "decision.list", z.object({ state: z.enum(["open", "resolved"]).optional() }), "read", params => params.state ? { state: params.state } : {});
  semantic("pisec_record_decision", "Record Pisec decision", "decision.record", z.object({ summary: z.string().min(1).max(512), context: z.any(), workstream_id: z.string().min(1).max(128).optional() }), "exec", params => ({ summary: params.summary, context: params.context, ...(params.workstream_id ? { workstreamId: params.workstream_id } : {}) }));
  semantic("pisec_list_coordination_requests", "List coordination requests", "coordination.list", z.object({ workstream_id: z.string().min(1).max(128).optional(), include_resolved: z.boolean().optional() }), "read", params => ({ ...(params.workstream_id ? { workstreamId: params.workstream_id } : {}), ...(params.include_resolved !== undefined ? { includeResolved: params.include_resolved } : {}) }));
  semantic("pisec_inspect_coordination_request", "Inspect coordination request", "coordination.inspect", z.object({ request_id: z.string().min(1).max(128) }), "read", params => ({ requestId: params.request_id }));
  semantic("pisec_answer_coordination_request", "Answer coordination request", "coordination.answer", z.object({ request_id: z.string().min(1).max(128), response: z.string().min(1).max(4096), decision_id: z.string().min(1).max(128).optional() }), "exec", params => ({ requestId: params.request_id, response: params.response, ...(params.decision_id ? { decisionId: params.decision_id } : {}) }));
  semantic("pisec_resolve_decision", "Resolve Pisec decision", "decision.resolve", z.object({ decision_id: z.string().min(1).max(128), resolution: z.string().min(1).max(4096) }), "exec", params => ({ decisionId: params.decision_id, resolution: params.resolution }));
  semantic("pisec_list_worker_research_requests", "List worker research requests", "research.list", z.object({ state: z.enum(["pending", "researching", "needs_context", "answered", "declined", "acknowledged"]).optional(), limit: z.number().int().min(1).max(100).optional(), workstream_id: z.string().min(1).max(128).optional() }), "read", params => ({ ...(params.state ? { state: params.state } : {}), ...(params.limit ? { limit: params.limit } : {}), ...(params.workstream_id ? { workstreamId: params.workstream_id } : {}) }));
  semantic("pisec_inspect_worker_research", "Inspect worker research", "research.inspect", z.object({ request_id: z.string().min(1).max(128), workstream_id: z.string().min(1).max(128).optional() }), "read", params => ({ requestId: params.request_id, ...(params.workstream_id ? { workstreamId: params.workstream_id } : {}) }));
  semantic("pisec_claim_worker_research", "Claim worker research", "research.claim", z.object({ request_id: z.string().min(1).max(128) }), "read", params => ({ requestId: params.request_id }));
  semantic("pisec_request_worker_research_context", "Request worker research context", "research.request_context", z.object({ request_id: z.string().min(1).max(128), context_request: z.any() }), "read", params => ({ requestId: params.request_id, contextRequest: params.context_request }));
  semantic("pisec_answer_worker_research", "Answer worker research", "research.answer", z.object({ request_id: z.string().min(1).max(128), result: z.any() }), "read", params => ({ requestId: params.request_id, result: params.result }));
  semantic("pisec_decline_worker_research", "Decline worker research", "research.decline", z.object({ request_id: z.string().min(1).max(128), decline: z.any() }), "read", params => ({ requestId: params.request_id, decline: params.decline }));
}
function fleetTools(pi: ExtensionAPI): void {
  const z = pi.zod;
  const fleet = (name: string, label: string, operation: string, parameters: unknown, approval: "read" | "exec", map?: (params: JsonObject) => JsonObject) => {
    pi.registerTool({
      name,
      label,
      description: `Use when the typed ${operation} transition is needed; inspect the semantic result and follow its next action.`,
      approval,
      parameters,
      async execute(_id, params, signal) {
        try {
          return textResult(await semanticRequest(operation, adapterPayload(operation, params as JsonObject, String(_id ?? ""), map), signal));
        } catch (error) {
          return textResult(error instanceof Error ? error.message : String(error), true);
        }
      },
    });
  };
  const projectId = z.string().min(1).max(128);
  fleet("pisec_fleet_list_attention", "List fleet attention", "attention.list", z.object({ limit: z.number().int().min(1).max(32).optional() }), "read", params => params.limit === undefined ? {} : { limit: params.limit });
  fleet("pisec_fleet_inspect_attention", "Inspect fleet attention", "attention.inspect", z.object({ attention_id: z.string().min(1).max(128) }), "read", params => ({ attentionId: params.attention_id }));
  fleet("pisec_fleet_list_issues", "List issues", "fleet.issue.list", z.object({ project_id: projectId.optional(), state: z.enum(["open", "acknowledged", "remediating", "verifying", "resolved"]).optional(), limit: z.number().int().min(1).max(1000).optional() }), "read", params => ({ ...(params.project_id ? { projectId: params.project_id } : {}), ...(params.state ? { state: params.state } : {}), ...(params.limit ? { limit: params.limit } : {}) }));
  fleet("pisec_fleet_inspect_issue", "Inspect issue", "fleet.issue.inspect", z.object({ issue_id: z.string().min(1).max(128), project_id: projectId.optional() }), "read", params => ({ issueId: params.issue_id, ...(params.project_id ? { projectId: params.project_id } : {}) }));
  fleet("pisec_fleet_add_issue_context", "Add issue context", "fleet.issue.add_context", z.object({ project_id: projectId, issue_id: z.string().min(1).max(128), context: z.any() }), "exec", params => ({ projectId: params.project_id, issueId: params.issue_id, context: params.context }));
  fleet("pisec_fleet_acknowledge_issue", "Acknowledge issue", "fleet.issue.acknowledge", z.object({ project_id: projectId, issue_id: z.string().min(1).max(128) }), "exec", params => ({ projectId: params.project_id, issueId: params.issue_id }));
  fleet("pisec_fleet_resolve_issue", "Resolve issue", "fleet.issue.resolve", z.object({ project_id: projectId, issue_id: z.string().min(1).max(128), disposition: z.enum(["declined", "duplicate", "not_reproducible"]), reason: z.string().min(1).max(4096), decision_id: z.string().min(1).max(128) }), "exec", params => ({ projectId: params.project_id, issueId: params.issue_id, disposition: params.disposition, reason: params.reason, decisionId: params.decision_id }));
  fleet("pisec_fleet_status", "Fleet status", "fleet.status", z.object({ project_id: projectId.optional() }), "read", params => params.project_id ? { projectId: params.project_id } : {});
  fleet("pisec_fleet_events", "Fleet events", "fleet.events", z.object({ after: z.number().int().min(0).optional(), limit: z.number().int().min(1).max(1000).optional() }), "read", params => ({ ...(params.after !== undefined ? { after: params.after } : {}), ...(params.limit !== undefined ? { limit: params.limit } : {}) }));
  fleet("pisec_fleet_list_workstreams", "List project workstreams", "fleet.workstream.list", z.object({ project_id: projectId }), "read", params => ({ projectId: params.project_id }));
  fleet("pisec_fleet_inspect_workstream", "Inspect project workstream", "fleet.workstream.inspect", z.object({ project_id: projectId, workstream_id: z.string().min(1).max(128) }), "read", params => ({ projectId: params.project_id, workstreamId: params.workstream_id }));
  fleet("pisec_fleet_list_integrations", "List project integrations", "fleet.integration.list", z.object({ project_id: projectId, state: z.enum(["queued", "refreshing", "awaiting_worker", "verifying", "applying", "integrated", "needs_attention"]).optional() }), "read", params => ({ projectId: params.project_id, ...(params.state ? { state: params.state } : {}) }));
  fleet("pisec_fleet_inspect_integration", "Inspect project integration", "fleet.integration.inspect", z.object({ project_id: projectId, integration_id: z.string().min(1).max(128) }), "read", params => ({ projectId: params.project_id, integrationId: params.integration_id }));
  fleet("pisec_fleet_git_changes", "Inspect project workstream changes", "fleet.git.workstream_changes", z.object({ project_id: projectId, workstream_id: z.string().min(1).max(128) }), "read", params => ({ projectId: params.project_id, workstreamId: params.workstream_id }));
  fleet("pisec_fleet_request_issue_remediation", "Request issue remediation", "fleet.issue.request_remediation", z.object({ project_id: projectId, issue_id: z.string().min(1).max(128), outcome: z.string().min(1).max(4096), allowed_paths: z.array(z.string().min(1).max(4096)).max(64), verification: z.array(z.string().min(1).max(4096)).max(32), non_effects: z.array(z.string().min(1).max(4096)).max(32) }), "exec", params => ({ projectId: params.project_id, issueId: params.issue_id, outcome: params.outcome, allowedPaths: params.allowed_paths, verification: params.verification, nonEffects: params.non_effects }));
  fleet("pisec_fleet_request_issue_verification", "Request issue verification", "fleet.issue.request_verification", z.object({ project_id: projectId, issue_id: z.string().min(1).max(128), evidence: z.any() }), "exec", params => ({ projectId: params.project_id, issueId: params.issue_id, evidence: params.evidence }));
}

function workerTools(pi: ExtensionAPI): void {
  const z = pi.zod;
  const runtimeTool = (name: string, label: string, description: string, parameters: unknown, operation: string, map?: (params: JsonObject) => JsonObject) => {
    pi.registerTool({
      name,
      label,
      description,
      approval: "read",
      parameters,
      async execute(_id, params, signal) {
        try {
          if (!workerCanUse(operation)) return textResult("Pisec runtime is blocked until broker attestation succeeds; project-changing tool denied.", true);
          return textResult(await runtimeOperation(operation, adapterPayload(operation, params as JsonObject, String(_id ?? ""), map), signal));
        } catch (error) {
          return textResult(error instanceof Error ? error.message : String(error), true);
        }
      },
    });
  };
  runtimeTool("pisec_list_attention", "List attention", "Read current authorized attention references.", z.object({ limit: z.number().int().min(1).max(32).optional() }), "attention.list", params => params.limit === undefined ? {} : { limit: params.limit });
  runtimeTool("pisec_inspect_attention", "Inspect attention", "Read one authorized typed-source attention pointer.", z.object({ attention_id: z.string().min(1).max(128) }), "attention.inspect", params => ({ attentionId: params.attention_id }));
  runtimeTool("pisec_checkpoint_workstream", "Checkpoint workstream", "Record semantic progress only. Use investigating, implementing, or verifying. Submit final acceptance evidence with pisec_submit_completion.", z.object({ phase: z.enum(["investigating", "implementing", "verifying"]), summary: z.string().min(1).max(1024), next_action: z.string().min(1).max(1024), evidence: z.array(z.any()).max(64) }), "workstream.checkpoint", params => ({ phase: params.phase, summary: params.summary, nextAction: params.next_action, evidence: params.evidence }));
  runtimeTool("pisec_submit_completion", "Submit completion", "Submit the sole immutable completion packet for the current worker commit. The broker validates and stores it, creates exactly one derived ready_review checkpoint, and wakes the Secretary.", z.object({ completion: z.object({ acceptance: z.array(z.object({ criterion: z.string().min(1).max(4096), status: z.literal("passed"), evidence: z.array(z.string().min(1).max(4096)).min(1)})).min(1).max(32), verification: z.array(z.object({ command: z.string().min(1).max(4096), result: z.string().min(1).max(8192)})).min(1).max(32), source_commit: z.string().regex(/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/), task_packet_sha256: z.string().regex(/^[0-9a-f]{64}$/), changed_surfaces: z.array(z.string().min(1).max(4096)).max(32), residual_risk: z.string().max(4096) }) }), "workstream.completion.submit", params => ({ completionPacket: { acceptance: params.completion.acceptance, verification: params.completion.verification, sourceCommit: params.completion.source_commit, taskPacketSha256: params.completion.task_packet_sha256, changedSurfaces: params.completion.changed_surfaces, residualRisk: params.completion.residual_risk } }));
  runtimeTool("pisec_request_help", "Request help", "Persist one bounded clarification, blocker, review, access, permission, tooling, or lifecycle request.", z.object({ kind: z.enum(["clarification", "blocker", "review", "access", "permission", "tooling", "lifecycle"]), summary: z.string().min(1).max(1024), details: z.string().min(1).max(4096), requested_action: z.string().min(1).max(4096).optional(), blocking: z.boolean().optional(), evidence: z.array(z.any()).max(64).optional() }), "help.request", params => ({ kind: params.kind, summary: params.summary, details: params.details, requestedAction: params.requested_action ?? "Provide guidance or remediation.", blocking: params.blocking ?? params.kind === "blocker", evidence: params.evidence ?? [] }));
  runtimeTool("pisec_list_coordination", "List coordination", "List compact coordination requests for this workstream.", z.object({ include_resolved: z.boolean().optional() }), "coordination.list", params => params.include_resolved === undefined ? {} : { includeResolved: params.include_resolved });
  runtimeTool("pisec_inspect_coordination", "Inspect coordination", "Inspect one coordination request and its latest response.", z.object({ request_id: z.string().min(1).max(128) }), "coordination.inspect", params => ({ requestId: params.request_id }));
  runtimeTool("pisec_report_issue", "Report issue", "Report a harness, access, lifecycle, or tooling issue.", z.object({ category: z.enum(["permission", "access", "lifecycle", "tooling", "other"]), severity: z.enum(["blocking", "degraded", "improvement"]), summary: z.string().min(1).max(1024), details: z.string().min(1).max(4096), requested_action: z.string().min(1).max(4096), evidence: z.array(z.any()).max(64) }), "issue.report", params => ({ category: params.category, severity: params.severity, summary: params.summary, details: params.details, requestedAction: params.requested_action, evidence: params.evidence }));
  runtimeTool("pisec_list_issues", "List issues", "List issues reported by or linked to this worker.", z.object({ state: z.enum(["open", "acknowledged", "remediating", "verifying", "resolved"]).optional(), limit: z.number().int().min(1).max(1000).optional() }), "issue.list", params => ({ ...(params.state ? { state: params.state } : {}), ...(params.limit ? { limit: params.limit } : {}) }));
  runtimeTool("pisec_inspect_issue", "Inspect issue", "Inspect one issue and its append-only action history.", z.object({ issue_id: z.string().min(1).max(128) }), "issue.inspect", params => ({ issueId: params.issue_id }));
  runtimeTool("pisec_add_issue_context", "Add issue context", "Append bounded evidence to a worker issue.", z.object({ issue_id: z.string().min(1).max(128), context: z.any() }), "issue.add_context", params => ({ issueId: params.issue_id, context: params.context }));
  runtimeTool("pisec_verify_issue", "Verify issue", "Verify a remediation and close it only when fixed.", z.object({ issue_id: z.string().min(1).max(128), status: z.enum(["fixed", "still_blocked"]), evidence: z.any() }), "issue.verify", params => ({ issueId: params.issue_id, status: params.status, evidence: params.evidence }));
  runtimeTool("pisec_show_task_packet", "Show immutable task packet", "Retrieve the broker-authenticated immutable task packet for this workstream.", z.object({}), "task.get");
  runtimeTool("pisec_request_secretary_research", "Request secretary research", "Persist a bounded research request without waiting for the secretary.", z.object({ summary: z.string().min(1).max(1024), question: z.string().min(1).max(4096), context: z.string().max(4096).optional(), attempted: z.array(z.string().min(1).max(4096)).max(16).optional(), candidate_sources: z.array(z.string().url().max(2048)).max(16).optional(), blocking: z.boolean().optional() }), "research.request", params => ({ request: { kind: "research", summary: params.summary, question: params.question, context: params.context ?? "", attempted: params.attempted ?? [], candidateSources: params.candidate_sources ?? [], blocking: params.blocking ?? true } }));
  runtimeTool("pisec_check_secretary_research", "Check secretary research", "List durable research request metadata for this workstream (no packet bodies). Fetch a specific request's full packets with pisec_inspect_secretary_research.", z.object({ state: z.enum(["pending", "researching", "needs_context", "answered", "declined", "acknowledged"]).optional(), limit: z.number().int().min(1).max(100).optional() }), "research.list", params => ({ ...(params.state ? { state: params.state } : {}), ...(params.limit ? { limit: params.limit } : {}) }));
  runtimeTool("pisec_inspect_secretary_research", "Inspect secretary research", "Fetch the full durable packets (including the answer) for one research request by request_id.", z.object({ request_id: z.string().min(1).max(128) }), "research.inspect", params => ({ requestId: params.request_id }));
  runtimeTool("pisec_add_secretary_research_context", "Add secretary research context", "Add bounded context requested by the secretary and return the request to pending.", z.object({ request_id: z.string().min(1).max(128), context: z.string().min(1).max(4096), attempted: z.array(z.string().min(1).max(4096)).max(16).optional(), candidate_sources: z.array(z.string().url().max(2048)).max(16).optional() }), "research.add_context", params => ({ requestId: params.request_id, context: { context: params.context, attempted: params.attempted ?? [], candidateSources: params.candidate_sources ?? [] } }));
  runtimeTool("pisec_acknowledge_secretary_research", "Acknowledge secretary research", "Acknowledge an answered or declined durable research request after consuming it.", z.object({ request_id: z.string().min(1).max(128) }), "research.acknowledge", params => ({ requestId: params.request_id }));
}

export default function pisec(pi: ExtensionAPI): void {
  if (!isPisecRole(ROLE) || !RUNTIME_SOCKET || !RUNTIME_TOKEN || !RUNTIME_GENERATION || !WORKSTREAM_ID || !INSTANCE_ID || !SURFACE_ID) return;
  if ((ROLE === "secretary" && !SECRETARY_SOCKET) || (ROLE === "first_mate" && !FLEET_SOCKET)) return;
  pi.setLabel(ROLE === "secretary" ? "Pisec Secretary" : ROLE === "first_mate" ? "Pisec First Mate" : "Pisec Worker");
  registerRuntime(pi);
  if (ROLE === "secretary") secretaryTools(pi);
  else if (ROLE === "first_mate") fleetTools(pi);
  else workerTools(pi);
}
