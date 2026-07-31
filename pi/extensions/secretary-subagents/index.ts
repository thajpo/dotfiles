import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";
import { discoverAgents } from "../../npm/node_modules/pi-subagents/src/agents/agents.ts";
import { loadConfig } from "../../npm/node_modules/pi-subagents/src/extension/config.ts";
import { SubagentParams } from "../../npm/node_modules/pi-subagents/src/extension/schemas.ts";
import { createSubagentExecutor, type SubagentParamsLike } from "../../npm/node_modules/pi-subagents/src/runs/foreground/subagent-executor.ts";
import { getArtifactsDir } from "../../npm/node_modules/pi-subagents/src/shared/artifacts.ts";
import { clearLegacyResultAnimationTimer, renderSubagentResult } from "../../npm/node_modules/pi-subagents/src/tui/render.ts";
import type { Details, SubagentState } from "../../npm/node_modules/pi-subagents/src/shared/types.ts";

const SAFE_ACTIONS = new Set(["list", "doctor"]);
const INVESTIGATOR_TOOLS = ["read", "grep", "find", "ls", "secretary_git", "contact_supervisor"];

function getSubagentSessionRoot(parentSessionFile: string | null): string {
  if (parentSessionFile) {
    const baseName = path.basename(parentSessionFile, ".jsonl");
    return path.join(path.dirname(parentSessionFile), baseName);
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

function hardenAgent<T extends { tools?: string[] }>(agent: T, gitExtension: string): T {
  const tools = INVESTIGATOR_TOOLS.filter((tool) => tool !== "contact_supervisor" || agent.tools?.includes(tool));
  return {
    ...agent,
    tools,
    mcpDirectTools: [],
    extensions: [],
    subagentOnlyExtensions: [gitExtension],
    inheritProjectContext: false,
    inheritSkills: false,
    output: undefined,
    defaultReads: undefined,
    defaultProgress: false,
    memory: undefined,
    interactive: false,
    completionGuard: false,
  } as T;
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

export default function secretarySubagents(pi: ExtensionAPI): void {
  // This file lives in the global extension tree for transactional installation,
  // but activates only in the explicitly constrained secretary launcher.
  if (process.env.PI_SECRETARY_READ_ONLY !== "1") return;
  const loaded = loadConfig();
  const agentDir = process.env.PI_CODING_AGENT_DIR ?? path.join(os.homedir(), ".pi", "agent");
  const gitExtension = path.join(agentDir, "extensions", "secretary-investigator-git", "index.ts");
  if (!fs.existsSync(gitExtension)) throw new Error("secretary investigator Git extension is unavailable");
  // Investigator count is intentionally policy-free. Runtime concurrency is a
  // scheduler, not a workflow prescription, and remains caller-selectable.
  const config = {
    ...loaded,
    asyncByDefault: false,
    maxSubagentSpawnsPerSession: 0,
    globalConcurrencyLimit: undefined,
    parallel: {
      ...(loaded.parallel ?? {}),
      maxTasks: Number.MAX_SAFE_INTEGER,
      concurrency: undefined,
    },
  };
  const state = createState();
  const executor = createSubagentExecutor({
    pi,
    state,
    config,
    asyncByDefault: false,
    waitToolEnabled: false,
    tempArtifactsDir: getArtifactsDir(null),
    getSubagentSessionRoot,
    expandTilde,
    discoverAgents(cwd, scope) {
      const discovered = discoverAgents(cwd, scope);
      return {
        ...discovered,
        agents: discovered.agents.filter(isReadOnlyAgent).map((agent) => hardenAgent(agent, gitExtension)),
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
      "Children share the secretary's mechanically read-only project view; worktrees, writers, background runs, and mutating management actions are unavailable.",
      "Use the agents' existing report formats and synthesize their returned findings for the user.",
    ].join("\n"),
    parameters: SubagentParams,
    execute(id, rawParams, signal, onUpdate, ctx) {
      const params = rawParams as SubagentParamsLike;
      if (requestsWorktree(params)) return Promise.resolve(rejected("Read-only secretary investigations never create Git worktrees."));
      if (params.async === true) return Promise.resolve(rejected("Secretary investigations run in the foreground so their findings return to the current discussion."));
      if (params.action && !SAFE_ACTIONS.has(params.action)) {
        return Promise.resolve(rejected(`Secretary subagents do not allow action='${params.action}'.`));
      }
      if (params.action === "list") {
        const scope = params.agentScope === "user" || params.agentScope === "project" ? params.agentScope : "both";
        return Promise.resolve(listInvestigators(ctx.cwd, scope));
      }
      if (params.chainDir || params.sessionDir || params.share === true) {
        return Promise.resolve(rejected("Secretary investigations cannot select persistence or sharing destinations."));
      }
      const hasExplicitOutput = params.output !== undefined && params.output !== false ||
        params.tasks?.some((task) => task.output !== undefined && task.output !== false) ||
        params.chain?.some((step) => {
          if ("output" in step && step.output !== undefined && step.output !== false) return true;
          if ("parallel" in step && Array.isArray(step.parallel)) {
            return step.parallel.some((task) => task.output !== undefined && task.output !== false);
          }
          if ("parallel" in step && step.parallel && !Array.isArray(step.parallel)) {
            return step.parallel.output !== undefined && step.parallel.output !== false;
          }
          return false;
        });
      if (hasExplicitOutput) return Promise.resolve(rejected("Secretary investigation results return inline and cannot write output files."));
      const acceptance = { level: "none" as const, reason: "mechanically read-only secretary investigation" };
      const safeParams = {
        ...params,
        async: false,
        artifacts: false,
        output: false,
        outputMode: undefined,
        acceptance,
        tasks: params.tasks?.map((task) => ({ ...task, output: false, outputMode: undefined, acceptance })),
        chain: params.chain?.map((step) => ({
          ...step,
          ...(step.agent ? { output: false, outputMode: undefined, acceptance } : {}),
          ...(Array.isArray(step.parallel) ? {
            parallel: step.parallel.map((task) => ({ ...task, output: false, outputMode: undefined, acceptance })),
          } : step.parallel ? {
            parallel: { ...step.parallel, output: false, outputMode: undefined, acceptance },
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
  pi.on("session_start", (_event, ctx) => {
    state.baseCwd = ctx.cwd;
    state.currentSessionId = ctx.sessionManager.getSessionId() ?? null;
    state.subagentSpawns = { sessionId: state.currentSessionId, count: 0, configuredLimit: null, granted: 0, grantHistory: [] };
  });
  pi.on("session_shutdown", () => {
    for (const control of state.foregroundControls.values()) control.interrupt?.();
    for (const timer of state.cleanupTimers.values()) clearTimeout(timer);
    state.cleanupTimers.clear();
    state.foregroundRuns.clear();
    state.foregroundControls.clear();
  });
}
