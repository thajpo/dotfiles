import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { CONFIG_DIR_NAME } from "@earendil-works/pi-coding-agent";
import { promises as fs } from "node:fs";
import os from "node:os";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import {
  ACTIVE_WORKER_STATUSES,
  activeWorkers,
  atomicWrite,
  chooseTaskId,
  chooseWorkerLabel,
  dispatchMarker,
  fileExists,
  latestWorker,
  loadState,
  newDispatchId,
  normalizeConfig,
  nowIso,
  parseNewArgs,
  parseSendArgs,
  readJson,
  reconcileWorkers,
  resolveInside,
  saveState,
  taskArtifactPaths,
  withLock,
  type RegistryAgent,
  type WorkTask,
  type WorkWorker,
  type WorkboardConfig,
  type WorkboardState,
  type WorkerRole,
} from "./core.ts";
import { ensureSideAgentSetup, type SetupResult } from "./setup.ts";

const STATUS_KEY = "workboard";
const MESSAGE_TYPE = "workboard-report";
const CONFIG_FILE = "workboard.json";
const STATE_FILE = "state.json";
const STATE_LOCK = "state.lock";

interface RegistryFile {
  version?: number;
  agents?: Record<string, RegistryAgent>;
}

interface CommandEnvironment {
  root: string;
  config: WorkboardConfig;
  statePath: string;
  lockPath: string;
  registryPath: string;
  runtimeRoot: string;
}

function stringifyError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function codingAgentDir(): string {
  const configured = process.env.PI_CODING_AGENT_DIR;
  if (!configured) return join(os.homedir(), ".pi", "agent");
  return resolve(configured.replace(/^~(?=$|\/)/, os.homedir()));
}

async function git(pi: ExtensionAPI, cwd: string, args: string[]): Promise<{ ok: boolean; stdout: string; stderr: string }> {
  const result = await pi.exec("git", ["-C", cwd, ...args], { timeout: 10_000 });
  return { ok: result.code === 0, stdout: result.stdout, stderr: result.stderr };
}

async function gitRoot(pi: ExtensionAPI, cwd: string): Promise<string | undefined> {
  const result = await git(pi, cwd, ["rev-parse", "--show-toplevel"]);
  const root = result.stdout.trim();
  return result.ok && root ? resolve(root) : undefined;
}

async function inferIntegrationBranch(pi: ExtensionAPI, root: string): Promise<string> {
  const remoteHead = await git(pi, root, ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"]);
  if (remoteHead.ok) {
    const branch = remoteHead.stdout.trim().replace(/^origin\//, "");
    if (branch && (await git(pi, root, ["show-ref", "--verify", "--quiet", `refs/heads/${branch}`])).ok) return branch;
  }
  for (const candidate of ["main", "master"]) {
    if ((await git(pi, root, ["show-ref", "--verify", "--quiet", `refs/heads/${candidate}`])).ok) return candidate;
  }
  const current = await git(pi, root, ["symbolic-ref", "--quiet", "--short", "HEAD"]);
  if (current.ok && current.stdout.trim()) return current.stdout.trim();
  throw new Error("Cannot infer the repository integration branch from origin/HEAD, main, master, or the current branch.");
}

function isWithin(root: string, candidate: string): boolean {
  const rel = relative(root, candidate);
  return rel === "" || (rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel));
}

async function checkedProjectPath(root: string, projectRelativePath: string): Promise<string> {
  const target = resolveInside(root, projectRelativePath);
  const realRoot = await fs.realpath(root);
  let existing = target;
  while (!(await fileExists(existing))) {
    const parent = dirname(existing);
    if (parent === existing) throw new Error(`Cannot resolve project path: ${projectRelativePath}`);
    existing = parent;
  }
  const realExisting = await fs.realpath(existing);
  if (!isWithin(realRoot, realExisting)) {
    throw new Error(`Project path resolves outside the repository through a symlink: ${projectRelativePath}`);
  }
  return target;
}

async function getEnvironment(pi: ExtensionAPI, ctx: ExtensionContext): Promise<CommandEnvironment> {
  if (!ctx.isProjectTrusted()) throw new Error("The current project is not trusted. Trust it before using /work.");
  const root = await gitRoot(pi, ctx.cwd);
  if (!root) throw new Error("/work requires a Git repository.");

  const configPath = join(codingAgentDir(), CONFIG_FILE);
  const config = normalizeConfig(await readJson<Partial<WorkboardConfig>>(configPath));
  const workboardDir = await checkedProjectPath(root, join(CONFIG_DIR_NAME, "workboard"));
  return {
    root,
    config,
    statePath: join(workboardDir, STATE_FILE),
    lockPath: join(workboardDir, STATE_LOCK),
    registryPath: join(root, CONFIG_DIR_NAME, "side-agents", "registry.json"),
    runtimeRoot: join(root, CONFIG_DIR_NAME, "side-agents", "runtime"),
  };
}

async function readRegistry(path: string): Promise<RegistryAgent[]> {
  const registry = await readJson<RegistryFile>(path);
  if (!registry?.agents || typeof registry.agents !== "object") return [];
  return Object.values(registry.agents).filter((agent) => agent && typeof agent.id === "string");
}

async function discoverAgentIdsFromRuntime(env: CommandEnvironment, state: WorkboardState): Promise<boolean> {
  let entries: string[] = [];
  try {
    entries = await fs.readdir(env.runtimeRoot);
  } catch (error: unknown) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    return false;
  }

  let changed = false;
  for (const task of Object.values(state.tasks)) {
    for (const worker of task.workers) {
      if (worker.agentId) continue;
      const marker = dispatchMarker(task.id, worker.dispatchId);
      for (const candidate of entries) {
        try {
          const kickoff = await fs.readFile(join(env.runtimeRoot, candidate, "kickoff.md"), "utf8");
          if (!kickoff.includes(marker)) continue;
          worker.agentId = candidate;
          worker.branch = worker.branch ?? `side-agent/${candidate}`;
          worker.updatedAt = nowIso();
          changed = true;
          break;
        } catch {
          // Runtime directories may disappear or be archived during reconciliation.
        }
      }
    }
  }
  return changed;
}

async function runtimeExitCodes(env: CommandEnvironment, state: WorkboardState): Promise<Record<string, number | undefined>> {
  const result: Record<string, number | undefined> = {};
  for (const task of Object.values(state.tasks)) {
    for (const worker of task.workers) {
      if (!worker.agentId || worker.agentId in result) continue;
      try {
        const marker = await readJson<{ exitCode?: number }>(join(env.runtimeRoot, worker.agentId, "exit.json"));
        result[worker.agentId] = typeof marker?.exitCode === "number" ? marker.exitCode : undefined;
      } catch {
        // The launcher writes this small marker directly; tolerate a concurrent partial read.
        result[worker.agentId] = undefined;
      }
    }
  }
  return result;
}

async function initializeSideAgents(pi: ExtensionAPI, env: CommandEnvironment): Promise<SetupResult> {
  const setupLock = join(dirname(env.statePath), "setup.lock");
  return withLock(setupLock, async () => {
    const mainBranch = await inferIntegrationBranch(pi, env.root);
    const referenceSkill = join(codingAgentDir(), "npm", "node_modules", "pi-side-agents", "skills", "agent-setup", "SKILL.md");
    return ensureSideAgentSetup(mainBranch, (path) => checkedProjectPath(env.root, path), referenceSkill);
  });
}

async function reconcile(env: CommandEnvironment): Promise<WorkboardState> {
  return withLock(env.lockPath, async () => {
    const state = await loadState(env.statePath);
    let changed = await discoverAgentIdsFromRuntime(env, state);
    const agents = await readRegistry(env.registryPath);
    const exits = await runtimeExitCodes(env, state);
    for (const task of Object.values(state.tasks)) {
      changed = reconcileWorkers(task, agents, exits) || changed;
    }
    if (changed) await saveState(env.statePath, state);
    return state;
  });
}

async function mutateState<T>(env: CommandEnvironment, mutator: (state: WorkboardState) => Promise<T> | T): Promise<{ state: WorkboardState; result: T }> {
  return withLock(env.lockPath, async () => {
    const state = await loadState(env.statePath);
    const result = await mutator(state);
    await saveState(env.statePath, state);
    return { state, result };
  });
}

function currentTask(state: WorkboardState): WorkTask {
  if (!state.activeTaskId) throw new Error("No active workboard task. Start one with /work new <description>.");
  const task = state.tasks[state.activeTaskId];
  if (!task) throw new Error(`Active task ${state.activeTaskId} is missing from workboard state.`);
  return task;
}

function updateStatus(ctx: ExtensionContext, state: WorkboardState): void {
  if (!ctx.hasUI) return;
  const task = state.activeTaskId ? state.tasks[state.activeTaskId] : undefined;
  if (!task || task.phase === "closed") {
    ctx.ui.setStatus(STATUS_KEY, undefined);
    return;
  }
  const active = activeWorkers(task).length;
  const suffix = active > 0 ? ` ${active}w` : "";
  ctx.ui.setStatus(STATUS_KEY, ctx.ui.theme.fg("muted", `work:${task.id}:${task.phase}${suffix}`));
}

function emit(pi: ExtensionAPI, ctx: ExtensionContext, title: string, lines: string[]): void {
  const content = [title, "", ...lines].join("\n");
  if (ctx.hasUI) {
    pi.sendMessage({ customType: MESSAGE_TYPE, content, display: true }, { triggerTurn: false });
  } else {
    console.log(content);
  }
}

async function gitObjectExists(pi: ExtensionAPI, root: string, spec: string): Promise<boolean> {
  const result = await git(pi, root, ["cat-file", "-e", spec]);
  return result.ok;
}

async function reportAvailable(pi: ExtensionAPI, env: CommandEnvironment, worker: WorkWorker): Promise<boolean> {
  if (!worker.expectedReport) return false;
  if (await fileExists(await checkedProjectPath(env.root, worker.expectedReport))) return true;
  if (!worker.branch) return false;
  return gitObjectExists(pi, env.root, `${worker.branch}:${worker.expectedReport}`);
}

function workerStatus(worker: WorkWorker): string {
  const window = worker.tmuxWindowIndex !== undefined ? ` window ${worker.tmuxWindowIndex}` : "";
  const id = worker.agentId ? ` [${worker.agentId}]` : "";
  return `${worker.label.padEnd(26)} ${worker.role.padEnd(9)} ${worker.status}${window}${id}`;
}

async function suggestedNext(pi: ExtensionAPI, env: CommandEnvironment, task: WorkTask): Promise<string> {
  if (task.openDecisions.length > 0) return `Resolve decision 1: ${task.openDecisions[0]}`;
  const scouts = task.workers.filter((worker) => worker.role === "scout");
  for (const scout of scouts) {
    if (scout.status === "waiting_user" || (await reportAvailable(pi, env, scout))) return `Review ${scout.label} result.`;
  }
  if (scouts.length === 0) return "Start a focused scout with /work scout <question>.";
  if (!(await fileExists(await checkedProjectPath(env.root, task.contractPath)))) return "Create the implementation contract with /work contract.";
  const implementation = latestWorker(task, "implement");
  if (!implementation) return "Start the bounded writer with /work implement.";
  if (implementation.status === "waiting_user") return "Inspect the implementation and run /work review.";
  if (implementation.status === "done") return "Run /work review against the worker branch.";
  if (implementation.status === "failed" || implementation.status === "crashed") return "Inspect the failed worker, then repair or start a replacement.";
  if (task.phase === "review") return "Act on the audit disposition; close only after human approval.";
  return `Wait for or steer ${implementation.label}.`;
}

async function renderDashboard(pi: ExtensionAPI, ctx: ExtensionContext, env: CommandEnvironment, state: WorkboardState): Promise<void> {
  if (!state.activeTaskId) {
    const open = Object.values(state.tasks).filter((task) => task.phase !== "closed");
    emit(pi, ctx, "workboard", open.length ? ["No active task.", `Open tasks: ${open.map((task) => task.id).join(", ")}`, "Use /work use <id>."] : ["No tasks yet.", "Use /work new <description>."]);
    return;
  }

  const task = currentTask(state);
  const scouts = task.workers.filter((worker) => worker.role === "scout");
  let availableReports = 0;
  for (const scout of scouts) if (await reportAvailable(pi, env, scout)) availableReports += 1;
  const contractExists = await fileExists(await checkedProjectPath(env.root, task.contractPath));
  const implementation = latestWorker(task, "implement");

  const lines = [
    `Current task: ${task.id} — ${task.description}`,
    "",
    `Phase: ${task.phase}`,
    "",
    "Workers:",
    ...(task.workers.length ? task.workers.map((worker) => `  ${workerStatus(worker)}`) : ["  (none)"]),
    "",
    "Artifacts:",
    `  intent              ${await fileExists(await checkedProjectPath(env.root, task.intentPath)) ? "complete" : "missing"}`,
    `  scout reports       ${availableReports} of ${scouts.length} available`,
    `  decisions           ${task.openDecisions.length ? `${task.openDecisions.length} open` : "complete"}`,
    `  contract            ${contractExists ? "complete" : "not created"}`,
    `  implementation      ${implementation ? implementation.status : "not started"}`,
  ];
  if (task.openDecisions.length) {
    lines.push("", "Open decisions:");
    task.openDecisions.forEach((decision, index) => lines.push(`  ${index + 1}. ${decision}`));
  }
  lines.push("", "Suggested next action:", `  ${await suggestedNext(pi, env, task)}`);
  emit(pi, ctx, "workboard", lines);
}

function queueUserMessage(pi: ExtensionAPI, ctx: ExtensionContext, message: string): void {
  if (ctx.isIdle()) pi.sendUserMessage(message);
  else pi.sendUserMessage(message, { deliverAs: "followUp" });
}

function ensureSideAgents(pi: ExtensionAPI): void {
  const tools = new Set(pi.getAllTools().map((tool) => tool.name));
  if (!tools.has("agent-start") || !tools.has("agent-send")) {
    throw new Error("pi-side-agents tools are unavailable. Reload Pi and verify the package is installed.");
  }
}

function agentStartInstruction(worker: WorkWorker, kickoff: string): string {
  return [
    "Call the agent-start tool exactly once with the following arguments, then report its returned id/window/branch and do no unrelated work:",
    JSON.stringify({ description: kickoff, branchHint: worker.label, model: worker.model, role: worker.role }, null, 2),
    "This is an asynchronous dispatch: do not wait for the child after agent-start returns.",
  ].join("\n");
}

function scoutKickoff(task: WorkTask, worker: WorkWorker): string {
  return [
    dispatchMarker(task.id, worker.dispatchId),
    "Role: read-only scout (artifact-only writing is permitted).",
    `Current task: ${task.id} — ${task.description}`,
    `Question: ${worker.description}`,
    `Expected report: ${worker.expectedReport}`,
    "",
    "Investigate only. Do not modify product source, tests, configuration, or dependencies.",
    "You may create/update only the expected report path, then commit that report on your side-agent branch.",
    "The report must contain concrete file/line evidence, findings, uncertainties, and recommendations.",
    "Do not merge. When the report is committed, summarize it and wait for the parent/user.",
  ].join("\n");
}

function implementationKickoff(task: WorkTask, worker: WorkWorker, contract: string): string {
  return [
    dispatchMarker(task.id, worker.dispatchId),
    "Role: bounded implementation worker.",
    `Current task: ${task.id} — ${task.description}`,
    `Contract source in parent: ${task.contractPath}`,
    "",
    "Implement only the approved contract embedded below. Honor allowed_paths, non-goals, compatibility rules, acceptance commands, and escalation triggers.",
    "If the contract is incomplete, conflicts with repository reality, or requires a consequential decision, stop and ask the parent/user rather than guessing.",
    "Run all required verification, commit completed work, and wait. Never merge unless the human sends the exact instruction 'LGTM, merge'.",
    "",
    "--- BEGIN APPROVED CONTRACT ---",
    contract.trimEnd(),
    "--- END APPROVED CONTRACT ---",
  ].join("\n");
}

async function createTaskArtifacts(env: CommandEnvironment, task: WorkTask): Promise<void> {
  const intent = await checkedProjectPath(env.root, task.intentPath);
  const decisions = await checkedProjectPath(env.root, task.decisionsPath);
  if (!(await fileExists(intent))) {
    await atomicWrite(intent, `# ${task.id}: ${task.description}\n\n## Intent\n\n${task.description}\n\n## Status\n\nArchitecture/discovery in progress.\n`);
  }
  if (!(await fileExists(decisions))) {
    await atomicWrite(decisions, `# ${task.id} decisions\n\nRecord consequential decisions and rationale here.\n`);
  }
}

async function handleNew(pi: ExtensionAPI, ctx: ExtensionContext, env: CommandEnvironment, raw: string): Promise<void> {
  const parsed = parseNewArgs(raw);
  if (!parsed.description) throw new Error("Usage: /work new [--id TASK-ID] <description>");
  const setup = await initializeSideAgents(pi, env);
  const { state, result: task } = await mutateState(env, async (state) => {
    const id = chooseTaskId(parsed.description, parsed.requestedId, new Set(Object.keys(state.tasks)));
    const timestamp = nowIso();
    const paths = taskArtifactPaths(env.config, id);
    const task: WorkTask = {
      id,
      description: parsed.description,
      phase: "architecture",
      ...paths,
      openDecisions: [],
      workers: [],
      createdAt: timestamp,
      updatedAt: timestamp,
    };
    await createTaskArtifacts(env, task);
    state.tasks[id] = task;
    state.activeTaskId = id;
    return task;
  });
  updateStatus(ctx, state);
  emit(pi, ctx, "workboard task created", [
    `${task.id} — ${task.description}`,
    `repository setup: ${setup.created.length ? `initialized ${setup.created.length} local file(s) for ${setup.mainBranch}` : `already ready for ${setup.mainBranch}`}`,
    `intent: ${task.intentPath}`,
    `decisions: ${task.decisionsPath}`,
    `contract: ${task.contractPath}`,
  ]);
}

async function addWorker(env: CommandEnvironment, role: WorkerRole, description: string, model: string): Promise<{ state: WorkboardState; task: WorkTask; worker: WorkWorker }> {
  const mutated = await mutateState(env, (state) => {
    const task = currentTask(state);
    if (task.phase === "closed") throw new Error("The active task is closed.");
    const timestamp = nowIso();
    const label = chooseWorkerLabel(role, description, task.workers);
    const worker: WorkWorker = {
      dispatchId: newDispatchId(),
      label,
      role,
      description,
      model,
      expectedReport: role === "scout" ? `${task.artifactDir}/reports/${label}.md` : undefined,
      status: "dispatching",
      startedAt: timestamp,
      updatedAt: timestamp,
    };
    task.workers.push(worker);
    task.updatedAt = timestamp;
    if (role === "implement") task.phase = "implementation";
    return { task, worker };
  });
  return { state: mutated.state, ...mutated.result };
}

async function handleScout(pi: ExtensionAPI, ctx: ExtensionContext, env: CommandEnvironment, question: string): Promise<void> {
  if (!question.trim()) throw new Error("Usage: /work scout <question>");
  ensureSideAgents(pi);
  const { state, task, worker } = await addWorker(env, "scout", question.trim(), env.config.scoutModel);
  updateStatus(ctx, state);
  queueUserMessage(pi, ctx, agentStartInstruction(worker, scoutKickoff(task, worker)));
  ctx.hasUI && ctx.ui.notify(`Dispatching ${worker.label} with ${worker.model}`, "info");
}

async function handleContract(pi: ExtensionAPI, ctx: ExtensionContext, env: CommandEnvironment): Promise<void> {
  const { state, result: task } = await mutateState(env, (state) => {
    const task = currentTask(state);
    if (task.openDecisions.length) throw new Error("Resolve open decisions before producing the implementation contract.");
    task.phase = "contract";
    task.updatedAt = nowIso();
    return task;
  });
  updateStatus(ctx, state);
  const scoutEvidence = task.workers
    .filter((worker) => worker.role === "scout")
    .map((worker) => `- ${worker.label}: branch=${worker.branch ?? worker.agentId ?? "dispatching"}; report=${worker.expectedReport ?? "none"}`);
  queueUserMessage(pi, ctx, [
    `Create an executable implementation contract for task ${task.id}: ${task.description}`,
    `Inspect the repository and available scout branches/reports first. Save the approved final YAML contract to ${task.contractPath}.`,
    ...(scoutEvidence.length ? ["Scout evidence (read branch-only reports with git show <branch>:<report>):", ...scoutEvidence] : []),
    "Include base commit, goal, user-visible behavior, decisions/rationale, interfaces/types/schema, compatibility and error semantics, allowed_paths, non-goals, acceptance commands with expected results, escalation triggers, and tests/skeletons to create before handoff.",
    "Resolve discoverable facts, stop for human approval at consequential choices, and do not implement the contract.",
  ].join("\n"));
}

async function handleImplement(pi: ExtensionAPI, ctx: ExtensionContext, env: CommandEnvironment): Promise<void> {
  ensureSideAgents(pi);
  const reconciled = await reconcile(env);
  const task = currentTask(reconciled);
  if (task.openDecisions.length) throw new Error("Resolve open decisions before implementation.");
  if (task.workers.some((worker) => worker.role === "implement" && ACTIVE_WORKER_STATUSES.has(worker.status))) {
    throw new Error("An implementation worker is already active for this task.");
  }
  await initializeSideAgents(pi, env);
  const contractPath = await checkedProjectPath(env.root, task.contractPath);
  let contract: string;
  try {
    const stat = await fs.stat(contractPath);
    if (stat.size > env.config.maxContractBytes) throw new Error(`Contract exceeds ${env.config.maxContractBytes} bytes.`);
    contract = await fs.readFile(contractPath, "utf8");
  } catch (error: unknown) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") throw new Error(`Approved contract not found: ${task.contractPath}. Run /work contract first.`);
    throw error;
  }
  if (!contract.trim()) throw new Error(`Contract is empty: ${task.contractPath}`);
  const added = await addWorker(env, "implement", task.description, env.config.implementModel);
  updateStatus(ctx, added.state);
  queueUserMessage(pi, ctx, agentStartInstruction(added.worker, implementationKickoff(added.task, added.worker, contract)));
  ctx.hasUI && ctx.ui.notify(`Dispatching ${added.worker.label} with ${added.worker.model}`, "info");
}

async function handleReview(pi: ExtensionAPI, ctx: ExtensionContext, env: CommandEnvironment): Promise<void> {
  const state = await reconcile(env);
  const task = currentTask(state);
  const worker = latestWorker(task, "implement");
  if (!worker) throw new Error("No implementation worker is recorded for the current task.");
  const branch = worker.branch ?? (worker.agentId ? `side-agent/${worker.agentId}` : undefined);
  if (!branch) throw new Error("The implementation worker has not been associated with a branch yet. Run /work again after dispatch completes.");
  if (!(await gitObjectExists(pi, env.root, branch))) throw new Error(`Worker branch does not exist: ${branch}`);
  const mergeBase = await git(pi, env.root, ["merge-base", "HEAD", branch]);
  if (!mergeBase.ok || !mergeBase.stdout.trim()) throw new Error(`Cannot resolve merge base for ${branch}: ${mergeBase.stderr.trim()}`);
  const range = `${mergeBase.stdout.trim()}..${branch}`;
  const mutated = await mutateState(env, (next) => {
    const current = currentTask(next);
    current.phase = "review";
    current.updatedAt = nowIso();
  });
  updateStatus(ctx, mutated.state);
  emit(pi, ctx, "workboard review resolved", [`task: ${task.id}`, `contract: ${task.contractPath}`, `worker: ${worker.agentId ?? worker.label}`, `branch: ${branch}`, `range: ${range}`]);
  queueUserMessage(pi, ctx, [
    `Audit Git range ${range} against contract ${task.contractPath}.`,
    "Inspect the actual diff, repository instructions, changed interfaces, and verification evidence—not worker summaries alone.",
    "Check correctness, scope, compatibility, security, concurrency/ownership, numerical behavior, and regression coverage as applicable.",
    "Return exactly one disposition as the first line: CERTIFY, REPAIR, or ESCALATE. Then list only concrete evidence and required actions.",
    "Do not merge, publish, or approve anything.",
  ].join("\n"));
}

async function handleSend(pi: ExtensionAPI, ctx: ExtensionContext, env: CommandEnvironment, raw: string): Promise<void> {
  ensureSideAgents(pi);
  const parsed = parseSendArgs(raw);
  if (!parsed.worker || !parsed.message) throw new Error("Usage: /work send <worker-label-or-id> <message>");
  const state = await reconcile(env);
  const task = currentTask(state);
  const worker = task.workers.find((candidate) => candidate.label === parsed.worker || candidate.agentId === parsed.worker);
  if (!worker) throw new Error(`Unknown worker for ${task.id}: ${parsed.worker}`);
  if (!worker.agentId) throw new Error(`${worker.label} has not received a side-agent id yet. Run /work after dispatch completes.`);
  queueUserMessage(pi, ctx, [
    "Use the agent-send tool exactly once.",
    `Agent id: ${worker.agentId}`,
    `Prompt (preserve exactly): ${JSON.stringify(parsed.message)}`,
    "Report the tool result and do no unrelated work.",
  ].join("\n"));
}

async function handleDecision(pi: ExtensionAPI, ctx: ExtensionContext, env: CommandEnvironment, raw: string): Promise<void> {
  const decision = raw.trim();
  if (!decision) throw new Error("Usage: /work decide <open decision>");
  const mutated = await mutateState(env, async (state) => {
    const task = currentTask(state);
    task.openDecisions.push(decision);
    task.updatedAt = nowIso();
    const decisionsPath = await checkedProjectPath(env.root, task.decisionsPath);
    await fs.appendFile(decisionsPath, `\n## Open: ${decision}\n\nPending human decision.\n`, "utf8");
    return task;
  });
  updateStatus(ctx, mutated.state);
  emit(pi, ctx, "workboard decision added", [`${mutated.result.id}: ${decision}`]);
}

async function handleResolve(pi: ExtensionAPI, ctx: ExtensionContext, env: CommandEnvironment, raw: string): Promise<void> {
  const match = raw.trim().match(/^(\d+)\s+([\s\S]+)$/);
  if (!match) throw new Error("Usage: /work resolve <decision-number> <resolution>");
  const index = Number(match[1]) - 1;
  const resolution = match[2].trim();
  const mutated = await mutateState(env, async (state) => {
    const task = currentTask(state);
    if (index < 0 || index >= task.openDecisions.length) throw new Error(`Decision number out of range: ${index + 1}`);
    const decision = task.openDecisions.splice(index, 1)[0];
    task.updatedAt = nowIso();
    await fs.appendFile(await checkedProjectPath(env.root, task.decisionsPath), `\n## Resolved: ${decision}\n\n${resolution}\n`, "utf8");
    return { task, decision };
  });
  updateStatus(ctx, mutated.state);
  emit(pi, ctx, "workboard decision resolved", [`Question: ${mutated.result.decision}`, `Resolution: ${resolution}`]);
}

async function handleUse(pi: ExtensionAPI, ctx: ExtensionContext, env: CommandEnvironment, raw: string): Promise<void> {
  const id = raw.trim().toUpperCase();
  if (!id) throw new Error("Usage: /work use <task-id>");
  const mutated = await mutateState(env, (state) => {
    const task = state.tasks[id];
    if (!task) throw new Error(`Unknown workboard task: ${id}`);
    if (task.phase === "closed") throw new Error(`Task ${id} is closed.`);
    state.activeTaskId = id;
    return task;
  });
  updateStatus(ctx, mutated.state);
  await renderDashboard(pi, ctx, env, mutated.state);
}

async function handleClose(pi: ExtensionAPI, ctx: ExtensionContext, env: CommandEnvironment): Promise<void> {
  const state = await reconcile(env);
  const task = currentTask(state);
  const active = activeWorkers(task);
  if (active.length) throw new Error(`Cannot close while workers are active: ${active.map((worker) => worker.label).join(", ")}. Steer/quit them first.`);
  if (task.openDecisions.length && (!ctx.hasUI || !(await ctx.ui.confirm("Close with open decisions?", `${task.openDecisions.length} decision(s) remain unresolved.`)))) return;
  const mutated = await mutateState(env, (next) => {
    const current = currentTask(next);
    current.phase = "closed";
    current.closedAt = nowIso();
    current.updatedAt = current.closedAt;
    next.activeTaskId = undefined;
    return current;
  });
  updateStatus(ctx, mutated.state);
  emit(pi, ctx, "workboard task closed", [`${mutated.result.id} — ${mutated.result.description}`, "No branches, worktrees, or runtime records were automatically deleted."]);
}

function helpLines(): string[] {
  return [
    "/work                                show current dashboard",
    "/work new [--id ID] <description>    create and activate a task",
    "/work scout <question>               dispatch configured scout model",
    "/work decide <question>              record an open human decision",
    "/work resolve <n> <resolution>        resolve and record a decision",
    "/work contract                        ask parent to create contract.yaml",
    "/work implement                       dispatch contract-gated writer",
    "/work review                          resolve branch/range and run /audit",
    "/work send <worker> <message>         route through agent-send",
    "/work use <task-id>                   switch active task",
    "/work close                           close if no workers are active",
  ];
}

export default function workboardExtension(pi: ExtensionAPI) {
  pi.registerCommand("work", {
    description: "Repo-local task dashboard over pi-side-agents: /work [new|scout|contract|implement|review|send|close]",
    getArgumentCompletions(prefix) {
      const options = ["new", "scout", "decide", "resolve", "contract", "implement", "review", "send", "use", "close", "help"];
      const token = prefix.trim();
      if (token.includes(" ")) return null;
      const items = options.filter((option) => option.startsWith(token)).map((option) => ({ value: option, label: option }));
      return items.length ? items : null;
    },
    handler: async (rawArgs, ctx) => {
      try {
        const env = await getEnvironment(pi, ctx);
        const trimmed = rawArgs.trim();
        const match = trimmed.match(/^(\S+)(?:\s+([\s\S]*))?$/);
        const action = match?.[1] ?? "status";
        const args = match?.[2] ?? "";

        switch (action) {
          case "status":
            break;
          case "new":
            await handleNew(pi, ctx, env, args);
            return;
          case "scout":
            await handleScout(pi, ctx, env, args);
            return;
          case "decide":
            await handleDecision(pi, ctx, env, args);
            return;
          case "resolve":
            await handleResolve(pi, ctx, env, args);
            return;
          case "contract":
            await handleContract(pi, ctx, env);
            return;
          case "implement":
            await handleImplement(pi, ctx, env);
            return;
          case "review":
            await handleReview(pi, ctx, env);
            return;
          case "send":
            await handleSend(pi, ctx, env, args);
            return;
          case "use":
            await handleUse(pi, ctx, env, args);
            return;
          case "close":
            await handleClose(pi, ctx, env);
            return;
          case "help":
            emit(pi, ctx, "workboard commands", helpLines());
            return;
          default:
            throw new Error(`Unknown /work action: ${action}. Use /work help.`);
        }

        const state = await reconcile(env);
        updateStatus(ctx, state);
        await renderDashboard(pi, ctx, env, state);
      } catch (error) {
        const message = stringifyError(error);
        if (ctx.hasUI) ctx.ui.notify(message, "error");
        else console.error(`workboard: ${message}`);
      }
    },
  });

  pi.on("session_start", async (_event, ctx) => {
    try {
      if (!ctx.isProjectTrusted()) return;
      const env = await getEnvironment(pi, ctx);
      const state = await loadState(env.statePath);
      updateStatus(ctx, state);
    } catch {
      // Workboard is inert outside trusted Git repositories.
    }
  });
}
