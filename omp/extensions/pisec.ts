import { lstatSync, realpathSync } from "node:fs";
import { isAbsolute, relative, resolve, sep } from "node:path";
import { randomBytes } from "node:crypto";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

type JsonObject = Record<string, unknown>;
type RuntimeState = "starting" | "working" | "blocked" | "idle" | "done" | "stopped" | "missing" | "error" | "unknown";
type SessionSwitchReason = "new" | "resume" | "fork" | "handoff";
type SessionReference = { kind: "path" | "id" | null; value: string | null };
type RuntimeContext = { hasUI: boolean; sessionManager: { getSessionFile?: () => string | undefined } };

const ROLE = process.env.PISEC_ROLE;
const RUNTIME_SOCKET = process.env.PISEC_RUNTIME_SOCKET;
const SECRETARY_SOCKET = process.env.PISEC_SECRETARY_SOCKET;
const FLEET_SOCKET = process.env.PISEC_FLEET_SOCKET;
const RUNTIME_TOKEN = process.env.PISEC_RUNTIME_TOKEN;
const WORKSTREAM_ID = process.env.PISEC_WORKSTREAM_ID;
const INSTANCE_ID = process.env.PISEC_RUNTIME_INSTANCE_ID;
const START_SOURCE = process.env.PISEC_SESSION_START_SOURCE === "resume" ? "resume" : "startup";
const SURFACE_ID = process.env.PISEC_SURFACE_ID;
const HARNESS_HOME = process.env.PI_CODING_AGENT_DIR;
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
    `implementation model: ${String(scope.implementationModel ?? "(configured default)")}`,
    `harness model: ${String(scope.harnessModel ?? "(adapter default)")}`,
    `reasoning effort: ${String(scope.reasoningEffort ?? "(adapter default)")}`,
    `workspace adapter: ${String(scope.workspaceAdapterId ?? "")}`,
    `execution profile: ${String(scope.executionProfile ?? "")}`,
    `target ref: ${String(scope.targetRef ?? "")}`,
    `base commit OID: ${String(scope.baseCommitOid ?? "")}`,
    `branch: ${String(scope.branchName ?? "")}`,
    `checkout path: ${String(scope.worktreePath ?? "")}`,
    `agent name: ${String(scope.agentName ?? "")}`,
    `exact external domains: ${externalDomains}`,
    `python env (read-only): ${scope.pythonEnv ? String(scope.pythonEnv) : "(none)"}`,
    `effects: ${effects}`,
    `non-effects: ${nonEffects}`,
    `immutable task packet: ${renderTaskPacket(scope.taskPacket)}`,
  ].join("\n");
}

function renderAcceptanceScope(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "Pisec refused: acceptance scope is not an object";
  const scope = value as JsonObject;
  const changedPaths = Array.isArray(scope.changedPaths) ? scope.changedPaths.join(", ") || "(none)" : "(invalid)";
  const acceptance = Array.isArray(scope.acceptance) ? JSON.stringify(scope.acceptance) : "(invalid)";
  const verification = Array.isArray(scope.verification) ? JSON.stringify(scope.verification) : "(invalid)";
  const effects = Array.isArray(scope.effects) ? scope.effects.join("; ") : "(invalid)";
  const nonEffects = Array.isArray(scope.nonEffects) ? scope.nonEffects.join("; ") : "(invalid)";
  return [
    "Pisec bounded workstream acceptance",
    `project id: ${String(scope.projectId ?? "")}`,
    `workstream id: ${String(scope.workstreamId ?? "")}`,
    `target branch: ${String(scope.targetBranch ?? "")}`,
    `completion packet digest: ${String(scope.completionPacketSha256 ?? "")}`,
    `task packet digest: ${String(scope.taskPacketSha256 ?? "")}`,
    `candidate patch digest: ${String(scope.candidatePatchSha256 ?? "")}`,
    `changed paths: ${changedPaths}`,
    `acceptance criteria: ${acceptance}`,
    `verification evidence: ${verification}`,
    `conflict policy: ${String(scope.conflictPolicy ?? "")}`,
    `merge policy: ${JSON.stringify(scope.mergePolicy ?? {})}`,
    `effects: ${effects}`,
    `non-effects: ${nonEffects}`,
  ].join("\n");
}

function renderAccessScope(value: unknown, action: string): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "Pisec refused: access approval scope is not an object";
  const scope = value as JsonObject;
  const effects = Array.isArray(scope.effects) ? scope.effects.join("; ") : "(invalid)";
  const nonEffects = Array.isArray(scope.nonEffects) ? scope.nonEffects.join("; ") : "(invalid)";
  return [
    `Pisec exact read access ${action} approval`,
    `operation id: ${String(scope.operationId ?? "")}`,
    `project id: ${String(scope.projectId ?? "")}`,
    `grant id: ${String(scope.grantId ?? "")}`,
    `subject: ${String(scope.subjectKind ?? "")}`,
    `workstream id: ${String(scope.workstreamId ?? "(all project workers)")}`,
    `path: ${String(scope.path ?? "")}`,
    `mode: ${String(scope.mode ?? "")}`,
    `effects: ${effects}`,
    `non-effects: ${nonEffects}`,
  ].join("\n");
}

function registerRuntime(pi: ExtensionAPI): void {
  let sequence = 0;
  let rootSession = false;
  let agentActive = false;
  let bootstrapGeneration = 0;
  let bootstrapSessionFile: string | null = null;
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
  pi.on("session_start", async (_event, ctx) => {
    rootSession = ctx.hasUI === true;
    if (!rootSession) return;
    agentActive = false;
    try {
      await report("idle", "session_start", ctx, sessionReference(ctx));
    } catch (error) {
      ctx.ui.notify(`Pisec runtime startup failed: ${error instanceof Error ? error.message : String(error)}`, "error");
      throw error;
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
    if (ROLE === "first_mate") {
      await runtimeOperation("runtime.turn.prepare");
      return {
        systemPrompt: [
          ...event.systemPrompt,
           "Pisec First Mate contract: you are the coordinator for projects in the configured First Mate fleet scope. Use explicit projectId on every fleet operation. You may inspect fleet status, in-scope project secretaries, in-scope worker worktrees, Git objects, and durable research metadata through the authenticated fleet broker. Read-only filesystem access covers Pisec-managed in-scope project worktrees and Git objects only. Never write project files, worktrees, or Git objects; never raw-push; never register projects, refresh runtimes, administer the host, read host secrets, or self-approve worker creation or workstream acceptance. Do not change lifecycle, Git, or host authority rules; use only brokered operations after exact user approval. Worker creation and bounded workstream acceptance require exact interactive user approval in this surface. After acceptance, the project secretary owns target refresh, bounded worker reconciliation, verification, fast-forward integration, completion, retirement, and cleanup without a second merge approval. Default replies must fit a short screen and use only Status, Needs attention, and Next action when applicable. Report material exceptions, active work, blockers, decisions needed, and next actions; omit healthy or idle project listings, raw metadata, timestamps, event history, and implementation narration. Include projectId or workstreamId only when the user must approve, inspect, or act on that item. If nothing needs action, say so in one sentence. Give detailed evidence only for explicit drill-down requests.",
        ],
      };
    }
    if (ROLE === "secretary") {
      await runtimeOperation("runtime.turn.prepare");
      return {
        systemPrompt: [
          ...event.systemPrompt,
           "Pisec secretary contract: you are trusted inside exactly one registered project Fence. You may use the full standard OMP tool surface, installed plugins, project MCP, copied user extensions/skills/rules/commands/themes/agents, normal local Git, project writes, and broad public web access. Plugins and MCP are trusted code inside this same Fence boundary, not extra sandboxes. Fence denies sibling projects, host secrets, metadata IP, and the real harness/workspace state. Raw git push remains denied; publish an existing non-default branch with pisec_push_branch, which performs only a pinned-origin fast-forward through the host broker without exposing credentials. Keep worker creation and bounded workstream acceptance behind exact interactive approval. After acceptance, own target refresh, bounded worker reconciliation, verification, fast-forward integration, completion, retirement, and cleanup without requesting a second merge approval. For independent worker research requests, list pending packets and launch the exact @smol pisec-web-research agent in one task batch; return every answer through durable Pisec research tools. Do not claim product state from memory; inspect through Pisec adapters.",
        ],
      };
    }
    try {
      bootstrapSessionFile = sessionReference(ctx).value ?? "implicit";
      const bootstrap = await runtimeOperation("runtime.bootstrap.get", { sessionFile: bootstrapSessionFile });
      const fullPacket = bootstrap && typeof bootstrap === "object" ? (bootstrap as JsonObject).fullPacket : null;
      bootstrapGeneration = bootstrap && typeof bootstrap === "object" && typeof (bootstrap as JsonObject).generation === "number"
        ? Number((bootstrap as JsonObject).generation)
        : 0;
      const taskPacket = fullPacket && typeof fullPacket === "object" ? (fullPacket as JsonObject).packet ?? fullPacket : {};
      const pythonEnv = fullPacket && typeof fullPacket === "object" && typeof (fullPacket as JsonObject).pythonEnv === "string"
        ? String((fullPacket as JsonObject).pythonEnv)
        : "";
      const pythonContract = pythonEnv
        ? `PYTHON_ENVIRONMENT\nAn approved python environment is exposed read-only inside this Fence at: ${pythonEnv}\nRun the project interpreter directly. Never run package-management writes inside the shared environment.`
        : "PYTHON_ENVIRONMENT\nNo python environment was approved for this workstream; the Fence exposes none.";
      await runtimeOperation("runtime.turn.prepare");
      return {
        systemPrompt: [
          ...event.systemPrompt,
           "Pisec worker contract: the following broker-authenticated immutable task packet is authoritative for this workstream. Use durable checkpoints and coordination requests for semantic progress. Ordinary chat is transient. When implementation and verification are complete, submit one ready_review checkpoint with exact completion evidence; that checkpoint submits the immutable completion packet automatically. Acceptance is a separate user gate owned by the secretary; never claim acceptance or request a second merge approval. If the secretary reports bounded target drift, rebase only within the accepted task scope, rerun verification, and submit a new ready_review checkpoint.",
          ...(fullPacket ? [`IMMUTABLE_TASK_PACKET\n${renderTaskPacket(taskPacket)}`] : ["IMMUTABLE_TASK_PACKET\nNo packet body changed in this session; retain the previously accepted packet."]),
          pythonContract,
        ],
      };
    } catch (error) {
      throw new Error(`Pisec worker startup failed closed: ${error instanceof Error ? error.message : String(error)}`);
    }
  });
  pi.on("agent_start", async (_event, ctx) => {
    if (!rootSession) return;
    agentActive = true;
    if (ROLE === "worker" && bootstrapSessionFile !== null) {
      await runtimeOperation("runtime.bootstrap.ack", { sessionFile: bootstrapSessionFile, generation: bootstrapGeneration });
    }
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
  semantic("pisec_project_activity", "Pisec project activity", "project.activity", z.object({ after: z.number().int().min(0).optional() }), "read", params => params.after === undefined ? {} : { after: params.after });
  semantic("pisec_refresh_project", "Refresh project runtimes", "project.refresh", z.object({ wait_seconds: z.number().min(0).max(3600).optional() }), "exec", params => params.wait_seconds === undefined ? {} : { waitSeconds: params.wait_seconds });
  semantic("pisec_report_secretary_issue", "Report issue", "issue.report", z.object({ category: z.enum(["permission", "access", "lifecycle", "tooling", "other"]), severity: z.enum(["blocking", "degraded", "improvement"]), summary: z.string().min(1).max(1024), details: z.string().min(1).max(4096), requested_action: z.string().min(1).max(4096), evidence: z.array(z.any()).max(64), idempotency_key: z.string().min(1).max(256) }), "exec", params => ({ category: params.category, severity: params.severity, summary: params.summary, details: params.details, requestedAction: params.requested_action, evidence: params.evidence, idempotencyKey: params.idempotency_key }));
  semantic("pisec_list_issues", "List issues", "issue.list", z.object({ state: z.enum(["open", "acknowledged", "remediating", "verifying", "resolved"]).optional(), limit: z.number().int().min(1).max(1000).optional() }), "read", params => ({ ...(params.state ? { state: params.state } : {}), ...(params.limit ? { limit: params.limit } : {}) }));
  semantic("pisec_inspect_issue", "Inspect issue", "issue.inspect", z.object({ issue_id: z.string().min(1).max(128) }), "read", params => ({ issueId: params.issue_id }));
  semantic("pisec_add_issue_context", "Add issue context", "issue.add_context", z.object({ issue_id: z.string().min(1).max(128), context: z.any(), idempotency_key: z.string().min(1).max(256) }), "exec", params => ({ issueId: params.issue_id, context: params.context, idempotencyKey: params.idempotency_key }));
  semantic("pisec_verify_issue", "Verify issue", "issue.verify", z.object({ issue_id: z.string().min(1).max(128), status: z.enum(["fixed", "still_blocked"]), evidence: z.any(), idempotency_key: z.string().min(1).max(256) }), "exec", params => ({ issueId: params.issue_id, status: params.status, evidence: params.evidence, idempotencyKey: params.idempotency_key }));

  semantic("pisec_project_status", "Pisec project status", "project.status", z.object({}), "read");
  semantic("pisec_git_status", "Pisec Git status", "git.status", z.object({}), "read");
  semantic("pisec_push_branch", "Push project branch", "git.push", z.object({ branch: z.string().min(1).max(512), expected_local_oid: z.string().min(40).max(64), expected_remote_oid: z.string().min(40).max(64) }), "exec", params => ({ branch: params.branch, expectedLocalOid: params.expected_local_oid, expectedRemoteOid: params.expected_remote_oid }));
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
  const taskPacketSchema = z.object({ schemaVersion: z.literal(1), outcome: z.string().min(1).max(4096), boundaries: z.array(z.string().min(1).max(4096)).max(16), acceptance: z.array(z.string().min(1).max(4096)).max(16), openQuestions: z.array(z.string().min(1).max(4096)).max(16), evidence: z.array(z.string().min(1).max(4096)).max(16) });
  semantic("pisec_prepare_workstream", "Prepare Pisec workstream", "workstream.prepare", z.object({ title: z.string().min(1).max(512), purpose: z.string().min(1).max(4096), brief: z.string().min(1).max(4096), task_packet: taskPacketSchema, idempotency_key: z.string().min(1).max(256), target_ref: z.string().min(1).max(512).optional(), implementation_model: z.string().min(1).max(256).optional(), execution_profile: z.enum(["worker-default", "worker-networked"]).optional(), work_mode: z.enum(["FAST", "RIP", "BUILD", "MAJOR"]).optional(), learning_overlay: z.enum(["OFF", "LIGHT", "DEEP"]).optional(), learning_seam: z.string().min(1).max(1024).optional(), decision_ids: z.array(z.string().min(1).max(128)).max(16).optional(), python_env: z.string().min(1).max(4096).optional() }), "read", params => ({ title: params.title, purpose: params.purpose, brief: params.brief, taskPacket: params.task_packet, idempotencyKey: params.idempotency_key, ...(params.target_ref ? { targetRef: params.target_ref } : {}), ...(params.implementation_model ? { implementationModel: params.implementation_model } : {}), ...(params.execution_profile ? { executionProfile: params.execution_profile } : {}), ...(params.work_mode ? { workMode: params.work_mode } : {}), ...(params.learning_overlay ? { learningOverlay: params.learning_overlay } : {}), ...(params.learning_seam ? { learningSeam: params.learning_seam } : {}), ...(params.decision_ids ? { decisionIds: params.decision_ids } : {}), ...(params.python_env ? { pythonEnv: params.python_env } : {}) }));
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
  semantic("pisec_retire_workstream", "Retire Pisec workstream", "workstream.retire", z.object({ workstream_id: z.string().min(1).max(128) }), "exec", params => ({ workstreamId: params.workstream_id }));
  semantic("pisec_list_decisions", "List Pisec decisions", "decision.list", z.object({ state: z.enum(["open", "resolved"]).optional() }), "read", params => params.state ? { state: params.state } : {});
  semantic("pisec_record_decision", "Record Pisec decision", "decision.record", z.object({ summary: z.string().min(1).max(512), context: z.any(), workstream_id: z.string().min(1).max(128).optional() }), "exec", params => ({ summary: params.summary, context: params.context, ...(params.workstream_id ? { workstreamId: params.workstream_id } : {}) }));
  semantic("pisec_list_coordination_requests", "List coordination requests", "coordination.list", z.object({ workstream_id: z.string().min(1).max(128).optional(), include_resolved: z.boolean().optional() }), "read", params => ({ ...(params.workstream_id ? { workstreamId: params.workstream_id } : {}), ...(params.include_resolved !== undefined ? { includeResolved: params.include_resolved } : {}) }));
  semantic("pisec_inspect_coordination_request", "Inspect coordination request", "coordination.inspect", z.object({ request_id: z.string().min(1).max(128) }), "read", params => ({ requestId: params.request_id }));
  semantic("pisec_answer_coordination_request", "Answer coordination request", "coordination.answer", z.object({ request_id: z.string().min(1).max(128), response: z.string().min(1).max(4096), idempotency_key: z.string().min(1).max(256), decision_id: z.string().min(1).max(128).optional() }), "exec", params => ({ requestId: params.request_id, response: params.response, idempotencyKey: params.idempotency_key, ...(params.decision_id ? { decisionId: params.decision_id } : {}) }));
  semantic("pisec_resolve_decision", "Resolve Pisec decision", "decision.resolve", z.object({ decision_id: z.string().min(1).max(128), resolution: z.string().min(1).max(4096) }), "exec", params => ({ decisionId: params.decision_id, resolution: params.resolution }));
  semantic("pisec_list_worker_research_requests", "List worker research requests", "research.list", z.object({ state: z.enum(["pending", "researching", "needs_context", "answered", "declined", "acknowledged"]).optional(), limit: z.number().int().min(1).max(100).optional(), workstream_id: z.string().min(1).max(128).optional() }), "read", params => ({ ...(params.state ? { state: params.state } : {}), ...(params.limit ? { limit: params.limit } : {}), ...(params.workstream_id ? { workstreamId: params.workstream_id } : {}) }));
  semantic("pisec_inspect_worker_research", "Inspect worker research", "research.inspect", z.object({ request_id: z.string().min(1).max(128), workstream_id: z.string().min(1).max(128).optional() }), "read", params => ({ requestId: params.request_id, ...(params.workstream_id ? { workstreamId: params.workstream_id } : {}) }));
  semantic("pisec_claim_worker_research", "Claim worker research", "research.claim", z.object({ request_id: z.string().min(1).max(128) }), "read", params => ({ requestId: params.request_id }));
  semantic("pisec_request_worker_research_context", "Request worker research context", "research.request_context", z.object({ request_id: z.string().min(1).max(128), idempotency_key: z.string().min(1).max(256), context_request: z.any() }), "read", params => ({ requestId: params.request_id, idempotencyKey: params.idempotency_key, contextRequest: params.context_request }));
  semantic("pisec_answer_worker_research", "Answer worker research", "research.answer", z.object({ request_id: z.string().min(1).max(128), idempotency_key: z.string().min(1).max(256), result: z.any() }), "read", params => ({ requestId: params.request_id, idempotencyKey: params.idempotency_key, result: params.result }));
  semantic("pisec_decline_worker_research", "Decline worker research", "research.decline", z.object({ request_id: z.string().min(1).max(128), idempotency_key: z.string().min(1).max(256), decline: z.any() }), "read", params => ({ requestId: params.request_id, idempotencyKey: params.idempotency_key, decline: params.decline }));
}
function fleetTools(pi: ExtensionAPI): void {
  const z = pi.zod;
  const fleet = (name: string, label: string, operation: string, parameters: unknown, approval: "read" | "exec", map?: (params: JsonObject) => JsonObject) => {
    pi.registerTool({
      name,
      label,
      description: `Use the Pisec fleet broker operation ${operation}.`,
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
  const projectId = z.string().min(1).max(128);
  fleet("pisec_fleet_list_access_grants", "List read grants", "fleet.access.list", z.object({ project_id: projectId, workstream_id: z.string().min(1).max(128).optional() }), "read", params => ({ projectId: params.project_id, ...(params.workstream_id ? { workstreamId: params.workstream_id } : {}) }));
  fleet("pisec_fleet_inspect_access_grant", "Inspect read grant", "fleet.access.inspect", z.object({ project_id: projectId, grant_id: z.string().min(1).max(128) }), "read", params => ({ projectId: params.project_id, grantId: params.grant_id }));
  fleet("pisec_fleet_prepare_access_grant", "Prepare read grant", "fleet.access.grant.prepare", z.object({ project_id: projectId, subject_kind: z.enum(["workstream", "project_workers"]), path: z.string().min(1).max(4096), idempotency_key: z.string().min(1).max(256), workstream_id: z.string().min(1).max(128).optional(), issue_id: z.string().min(1).max(128).optional() }), "read", params => ({ projectId: params.project_id, subjectKind: params.subject_kind, path: params.path, idempotencyKey: params.idempotency_key, ...(params.workstream_id ? { workstreamId: params.workstream_id } : {}), ...(params.issue_id ? { issueId: params.issue_id } : {}) }));
  const accessApproval = (name: string, label: string, operation: string, action: string) => {
    pi.registerTool({
      name,
      label,
      description: `Apply one exact prepared read access ${action}.`,
      approval: scope => ({ tier: "exec", policy: "prompt", reason: renderAccessScope(scope, action) }),
      parameters: z.object({ project_id: projectId, approval_scope: z.any() }),
      async execute(_id, params, _signal, _onUpdate, ctx) {
        if (!ctx.hasUI) return textResult(`Pisec refused read access ${action} because interactive approval is unavailable.`, true);
        try { return textResult(await semanticRequest(operation, { projectId: params.project_id, approvalScope: params.approval_scope as JsonObject })); }
        catch (error) { return textResult(error instanceof Error ? error.message : String(error), true); }
      },
    });
  };
  accessApproval("pisec_fleet_apply_access_grant", "Apply read grant", "fleet.access.grant.apply", "grant");
  fleet("pisec_fleet_prepare_access_revoke", "Prepare read grant revoke", "fleet.access.revoke.prepare", z.object({ project_id: projectId, grant_id: z.string().min(1).max(128), idempotency_key: z.string().min(1).max(256) }), "read", params => ({ projectId: params.project_id, grantId: params.grant_id, idempotencyKey: params.idempotency_key }));
  accessApproval("pisec_fleet_apply_access_revoke", "Apply read grant revoke", "fleet.access.revoke.apply", "revoke");
  fleet("pisec_fleet_list_issues", "List issues", "fleet.issue.list", z.object({ project_id: projectId.optional(), state: z.enum(["open", "acknowledged", "remediating", "verifying", "resolved"]).optional(), limit: z.number().int().min(1).max(1000).optional() }), "read", params => ({ ...(params.project_id ? { projectId: params.project_id } : {}), ...(params.state ? { state: params.state } : {}), ...(params.limit ? { limit: params.limit } : {}) }));
  fleet("pisec_fleet_inspect_issue", "Inspect issue", "fleet.issue.inspect", z.object({ issue_id: z.string().min(1).max(128), project_id: projectId.optional() }), "read", params => ({ issueId: params.issue_id, ...(params.project_id ? { projectId: params.project_id } : {}) }));
  fleet("pisec_fleet_add_issue_context", "Add issue context", "fleet.issue.add_context", z.object({ project_id: projectId, issue_id: z.string().min(1).max(128), context: z.any(), idempotency_key: z.string().min(1).max(256) }), "exec", params => ({ projectId: params.project_id, issueId: params.issue_id, context: params.context, idempotencyKey: params.idempotency_key }));
  fleet("pisec_fleet_acknowledge_issue", "Acknowledge issue", "fleet.issue.acknowledge", z.object({ project_id: projectId, issue_id: z.string().min(1).max(128) }), "exec", params => ({ projectId: params.project_id, issueId: params.issue_id }));
  fleet("pisec_fleet_resolve_issue", "Resolve issue", "fleet.issue.resolve", z.object({ project_id: projectId, issue_id: z.string().min(1).max(128), disposition: z.enum(["declined", "duplicate", "not_reproducible"]), reason: z.string().min(1).max(4096), decision_id: z.string().min(1).max(128) }), "exec", params => ({ projectId: params.project_id, issueId: params.issue_id, disposition: params.disposition, reason: params.reason, decisionId: params.decision_id }));
  fleet("pisec_fleet_status", "Fleet status", "fleet.status", z.object({ project_id: projectId.optional() }), "read", params => params.project_id ? { projectId: params.project_id } : {});
  fleet("pisec_fleet_events", "Fleet events", "fleet.events", z.object({ after: z.number().int().min(0).optional(), limit: z.number().int().min(1).max(1000).optional() }), "read", params => ({ ...(params.after !== undefined ? { after: params.after } : {}), ...(params.limit !== undefined ? { limit: params.limit } : {}) }));
  fleet("pisec_fleet_send_secretary", "Message project secretary", "fleet.secretary.send", z.object({ project_id: projectId, message: z.string().min(1).max(4096), workstream_id: z.string().min(1).max(128).optional() }), "exec", params => ({ projectId: params.project_id, text: params.message, ...(params.workstream_id ? { workstreamId: params.workstream_id } : {}) }));
  fleet("pisec_fleet_list_workstreams", "List project workstreams", "fleet.workstream.list", z.object({ project_id: projectId }), "read", params => ({ projectId: params.project_id }));
  fleet("pisec_fleet_inspect_workstream", "Inspect project workstream", "fleet.workstream.inspect", z.object({ project_id: projectId, workstream_id: z.string().min(1).max(128) }), "read", params => ({ projectId: params.project_id, workstreamId: params.workstream_id }));
  fleet("pisec_fleet_list_integrations", "List project integrations", "fleet.integration.list", z.object({ project_id: projectId, state: z.enum(["queued", "refreshing", "awaiting_worker", "verifying", "applying", "integrated", "needs_attention"]).optional() }), "read", params => ({ projectId: params.project_id, ...(params.state ? { state: params.state } : {}) }));
  fleet("pisec_fleet_inspect_integration", "Inspect project integration", "fleet.integration.inspect", z.object({ project_id: projectId, integration_id: z.string().min(1).max(128) }), "read", params => ({ projectId: params.project_id, integrationId: params.integration_id }));
  fleet("pisec_fleet_git_changes", "Inspect project workstream changes", "fleet.git.workstream_changes", z.object({ project_id: projectId, workstream_id: z.string().min(1).max(128) }), "read", params => ({ projectId: params.project_id, workstreamId: params.workstream_id }));
  const taskPacketSchema = z.object({ schemaVersion: z.literal(1), outcome: z.string().min(1).max(4096), boundaries: z.array(z.string().min(1).max(4096)).max(16), acceptance: z.array(z.string().min(1).max(4096)).max(16), openQuestions: z.array(z.string().min(1).max(4096)).max(16), evidence: z.array(z.string().min(1).max(4096)).max(16) });
  fleet("pisec_fleet_prepare_workstream", "Prepare project worker", "fleet.workstream.prepare", z.object({ project_id: projectId, title: z.string().min(1).max(512), purpose: z.string().min(1).max(4096), brief: z.string().min(1).max(4096), task_packet: taskPacketSchema, idempotency_key: z.string().min(1).max(256), target_ref: z.string().min(1).max(512).optional(), implementation_model: z.string().min(1).max(256).optional(), execution_profile: z.enum(["worker-default", "worker-networked"]).optional(), python_env: z.string().min(1).max(4096).optional() }), "read", params => ({ projectId: params.project_id, title: params.title, purpose: params.purpose, brief: params.brief, taskPacket: params.task_packet, idempotencyKey: params.idempotency_key, ...(params.target_ref ? { targetRef: params.target_ref } : {}), ...(params.implementation_model ? { implementationModel: params.implementation_model } : {}), ...(params.execution_profile ? { executionProfile: params.execution_profile } : {}), ...(params.python_env ? { pythonEnv: params.python_env } : {}) }));
  pi.registerTool({
    name: "pisec_fleet_create_worker",
    label: "Create project worker",
    description: "Apply one exact prepared worker scope after interactive user approval.",
    approval: scope => ({ tier: "exec", policy: "prompt", reason: renderExactScope(scope) }),
    parameters: z.object({ project_id: projectId, approval_scope: z.any() }),
    async execute(_id, params, _signal, _onUpdate, ctx) {
      if (!ctx.hasUI) return textResult("Pisec refused worker creation because interactive approval is unavailable.", true);
      try { return textResult(await semanticRequest("fleet.workstream.authorize_apply", { projectId: params.project_id, approvalScope: params.approval_scope as JsonObject })); }
      catch (error) { return textResult(error instanceof Error ? error.message : String(error), true); }
    },
  });
  fleet("pisec_fleet_prepare_acceptance", "Prepare project acceptance", "fleet.workstream.accept.prepare", z.object({ project_id: projectId, workstream_id: z.string().min(1).max(128) }), "read", params => ({ projectId: params.project_id, workstreamId: params.workstream_id }));
  pi.registerTool({
    name: "pisec_fleet_accept_workstream",
    label: "Accept project workstream",
    description: "Accept one exact bounded project workstream candidate. The secretary owns later integration and closeout.",
    approval: scope => ({ tier: "exec", policy: "prompt", reason: renderAcceptanceScope(scope) }),
    parameters: z.object({ project_id: projectId, approval_scope: z.any() }),
    async execute(_id, params, _signal, _onUpdate, ctx) {
      if (!ctx.hasUI) return textResult("Pisec refused workstream acceptance because interactive approval is unavailable.", true);
      try { return textResult(await semanticRequest("fleet.workstream.accept.apply", { projectId: params.project_id, approvalScope: params.approval_scope as JsonObject })); }
      catch (error) { return textResult(error instanceof Error ? error.message : String(error), true); }
    },
  });
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
  runtimeTool("pisec_checkpoint_workstream", "Checkpoint workstream", "Record a semantic checkpoint. The ready_review phase is the only completion path: include immutable completion evidence in the same call; the broker stores the checkpoint and completion atomically.", z.object({ idempotency_key: z.string().min(1).max(256), phase: z.enum(["investigating", "implementing", "verifying", "needs_input", "ready_review"]), summary: z.string().min(1).max(1024), next_action: z.string().min(1).max(1024), blocker_code: z.string().min(1).max(128).optional(), blocker: z.string().min(1).max(2048).optional(), evidence: z.array(z.any()).max(64), completion: z.object({ acceptance: z.array(z.object({ criterion: z.string().min(1).max(4096), status: z.literal("passed"), evidence: z.array(z.string().min(1).max(4096)).min(1)})).min(1).max(32), verification: z.array(z.object({ command: z.string().min(1).max(4096), result: z.string().min(1).max(8192)})).min(1).max(32), source_commit: z.string().min(40).max(64), task_packet_sha256: z.string().min(64).max(64), changed_surfaces: z.array(z.string().min(1).max(4096)).max(32), residual_risk: z.string().max(4096) }).optional() }), "workstream.checkpoint", params => ({ idempotencyKey: params.idempotency_key, phase: params.phase, summary: params.summary, nextAction: params.next_action, ...(params.blocker_code ? { blockerCode: params.blocker_code } : {}), ...(params.blocker ? { blocker: params.blocker } : {}), evidence: params.evidence, ...(params.completion ? { completionPacket: { acceptance: params.completion.acceptance, verification: params.completion.verification, sourceCommit: params.completion.source_commit, taskPacketSha256: params.completion.task_packet_sha256, changedSurfaces: params.completion.changed_surfaces, residualRisk: params.completion.residual_risk } } : {}) }));
  runtimeTool("pisec_submit_completion", "Submit completion", "Submit immutable completion evidence for the current worker commit. Use pisec_checkpoint_workstream with ready_review when possible; use this fallback only when the checkpoint call cannot carry the completion object.", z.object({ completion: z.object({ acceptance: z.array(z.object({ criterion: z.string().min(1).max(4096), status: z.literal("passed"), evidence: z.array(z.string().min(1).max(4096)).min(1)})).min(1).max(32), verification: z.array(z.object({ command: z.string().min(1).max(4096), result: z.string().min(1).max(8192)})).min(1).max(32), source_commit: z.string().min(40).max(64), task_packet_sha256: z.string().length(64), changed_surfaces: z.array(z.string().min(1).max(4096)).max(32), residual_risk: z.string().max(4096) }) }), "workstream.completion.submit", params => ({ completionPacket: { acceptance: params.completion.acceptance, verification: params.completion.verification, sourceCommit: params.completion.source_commit, taskPacketSha256: params.completion.task_packet_sha256, changedSurfaces: params.completion.changed_surfaces, residualRisk: params.completion.residual_risk } }));
  runtimeTool("pisec_request_help", "Request help", "Persist one bounded clarification, blocker, review, access, permission, tooling, or lifecycle request.", z.object({ kind: z.enum(["clarification", "blocker", "review", "access", "permission", "tooling", "lifecycle"]), summary: z.string().min(1).max(1024), details: z.string().min(1).max(4096), requested_action: z.string().min(1).max(4096).optional(), blocking: z.boolean().optional(), evidence: z.array(z.any()).max(64).optional(), idempotency_key: z.string().min(1).max(256) }), "help.request", params => ({ kind: params.kind, summary: params.summary, details: params.details, requestedAction: params.requested_action ?? "Provide guidance or remediation.", blocking: params.blocking ?? params.kind === "blocker", evidence: params.evidence ?? [], idempotencyKey: params.idempotency_key }));
  runtimeTool("pisec_request_coordination", "Request coordination", "Persist a bounded clarification, blocker, or review request.", z.object({ kind: z.enum(["clarification", "blocker", "review_request"]), summary: z.string().min(1).max(1024), question: z.string().min(1).max(4096), blocking: z.boolean(), idempotency_key: z.string().min(1).max(256) }), "coordination.request", params => ({ kind: params.kind, summary: params.summary, question: params.question, blocking: params.blocking, idempotencyKey: params.idempotency_key }));
  runtimeTool("pisec_list_coordination", "List coordination", "List compact coordination requests for this workstream.", z.object({ include_resolved: z.boolean().optional() }), "coordination.list", params => params.include_resolved === undefined ? {} : { includeResolved: params.include_resolved });
  runtimeTool("pisec_inspect_coordination", "Inspect coordination", "Inspect one coordination request and its latest response.", z.object({ request_id: z.string().min(1).max(128) }), "coordination.inspect", params => ({ requestId: params.request_id }));
  runtimeTool("pisec_report_issue", "Report issue", "Report a harness, access, lifecycle, or tooling issue.", z.object({ category: z.enum(["permission", "access", "lifecycle", "tooling", "other"]), severity: z.enum(["blocking", "degraded", "improvement"]), summary: z.string().min(1).max(1024), details: z.string().min(1).max(4096), requested_action: z.string().min(1).max(4096), evidence: z.array(z.any()).max(64), idempotency_key: z.string().min(1).max(256) }), "issue.report", params => ({ category: params.category, severity: params.severity, summary: params.summary, details: params.details, requestedAction: params.requested_action, evidence: params.evidence, idempotencyKey: params.idempotency_key }));
  runtimeTool("pisec_list_issues", "List issues", "List issues reported by or linked to this worker.", z.object({ state: z.enum(["open", "acknowledged", "remediating", "verifying", "resolved"]).optional(), limit: z.number().int().min(1).max(1000).optional() }), "issue.list", params => ({ ...(params.state ? { state: params.state } : {}), ...(params.limit ? { limit: params.limit } : {}) }));
  runtimeTool("pisec_inspect_issue", "Inspect issue", "Inspect one issue and its append-only action history.", z.object({ issue_id: z.string().min(1).max(128) }), "issue.inspect", params => ({ issueId: params.issue_id }));
  runtimeTool("pisec_add_issue_context", "Add issue context", "Append bounded evidence to a worker issue.", z.object({ issue_id: z.string().min(1).max(128), context: z.any(), idempotency_key: z.string().min(1).max(256) }), "issue.add_context", params => ({ issueId: params.issue_id, context: params.context, idempotencyKey: params.idempotency_key }));
  runtimeTool("pisec_verify_issue", "Verify issue", "Verify a remediation and close it only when fixed.", z.object({ issue_id: z.string().min(1).max(128), status: z.enum(["fixed", "still_blocked"]), evidence: z.any(), idempotency_key: z.string().min(1).max(256) }), "issue.verify", params => ({ issueId: params.issue_id, status: params.status, evidence: params.evidence, idempotencyKey: params.idempotency_key }));
  runtimeTool("pisec_show_task_packet", "Show immutable task packet", "Retrieve the broker-authenticated immutable task packet for this workstream.", z.object({}), "task.get");
  runtimeTool("pisec_request_secretary_research", "Request secretary research", "Persist a bounded research request without waiting for the secretary.", z.object({ summary: z.string().min(1).max(1024), question: z.string().min(1).max(4096), context: z.string().max(4096).optional(), attempted: z.array(z.string().min(1).max(4096)).max(16).optional(), candidate_sources: z.array(z.string().url().max(2048)).max(16).optional(), blocking: z.boolean().optional(), idempotency_key: z.string().min(1).max(256) }), "research.request", params => ({ idempotencyKey: params.idempotency_key, request: { kind: "research", summary: params.summary, question: params.question, context: params.context ?? "", attempted: params.attempted ?? [], candidateSources: params.candidate_sources ?? [], blocking: params.blocking ?? true } }));
  runtimeTool("pisec_check_secretary_research", "Check secretary research", "List durable research request metadata for this workstream (no packet bodies). Fetch a specific request's full packets with pisec_inspect_secretary_research.", z.object({ state: z.enum(["pending", "researching", "needs_context", "answered", "declined", "acknowledged"]).optional(), limit: z.number().int().min(1).max(100).optional() }), "research.list", params => ({ ...(params.state ? { state: params.state } : {}), ...(params.limit ? { limit: params.limit } : {}) }));
  runtimeTool("pisec_inspect_secretary_research", "Inspect secretary research", "Fetch the full durable packets (including the answer) for one research request by request_id.", z.object({ request_id: z.string().min(1).max(128) }), "research.inspect", params => ({ requestId: params.request_id }));
  runtimeTool("pisec_add_secretary_research_context", "Add secretary research context", "Add bounded context requested by the secretary and return the request to pending.", z.object({ request_id: z.string().min(1).max(128), idempotency_key: z.string().min(1).max(256), context: z.string().min(1).max(4096), attempted: z.array(z.string().min(1).max(4096)).max(16).optional(), candidate_sources: z.array(z.string().url().max(2048)).max(16).optional() }), "research.add_context", params => ({ requestId: params.request_id, idempotencyKey: params.idempotency_key, context: { context: params.context, attempted: params.attempted ?? [], candidateSources: params.candidate_sources ?? [] } }));
  runtimeTool("pisec_acknowledge_secretary_research", "Acknowledge secretary research", "Acknowledge an answered or declined durable research request after consuming it.", z.object({ request_id: z.string().min(1).max(128) }), "research.acknowledge", params => ({ requestId: params.request_id }));
}

export default function pisec(pi: ExtensionAPI): void {
  if (!isPisecRole(ROLE) || !RUNTIME_SOCKET || !RUNTIME_TOKEN || !WORKSTREAM_ID || !INSTANCE_ID || !SURFACE_ID) return;
  if ((ROLE === "secretary" && !SECRETARY_SOCKET) || (ROLE === "first_mate" && !FLEET_SOCKET)) return;
  pi.setLabel(ROLE === "secretary" ? "Pisec Secretary" : ROLE === "first_mate" ? "Pisec First Mate" : "Pisec Worker");
  registerRuntime(pi);
  if (ROLE === "secretary") secretaryTools(pi);
  else if (ROLE === "first_mate") fleetTools(pi);
  else workerTools(pi);
}
