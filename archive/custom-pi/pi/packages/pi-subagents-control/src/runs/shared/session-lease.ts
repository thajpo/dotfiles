import { spawnSync } from "node:child_process";
import { randomUUID, createHash } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { createAtomicJsonWriter, writePrivateAtomicJson } from "../../shared/atomic-json.ts";
import { TEMP_ROOT_DIR } from "../../shared/types.ts";

export const SESSION_LEASES_DIR = path.join(TEMP_ROOT_DIR, "session-leases");

export interface SessionLeaseRequest {
	sessionFile: string;
	runId: string;
	sourceRunId: string;
	parentSessionId?: string;
}

export interface SessionLeaseOwner {
	version: 1;
	token: string;
	canonicalSessionFile: string;
	runId: string;
	sourceRunId: string;
	parentSessionId?: string;
	pid: number;
	hostname: string;
	processStartIdentity?: string;
	writerState: "none" | "spawning" | "running";
	writerPid?: number;
	writerProcessStartIdentity?: string;
	acquiredAt: string;
	acquiredAtMs: number;
	updatedAtMs: number;
}

export interface SessionLeaseHandle {
	leaseDir: string;
	owner: SessionLeaseOwner;
	updateWriter(writer: { state: "none" | "spawning" } | { state: "running"; pid: number }): void;
	release(): void;
}

export const SANDBOX_CHILD_LEASE_DIR = "subagent-sandbox-leases";
export const SANDBOX_PARENT_TRANSITION_FILE = "parent-transition.json";

function sandboxLeaseRoot(canonicalRoute: string): string {
	const routeKey = createHash("sha256").update(canonicalRoute).digest("hex");
	return path.join(path.dirname(canonicalRoute), SANDBOX_CHILD_LEASE_DIR, routeKey);
}

export interface SandboxParentTransitionOwner {
	version: 1;
	token: string;
	routePath: string;
	operation: string;
	pid: number;
	hostname: string;
	processStartIdentity?: string;
	acquiredAt: string;
	acquiredAtMs: number;
}

export interface SandboxChildLeaseRequest {
	runId: string;
	sessionId?: string;
	source: "async" | "foreground";
	routePath?: string;
}

export interface SandboxChildLeaseOwner {
	version: 1;
	token: string;
	routePath: string;
	runId: string;
	sessionId?: string;
	source: "async" | "foreground";
	pid: number;
	hostname: string;
	processStartIdentity?: string;
	acquiredAt: string;
	acquiredAtMs: number;
}

export interface SandboxChildLeaseHandle {
	leaseDir: string;
	owner: SandboxChildLeaseOwner;
	release(): void;
}

interface SessionLeaseOptions {
	rootDir?: string;
	now?: () => number;
	token?: () => string;
	pid?: number;
	hostname?: string;
	processStartIdentity?: string;
	isProcessAlive?: (pid: number) => boolean | undefined;
	getProcessStartIdentity?: (pid: number) => string | undefined;
}

export class SessionLeaseConflictError extends Error {
	readonly owner?: SessionLeaseOwner;

	constructor(message: string, owner?: SessionLeaseOwner) {
		super(message);
		this.name = "SessionLeaseConflictError";
		this.owner = owner;
	}
}

function getProcessStartIdentity(pid: number): string | undefined {
	if (process.platform === "linux") {
		try {
			const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf-8");
			const commandEnd = stat.lastIndexOf(")");
			if (commandEnd === -1) return undefined;
			const fields = stat.slice(commandEnd + 1).trim().split(/\s+/);
			const startTicks = fields.length > 19 && /^\d+$/.test(fields[19]) ? fields[19] : undefined;
			return startTicks ? `linux:${startTicks}` : undefined;
		} catch {
			return undefined;
		}
	}
	if (process.platform === "darwin" || process.platform === "freebsd") {
		const result = spawnSync("/bin/ps", ["-o", "lstart=", "-p", String(pid)], { encoding: "utf-8" });
		const started = result.status === 0 ? result.stdout.trim() : "";
		return started ? `${process.platform}:${started}` : undefined;
	}
	return undefined;
}

function processIsAlive(pid: number): boolean | undefined {
	try {
		process.kill(pid, 0);
		return true;
	} catch (error) {
		const code = (error as NodeJS.ErrnoException).code;
		if (code === "ESRCH") return false;
		if (code === "EPERM") return true;
		return undefined;
	}
}

export function canonicalSessionFilePath(sessionFile: string): string {
	return fs.realpathSync.native(path.resolve(sessionFile));
}

export function sessionLeaseDir(sessionFile: string, rootDir = SESSION_LEASES_DIR): string {
	const canonical = canonicalSessionFilePath(sessionFile);
	const key = process.platform === "win32" ? canonical.toLowerCase() : canonical;
	const digest = createHash("sha256").update(key).digest("hex");
	return path.join(rootDir, digest);
}

function parseOwner(value: unknown): SessionLeaseOwner | undefined {
	if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
	const owner = value as Partial<SessionLeaseOwner>;
	if (owner.version !== 1
		|| typeof owner.token !== "string"
		|| typeof owner.canonicalSessionFile !== "string"
		|| typeof owner.runId !== "string"
		|| typeof owner.sourceRunId !== "string"
		|| typeof owner.pid !== "number"
		|| !Number.isInteger(owner.pid)
		|| owner.pid <= 0
		|| typeof owner.hostname !== "string"
		|| (owner.writerState !== "none" && owner.writerState !== "spawning" && owner.writerState !== "running")
		|| typeof owner.acquiredAt !== "string"
		|| typeof owner.acquiredAtMs !== "number"
		|| typeof owner.updatedAtMs !== "number") return undefined;
	if (owner.parentSessionId !== undefined && typeof owner.parentSessionId !== "string") return undefined;
	if (owner.processStartIdentity !== undefined && typeof owner.processStartIdentity !== "string") return undefined;
	if (owner.writerPid !== undefined && (typeof owner.writerPid !== "number" || !Number.isInteger(owner.writerPid) || owner.writerPid <= 0)) return undefined;
	if (owner.writerProcessStartIdentity !== undefined && typeof owner.writerProcessStartIdentity !== "string") return undefined;
	if (owner.writerState === "running" && owner.writerPid === undefined) return undefined;
	if (owner.writerState !== "running" && (owner.writerPid !== undefined || owner.writerProcessStartIdentity !== undefined)) return undefined;
	return owner as SessionLeaseOwner;
}

function readLeaseOwner(leaseDir: string): SessionLeaseOwner | undefined {
	try {
		return parseOwner(JSON.parse(fs.readFileSync(path.join(leaseDir, "owner.json"), "utf-8")));
	} catch {
		return undefined;
	}
}

function hasDirectoryReclaimMarker(directory: string): boolean {
	try {
		return fs.readdirSync(directory).some((entry) => entry.startsWith(".reclaim-"));
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
		throw error;
	}
}

function tryReclaimDirectory(directory: string, expectedToken: string, readToken: (directory: string) => string | undefined): boolean {
	const marker = path.join(directory, `.reclaim-${expectedToken.replace(/[^A-Za-z0-9._-]/g, "-")}`);
	try {
		fs.writeFileSync(marker, expectedToken, { encoding: "utf-8", mode: 0o600, flag: "wx" });
	} catch (error) {
		const code = (error as NodeJS.ErrnoException).code;
		if (code === "EEXIST" || code === "ENOENT") return false;
		throw error;
	}
	let moved = false;
	try {
		if (readToken(directory) !== expectedToken) return false;
		const tombstone = `${directory}.stale-${expectedToken.replace(/[^A-Za-z0-9._-]/g, "-")}-${randomUUID()}`;
		fs.renameSync(directory, tombstone);
		moved = true;
		return true;
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
		throw error;
	} finally {
		if (!moved) fs.rmSync(marker, { force: true });
	}
}

function readSandboxChildLeaseOwner(leaseDir: string): SandboxChildLeaseOwner | undefined {
	try {
		const value = JSON.parse(fs.readFileSync(path.join(leaseDir, "owner.json"), "utf-8")) as Partial<SandboxChildLeaseOwner>;
		if (value.version !== 1
			|| typeof value.token !== "string"
			|| typeof value.routePath !== "string"
			|| typeof value.runId !== "string"
			|| (value.source !== "async" && value.source !== "foreground")
			|| typeof value.pid !== "number"
			|| !Number.isInteger(value.pid)
			|| value.pid <= 0
			|| typeof value.hostname !== "string"
			|| typeof value.acquiredAt !== "string"
			|| typeof value.acquiredAtMs !== "number"
			|| (value.sessionId !== undefined && typeof value.sessionId !== "string")
			|| (value.processStartIdentity !== undefined && typeof value.processStartIdentity !== "string")) return undefined;
		return value as SandboxChildLeaseOwner;
	} catch {
		return undefined;
	}
}

function sandboxProcessOwnerIsStale(owner: Pick<SandboxChildLeaseOwner, "pid" | "hostname" | "processStartIdentity">, hostname: string, getIdentity: (pid: number) => string | undefined): boolean {
	if (owner.hostname !== hostname) return false;
	const alive = processIsAlive(owner.pid);
	if (alive === false) return true;
	if (alive !== true || !owner.processStartIdentity) return false;
	const currentIdentity = getIdentity(owner.pid);
	return currentIdentity !== undefined && currentIdentity !== owner.processStartIdentity;
}

function sandboxChildLeaseIsStale(owner: SandboxChildLeaseOwner, hostname: string, getIdentity: (pid: number) => string | undefined): boolean {
	return sandboxProcessOwnerIsStale(owner, hostname, getIdentity);
}

function readSandboxParentTransition(transitionPath: string): SandboxParentTransitionOwner | undefined {
	try {
		const value = JSON.parse(fs.readFileSync(transitionPath, "utf-8")) as Partial<SandboxParentTransitionOwner>;
		if (value.version !== 1
			|| typeof value.token !== "string"
			|| typeof value.routePath !== "string"
			|| !path.isAbsolute(value.routePath)
			|| typeof value.operation !== "string"
			|| value.operation.length === 0
			|| value.operation.length > 128
			|| typeof value.pid !== "number"
			|| !Number.isInteger(value.pid)
			|| value.pid <= 0
			|| typeof value.hostname !== "string"
			|| typeof value.acquiredAt !== "string"
			|| typeof value.acquiredAtMs !== "number"
			|| !Number.isFinite(value.acquiredAtMs)
			|| (value.processStartIdentity !== undefined && typeof value.processStartIdentity !== "string")) return undefined;
		return value as SandboxParentTransitionOwner;
	} catch {
		return undefined;
	}
}

function ensureNoSandboxParentTransition(rootDir: string, hostname: string, getIdentity: (pid: number) => string | undefined): void {
	const transitionPath = path.join(rootDir, SANDBOX_PARENT_TRANSITION_FILE);
	let info: fs.Stats;
	try {
		info = fs.lstatSync(transitionPath);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
		throw error;
	}
	if (info.isSymbolicLink() || !info.isFile() || (info.mode & 0o077) !== 0) {
		throw new Error(`Sandbox parent transition metadata is unsafe: ${transitionPath}`);
	}
	const owner = readSandboxParentTransition(transitionPath);
	if (!owner) throw new Error(`Sandbox parent transition metadata is invalid: ${transitionPath}`);
	if (sandboxProcessOwnerIsStale(owner, hostname, getIdentity)) {
		const marker = `${transitionPath}.reclaim-${owner.token.replace(/[^A-Za-z0-9._-]/g, "-")}`;
		try {
			fs.writeFileSync(marker, owner.token, { encoding: "utf-8", mode: 0o600, flag: "wx" });
		} catch (error) {
			if ((error as NodeJS.ErrnoException).code === "EEXIST") throw new Error(`Sandbox parent transition reclamation is already in progress: ${transitionPath}`);
			throw error;
		}
		let moved = false;
		try {
			const current = readSandboxParentTransition(transitionPath);
			if (current?.token !== owner.token) return;
			const tombstone = `${transitionPath}.stale-${owner.token.replace(/[^A-Za-z0-9._-]/g, "-")}-${randomUUID()}`;
			fs.renameSync(transitionPath, tombstone);
			moved = true;
		} catch (error) {
			if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
		} finally {
			if (!moved) fs.rmSync(marker, { force: true });
		}
		return;
	}
	throw new Error(`Sandbox parent is moving the task ref (${owner.operation}); child start is blocked until it finishes`);
}

function conflictMessage(canonicalSessionFile: string, owner: SessionLeaseOwner | undefined): string {
	if (!owner) {
		return `Direct revival of session '${canonicalSessionFile}' is blocked by an existing lease with unreadable owner metadata. Refusing to reclaim it without proof that the owner is stale.`;
	}
	const parent = owner.parentSessionId ? `, parent session '${owner.parentSessionId}'` : "";
	return `Direct revival of session '${canonicalSessionFile}' is already owned by run '${owner.runId}' (source run '${owner.sourceRunId}'${parent}, pid ${owner.pid} on ${owner.hostname}). Wait for that revival to finish or start a separate continuation without reusing this session file.`;
}

function processDemonstrablyGone(
	pid: number,
	startIdentity: string | undefined,
	options: Required<Pick<SessionLeaseOptions, "isProcessAlive" | "getProcessStartIdentity">>,
): boolean {
	const alive = options.isProcessAlive(pid);
	if (alive === false) return true;
	if (alive !== true || !startIdentity) return false;
	const currentIdentity = options.getProcessStartIdentity(pid);
	return currentIdentity !== undefined && currentIdentity !== startIdentity;
}

function demonstrablyStale(owner: SessionLeaseOwner, options: Required<Pick<SessionLeaseOptions, "hostname" | "isProcessAlive" | "getProcessStartIdentity">>): boolean {
	if (owner.hostname !== options.hostname) return false;
	if (!processDemonstrablyGone(owner.pid, owner.processStartIdentity, options)) return false;
	if (owner.writerState === "spawning") return false;
	if (owner.writerState === "none") return true;
	return owner.writerPid !== undefined
		&& processDemonstrablyGone(owner.writerPid, owner.writerProcessStartIdentity, options);
}

function createLeaseDirectory(leaseDir: string, owner: SessionLeaseOwner): boolean {
	const tempDir = `${leaseDir}.candidate-${owner.token}`;
	fs.mkdirSync(path.dirname(leaseDir), { recursive: true, mode: 0o700 });
	fs.rmSync(tempDir, { recursive: true, force: true });
	fs.mkdirSync(tempDir, { mode: 0o700 });
	try {
		fs.writeFileSync(path.join(tempDir, "owner.json"), JSON.stringify(owner, null, 2), { encoding: "utf-8", mode: 0o600 });
		try {
			fs.renameSync(tempDir, leaseDir);
			return true;
		} catch (error) {
			if (fs.existsSync(leaseDir)) return false;
			throw error;
		}
	} finally {
		fs.rmSync(tempDir, { recursive: true, force: true });
	}
}

export function acquireSandboxChildLease(request: SandboxChildLeaseRequest): SandboxChildLeaseHandle | undefined {
	if (process.env.PI_SUBAGENT_CHILD === "1") return undefined;
	const configuredRoute = request.routePath ?? process.env.PI_TASK_ROUTE_FILE;
	if (!configuredRoute) return undefined;
	if (!request.runId || request.runId.length > 512) throw new Error("Sandbox child lease run id is invalid");
	const routePath = fs.realpathSync.native(path.resolve(configuredRoute));
	const rootDir = sandboxLeaseRoot(routePath);
	let rootInfo: fs.Stats;
	try {
		rootInfo = fs.lstatSync(rootDir);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
		fs.mkdirSync(rootDir, { recursive: true, mode: 0o700 });
		rootInfo = fs.lstatSync(rootDir);
	}
	if (!rootInfo.isSymbolicLink() && rootInfo.isDirectory()) {
		fs.chmodSync(rootDir, 0o700);
		rootInfo = fs.lstatSync(rootDir);
	}
	const uid = process.getuid?.();
	if (rootInfo.isSymbolicLink() || !rootInfo.isDirectory() || (uid !== undefined && rootInfo.uid !== uid) || (rootInfo.mode & 0o077) !== 0) {
		throw new Error(`Sandbox child lease root is not a private user-owned directory: ${rootDir}`);
	}
	const hostname = os.hostname();
	const getIdentity = getProcessStartIdentity;
	ensureNoSandboxParentTransition(rootDir, hostname, getIdentity);
	const token = randomUUID();
	const leaseKey = createHash("sha256").update(`${routePath}\0${request.source}\0${request.runId}`).digest("hex");
	const leaseDir = path.join(rootDir, leaseKey);
	const now = Date.now();
	const owner: SandboxChildLeaseOwner = {
		version: 1,
		token,
		routePath,
		runId: request.runId,
		...(request.sessionId ? { sessionId: request.sessionId } : {}),
		source: request.source,
		pid: process.pid,
		hostname,
		...(getIdentity(process.pid) ? { processStartIdentity: getIdentity(process.pid) } : {}),
		acquiredAt: new Date(now).toISOString(),
		acquiredAtMs: now,
	};
	for (let attempt = 0; attempt < 2; attempt++) {
		try {
			fs.mkdirSync(leaseDir, { mode: 0o700 });
			writePrivateAtomicJson(path.join(leaseDir, "owner.json"), owner);
			try {
				ensureNoSandboxParentTransition(rootDir, hostname, getIdentity);
			} catch (error) {
				tryReclaimDirectory(leaseDir, owner.token, (directory) => readSandboxChildLeaseOwner(directory)?.token);
				throw error;
			}
			return {
				leaseDir,
				owner,
				release() {
					const current = readSandboxChildLeaseOwner(leaseDir);
					if (!current || current.token !== owner.token || hasDirectoryReclaimMarker(leaseDir)) return;
					fs.rmSync(leaseDir, { recursive: true, force: true });
				},
			};
		} catch (error) {
			if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
			const existing = readSandboxChildLeaseOwner(leaseDir);
			if (!existing || !sandboxChildLeaseIsStale(existing, owner.hostname, getIdentity)) {
				throw new Error(`Sandbox child lease is already held for run '${request.runId}'`);
			}
			if (!tryReclaimDirectory(leaseDir, existing.token, (directory) => readSandboxChildLeaseOwner(directory)?.token)) continue;
		}
	}
	throw new Error(`Could not acquire sandbox child lease for run '${request.runId}'`);
}

export function acquireSessionLease(request: SessionLeaseRequest, options: SessionLeaseOptions = {}): SessionLeaseHandle {
	const canonicalSessionFile = canonicalSessionFilePath(request.sessionFile);
	const rootDir = options.rootDir ?? SESSION_LEASES_DIR;
	const leaseDir = sessionLeaseDir(canonicalSessionFile, rootDir);
	const now = options.now ?? Date.now;
	const pid = options.pid ?? process.pid;
	const hostname = options.hostname ?? os.hostname();
	const getIdentity = options.getProcessStartIdentity ?? getProcessStartIdentity;
	const processStartIdentity = options.processStartIdentity
		?? getIdentity(pid)
		?? (pid === process.pid ? `runtime:${Math.round(Date.now() - process.uptime() * 1000)}` : undefined);
	const acquiredAtMs = now();
	const owner: SessionLeaseOwner = {
		version: 1,
		token: options.token?.() ?? randomUUID(),
		canonicalSessionFile,
		runId: request.runId,
		sourceRunId: request.sourceRunId,
		...(request.parentSessionId ? { parentSessionId: request.parentSessionId } : {}),
		pid,
		hostname,
		...(processStartIdentity ? { processStartIdentity } : {}),
		writerState: "none",
		acquiredAt: new Date(acquiredAtMs).toISOString(),
		acquiredAtMs,
		updatedAtMs: acquiredAtMs,
	};
	const staleOptions = {
		hostname,
		isProcessAlive: options.isProcessAlive ?? processIsAlive,
		getProcessStartIdentity: getIdentity,
	};

	for (let attempt = 0; attempt < 4; attempt++) {
		if (createLeaseDirectory(leaseDir, owner)) {
			const writeOwner = createAtomicJsonWriter();
			return {
				leaseDir,
				owner,
				updateWriter(writer) {
					const currentOwner = readLeaseOwner(leaseDir);
					if (!currentOwner || currentOwner.token !== owner.token) {
						throw new Error(`Session revival lease ownership changed for run '${owner.runId}'.`);
					}
					const writerProcessStartIdentity = writer.state === "running" ? getIdentity(writer.pid) : undefined;
					const nextOwner: SessionLeaseOwner = {
						...owner,
						writerState: writer.state,
						...(writer.state === "running" ? { writerPid: writer.pid } : {}),
						...(writerProcessStartIdentity ? { writerProcessStartIdentity } : {}),
						updatedAtMs: now(),
					};
					delete nextOwner.writerPid;
					delete nextOwner.writerProcessStartIdentity;
					if (writer.state === "running") {
						nextOwner.writerPid = writer.pid;
						if (writerProcessStartIdentity) nextOwner.writerProcessStartIdentity = writerProcessStartIdentity;
					}
					writeOwner(path.join(leaseDir, "owner.json"), nextOwner);
					delete owner.writerPid;
					delete owner.writerProcessStartIdentity;
					Object.assign(owner, nextOwner);
				},
				release() {
					const currentOwner = readLeaseOwner(leaseDir);
					if (!currentOwner || currentOwner.token !== owner.token || hasDirectoryReclaimMarker(leaseDir)) return;
					fs.rmSync(leaseDir, { recursive: true, force: true });
				},
			};
		}

		const existingOwner = readLeaseOwner(leaseDir);
		if (!existingOwner || !demonstrablyStale(existingOwner, staleOptions)) {
			throw new SessionLeaseConflictError(conflictMessage(canonicalSessionFile, existingOwner), existingOwner);
		}
		// A deterministic token marker serializes stale reclamation before the
		// directory is renamed, so a successor lease cannot be moved by a loser.
		if (!tryReclaimDirectory(leaseDir, existingOwner.token, (directory) => readLeaseOwner(directory)?.token)) continue;
	}

	const existingOwner = readLeaseOwner(leaseDir);
	throw new SessionLeaseConflictError(conflictMessage(canonicalSessionFile, existingOwner), existingOwner);
}
