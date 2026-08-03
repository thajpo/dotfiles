import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

export interface SecretaryUsageStats {
	inputTokens: number;
	outputTokens: number;
	cacheReadTokens: number;
	cacheWriteTokens: number;
	totalTokens: number;
	costUsd: number;
}

interface StatsStep {
	agent: string;
	status?: string;
	model?: string;
	thinking?: string;
	startedAt?: number;
	endedAt?: number;
	durationMs?: number;
	turns?: number;
	toolCalls?: number;
	error?: string;
	acceptanceLevel?: string;
	acceptanceStatus?: string;
	acceptanceFailedChecks?: string[];
	tokens: SecretaryUsageStats;
}

const STATS_SCHEMA_VERSION = 1;
const recordedRuns = new Set<string>();

function asRecord(value: unknown): Record<string, unknown> | undefined {
	return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

function finiteNonnegative(value: unknown): number | undefined {
	return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}

function stringValue(value: unknown, maxLength = 256): string | undefined {
	if (typeof value !== "string") return undefined;
	const trimmed = value.trim();
	return trimmed ? trimmed.slice(0, maxLength) : undefined;
}

function integerValue(value: unknown): number | undefined {
	const number = finiteNonnegative(value);
	return number === undefined ? undefined : Math.floor(number);
}

export function emptySecretaryUsage(): SecretaryUsageStats {
	return {
		inputTokens: 0,
		outputTokens: 0,
		cacheReadTokens: 0,
		cacheWriteTokens: 0,
		totalTokens: 0,
		costUsd: 0,
	};
}

export function secretaryUsage(value: unknown): SecretaryUsageStats {
	const raw = asRecord(value);
	if (!raw) return emptySecretaryUsage();
	const cost = asRecord(raw.cost);
	const inputTokens = finiteNonnegative(raw.inputTokens ?? raw.input) ?? 0;
	const outputTokens = finiteNonnegative(raw.outputTokens ?? raw.output) ?? 0;
	const cacheReadTokens = finiteNonnegative(raw.cacheReadTokens ?? raw.cacheRead) ?? 0;
	const cacheWriteTokens = finiteNonnegative(raw.cacheWriteTokens ?? raw.cacheWrite) ?? 0;
	const reportedTotal = finiteNonnegative(raw.totalTokens ?? raw.total);
	const costUsd = finiteNonnegative(raw.costUsd) ?? finiteNonnegative(cost?.total) ?? 0;
	return {
		inputTokens,
		outputTokens,
		cacheReadTokens,
		cacheWriteTokens,
		totalTokens: reportedTotal ?? inputTokens + outputTokens,
		costUsd,
	};
}

export function addSecretaryUsage(target: SecretaryUsageStats, value: unknown): void {
	const next = secretaryUsage(value);
	target.inputTokens += next.inputTokens;
	target.outputTokens += next.outputTokens;
	target.cacheReadTokens += next.cacheReadTokens;
	target.cacheWriteTokens += next.cacheWriteTokens;
	target.totalTokens += next.totalTokens;
	target.costUsd += next.costUsd;
}

function secretaryAgentDir(): string {
	const configured = process.env.PI_CODING_AGENT_DIR;
	if (configured === "~") return os.homedir();
	if (configured?.startsWith("~/")) return path.join(os.homedir(), configured.slice(2));
	return configured || path.join(os.homedir(), ".pi", "agent");
}

export function secretaryStatsPath(): string {
	return path.join(secretaryAgentDir(), "secretary-stats.jsonl");
}

function appendStats(entry: Record<string, unknown>): void {
	try {
		const statsPath = secretaryStatsPath();
		fs.mkdirSync(path.dirname(statsPath), { recursive: true, mode: 0o700 });
		fs.appendFileSync(statsPath, `${JSON.stringify(entry)}\n`, { encoding: "utf8", mode: 0o600 });
		fs.chmodSync(statsPath, 0o600);
	} catch (error) {
		// Statistics must never interfere with the secretary or its investigators.
		console.error(`Failed to write secretary statistics: ${error instanceof Error ? error.message : String(error)}`);
	}
}

function failureDetails(input: {
	state: string;
	timedOut: boolean;
	event: Record<string, unknown>;
	status: Record<string, unknown> | undefined;
	steps: StatsStep[];
}): Record<string, unknown> | undefined {
	if (input.state === "complete" && !input.timedOut) return undefined;
	const acceptanceStatuses = input.steps.map((step) => step.acceptanceStatus).filter(Boolean);
	const failedChecks = input.steps.flatMap((step) => step.acceptanceFailedChecks ?? []).slice(0, 8);
	const message = stringValue(input.status?.error, 512)
		?? stringValue(input.event.error, 512)
		?? input.steps.map((step) => step.error).find((value): value is string => Boolean(value));
	const kind = input.timedOut
		? "timeout"
		: acceptanceStatuses.includes("rejected")
			? "acceptance"
			: input.state === "stopped"
				? "stopped"
				: "run";
	return {
		kind,
		...(message ? { message } : {}),
		...(acceptanceStatuses.includes("rejected") ? { acceptanceStatus: "rejected" } : {}),
		...(failedChecks.length > 0 ? { acceptanceFailedChecks: failedChecks } : {}),
	};
}

function commonEntry(input: { projectAlias?: string; sessionId?: string | null }): Record<string, unknown> {
	return {
		schemaVersion: STATS_SCHEMA_VERSION,
		recordedAt: Date.now(),
		source: "secretary",
		...(stringValue(input.projectAlias, 128) ? { projectAlias: stringValue(input.projectAlias, 128) } : {}),
		...(stringValue(input.sessionId, 256) ? { sessionId: stringValue(input.sessionId, 256) } : {}),
	};
}

export function recordSecretarySessionStats(input: {
	projectAlias?: string;
	sessionId?: string | null;
	startedAt: number;
	endedAt: number;
	turns: number;
	usage: SecretaryUsageStats;
	reason: string;
}): void {
	const durationMs = Math.max(0, input.endedAt - input.startedAt);
	appendStats({
		...commonEntry(input),
		kind: "session",
		reason: stringValue(input.reason, 32) ?? "unknown",
		startedAt: input.startedAt,
		endedAt: input.endedAt,
		durationMs,
		turns: integerValue(input.turns) ?? 0,
		tokens: { ...input.usage },
	});
}

function readAsyncStatus(asyncDir: string | undefined): Record<string, unknown> | undefined {
	if (!asyncDir) return undefined;
	try {
		const statusPath = path.join(asyncDir, "status.json");
		return asRecord(JSON.parse(fs.readFileSync(statusPath, "utf8")));
	} catch {
		return undefined;
	}
}

function stepStats(value: unknown): StatsStep | undefined {
	const raw = asRecord(value);
	const agent = stringValue(raw?.agent, 128);
	if (!agent) return undefined;
	const startedAt = finiteNonnegative(raw?.startedAt);
	const endedAt = finiteNonnegative(raw?.endedAt);
	const durationMs = finiteNonnegative(raw?.durationMs);
	const acceptance = asRecord(raw?.acceptance);
	const effectiveAcceptance = asRecord(acceptance?.effectiveAcceptance);
	const failedChecks = Array.isArray(acceptance?.runtimeChecks)
		? acceptance.runtimeChecks
			.map((item) => asRecord(item))
			.filter((item) => item?.status === "failed")
			.map((item) => stringValue(item?.message, 512))
			.filter((item): item is string => Boolean(item))
			.slice(0, 8)
		: [];
	return {
		agent,
		...(stringValue(raw?.status, 32) ? { status: stringValue(raw?.status, 32) } : {}),
		...(stringValue(raw?.model, 256) ? { model: stringValue(raw?.model, 256) } : {}),
		...(stringValue(raw?.thinking, 32) ? { thinking: stringValue(raw?.thinking, 32) } : {}),
		...(startedAt !== undefined ? { startedAt } : {}),
		...(endedAt !== undefined ? { endedAt } : {}),
		...(durationMs !== undefined ? { durationMs } : {}),
		...(integerValue(raw?.turnCount) !== undefined ? { turns: integerValue(raw?.turnCount) } : {}),
		...(integerValue(raw?.toolCount) !== undefined ? { toolCalls: integerValue(raw?.toolCount) } : {}),
		...(stringValue(raw?.error, 512) ? { error: stringValue(raw?.error, 512) } : {}),
		...(stringValue(effectiveAcceptance?.level ?? acceptance?.level, 32) ? { acceptanceLevel: stringValue(effectiveAcceptance?.level ?? acceptance?.level, 32) } : {}),
		...(stringValue(acceptance?.status, 32) ? { acceptanceStatus: stringValue(acceptance?.status, 32) } : {}),
		...(failedChecks.length > 0 ? { acceptanceFailedChecks: failedChecks } : {}),
		tokens: secretaryUsage(raw?.tokens),
	};
}

export function recordSecretarySubagentStats(input: {
	data: unknown;
	projectAlias?: string;
}): void {
	const event = asRecord(input.data);
	if (!event) return;
	const status = readAsyncStatus(stringValue(event.asyncDir, 4096));
	const runId = stringValue(status?.runId ?? event.runId ?? event.id, 256);
	if (!runId || recordedRuns.has(runId)) return;
	recordedRuns.add(runId);

	const startedAt = finiteNonnegative(status?.startedAt) ?? finiteNonnegative(event.startedAt);
	const endedAt = finiteNonnegative(status?.endedAt) ?? finiteNonnegative(event.timestamp) ?? Date.now();
	const eventDuration = finiteNonnegative(event.durationMs);
	const effectiveStartedAt = startedAt ?? (eventDuration !== undefined ? Math.max(0, endedAt - eventDuration) : endedAt);
	const durationMs = finiteNonnegative(status?.endedAt !== undefined && startedAt !== undefined
		? endedAt - effectiveStartedAt
		: eventDuration) ?? Math.max(0, endedAt - effectiveStartedAt);
	const state = stringValue(status?.state ?? event.state, 32)
		?? (event.success === true ? "complete" : event.success === false ? "failed" : "unknown");
	const steps = Array.isArray(status?.steps)
		? status.steps.map(stepStats).filter((step): step is StatsStep => Boolean(step))
		: [];
	const usage = secretaryUsage(status?.totalTokens ?? event.totalTokens);
	const totalCost = asRecord(status?.totalCost ?? event.totalCost);
	if (totalCost) {
		usage.costUsd = finiteNonnegative(totalCost.costUsd) ?? usage.costUsd;
	}
	const failure = failureDetails({
		state,
		timedOut: status?.timedOut === true || event.timedOut === true,
		event,
		status,
		steps,
	});

	appendStats({
		...commonEntry({
			projectAlias: input.projectAlias,
			sessionId: stringValue(status?.sessionId ?? event.sessionId, 256),
		}),
		kind: "subagent_run",
		runId,
		mode: stringValue(status?.mode ?? event.mode, 32),
		state,
		startedAt: effectiveStartedAt,
		endedAt,
		durationMs,
		turns: integerValue(status?.turnCount) ?? 0,
		toolCalls: integerValue(status?.toolCount) ?? 0,
		timedOut: status?.timedOut === true || event.timedOut === true,
		tokens: usage,
		steps,
		...(failure ? { failure } : {}),
	});
}
