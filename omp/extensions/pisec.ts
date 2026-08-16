import { randomBytes } from "node:crypto";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

type JsonObject = Record<string, unknown>;
type RuntimeState = "starting" | "working" | "blocked" | "idle" | "done" | "stopped" | "missing" | "error" | "unknown";
type SessionReference = { kind: "path" | "id" | null; value: string | null };

type RuntimeContext = { sessionManager: { getSessionFile?: () => string | undefined } };

const ROLE = process.env.PISEC_ROLE;
const RUNTIME_SOCKET = process.env.PISEC_RUNTIME_SOCKET;
const SECRETARY_SOCKET = process.env.PISEC_SECRETARY_SOCKET;
const RUNTIME_TOKEN = process.env.PISEC_RUNTIME_TOKEN;
const WORKSTREAM_ID = process.env.PISEC_WORKSTREAM_ID;
const INSTANCE_ID = process.env.PISEC_RUNTIME_INSTANCE_ID;
const START_SOURCE = process.env.PISEC_SESSION_START_SOURCE === "resume" ? "resume" : "startup";
const SURFACE_ID = process.env.PISEC_SURFACE_ID;

function isPisecRole(value: string | undefined): value is "secretary" | "worker" {
  return value === "secretary" || value === "worker";
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
  if (value.startsWith("/")) return { kind: "path", value };
  return { kind: "id", value };
}

function runtimePayload(
  state: RuntimeState,
  event: "session_start" | "lifecycle" | "session_shutdown",
  seq: number,
  ctx: RuntimeContext,
  reference?: SessionReference,
): JsonObject {
  const current = reference ?? { kind: null, value: null };
  return {
    workstreamId: WORKSTREAM_ID,
    runtimeInstanceId: INSTANCE_ID,
    seq,
    event,
    state,
    nativeSessionKind: current.kind,
    nativeSessionValue: current.value,
    startSource: START_SOURCE,
    surfaceId: SURFACE_ID,
    token: RUNTIME_TOKEN,
  };
}

function runtimeAuth(): JsonObject {
  return {
    workstreamId: WORKSTREAM_ID,
    runtimeInstanceId: INSTANCE_ID,
    surfaceId: SURFACE_ID,
    token: RUNTIME_TOKEN,
  };
}

function socketRequest(socketPath: string, operation: string, payload: JsonObject, signal?: AbortSignal): Promise<unknown> {
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
    if (!RUNTIME_SOCKET || !RUNTIME_TOKEN || !WORKSTREAM_ID || !INSTANCE_ID || !SURFACE_ID) throw new Error("Pisec runtime binding is incomplete");
  return socketRequest(RUNTIME_SOCKET, "runtime.report", payload, signal);
}

async function runtimeOperation(operation: string, payload: JsonObject = {}, signal?: AbortSignal): Promise<unknown> {
  if (!RUNTIME_SOCKET || !RUNTIME_TOKEN || !WORKSTREAM_ID || !INSTANCE_ID || !SURFACE_ID) throw new Error("Pisec runtime binding is incomplete");
  return socketRequest(RUNTIME_SOCKET, operation, { ...runtimeAuth(), ...payload }, signal);
}

async function semanticRequest(operation: string, payload: JsonObject, signal?: AbortSignal): Promise<unknown> {
  if (!SECRETARY_SOCKET || !RUNTIME_TOKEN) throw new Error("Pisec secretary binding is incomplete");
  return socketRequest(SECRETARY_SOCKET, operation, { ...payload, authToken: RUNTIME_TOKEN }, signal);
}

function renderTaskPacket(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "(unrenderable task packet)";
  }
}

function renderExactScope(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "Pisec refused: approval scope is not an object";
  const scope = value as JsonObject;
  const externalDomains = Array.isArray(scope.externalDomains) ? scope.externalDomains.join(", ") || "(empty)" : "(invalid)";
  const effects = Array.isArray(scope.effects) ? scope.effects.join("; ") : "(invalid)";
  const nonEffects = Array.isArray(scope.nonEffects) ? scope.nonEffects.join("; ") : "(invalid)";
  return [
    "Pisec exact workstream creation approval",
    `operation id: ${String(scope.operationId ?? "")}`,
    `project id: ${String(scope.projectId ?? "")}`,
    `workstream id: ${String(scope.workstreamId ?? "")}`,
    `title: ${String(scope.title ?? "")}`,
    `purpose: ${String(scope.purpose ?? "")}`,
    `full brief: ${String(scope.brief ?? "")}`,
    `harness adapter: ${String(scope.harnessId ?? "")}`,
    `workspace adapter: ${String(scope.workspaceAdapterId ?? "")}`,
    `execution profile: ${String(scope.executionProfile ?? "")}`,
    `target ref: ${String(scope.targetRef ?? "")}`,
    `base commit OID: ${String(scope.baseCommitOid ?? "")}`,
    `branch: ${String(scope.branchName ?? "")}`,
    `checkout path: ${String(scope.worktreePath ?? "")}`,
    `agent name: ${String(scope.agentName ?? "")}`,
    `exact external domains: ${externalDomains}`,
    `effects: ${effects}`,
    `non-effects: ${nonEffects}`,
    `immutable task packet: ${renderTaskPacket(scope.taskPacket)}`,
  ].join("\n");
}

function renderMergeScope(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "Pisec refused: merge approval scope is not an object";
  const scope = value as JsonObject;
  const effects = Array.isArray(scope.effects) ? scope.effects.join("; ") : "(invalid)";
  const nonEffects = Array.isArray(scope.nonEffects) ? scope.nonEffects.join("; ") : "(invalid)";
  return [
    "Pisec exact fast-forward merge approval",
    `project id: ${String(scope.projectId ?? "")}`,
    `workstream id: ${String(scope.workstreamId ?? "")}`,
    `target branch: ${String(scope.targetBranch ?? "")}`,
    `target commit OID: ${String(scope.targetCommitOid ?? "")}`,
    `source branch: ${String(scope.sourceBranch ?? "")}`,
    `source commit OID: ${String(scope.sourceCommitOid ?? "")}`,
    `strategy: ${String(scope.strategy ?? "")}`,
    `effects: ${effects}`,
    `non-effects: ${nonEffects}`,
  ].join("\n");
}

function registerRuntime(pi: ExtensionAPI): void {
  let sequence = 0;
  let state: RuntimeState = "idle";
  let reportQueue: Promise<void> = Promise.resolve();
  const report = (
    nextState: RuntimeState,
    event: "session_start" | "lifecycle" | "session_shutdown" = "lifecycle",
    ctx: RuntimeContext,
    reference?: SessionReference,
  ) => {
    sequence += 1;
    state = nextState;
    const payload = runtimePayload(nextState, event, sequence, ctx, reference);
    const task = reportQueue.catch(() => undefined).then(async () => {
      await runtimeRequest(payload);
    });
    reportQueue = task;
    return task;
  };

  pi.on("session_start", async (_event, ctx) => {
    try {
      await report("idle", "session_start", ctx, sessionReference(ctx));
    } catch (error) {
      ctx.ui.notify(`Pisec runtime startup failed: ${error instanceof Error ? error.message : String(error)}`, "error");
      throw error;
    }
  });
  pi.on("session_switch", async (_event, ctx) => report("idle", "lifecycle", ctx, sessionReference(ctx)));
  pi.on("session_branch", async (_event, ctx) => report("idle", "lifecycle", ctx, sessionReference(ctx)));
  pi.on("session_tree", async (_event, ctx) => report("idle", "lifecycle", ctx, sessionReference(ctx)));
  pi.on("before_agent_start", async (event, ctx) => {
    await report("working", "lifecycle", ctx);
    await reportQueue;
    if (ROLE === "secretary") {
      return {
        systemPrompt: [
          ...event.systemPrompt,
          "Pisec secretary contract: you are trusted inside exactly one registered project Fence. You may use the full standard OMP tool surface, installed plugins, project MCP, copied user extensions/skills/rules/commands/themes/agents, normal local Git, project writes, and broad public web access. Plugins and MCP are trusted code inside this same Fence boundary, not extra sandboxes. Fence denies sibling projects, host secrets, metadata IP, and the real harness/workspace state. Keep worker creation and merge application behind exact interactive approval. For independent worker research requests, list pending packets and launch the exact @smol pisec-web-research agent in one task batch; return every answer through durable Pisec research tools. Do not claim product state from memory; inspect through Pisec adapters.",
        ],
      };
    }
    try {
      const taskPacket = await runtimeOperation("task.get");
      const research = await runtimeOperation("research.list");
      const requests = research && typeof research === "object" && Array.isArray((research as JsonObject).requests)
        ? ((research as JsonObject).requests as unknown[]).filter(item => item && typeof item === "object" && (item as JsonObject).state !== "acknowledged")
        : [];
      return {
        systemPrompt: [
          ...event.systemPrompt,
          "Pisec worker contract: the following broker-authenticated immutable task packet is authoritative for this workstream. Do not edit or reinterpret its execution identity. Built-in web search is available only within the approved Fence domains. If information is unavailable, persist a bounded secretary research request instead of trying to widen Fence, and do not block synchronously waiting for a response. On resume, consume replayed secretary packets and acknowledge them only after using their contents.",
          `IMMUTABLE_TASK_PACKET\n${renderTaskPacket(taskPacket)}`,
          `UNACKNOWLEDGED_SECRETARY_RESEARCH\n${renderTaskPacket(requests)}`,
        ],
      };
    } catch (error) {
      throw new Error(`Pisec worker startup failed closed: ${error instanceof Error ? error.message : String(error)}`);
    }
  });
  pi.on("agent_end", async (_event, ctx) => report("idle", "lifecycle", ctx));
  pi.on("tool_approval_requested", async (_event, ctx) => report("blocked", "lifecycle", ctx));
  pi.on("tool_approval_resolved", async (_event, ctx) => report(state === "blocked" ? "working" : state, "lifecycle", ctx));
  pi.on("auto_retry_start", async (_event, ctx) => report("blocked", "lifecycle", ctx));
  pi.on("auto_retry_end", async (_event, ctx) => report(state === "blocked" ? "working" : state, "lifecycle", ctx));
  pi.on("session_shutdown", async (_event, ctx) => report("stopped", "session_shutdown", ctx));
}

function secretaryTools(pi: ExtensionAPI): void {
  const z = pi.zod;
  const semantic = (name: string, label: string, operation: string, parameters: unknown, approval: "read" | "exec", map?: (params: JsonObject) => JsonObject) => {
    pi.registerTool({
      name,
      label,
      description: `Use the Pisec broker operation ${operation}.`,
      approval,
      parameters,
      async execute(_id, params, signal) {
        try {
          return textResult(await semanticRequest(operation, map ? map(params as JsonObject) : (params as JsonObject), signal));
        } catch (error) {
          return textResult(error instanceof Error ? error.message : String(error), true);
        }
      },
    });
  };

  semantic("pisec_project_status", "Pisec project status", "project.status", z.object({}), "read");
  semantic("pisec_git_status", "Pisec Git status", "git.status", z.object({}), "read");
  semantic("pisec_inspect_workstream_changes", "Inspect workstream Git changes", "git.workstream_changes", z.object({ workstream_id: z.string().min(1).max(128) }), "read", params => ({ workstreamId: params.workstream_id }));
  semantic("pisec_prepare_workstream_merge", "Prepare workstream merge", "git.merge.prepare", z.object({ workstream_id: z.string().min(1).max(128) }), "read", params => ({ workstreamId: params.workstream_id }));
  pi.registerTool({
    name: "pisec_merge_workstream",
    label: "Merge Pisec workstream",
    description: "Apply one exact, previously prepared fast-forward merge scope.",
    approval: scope => ({ tier: "exec", policy: "prompt", reason: renderMergeScope(scope) }),
    parameters: z.object({ approval_scope: z.any() }),
    async execute(_id, params, _signal, _onUpdate, ctx) {
      if (!ctx.hasUI) return textResult("Pisec refused Git merge because an interactive approval UI is unavailable.", true);
      try {
        return textResult(await semanticRequest("git.merge.apply", { approvalScope: params.approval_scope as JsonObject }));
      } catch (error) {
        return textResult(error instanceof Error ? error.message : String(error), true);
      }
    },
  });
  semantic("pisec_list_workstreams", "List Pisec workstreams", "workstream.list", z.object({}), "read");
  semantic("pisec_inspect_workstream", "Inspect Pisec workstream", "workstream.inspect", z.object({ workstream_id: z.string().min(1).max(128) }), "read", params => ({ workstreamId: params.workstream_id }));
  const taskPacketSchema = z.object({ schemaVersion: z.literal(1), outcome: z.string().min(1).max(4096), boundaries: z.array(z.string().min(1).max(4096)).max(16), acceptance: z.array(z.string().min(1).max(4096)).max(16), openQuestions: z.array(z.string().min(1).max(4096)).max(16), evidence: z.array(z.string().min(1).max(4096)).max(16) });
  semantic("pisec_prepare_workstream", "Prepare Pisec workstream", "workstream.prepare", z.object({ title: z.string().min(1).max(512), purpose: z.string().min(1).max(4096), brief: z.string().min(1).max(4096), task_packet: taskPacketSchema, idempotency_key: z.string().min(1).max(256), target_ref: z.string().min(1).max(512).optional(), execution_profile: z.enum(["worker-default", "worker-networked"]).optional() }), "read", params => ({ title: params.title, purpose: params.purpose, brief: params.brief, taskPacket: params.task_packet, idempotencyKey: params.idempotency_key, ...(params.target_ref ? { targetRef: params.target_ref } : {}), ...(params.execution_profile ? { executionProfile: params.execution_profile } : {}) }));
  pi.registerTool({
    name: "pisec_create_workstream",
    label: "Create Pisec workstream",
    description: "Apply one previously prepared immutable Pisec workstream scope. This is the only semantic tool that creates external resources and always requires exact user approval.",
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
  semantic("pisec_send_workstream", "Send Pisec workstream message", "workstream.send", z.object({ workstream_id: z.string().min(1).max(128), message: z.string().min(1).max(4096) }), "exec", params => ({ workstreamId: params.workstream_id, text: params.message }));
  semantic("pisec_focus_workstream", "Focus Pisec workstream", "workstream.focus", z.object({ workstream_id: z.string().min(1).max(128) }), "exec", params => ({ workstreamId: params.workstream_id }));
  semantic("pisec_complete_workstream", "Complete Pisec workstream", "workstream.complete", z.object({ workstream_id: z.string().min(1).max(128) }), "exec", params => ({ workstreamId: params.workstream_id }));
  semantic("pisec_retire_workstream", "Retire Pisec workstream", "workstream.retire", z.object({ workstream_id: z.string().min(1).max(128) }), "exec", params => ({ workstreamId: params.workstream_id }));
  semantic("pisec_list_decisions", "List Pisec decisions", "decision.list", z.object({ state: z.enum(["open", "resolved"]).optional() }), "read", params => params.state ? { state: params.state } : {});
  semantic("pisec_record_decision", "Record Pisec decision", "decision.record", z.object({ summary: z.string().min(1).max(512), context: z.any(), workstream_id: z.string().min(1).max(128).optional() }), "exec", params => ({ summary: params.summary, context: params.context, ...(params.workstream_id ? { workstreamId: params.workstream_id } : {}) }));
  semantic("pisec_resolve_decision", "Resolve Pisec decision", "decision.resolve", z.object({ decision_id: z.string().min(1).max(128), resolution: z.string().min(1).max(4096) }), "exec", params => ({ decisionId: params.decision_id, resolution: params.resolution }));
  semantic("pisec_list_worker_research_requests", "List worker research requests", "research.list", z.object({ state: z.enum(["pending", "researching", "needs_context", "answered", "declined", "acknowledged"]).optional(), limit: z.number().int().min(1).max(100).optional() }), "read", params => ({ ...(params.state ? { state: params.state } : {}), ...(params.limit ? { limit: params.limit } : {}) }));
  semantic("pisec_claim_worker_research", "Claim worker research", "research.claim", z.object({ request_id: z.string().min(1).max(128) }), "read", params => ({ requestId: params.request_id }));
  semantic("pisec_request_worker_research_context", "Request worker research context", "research.request_context", z.object({ request_id: z.string().min(1).max(128), idempotency_key: z.string().min(1).max(256), context_request: z.any() }), "read", params => ({ requestId: params.request_id, idempotencyKey: params.idempotency_key, contextRequest: params.context_request }));
  semantic("pisec_answer_worker_research", "Answer worker research", "research.answer", z.object({ request_id: z.string().min(1).max(128), idempotency_key: z.string().min(1).max(256), result: z.any() }), "read", params => ({ requestId: params.request_id, idempotencyKey: params.idempotency_key, result: params.result }));
  semantic("pisec_decline_worker_research", "Decline worker research", "research.decline", z.object({ request_id: z.string().min(1).max(128), idempotency_key: z.string().min(1).max(256), decline: z.any() }), "read", params => ({ requestId: params.request_id, idempotencyKey: params.idempotency_key, decline: params.decline }));
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
          return textResult(await runtimeOperation(operation, map ? map(params as JsonObject) : {}, signal));
        } catch (error) {
          return textResult(error instanceof Error ? error.message : String(error), true);
        }
      },
    });
  };
  runtimeTool("pisec_show_task_packet", "Show immutable task packet", "Retrieve the broker-authenticated immutable task packet for this workstream.", z.object({}), "task.get");
  runtimeTool("pisec_request_secretary_research", "Request secretary research", "Persist a bounded research request without waiting for the secretary.", z.object({ summary: z.string().min(1).max(1024), question: z.string().min(1).max(4096), context: z.string().max(4096).optional(), attempted: z.array(z.string().min(1).max(4096)).max(16).optional(), candidate_sources: z.array(z.string().url().max(2048)).max(16).optional(), blocking: z.boolean().optional(), idempotency_key: z.string().min(1).max(256) }), "research.request", params => ({ idempotencyKey: params.idempotency_key, request: { kind: "research", summary: params.summary, question: params.question, context: params.context ?? "", attempted: params.attempted ?? [], candidateSources: params.candidate_sources ?? [], blocking: params.blocking ?? true } }));
  runtimeTool("pisec_check_secretary_research", "Check secretary research", "List durable research packets for this workstream, including replayed answers.", z.object({}), "research.list");
  runtimeTool("pisec_add_secretary_research_context", "Add secretary research context", "Add bounded context requested by the secretary and return the request to pending.", z.object({ request_id: z.string().min(1).max(128), idempotency_key: z.string().min(1).max(256), context: z.string().min(1).max(4096), attempted: z.array(z.string().min(1).max(4096)).max(16).optional(), candidate_sources: z.array(z.string().url().max(2048)).max(16).optional() }), "research.add_context", params => ({ requestId: params.request_id, idempotencyKey: params.idempotency_key, context: { context: params.context, attempted: params.attempted ?? [], candidateSources: params.candidate_sources ?? [] } }));
  runtimeTool("pisec_acknowledge_secretary_research", "Acknowledge secretary research", "Acknowledge an answered or declined durable research request after consuming it.", z.object({ request_id: z.string().min(1).max(128) }), "research.acknowledge", params => ({ requestId: params.request_id }));
}

export default function pisec(pi: ExtensionAPI): void {
  if (!isPisecRole(ROLE) || !RUNTIME_SOCKET || !RUNTIME_TOKEN || !WORKSTREAM_ID || !INSTANCE_ID || !SURFACE_ID) return;
  if (ROLE === "secretary" && !SECRETARY_SOCKET) return;
  pi.setLabel(ROLE === "secretary" ? "Pisec Secretary" : "Pisec Worker");
  registerRuntime(pi);
  if (ROLE === "secretary") secretaryTools(pi);
  else workerTools(pi);
}
