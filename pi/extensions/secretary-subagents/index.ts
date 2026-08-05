import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";
import { discoverAgents } from "../../npm/node_modules/pi-subagents/src/agents/agents.ts";
import { loadConfig } from "../../npm/node_modules/pi-subagents/src/extension/config.ts";
import { SubagentParams } from "../../npm/node_modules/pi-subagents/src/extension/schemas.ts";
import { createSubagentExecutor, type SubagentParamsLike } from "../../npm/node_modules/pi-subagents/src/runs/foreground/subagent-executor.ts";
import { createAsyncJobTracker } from "../../npm/node_modules/pi-subagents/src/runs/background/async-job-tracker.ts";
import { createResultWatcher } from "../../npm/node_modules/pi-subagents/src/runs/background/result-watcher.ts";
import registerSubagentNotify from "../../npm/node_modules/pi-subagents/src/runs/background/notify.ts";
import { registerWaitTool } from "../../npm/node_modules/pi-subagents/src/runs/background/wait-tool.ts";
import { resolveWaitToolConfig } from "../../npm/node_modules/pi-subagents/src/runs/background/wait-config.ts";
import { getArtifactsDir } from "../../npm/node_modules/pi-subagents/src/shared/artifacts.ts";
import { clearLegacyResultAnimationTimer, renderSubagentResult } from "../../npm/node_modules/pi-subagents/src/tui/render.ts";
import {
  ASYNC_DIR, RESULTS_DIR, SUBAGENT_ASYNC_COMPLETE_EVENT, SUBAGENT_ASYNC_STARTED_EVENT,
  type Details, type SubagentState,
} from "../../npm/node_modules/pi-subagents/src/shared/types.ts";
import { actionAuthorization, actionTargetIsAuthorized, type ActionAuthorization } from "./lifecycle.ts";
import { addSecretaryUsage, emptySecretaryUsage, recordSecretarySessionStats, recordSecretarySubagentStats, secretaryUsage } from "./stats.ts";
import { createNativeSupervisorChannel } from "../../npm/node_modules/pi-subagents/src/intercom/native-supervisor-channel.ts";

type SafeAction = "list" | "doctor" | "status" | "interrupt" | "stop" | "resume" | "steer";
const SAFE_ACTIONS = new Set<SafeAction>(["list", "doctor", "status", "interrupt", "stop", "resume", "steer"]);
const READ_ONLY_ACTIONS = new Set<SafeAction>(["list", "doctor", "status"]);
const INVESTIGATOR_TOOLS = ["read", "grep", "find", "ls", "web_search", "fetch_content", "get_search_content", "source_check", "secretary_git", "contact_supervisor", "intercom", "host_command"];
function getSubagentSessionRoot(parentSessionFile: string | null): string {
  const agentDir = process.env.PI_CODING_AGENT_DIR ?? path.join(os.homedir(), ".pi", "agent");
  if (parentSessionFile) {
    const baseName = path.basename(parentSessionFile, ".jsonl");
    // Root JSONL files stay flat and globally resumable. Private child
    // sessions live in the separate subagent namespace instead.
    return path.join(agentDir, "sessions", "subagent", baseName);
  }
  return fs.mkdtempSync(path.join(os.tmpdir(), "pi-secretary-investigation-"));
}

function expandTilde(value: string): string {
  return value.startsWith("~/") ? path.join(os.homedir(), value.slice(2)) : value;
}

function createState(): SubagentState {
  return {
    baseCwd: "",
    currentSessionId: null,
    subagentInProgress: false,
    subagentSpawns: { sessionId: null, count: 0, configuredLimit: null, granted: 0, grantHistory: [] },
    asyncJobs: new Map(),
    fleetJobs: new Map(),
    foregroundRuns: new Map(),
    foregroundControls: new Map(),
    lastForegroundControlId: null,
    pendingForegroundControlNotices: new Map(),
    cleanupTimers: new Map(),
    lastUiContext: null,
    poller: null,
    completionSeen: new Map(),
    watcher: null,
    watcherRestartTimer: null,
    resultFileCoalescer: { schedule: () => false, clear: () => {} },
  };
}

function isReadOnlyAgent(agent: { acceptanceRole?: string; tools?: string[] }): boolean {
  if (agent.acceptanceRole !== "read-only" || !Array.isArray(agent.tools)) return false;
  return !agent.tools.some((tool) => tool === "edit" || tool === "write" || tool === "subagent");
}

function hardenAgent<T extends { tools?: string[] }>(agent: T, gitExtension: string, autoContinueExtension: string, hostCommandExtension: string, webAccessExtension: string): T {
  // Every eligible investigator gets the native parent-feedback channel even
  // when a custom agent omitted it from its frontmatter. The runtime registers
  // the tool for child sessions; keeping it in this strict allowlist makes the
  // capability explicit without granting any mutation tools.
  const tools = [...INVESTIGATOR_TOOLS];
  return {
    ...agent,
    tools,
    mcpDirectTools: [],
    extensions: [],
    subagentOnlyExtensions: [gitExtension, autoContinueExtension, hostCommandExtension, webAccessExtension],
    inheritProjectContext: false,
    inheritSkills: false,
    output: undefined,
    defaultReads: undefined,
    defaultProgress: false,
    defaultTimeoutMs: undefined,
    defaultTurnBudget: undefined,
    toolBudget: undefined,
    memory: undefined,
    interactive: false,
    completionGuard: false,
  } as T;
}

function stripTaskLimits<T extends { toolBudget?: unknown }>(task: T): T {
  return { ...task, toolBudget: undefined } as T;
}

function requestsWorktree(params: SubagentParamsLike): boolean {
  if (params.worktree === true) return true;
  return (params.chain ?? []).some((step) => "worktree" in step && step.worktree === true);
}

function listInvestigators(cwd: string, scope: "user" | "project" | "both"): { content: Array<{ type: "text"; text: string }>; details: Details } {
  const agents = discoverAgents(cwd, scope).agents.filter(isReadOnlyAgent).sort((a, b) => a.name.localeCompare(b.name));
  const lines = [
    "Executable agents:",
    ...(agents.length
      ? agents.map((agent) => `- ${agent.name} (${agent.source}${agent.defaultContext ? `, context: ${agent.defaultContext}` : ""}): ${agent.description}`)
      : ["- (none)"]),
    "",
    "Chains:",
    "- (none)",
  ];
  return { content: [{ type: "text", text: lines.join("\n") }], details: { mode: "management", results: [] } };
}

function rejected(message: string): { content: Array<{ type: "text"; text: string }>; isError: true; details: Details } {
  return {
    content: [{ type: "text", text: message }],
    isError: true,
    details: { mode: "management", results: [] },
  };
}

function actionWasDenied(value: string, action: SafeAction): boolean {
  const words = action === "list"
    ? "list|show|enumerate"
    : action === "doctor"
      ? "doctor|diagnos(?:e|is)|health"
      : action === "status"
        ? "status|progress|state"
        : action === "interrupt"
          ? "interrupt|pause"
          : action === "stop"
            ? "stop|cancel|terminate"
            : action === "resume"
              ? "resume|continue"
              : "steer|redirect|guide";
  return new RegExp(`(?:^|[.!?,;]\\s*)[^.!?,;]{0,50}\\b(?:don't|do not|never|not|without|avoid|can't|cannot|shouldn't|should not)\\b[^.!?,;]{0,50}\\b(?:${words})\\b`).test(value) ||
    new RegExp(`(?:^|[.!?,;]\\s*)[^.!?,;]{0,50}\\b(?:${words})\\b[^.!?,;]{0,40}\\b(?:not needed|unnecessary|not required)\\b`).test(value);
}

function actionWasAuthorized(value: string, action: SafeAction): boolean {
  if (actionWasDenied(value, action)) return false;
  if (action === "list") return /\b(?:list|show|enumerate)\b[\s\S]{0,80}\b(?:investigator|agent|subagent)s?\b|\b(?:investigator|agent|subagent)s?\b[\s\S]{0,80}\b(?:available|list)\b/.test(value);
  if (action === "doctor") return /\b(?:doctor|diagnos(?:e|is)|health check)\b/.test(value);
  if (action === "status") return /^\s*(?:status|progress|state)\b/i.test(value) ||
    /\b(?:show|check|get|report|tell me|what is|give me)\b[\s\S]{0,40}\b(?:status|progress|state)\b|\b(?:status|progress|state)\b\s+(?:of|for|on)\b/.test(value);
  const target = "run|investigator|agent|subagent|job|task|investigation|scout|research|child|it|them|that";
  if (action === "interrupt") return /^\s*(?:interrupt|pause)\b/.test(value) || new RegExp(`\\b(?:interrupt|pause)\\b[\\s\\S]{0,50}\\b(?:${target})\\b`).test(value);
  if (action === "stop") return /^\s*(?:stop|cancel|terminate)\b/.test(value) || new RegExp(`\\b(?:stop|cancel|terminate)\\b[\\s\\S]{0,50}\\b(?:${target})\\b`).test(value);
  if (action === "resume") return /^\s*(?:resume|continue)\\b/.test(value) || new RegExp(`\\b(?:resume|continue)\\b[\\s\\S]{0,50}\\b(?:${target})\\b`).test(value);
  return /^\s*(?:steer|redirect|guide)\\b/.test(value) || new RegExp(`\\b(?:steer|redirect|guide)\\b[\\s\\S]{0,50}\\b(?:${target})\\b`).test(value);
}

export default function secretarySubagents(pi: ExtensionAPI): void {
  // This file lives in the global extension tree for transactional installation,
  // but activates only in the explicitly constrained secretary launcher.
  if (process.env.PI_SECRETARY_READ_ONLY !== "1") return;
  const loaded = loadConfig();
  const agentDir = process.env.PI_CODING_AGENT_DIR ?? path.join(os.homedir(), ".pi", "agent");
  const gitExtension = path.join(agentDir, "extensions", "secretary-investigator-git", "index.ts");
  const autoContinueExtension = path.join(agentDir, "extensions", "auto-continue", "index.ts");
  const hostCommandExtension = path.join(agentDir, "extensions", "host-command", "index.ts");
  const webAccessExtension = path.join(agentDir, "npm", "node_modules", "pi-web-access", "index.ts");
  if (!fs.existsSync(gitExtension)) throw new Error("secretary investigator Git extension is unavailable");
  if (!fs.existsSync(autoContinueExtension)) throw new Error("auto-continue extension is unavailable");
  if (!fs.existsSync(hostCommandExtension)) throw new Error("host-command extension is unavailable");
  if (!fs.existsSync(webAccessExtension)) throw new Error("pi-web-access extension is unavailable");
  // Investigator count is intentionally policy-free. Runtime concurrency is a
  // scheduler, not a workflow prescription, and remains caller-selectable.
  // Interactive secretary sessions must fail closed: an older or missing
  // config must not silently re-enable a blocking wait tool.
  const waitToolEnabled = loaded.waitTool === undefined
    ? false
    : resolveWaitToolConfig(loaded.waitTool).enabled;
  const config = {
    ...loaded,
    asyncByDefault: true,
    forceTopLevelAsync: true,
    // The secretary deliberately does not impose run, turn, or tool budgets.
    // Caller-supplied limits are stripped at the read-only boundary below.
    turnBudget: undefined,
    toolBudget: undefined,
    worktreeSetupHookTimeoutMs: undefined,
    defaultSessionDir: undefined,
    singleRunOutputBaseDir: undefined,
    maxSubagentSpawnsPerSession: 0,
    globalConcurrencyLimit: undefined,
    parallel: {
      ...(loaded.parallel ?? {}),
      maxTasks: Number.MAX_SAFE_INTEGER,
      concurrency: undefined,
    },
  };
  const state = createState();
  const supervisorChannel = createNativeSupervisorChannel(pi, state);
  let sessionStartedAt: number | undefined;
  let sessionStatsId: string | null = null;
  let sessionTurns = 0;
  let sessionUsage = emptySecretaryUsage();
  const projectAlias = process.env.PI_SECRETARY_ALIAS;
  const runtimeStore = globalThis as Record<string, unknown>;
  const runtimeCleanupKey = "__pi_secretary_subagents_runtime_cleanup__";
  const previousRuntimeCleanup = runtimeStore[runtimeCleanupKey];
  if (typeof previousRuntimeCleanup === "function") previousRuntimeCleanup();
  let authorizedActions = new Map<SafeAction, ActionAuthorization>();
  pi.on("input", (event) => {
    authorizedActions = new Map();
    if (event.source !== "extension") {
      const value = event.text.toLowerCase();
      const authorization = actionAuthorization(value);
      for (const action of SAFE_ACTIONS) {
        if (actionWasAuthorized(value, action)) authorizedActions.set(action, authorization);
      }
    }
    return { action: "continue" };
  });
  fs.mkdirSync(ASYNC_DIR, { recursive: true });
  fs.mkdirSync(RESULTS_DIR, { recursive: true });
  const { handleStarted, handleComplete, resetJobs, restoreActiveJobs } = createAsyncJobTracker(
    pi, state, ASYNC_DIR, { widgetEnabled: false },
  );
  const resultWatcher = createResultWatcher(pi, state, RESULTS_DIR, 10 * 60 * 1000);
  resultWatcher.startResultWatcher();
  resultWatcher.primeExistingResults();
  const eventUnsubscribes = [
    pi.events.on(SUBAGENT_ASYNC_STARTED_EVENT, handleStarted),
    pi.events.on(SUBAGENT_ASYNC_COMPLETE_EVENT, (data) => {
      recordSecretarySubagentStats({ data, projectAlias });
      handleComplete(data);
    }),
  ].filter((unsubscribe): unsubscribe is () => void => typeof unsubscribe === "function");
  const disposeNotify = registerSubagentNotify(pi, state, { completionVisibility: "hidden-success" });
  let runtimeDisposed = false;
  const cleanupRuntime = () => {
    if (runtimeDisposed) return;
    runtimeDisposed = true;
    supervisorChannel.dispose();
    disposeNotify();
    resultWatcher.stopResultWatcher();
    for (const unsubscribe of eventUnsubscribes) unsubscribe();
    for (const control of state.foregroundControls.values()) control.interrupt?.();
    for (const timer of state.cleanupTimers.values()) clearTimeout(timer);
    state.cleanupTimers.clear();
    state.foregroundRuns.clear();
    state.foregroundControls.clear();
    if (state.poller) {
      clearInterval(state.poller);
      state.poller = null;
    }
    if (runtimeStore[runtimeCleanupKey] === cleanupRuntime) delete runtimeStore[runtimeCleanupKey];
  };
  runtimeStore[runtimeCleanupKey] = cleanupRuntime;

  const executor = createSubagentExecutor({
    pi,
    state,
    config,
    asyncByDefault: true,
    waitToolEnabled,
    tempArtifactsDir: getArtifactsDir(null),
    getSubagentSessionRoot,
    expandTilde,
    discoverAgents(cwd, scope) {
      const discovered = discoverAgents(cwd, scope);
      return {
        ...discovered,
        agents: discovered.agents.filter(isReadOnlyAgent).map((agent) => hardenAgent(agent, gitExtension, autoContinueExtension, hostCommandExtension, webAccessExtension)),
      };
    },
    allowMutatingManagementActions: false,
  });

  const tool: ToolDefinition<typeof SubagentParams, Details> = {
    name: "subagent",
    label: "Read-only investigators",
    description: [
      "Delegate repository investigation to configured read-only agents.",
      "Use single, parallel, or chain execution and choose the number of investigators according to the task.",
      "Children share the secretary's mechanically read-only project view; worktrees, writers, and mutating management actions are unavailable.",
      "Investigations run asynchronously by default and completion results are delivered back to this secretary session.",
      "Secretary investigations have no elapsed-time, assistant-turn, or tool-call budgets; let the investigator finish naturally and use completion notifications.",
      "List, doctor, and status are always read-only; interrupt, stop, resume, and steer require an explicit current-turn user request for a selected run.",
      "Use the agents' existing report formats and synthesize their returned findings for the user.",
    ].join("\n"),
    parameters: SubagentParams,
    execute(id, rawParams, signal, onUpdate, ctx) {
      const params = rawParams as SubagentParamsLike;
      if (requestsWorktree(params)) return Promise.resolve(rejected("Read-only secretary investigations never create Git worktrees."));
      if (params.async === false) return Promise.resolve(rejected("Secretary investigations are always asynchronous; return control to the user and rely on completion notifications."));
      if (params.action) {
        const action = params.action.toLowerCase() as SafeAction;
        if (!SAFE_ACTIONS.has(action)) {
          return Promise.resolve(rejected(`Secretary subagents do not allow action='${params.action}'.`));
        }
        const authorization = authorizedActions.get(action) ?? { ids: [], agents: [] };
        if (!READ_ONLY_ACTIONS.has(action) && !authorizedActions.has(action)) {
          return Promise.resolve(rejected(`Secretary subagent action '${action}' requires explicit current-turn user intent.`));
        }
        if (!actionTargetIsAuthorized(action, params, authorization, state)) {
          return Promise.resolve(rejected(`Secretary subagent action '${action}' must target the run selected by the current user turn.`));
        }
        authorizedActions.delete(action);
        if (action === "list") {
          const scope = params.agentScope === "user" || params.agentScope === "project" ? params.agentScope : "both";
          return Promise.resolve(listInvestigators(ctx.cwd, scope));
        }
      }
      if (params.chainDir || params.sessionDir || params.share === true || params.dir) {
        return Promise.resolve(rejected("Secretary investigations cannot select persistence, sharing, or arbitrary run destinations."));
      }
      const acceptance = { level: "none" as const, reason: "mechanically read-only secretary investigation" };
      const safeParams = {
        ...params,
        ...(params.action ? { action: params.action.toLowerCase() } : {}),
        // Ignore every caller/config-provided run, turn, and tool limit at the
        // secretary boundary. Stats record actual usage instead.
        timeoutMs: undefined,
        maxRuntimeMs: undefined,
        deadlineAt: undefined,
        absoluteDeadlineAt: undefined,
        turnBudget: undefined,
        toolBudget: undefined,
        async: true,
        clarify: false,
        artifacts: false,
        output: false,
        outputMode: undefined,
        acceptance,
        tasks: params.tasks?.map((task) => ({ ...stripTaskLimits(task), output: false, outputMode: undefined, acceptance })),
        chain: params.chain?.map((step) => ({
          ...stripTaskLimits(step),
          output: false,
          outputMode: undefined,
          ...(step.agent ? { acceptance } : {}),
          ...(Array.isArray(step.parallel) ? {
            parallel: step.parallel.map((task) => ({ ...stripTaskLimits(task), output: false, outputMode: undefined, acceptance })),
          } : step.parallel ? {
            parallel: { ...stripTaskLimits(step.parallel), output: false, outputMode: undefined, acceptance },
          } : {}),
          worktree: false,
        })),
      } as SubagentParamsLike;
      return executor.execute(id, safeParams, signal, onUpdate, ctx);
    },
    renderCall(args, theme) {
      const count = args.tasks?.reduce((total, task) => total + (task.count ?? 1), 0);
      const target = count ? `${count} investigators` : args.agent ?? args.action ?? "investigation";
      return new Text(`${theme.fg("toolTitle", theme.bold("subagent "))}${theme.fg("accent", target)}`, 0, 0);
    },
    renderResult(result, options, theme, context) {
      clearLegacyResultAnimationTimer(context);
      return renderSubagentResult(result, options, theme);
    },
  };

  pi.registerTool(tool);
  if (waitToolEnabled) registerWaitTool(pi, state, true);
  pi.on("tool_result", (event, ctx) => {
    if (event.toolName === "subagent" && ctx.hasUI) state.lastUiContext = ctx;
  });
  pi.on("message_end", (event) => {
    if (event.message.role !== "assistant") return;
    sessionTurns++;
    addSecretaryUsage(sessionUsage, secretaryUsage((event.message as { usage?: unknown }).usage));
  });
  pi.on("session_start", (_event, ctx) => {
    state.baseCwd = ctx.cwd;
    supervisorChannel.start();
    state.lastUiContext = ctx;
    state.currentSessionId = ctx.sessionManager.getSessionId() ?? null;
    sessionStartedAt = Date.now();
    sessionStatsId = state.currentSessionId;
    sessionTurns = 0;
    sessionUsage = emptySecretaryUsage();
    state.subagentSpawns = { sessionId: state.currentSessionId, count: 0, configuredLimit: null, granted: 0, grantHistory: [] };
    resetJobs(ctx);
    restoreActiveJobs(ctx);
    resultWatcher.primeExistingResults();
  });
  pi.on("session_shutdown", (event) => {
    if (sessionStartedAt !== undefined) {
      recordSecretarySessionStats({
        projectAlias,
        sessionId: sessionStatsId,
        startedAt: sessionStartedAt,
        endedAt: Date.now(),
        turns: sessionTurns,
        usage: sessionUsage,
        reason: event.reason,
      });
    }
    sessionStartedAt = undefined;
    sessionStatsId = null;
    cleanupRuntime();
  });
}
