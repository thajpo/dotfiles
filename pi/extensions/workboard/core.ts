import { randomUUID } from "node:crypto";
import { promises as fs } from "node:fs";
import { dirname, isAbsolute, join, normalize, relative, resolve } from "node:path";

export const WORKBOARD_VERSION = 1 as const;
export const ACTIVE_WORKER_STATUSES = new Set([
  "dispatching",
  "allocating_worktree",
  "spawning_tmux",
  "running",
  "waiting_user",
]);

export type WorkPhase = "architecture" | "contract" | "implementation" | "review" | "closed";
export type WorkerRole = "scout" | "implement";

export interface WorkboardConfig {
  version: 1;
  artifactRoot: string;
  scoutModel: string;
  implementModel: string;
  maxContractBytes: number;
}

export interface WorkWorker {
  dispatchId: string;
  label: string;
  role: WorkerRole;
  description: string;
  model: string;
  expectedReport?: string;
  agentId?: string;
  branch?: string;
  worktreePath?: string;
  tmuxWindowIndex?: number;
  status: string;
  startedAt: string;
  updatedAt: string;
  finishedAt?: string;
  error?: string;
}

export interface WorkTask {
  id: string;
  description: string;
  phase: WorkPhase;
  artifactDir: string;
  intentPath: string;
  decisionsPath: string;
  contractPath: string;
  openDecisions: string[];
  workers: WorkWorker[];
  createdAt: string;
  updatedAt: string;
  closedAt?: string;
}

export interface WorkboardState {
  version: 1;
  activeTaskId?: string;
  tasks: Record<string, WorkTask>;
}

export interface RegistryAgent {
  id: string;
  task?: string;
  status?: string;
  branch?: string;
  worktreePath?: string;
  tmuxWindowIndex?: number;
  error?: string;
  finishedAt?: string;
}

export const DEFAULT_CONFIG: WorkboardConfig = {
  version: 1,
  artifactRoot: ".agent/tasks",
  scoutModel: "deepseek/deepseek-v4-flash:high",
  implementModel: "deepseek/deepseek-v4-flash:high",
  maxContractBytes: 64 * 1024,
};

export function nowIso(): string {
  return new Date().toISOString();
}

export function emptyState(): WorkboardState {
  return { version: WORKBOARD_VERSION, tasks: {} };
}

export function sanitizeTaskId(raw: string): string {
  const id = raw.trim().toUpperCase();
  if (!/^[A-Z0-9][A-Z0-9._-]{1,63}$/.test(id)) {
    throw new Error("Task id must be 2-64 characters using letters, numbers, '.', '_', or '-'.");
  }
  return id;
}

export function slugWords(raw: string, maxWords = 4): string {
  const stop = new Set(["a", "an", "the", "to", "in", "on", "at", "of", "for", "and", "or", "is", "it", "be", "with"]);
  const words = raw
    .normalize("NFKD")
    .replace(/[^a-zA-Z0-9\s-]/g, " ")
    .split(/[\s-]+/)
    .map((word) => word.toLowerCase())
    .filter((word) => word && !stop.has(word));
  return words.slice(0, maxWords).join("-") || "task";
}

export function chooseTaskId(description: string, requested: string | undefined, existing: Set<string>): string {
  let base: string;
  if (requested) {
    base = sanitizeTaskId(requested);
  } else {
    const issue = description.trim().match(/^([A-Za-z][A-Za-z0-9]+-\d+)\b/);
    base = issue ? sanitizeTaskId(issue[1]) : `WORK-${slugWords(description).toUpperCase()}`;
  }
  if (!existing.has(base)) return base;
  for (let index = 2; ; index += 1) {
    const candidate = `${base}-${index}`;
    if (!existing.has(candidate)) return candidate;
  }
}

export function chooseWorkerLabel(role: WorkerRole, description: string, workers: WorkWorker[]): string {
  const stem = slugWords(description, 3);
  const base = `${role}-${stem}`;
  const existing = new Set(workers.map((worker) => worker.label));
  if (!existing.has(base)) return base;
  for (let index = 2; ; index += 1) {
    const candidate = `${base}-${index}`;
    if (!existing.has(candidate)) return candidate;
  }
}

export function newDispatchId(): string {
  return randomUUID();
}

export function assertSafeRelativePath(path: string, label = "path"): string {
  const cleaned = path.trim().replaceAll("\\", "/");
  if (!cleaned || isAbsolute(cleaned) || cleaned.includes("\0")) {
    throw new Error(`${label} must be a non-empty project-relative path.`);
  }
  const normalized = normalize(cleaned).replaceAll("\\", "/");
  if (normalized === ".." || normalized.startsWith("../")) {
    throw new Error(`${label} must stay inside the project.`);
  }
  return normalized.replace(/^\.\//, "");
}

export function resolveInside(root: string, projectRelativePath: string): string {
  const safe = assertSafeRelativePath(projectRelativePath);
  const target = resolve(root, safe);
  const rel = relative(resolve(root), target);
  if (rel === ".." || rel.startsWith(`..${process.platform === "win32" ? "\\" : "/"}`) || isAbsolute(rel)) {
    throw new Error(`Path escapes project root: ${projectRelativePath}`);
  }
  return target;
}

export function parseNewArgs(raw: string): { requestedId?: string; description: string } {
  let rest = raw.trim();
  let requestedId: string | undefined;
  const match = rest.match(/^--id\s+(\S+)\s*/);
  if (match) {
    requestedId = match[1];
    rest = rest.slice(match[0].length);
  }
  return { requestedId, description: rest.trim() };
}

export function parseSendArgs(raw: string): { worker: string; message: string } {
  const match = raw.trim().match(/^(\S+)\s+([\s\S]+)$/);
  if (!match) return { worker: "", message: "" };
  return { worker: match[1], message: match[2].trim() };
}

export function dispatchMarker(taskId: string, dispatchId: string): string {
  return `[workboard task=${taskId} dispatch=${dispatchId}]`;
}

export function reconcileWorkers(task: WorkTask, agents: RegistryAgent[], runtimeExitCodes: Record<string, number | undefined> = {}): boolean {
  let changed = false;
  const timestamp = nowIso();

  for (const worker of task.workers) {
    let workerChanged = false;
    const marker = dispatchMarker(task.id, worker.dispatchId);
    const agent = agents.find((candidate) => candidate.id === worker.agentId || candidate.task?.includes(marker));
    if (agent) {
      const patch: Partial<WorkWorker> = {
        agentId: agent.id,
        branch: agent.branch,
        worktreePath: agent.worktreePath,
        tmuxWindowIndex: agent.tmuxWindowIndex,
        status: agent.status ?? worker.status,
        error: agent.error,
        finishedAt: agent.finishedAt,
      };
      for (const [key, value] of Object.entries(patch)) {
        if (value !== undefined && (worker as unknown as Record<string, unknown>)[key] !== value) {
          (worker as unknown as Record<string, unknown>)[key] = value;
          workerChanged = true;
        }
      }
    } else if (worker.agentId) {
      const exitCode = runtimeExitCodes[worker.agentId];
      if (exitCode !== undefined) {
        const nextStatus = exitCode === 0 ? "done" : "failed";
        if (worker.status !== nextStatus) {
          worker.status = nextStatus;
          worker.finishedAt = worker.finishedAt ?? timestamp;
          workerChanged = true;
        }
      }
    }
    if (workerChanged) {
      worker.updatedAt = timestamp;
      changed = true;
    }
  }

  if (changed) task.updatedAt = timestamp;
  return changed;
}

export function activeWorkers(task: WorkTask): WorkWorker[] {
  return task.workers.filter((worker) => ACTIVE_WORKER_STATUSES.has(worker.status));
}

export function latestWorker(task: WorkTask, role: WorkerRole): WorkWorker | undefined {
  return [...task.workers].reverse().find((worker) => worker.role === role);
}

export function taskArtifactPaths(config: WorkboardConfig, taskId: string): Pick<WorkTask, "artifactDir" | "intentPath" | "decisionsPath" | "contractPath"> {
  const root = assertSafeRelativePath(config.artifactRoot, "artifactRoot");
  const artifactDir = join(root, taskId).replaceAll("\\", "/");
  return {
    artifactDir,
    intentPath: `${artifactDir}/intent.md`,
    decisionsPath: `${artifactDir}/decisions.md`,
    contractPath: `${artifactDir}/contract.yaml`,
  };
}

export async function readJson<T>(path: string): Promise<T | undefined> {
  try {
    return JSON.parse(await fs.readFile(path, "utf8")) as T;
  } catch (error: unknown) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw new Error(`Cannot read ${path}: ${error instanceof Error ? error.message : String(error)}`);
  }
}

export async function atomicWrite(path: string, content: string, mode = 0o644): Promise<void> {
  await fs.mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.tmp.${process.pid}.${randomUUID().slice(0, 8)}`;
  await fs.writeFile(temporary, content, { encoding: "utf8", mode });
  await fs.rename(temporary, path);
}

export async function fileExists(path: string): Promise<boolean> {
  try {
    await fs.access(path);
    return true;
  } catch {
    return false;
  }
}

export async function loadState(path: string): Promise<WorkboardState> {
  const state = await readJson<WorkboardState>(path);
  if (!state) return emptyState();
  if (state.version !== WORKBOARD_VERSION || !state.tasks || typeof state.tasks !== "object") {
    throw new Error(`Unsupported or malformed workboard state: ${path}`);
  }
  return state;
}

export async function withLock<T>(lockPath: string, operation: () => Promise<T>): Promise<T> {
  await fs.mkdir(dirname(lockPath), { recursive: true });
  const started = Date.now();
  while (true) {
    try {
      const handle = await fs.open(lockPath, "wx", 0o600);
      try {
        await handle.writeFile(`${JSON.stringify({ pid: process.pid, createdAt: nowIso() })}\n`);
        return await operation();
      } finally {
        await handle.close().catch(() => undefined);
        await fs.unlink(lockPath).catch(() => undefined);
      }
    } catch (error: unknown) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code !== "EEXIST") throw error;
      try {
        const stat = await fs.stat(lockPath);
        if (Date.now() - stat.mtimeMs > 30_000) {
          await fs.unlink(lockPath).catch(() => undefined);
          continue;
        }
      } catch {
        continue;
      }
      if (Date.now() - started > 5_000) throw new Error(`Timed out waiting for workboard lock: ${lockPath}`);
      await new Promise((resolveWait) => setTimeout(resolveWait, 40 + Math.random() * 60));
    }
  }
}

export async function saveState(path: string, state: WorkboardState): Promise<void> {
  await atomicWrite(path, `${JSON.stringify(state, null, 2)}\n`);
}

export function normalizeConfig(input: Partial<WorkboardConfig> | undefined): WorkboardConfig {
  const merged = { ...DEFAULT_CONFIG, ...(input ?? {}) };
  if (merged.version !== WORKBOARD_VERSION) throw new Error(`Unsupported workboard config version: ${merged.version}`);
  merged.artifactRoot = assertSafeRelativePath(merged.artifactRoot, "artifactRoot");
  if (!merged.scoutModel.trim() || !merged.implementModel.trim()) throw new Error("Workboard role models cannot be empty.");
  if (!Number.isInteger(merged.maxContractBytes) || merged.maxContractBytes < 1024 || merged.maxContractBytes > 1024 * 1024) {
    throw new Error("maxContractBytes must be an integer between 1024 and 1048576.");
  }
  return merged;
}
