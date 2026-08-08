import { createHash, randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { AgentToolResult } from "@earendil-works/pi-agent-core";
import type { ExtensionAPI, ExtensionContext, ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import {
	SUBAGENT_CHILD_AGENT_ENV,
	SUBAGENT_CHILD_INDEX_ENV,
	SUBAGENT_ORCHESTRATOR_SESSION_ID_ENV,
	SUBAGENT_ORCHESTRATOR_TARGET_ENV,
	SUBAGENT_RUN_ID_ENV,
	SUBAGENT_SUPERVISOR_CHANNEL_DIR_ENV,
} from "../runs/shared/pi-args.ts";
import { INTERCOM_DETACH_REQUEST_EVENT, POLL_INTERVAL_MS, TEMP_ROOT_DIR, type IntercomEventBus, type SubagentState } from "../shared/types.ts";
import { writeAtomicJson, writePrivateAtomicJson } from "../shared/atomic-json.ts";

const SUPERVISOR_CHANNEL_ROOT = path.join(TEMP_ROOT_DIR, "supervisor-channels");
const REQUESTS_DIR = "requests";
const REPLIES_DIR = "replies";
export const NATIVE_SUPERVISOR_TOOL_NAME = "subagent_supervisor";
const MAX_MESSAGE_BYTES = 64 * 1024;
const DEFAULT_ASK_TIMEOUT_MS = 10 * 60 * 1000;
const CHANNEL_POLL_MS = Math.min(POLL_INTERVAL_MS, 500);
const STALE_EMPTY_CHANNEL_AGE_MS = 60 * 1000;
const STALE_EMPTY_CHANNEL_CLEANUP_INTERVAL_MS = 60 * 1000;
const FEEDBACK_MAX_TEXT_BYTES = 4096;
const FEEDBACK_MAX_RAW_BYTES = 16 * 1024;
const FEEDBACK_OUTCOMES = ["unreviewed", "replied", "accepted", "rejected", "deferred"] as const;
type FeedbackOutcome = typeof FEEDBACK_OUTCOMES[number];
type FeedbackLifecycle = "submitted" | "delivered" | "awaiting_reply" | "replied" | "reviewed" | "expired" | "inactive";

type SupervisorReason = "need_decision" | "interview_request" | "progress_update";

interface SupervisorRequest {
	type: "subagent.supervisor.request";
	id: string;
	createdAt: number;
	expiresAt?: number;
	reason: SupervisorReason;
	message: string;
	expectsReply: boolean;
	orchestratorTarget?: string;
	orchestratorSessionId?: string;
	runId: string;
	agent: string;
	childIndex: number;
	childTarget?: string;
	interview?: unknown;
}

interface PendingSupervisorRequest extends SupervisorRequest {
	channelDir: string;
	requestFile: string;
}

interface SupervisorReply {
	type: "subagent.supervisor.reply";
	requestId: string;
	createdAt: number;
	message: string;
	outcome?: FeedbackOutcome;
}

interface ContactSupervisorParams {
	reason: SupervisorReason;
	message?: string;
	interview?: unknown;
}

interface IntercomParams {
	action: "list" | "send" | "ask" | "reply" | "pending" | "status" | "review";
	to?: string;
	message?: string;
	replyTo?: string;
	feedbackId?: string;
	outcome?: FeedbackOutcome;
}

interface FeedbackRecord {
	schemaVersion: 1;
	feedbackId: string;
	createdAt: string;
	updatedAt: string;
	source: {
		agent: string;
		runId: string;
		childIndex: number;
		orchestratorSessionId: string;
		orchestratorTarget?: string;
		childTarget?: string;
		projectId?: string;
		workstreamId?: string;
		repository?: string;
	};
	reason: SupervisorReason;
	form: Record<string, unknown>;
	contentDigest: string;
	lifecycle: FeedbackLifecycle;
	outcome: FeedbackOutcome;
	response?: { message?: string; outcome: FeedbackOutcome; updatedAt: string };
	raw?: { message: string; interview?: unknown };
}

const ContactSupervisorParamsSchema = Type.Object({
	reason: Type.String({ enum: ["need_decision", "interview_request", "progress_update"] }),
	message: Type.Optional(Type.String()),
	interview: Type.Optional(Type.Unsafe({ type: "object", additionalProperties: true })),
}, { additionalProperties: false });

const IntercomParamsSchema = Type.Object({
	action: Type.String({ enum: ["list", "send", "ask", "reply", "pending", "status", "review"] }),
	to: Type.Optional(Type.String()),
	message: Type.Optional(Type.String()),
	replyTo: Type.Optional(Type.String()),
	feedbackId: Type.Optional(Type.String({ maxLength: 128 })),
	outcome: Type.Optional(Type.String({ enum: ["replied", "accepted", "rejected", "deferred"] })),
}, { additionalProperties: false });

function safeSegment(value: string): string {
	return value.trim().replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "unknown";
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function truncateUtf8(value: string, maxBytes: number): string {
	if (Buffer.byteLength(value, "utf8") <= maxBytes) return value;
	const suffix = maxBytes >= Buffer.byteLength("…", "utf8") ? "…" : "";
	const budget = Math.max(0, maxBytes - Buffer.byteLength(suffix, "utf8"));
	const characters = Array.from(value);
	let low = 0;
	let high = characters.length;
	while (low < high) {
		const middle = Math.ceil((low + high) / 2);
		if (Buffer.byteLength(characters.slice(0, middle).join(""), "utf8") <= budget) low = middle;
		else high = middle - 1;
	}
	return `${characters.slice(0, low).join("")}${suffix}`;
}

function boundedText(value: unknown, maxBytes = FEEDBACK_MAX_TEXT_BYTES): string | undefined {
	if (typeof value !== "string") return undefined;
	const text = value.trim()
		.replace(/\r\n?/g, "\n")
		.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F\u202A-\u202E\u2066-\u2069]/g, "");
	if (!text) return undefined;
	return truncateUtf8(text, maxBytes);
}

function boundedUnknown(value: unknown, maxBytes = FEEDBACK_MAX_RAW_BYTES): unknown {
	try {
		const serialized = JSON.stringify(value);
		if (serialized === undefined || Buffer.byteLength(serialized, "utf8") <= maxBytes) return value;
		return { truncated: true, contentDigest: createHash("sha256").update(serialized).digest("hex") };
	} catch {
		return { unavailable: true };
	}
}

function parseProgressFeedback(message: string): Record<string, unknown> | undefined {
	const marker = message.match(/AGENT_FEEDBACK\s*:?\s*([\s\S]*)/i)?.[1]?.trim();
	const candidates = [message.trim(), ...(marker ? [marker] : [])];
	for (const rawCandidate of candidates) {
		const candidate = rawCandidate.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i)?.[1]?.trim() ?? rawCandidate;
		try {
			const parsed = JSON.parse(candidate) as unknown;
			if (isRecord(parsed) && isRecord(parsed.AGENT_FEEDBACK)) return parsed.AGENT_FEEDBACK;
			if (isRecord(parsed)) return parsed;
		} catch {
			// Try the next candidate; malformed feedback is still recorded as a bounded summary.
		}
	}
	return undefined;
}

function normalizeFeedbackForm(reason: SupervisorReason, message: string, interview: unknown): Record<string, unknown> {
	const candidate = isRecord(interview) ? interview : reason === "progress_update" ? parseProgressFeedback(message) : undefined;
	const form: Record<string, unknown> = {
		schema: boundedText(candidate?.schema, 128) ?? (reason === "progress_update" ? "AGENT_FEEDBACK" : "agent-feedback.v1"),
	};
	for (const key of ["kind", "title", "want", "blocked_by", "why", "recommendation"] as const) {
		const value = boundedText(candidate?.[key]);
		if (value !== undefined) form[key] = value;
	}
	if (Array.isArray(candidate?.evidence)) {
		form.evidence = candidate.evidence.slice(0, 32).map((item) => boundedText(item, 1000)).filter((item): item is string => item !== undefined);
	}
	if (Array.isArray(candidate?.options)) {
		form.options = candidate.options.slice(0, 16).map((option) => {
			if (!isRecord(option)) return { value: boundedText(option, 1000) ?? "" };
			const normalized: Record<string, string> = {};
			for (const [key, value] of Object.entries(option).slice(0, 8)) {
				const text = boundedText(value, 1000);
				if (text !== undefined) normalized[key] = text;
			}
			return normalized;
		});
	}
	if (typeof candidate?.decision_needed === "boolean") form.decision_needed = candidate.decision_needed;
	if (candidate === undefined) {
		const summary = boundedText(message);
		if (summary !== undefined) form.summary = summary;
	}
	return form;
}

function feedbackStoreRoot(): string {
	const configured = process.env.PI_CODING_AGENT_DIR?.trim();
	const agentDir = configured && path.isAbsolute(configured) ? configured : path.join(os.homedir(), ".pi", "agent");
	return path.join(agentDir, "feedback", "records");
}

function feedbackRecordPath(feedbackId: string): string {
	if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(feedbackId)) throw new Error("invalid feedback id");
	return path.join(feedbackStoreRoot(), `${safeSegment(feedbackId)}.json`);
}

function ensureFeedbackStore(): void {
	const root = feedbackStoreRoot();
	fs.mkdirSync(root, { recursive: true, mode: 0o700 });
	try { fs.chmodSync(path.dirname(root), 0o700); } catch { /* best effort */ }
	try { fs.chmodSync(root, 0o700); } catch { /* best effort */ }
}

function feedbackProjectFields(): { projectId?: string; workstreamId?: string; repository?: string } {
	const projectId = process.env.PI_HARNESS_PROJECT_ID?.trim() || process.env.PI_SECRETARY_PROJECT_ID?.trim() || process.env.PI_WORKSTREAM_PROJECT_ID?.trim();
	const workstreamId = process.env.PI_WORKSTREAM_ID?.trim();
	const repository = process.env.PI_HARNESS_REPOSITORY?.trim();
	return {
		...(projectId && /^[0-9a-f]{64}$/.test(projectId) ? { projectId } : {}),
		...(workstreamId && /^[a-z0-9][a-z0-9-]{0,62}$/.test(workstreamId) ? { workstreamId } : {}),
		...(repository && repository.startsWith("/") ? { repository: repository.slice(0, 512) } : {}),
	};
}

function persistFeedbackRequest(request: SupervisorRequest, message: string, interview: unknown): void {
	ensureFeedbackStore();
	const now = new Date().toISOString();
	const input = JSON.stringify({ message, interview });
	const source = {
		agent: request.agent,
		runId: request.runId,
		childIndex: request.childIndex,
		orchestratorSessionId: request.orchestratorSessionId ?? "unknown",
		...(request.orchestratorTarget ? { orchestratorTarget: request.orchestratorTarget } : {}),
		...(request.childTarget ? { childTarget: request.childTarget } : {}),
		...feedbackProjectFields(),
	};
	const record: FeedbackRecord = {
		schemaVersion: 1,
		feedbackId: request.id,
		createdAt: now,
		updatedAt: now,
		source,
		reason: request.reason,
		form: normalizeFeedbackForm(request.reason, message, interview),
		contentDigest: createHash("sha256").update(input).digest("hex"),
		lifecycle: request.expectsReply ? "awaiting_reply" : "submitted",
		outcome: "unreviewed",
	};
	if (process.env.PI_AGENT_FEEDBACK_RAW === "1") {
		record.raw = { message: boundedText(message, FEEDBACK_MAX_RAW_BYTES) ?? "", ...(interview !== undefined ? { interview: boundedUnknown(interview) } : {}) };
	}
	writePrivateAtomicJson(feedbackRecordPath(request.id), record);
}

function readFeedbackRecord(feedbackId: string): FeedbackRecord | undefined {
	try {
		const parsed = JSON.parse(fs.readFileSync(feedbackRecordPath(feedbackId), "utf8")) as FeedbackRecord;
		return parsed?.schemaVersion === 1 && parsed.feedbackId === feedbackId ? parsed : undefined;
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
		throw error;
	}
}

function updateFeedbackRecord(feedbackId: string, update: (record: FeedbackRecord) => FeedbackRecord, required = false): void {
	const current = readFeedbackRecord(feedbackId);
	if (!current) {
		if (required) throw new Error(`feedback record not found: ${feedbackId}`);
		return;
	}
	ensureFeedbackStore();
	const next = { ...update(current), updatedAt: new Date().toISOString() };
	writePrivateAtomicJson(feedbackRecordPath(feedbackId), next);
}

function safeUpdateFeedbackRecord(feedbackId: string, update: (record: FeedbackRecord) => FeedbackRecord): void {
	try {
		updateFeedbackRecord(feedbackId, update);
	} catch (error) {
		console.error(`pi-subagents: could not update feedback record ${feedbackId}:`, error);
	}
}

function reviewFeedback(feedbackId: string, outcome: FeedbackOutcome, message?: string): void {
	if (outcome === "unreviewed" || outcome === "replied") throw new Error("feedback review requires accepted, rejected, or deferred outcome");
	updateFeedbackRecord(feedbackId, (record) => ({
		...record,
		lifecycle: "reviewed",
		outcome,
		...(message?.trim() ? { response: { message: boundedText(message, FEEDBACK_MAX_TEXT_BYTES), outcome, updatedAt: new Date().toISOString() } } : {}),
	}), true);
}

export function resolveSupervisorChannelDir(runId: string, agent: string, childIndex: number): string {
	return path.join(SUPERVISOR_CHANNEL_ROOT, `${safeSegment(runId)}-${safeSegment(agent)}-${childIndex}`);
}

export function ensureSupervisorChannelDir(channelDir: string): void {
	fs.mkdirSync(path.join(channelDir, REQUESTS_DIR), { recursive: true, mode: 0o700 });
	fs.mkdirSync(path.join(channelDir, REPLIES_DIR), { recursive: true, mode: 0o700 });
}

function requestPath(channelDir: string, requestId: string): string {
	return path.join(channelDir, REQUESTS_DIR, `${safeSegment(requestId)}.json`);
}

function replyPath(channelDir: string, requestId: string): string {
	return path.join(channelDir, REPLIES_DIR, `${safeSegment(requestId)}.json`);
}

function readTextEnv(name: string): string | undefined {
	const value = process.env[name]?.trim();
	return value ? value : undefined;
}

function readChildMetadata(): {
	channelDir: string;
	runId: string;
	agent: string;
	childIndex: number;
	orchestratorTarget?: string;
	orchestratorSessionId?: string;
	childTarget?: string;
} | undefined {
	const channelDir = readTextEnv(SUBAGENT_SUPERVISOR_CHANNEL_DIR_ENV);
	const runId = readTextEnv(SUBAGENT_RUN_ID_ENV);
	const agent = readTextEnv(SUBAGENT_CHILD_AGENT_ENV);
	const rawIndex = readTextEnv(SUBAGENT_CHILD_INDEX_ENV);
	const orchestratorSessionId = readTextEnv(SUBAGENT_ORCHESTRATOR_SESSION_ID_ENV);
	if (!channelDir || !runId || !agent || !orchestratorSessionId || rawIndex === undefined || !/^\d+$/.test(rawIndex)) return undefined;
	return {
		channelDir,
		runId,
		agent,
		childIndex: Number(rawIndex),
		orchestratorTarget: readTextEnv(SUBAGENT_ORCHESTRATOR_TARGET_ENV),
		orchestratorSessionId,
		childTarget: readTextEnv("PI_SUBAGENT_INTERCOM_SESSION_NAME"),
	};
}

function reasonHeading(reason: SupervisorReason): string {
	if (reason === "interview_request") return "Subagent requests a structured supervisor interview.";
	if (reason === "progress_update") return "Subagent progress update.";
	return "Subagent needs a supervisor decision.";
}

function formatChildMessage(input: {
	reason: SupervisorReason;
	message?: string;
	interview?: unknown;
	runId: string;
	agent: string;
	childIndex: number;
	childTarget?: string;
}): string {
	const lines = [
		reasonHeading(input.reason),
		`Run: ${input.runId}`,
		`Agent: ${input.agent}`,
		`Child index: ${input.childIndex}`,
	];
	if (input.childTarget) lines.push(`Child intercom target: ${input.childTarget}`);
	lines.push("");
	if (input.message?.trim()) lines.push(input.message.trim());
	if (input.reason === "interview_request") {
		lines.push(
			"",
			"Structured response requested. Reply with JSON, optionally fenced in ```json, matching the requested interview shape.",
		);
		if (input.interview !== undefined) lines.push(JSON.stringify(input.interview, null, "\t"));
	}
	return lines.join("\n").trimEnd();
}

function parseStructuredReply(message: string): { value?: unknown; error?: string } {
	const trimmed = message.trim();
	const fenced = trimmed.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i)?.[1]?.trim();
	try {
		return { value: JSON.parse(fenced ?? trimmed) };
	} catch (error) {
		return { error: error instanceof Error ? `${error.name}: ${error.message}` : String(error) };
	}
}

function askTimeoutMs(): number {
	const parsed = Number(process.env.PI_INTERCOM_ASK_TIMEOUT_MS);
	return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_ASK_TIMEOUT_MS;
}

function delay(ms: number, signal?: AbortSignal): Promise<void> {
	return new Promise((resolve, reject) => {
		if (signal?.aborted) {
			reject(new Error("Supervisor request cancelled."));
			return;
		}
		let timer: ReturnType<typeof setTimeout> | undefined;
		const cleanup = () => {
			if (timer) clearTimeout(timer);
			signal?.removeEventListener("abort", onAbort);
		};
		const onAbort = () => {
			cleanup();
			reject(new Error("Supervisor request cancelled."));
		};
		timer = setTimeout(() => {
			cleanup();
			resolve();
		}, ms);
		signal?.addEventListener("abort", onAbort, { once: true });
	});
}

async function waitForReply(channelDir: string, requestId: string, deadline: number, signal?: AbortSignal): Promise<SupervisorReply> {
	const file = replyPath(channelDir, requestId);
	while (Date.now() <= deadline) {
		if (signal?.aborted) throw new Error("Supervisor request cancelled.");
		if (fs.existsSync(file)) {
			const parsed = JSON.parse(fs.readFileSync(file, "utf-8")) as Partial<SupervisorReply>;
			if (parsed.type === "subagent.supervisor.reply" && parsed.requestId === requestId && typeof parsed.message === "string") {
				return parsed as SupervisorReply;
			}
		}
		await delay(250, signal);
	}
	throw new Error("Timed out waiting for supervisor reply.");
}

async function sendSupervisorRequest(params: ContactSupervisorParams, signal?: AbortSignal): Promise<AgentToolResult<Record<string, unknown>>> {
	const metadata = readChildMetadata();
	if (!metadata) throw new Error("Native supervisor channel is not available for this subagent.");
	if (params.reason !== "progress_update" && !params.message?.trim() && params.reason !== "interview_request") {
		throw new Error("message is required for supervisor decisions.");
	}
	ensureSupervisorChannelDir(metadata.channelDir);
	const requestId = randomUUID();
	const expectsReply = params.reason !== "progress_update";
	const createdAt = Date.now();
	const replyDeadline = createdAt + askTimeoutMs();
	const expiresAt = expectsReply ? replyDeadline : undefined;
	const message = formatChildMessage({ ...metadata, reason: params.reason, message: params.message, interview: params.interview });
	const request: SupervisorRequest = {
		type: "subagent.supervisor.request",
		id: requestId,
		createdAt,
		...(expiresAt !== undefined ? { expiresAt } : {}),
		reason: params.reason,
		message,
		expectsReply,
		...(metadata.orchestratorTarget ? { orchestratorTarget: metadata.orchestratorTarget } : {}),
		...(metadata.orchestratorSessionId ? { orchestratorSessionId: metadata.orchestratorSessionId } : {}),
		runId: metadata.runId,
		agent: metadata.agent,
		childIndex: metadata.childIndex,
		...(metadata.childTarget ? { childTarget: metadata.childTarget } : {}),
		...(params.interview !== undefined ? { interview: params.interview } : {}),
	};
	const serialized = JSON.stringify(request, null, "\t");
	if (Buffer.byteLength(serialized, "utf-8") > MAX_MESSAGE_BYTES) throw new Error("Supervisor request is too large.");
	persistFeedbackRequest(request, params.message ?? "", params.interview);
	writeAtomicJson(requestPath(metadata.channelDir, requestId), request);

	if (!expectsReply) {
		return {
			content: [{ type: "text", text: "Supervisor progress update queued." }],
			details: { delivered: true, requestId, reason: params.reason },
		};
	}

	try {
		const reply = await waitForReply(metadata.channelDir, requestId, replyDeadline, signal);
		const details: Record<string, unknown> = { requestId, reason: params.reason, ...(reply.outcome ? { outcome: reply.outcome } : {}) };
		if (params.reason === "interview_request") {
			const structured = parseStructuredReply(reply.message);
			if (structured.error) details.structuredReplyParseError = structured.error;
			else details.structuredReply = structured.value;
		}
		return {
			content: [{ type: "text", text: `**Reply from supervisor:**\n${reply.message}` }],
			details,
		};
	} catch (error) {
		removeRequestFile(requestPath(metadata.channelDir, requestId));
		throw error;
	}
}

function hasTool(pi: ExtensionAPI, name: string): boolean {
	try {
		return pi.getAllTools?.().some((tool: { name?: unknown }) => tool.name === name) === true;
	} catch {
		return false;
	}
}

export function registerNativeSupervisorClient(pi: ExtensionAPI, options: { includeIntercomFallback?: boolean } = {}): void {
	if (!readChildMetadata()) return;
	const includeIntercomFallback = options.includeIntercomFallback !== false;
	if (!hasTool(pi, "contact_supervisor")) {
		const tool: ToolDefinition<typeof ContactSupervisorParamsSchema, Record<string, unknown>> = {
			name: "contact_supervisor",
			label: "Contact Supervisor",
			description: "Contact the parent/supervisor session for a blocking decision, structured interview, or progress update.",
			parameters: ContactSupervisorParamsSchema,
			execute(_id, params, signal) {
				return sendSupervisorRequest(params as ContactSupervisorParams, signal);
			},
		};
		pi.registerTool(tool);
	}
	if (includeIntercomFallback && !hasTool(pi, "intercom")) {
		const tool: ToolDefinition<typeof IntercomParamsSchema, Record<string, unknown>> = {
			name: "intercom",
			label: "Intercom",
			description: "Native supervisor-channel intercom fallback for subagents. Prefer contact_supervisor when available.",
			parameters: IntercomParamsSchema,
			async execute(_id, params, signal) {
				const action = (params as IntercomParams).action;
				if (action === "status") return { content: [{ type: "text", text: "Native supervisor channel is active." }], details: { active: true } };
				if (action === "list") return { content: [{ type: "text", text: "Supervisor session available through contact_supervisor." }], details: { sessions: [] } };
				if (action === "send") return sendSupervisorRequest({ reason: "progress_update", message: (params as IntercomParams).message ?? "" }, signal);
				if (action === "ask") return sendSupervisorRequest({ reason: "need_decision", message: (params as IntercomParams).message ?? "" }, signal);
				throw new Error("Native child intercom supports status, list, send, and ask. Use parent intercom reply from the supervisor session.");
			},
		};
		pi.registerTool(tool);
	}
}

function parseRequestFile(file: string, channelDir: string): PendingSupervisorRequest | undefined {
	try {
		const parsed = JSON.parse(fs.readFileSync(file, "utf-8")) as Partial<SupervisorRequest>;
		if (parsed.type !== "subagent.supervisor.request") return undefined;
		if (typeof parsed.id !== "string" || !parsed.id) return undefined;
		if (parsed.reason !== "need_decision" && parsed.reason !== "interview_request" && parsed.reason !== "progress_update") return undefined;
		if (typeof parsed.message !== "string" || !parsed.message) return undefined;
		if (typeof parsed.runId !== "string" || typeof parsed.agent !== "string" || typeof parsed.childIndex !== "number") return undefined;
		return { ...parsed as SupervisorRequest, channelDir, requestFile: file };
	} catch {
		return undefined;
	}
}

function listRequestFiles(): Array<{ channelDir: string; file: string }> {
	let channelEntries: fs.Dirent[];
	try {
		channelEntries = fs.readdirSync(SUPERVISOR_CHANNEL_ROOT, { withFileTypes: true });
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
		throw error;
	}
	const files: Array<{ channelDir: string; file: string }> = [];
	for (const entry of channelEntries) {
		if (!entry.isDirectory()) continue;
		const channelDir = path.join(SUPERVISOR_CHANNEL_ROOT, entry.name);
		const requestsDir = path.join(channelDir, REQUESTS_DIR);
		let requestEntries: fs.Dirent[];
		try {
			requestEntries = fs.readdirSync(requestsDir, { withFileTypes: true });
		} catch {
			continue;
		}
		for (const requestEntry of requestEntries) {
			if (requestEntry.isFile() && requestEntry.name.endsWith(".json")) files.push({ channelDir, file: path.join(requestsDir, requestEntry.name) });
		}
	}
	return files;
}

function readDirectoryEntries(dir: string): fs.Dirent[] | undefined {
	try {
		return fs.readdirSync(dir, { withFileTypes: true });
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
		return undefined;
	}
}

function directoryMtimeMs(dir: string): number {
	try {
		return fs.statSync(dir).mtimeMs;
	} catch {
		return 0;
	}
}

function removeEmptyDirectory(dir: string): boolean {
	try {
		fs.rmdirSync(dir);
		return true;
	} catch (error) {
		const code = (error as NodeJS.ErrnoException).code;
		if (code === "ENOENT") return true;
		if (code === "ENOTEMPTY" || code === "EEXIST" || code === "EPERM" || code === "EBUSY") return false;
		throw error;
	}
}

function removeStaleEmptySupervisorChannel(channelDir: string, nowMs: number): boolean {
	const requestsDir = path.join(channelDir, REQUESTS_DIR);
	const repliesDir = path.join(channelDir, REPLIES_DIR);
	const newestKnownMtimeMs = Math.max(
		directoryMtimeMs(channelDir),
		directoryMtimeMs(requestsDir),
		directoryMtimeMs(repliesDir),
	);
	if (nowMs - newestKnownMtimeMs < STALE_EMPTY_CHANNEL_AGE_MS) return false;

	const requestEntries = readDirectoryEntries(requestsDir);
	if (!requestEntries || requestEntries.length > 0) return false;
	const replyEntries = readDirectoryEntries(repliesDir);
	if (!replyEntries || replyEntries.length > 0) return false;

	if (!removeEmptyDirectory(requestsDir)) return false;
	if (!removeEmptyDirectory(repliesDir)) return false;
	if (!removeEmptyDirectory(channelDir)) return false;
	return true;
}

function cleanupStaleEmptySupervisorChannels(nowMs = Date.now()): number {
	let channelEntries: fs.Dirent[];
	try {
		channelEntries = fs.readdirSync(SUPERVISOR_CHANNEL_ROOT, { withFileTypes: true });
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return 0;
		throw error;
	}

	let removed = 0;
	for (const entry of channelEntries) {
		if (!entry.isDirectory()) continue;
		try {
			if (removeStaleEmptySupervisorChannel(path.join(SUPERVISOR_CHANNEL_ROOT, entry.name), nowMs)) removed++;
		} catch {
			// Cleanup is opportunistic; active writers can race with us and will be picked up by a later pass.
		}
	}
	return removed;
}

function currentContextSessionId(state: Pick<SubagentState, "currentSessionId">, ctx: ExtensionContext): string | undefined {
	try {
		const sessionId = ctx.sessionManager.getSessionId();
		if (sessionId) return sessionId;
	} catch {
		// Fall through to the last known identity.
	}
	return state.currentSessionId ?? undefined;
}

function requestMatchesContext(request: SupervisorRequest, state: Pick<SubagentState, "currentSessionId">, ctx: ExtensionContext): boolean {
	const currentSessionId = currentContextSessionId(state, ctx);
	return Boolean(currentSessionId && request.orchestratorSessionId === currentSessionId);
}

function removeRequestFile(file: string): void {
	try {
		fs.rmSync(file, { force: true });
	} catch {
		// Request cleanup is best-effort; reply files and timeout errors remain authoritative.
	}
}

type SupervisorRequestLifecycle = "pending" | "resolved" | "expired" | "inactive" | "missing" | "wrong-session";

function requestExpiresAt(request: SupervisorRequest, now: number): number {
	const expiresAt = (request as { expiresAt?: unknown }).expiresAt;
	if (typeof expiresAt === "number" && Number.isFinite(expiresAt)) return expiresAt;
	return Number.isFinite(request.createdAt) ? request.createdAt + askTimeoutMs() : now;
}

function requestRunInactive(request: SupervisorRequest, state: SubagentState): boolean {
	if (state.foregroundControls.has(request.runId)) return false;
	const foregroundRun = state.foregroundRuns?.get(request.runId);
	const foregroundChild = foregroundRun?.children.find((child) => child.index === request.childIndex && child.agent === request.agent)
		?? foregroundRun?.children[request.childIndex];
	if (foregroundChild) return foregroundChild.status !== "detached";

	const asyncJob = state.asyncJobs.get(request.runId);
	if (!asyncJob) return false;
	if (asyncJob.status === "complete" || asyncJob.status === "failed" || asyncJob.status === "paused") return true;
	const stepStatus = asyncJob.steps?.[request.childIndex]?.status;
	return stepStatus === "complete" || stepStatus === "completed" || stepStatus === "failed" || stepStatus === "paused";
}

function requestLifecycle(request: PendingSupervisorRequest, state: SubagentState, ctx: ExtensionContext | undefined, now: number): SupervisorRequestLifecycle {
	if (ctx && !requestMatchesContext(request, state, ctx)) return "wrong-session";
	if (!fs.existsSync(request.requestFile)) return "missing";
	if (request.expectsReply && fs.existsSync(replyPath(request.channelDir, request.id))) return "resolved";
	if (request.expectsReply && now > requestExpiresAt(request, now)) return "expired";
	if (request.expectsReply && requestRunInactive(request, state)) return "inactive";
	return "pending";
}

function cleanupRequestLifecycle(request: PendingSupervisorRequest, lifecycle: SupervisorRequestLifecycle): void {
	if (lifecycle === "expired" || lifecycle === "inactive") {
		safeUpdateFeedbackRecord(request.id, (record) => ({ ...record, lifecycle, outcome: record.outcome === "unreviewed" ? "deferred" : record.outcome }));
	}
	if (lifecycle === "resolved") {
		safeUpdateFeedbackRecord(request.id, (record) => ({ ...record, lifecycle: record.lifecycle === "awaiting_reply" ? "replied" : record.lifecycle, outcome: record.outcome === "unreviewed" ? "replied" : record.outcome }));
	}
	if (lifecycle === "resolved" || lifecycle === "expired" || lifecycle === "inactive") removeRequestFile(request.requestFile);
}

function refreshPendingRequests(pending: Map<string, PendingSupervisorRequest>, state: SubagentState, ctx: ExtensionContext | undefined): void {
	const now = Date.now();
	for (const request of pending.values()) {
		const lifecycle = requestLifecycle(request, state, ctx, now);
		if (lifecycle === "pending") continue;
		pending.delete(request.id);
		cleanupRequestLifecycle(request, lifecycle);
	}
}

function formatPendingLine(request: PendingSupervisorRequest): string {
	const replyHint = request.expectsReply ? ` Reply: ${NATIVE_SUPERVISOR_TOOL_NAME}({ action: "reply", replyTo: "${request.id}", message: "..." })` : "";
	return `- ${request.id}: ${request.agent} [${request.runId}#${request.childIndex}] ${request.reason}.${replyHint}`;
}

function requestVisibleText(request: PendingSupervisorRequest): string {
	const lines = [request.message];
	if (request.expectsReply) {
		lines.push("", `Reply with: ${NATIVE_SUPERVISOR_TOOL_NAME}({ action: "reply", replyTo: "${request.id}", message: "..." })`);
	} else {
		lines.push("", `Feedback record: ${request.id}. Review later with ${NATIVE_SUPERVISOR_TOOL_NAME}({ action: "review", feedbackId: "${request.id}", outcome: "accepted|rejected|deferred" })`);
	}
	return lines.join("\n");
}

function writeReply(request: PendingSupervisorRequest, message: string, requestedOutcome?: FeedbackOutcome): void {
	if (!message.trim()) throw new Error("message is required for supervisor replies.");
	const outcome = requestedOutcome ?? "replied";
	if (outcome === "unreviewed") throw new Error("invalid supervisor reply outcome");
	const updatedAt = new Date().toISOString();
	updateFeedbackRecord(request.id, (record) => ({
		...record,
		lifecycle: outcome === "replied" ? "replied" : "reviewed",
		outcome,
		response: { message: boundedText(message, FEEDBACK_MAX_TEXT_BYTES), outcome, updatedAt },
	}), true);
	const reply: SupervisorReply = {
		type: "subagent.supervisor.reply",
		requestId: request.id,
		createdAt: Date.now(),
		message: message.trim(),
		outcome,
	};
	writeAtomicJson(replyPath(request.channelDir, request.id), reply);
	removeRequestFile(request.requestFile);
}

function resolvePendingRequest(pending: Map<string, PendingSupervisorRequest>, params: IntercomParams): PendingSupervisorRequest {
	if (params.replyTo) {
		const request = pending.get(params.replyTo);
		if (!request) throw new Error(`No pending supervisor request found for replyTo '${params.replyTo}'.`);
		return request;
	}
	const requests = [...pending.values()].filter((request) => request.expectsReply);
	if (params.to) {
		const normalizedTo = params.to.toLowerCase();
		const matches = requests.filter((request) =>
			request.id.toLowerCase().startsWith(normalizedTo)
			|| request.agent.toLowerCase() === normalizedTo
			|| request.childTarget?.toLowerCase() === normalizedTo,
		);
		if (matches.length === 1) return matches[0]!;
		if (matches.length > 1) throw new Error(`Multiple pending supervisor requests match '${params.to}'. Use replyTo.`);
	}
	if (requests.length === 1) return requests[0]!;
	if (requests.length === 0) throw new Error("No pending supervisor requests need a reply.");
	throw new Error("Multiple pending supervisor requests need replies. Use replyTo.");
}

function publicPendingRequests(pending: Map<string, PendingSupervisorRequest>): Array<Record<string, unknown>> {
	return [...pending.values()].map((request) => ({
		id: request.id,
		runId: request.runId,
		agent: request.agent,
		childIndex: request.childIndex,
		reason: request.reason,
		expectsReply: request.expectsReply,
	}));
}

function buildParentIntercomTool(pending: Map<string, PendingSupervisorRequest>, state: SubagentState, name = "intercom"): ToolDefinition<typeof IntercomParamsSchema, Record<string, unknown>> {
	return {
		name,
		label: name === "intercom" ? "Intercom" : "Subagent Supervisor",
		description: name === "intercom"
			? "Native pi-subagents supervisor channel. Use reply/pending/status/review to answer child requests and disposition persisted feedback."
			: "Native pi-subagents supervisor channel. Use reply/pending/status/review to answer child requests and disposition persisted feedback without overriding pi-intercom.",
		parameters: IntercomParamsSchema,
		async execute(_id, params) {
			refreshPendingRequests(pending, state, state.lastUiContext ?? undefined);
			const input = params as IntercomParams;
			if (input.action === "status") {
				return { content: [{ type: "text", text: `Native supervisor channel active. Pending replies: ${pending.size}.` }], details: { active: true, pending: pending.size, root: SUPERVISOR_CHANNEL_ROOT } };
			}
			if (input.action === "pending" || input.action === "list") {
				const lines = [...pending.values()].filter((request) => request.expectsReply).map(formatPendingLine);
				return { content: [{ type: "text", text: lines.length ? lines.join("\n") : "No pending supervisor requests." }], details: { pending: publicPendingRequests(pending) } };
			}
			if (input.action === "reply") {
				const request = resolvePendingRequest(pending, input);
				writeReply(request, input.message ?? "", input.outcome);
				pending.delete(request.id);
				return { content: [{ type: "text", text: `Replied to supervisor request ${request.id}.` }], details: { replyTo: request.id, runId: request.runId, agent: request.agent, outcome: input.outcome ?? "replied" } };
			}
			if (input.action === "review") {
				if (!input.feedbackId || !input.outcome) throw new Error("feedbackId and outcome are required to review feedback");
				reviewFeedback(input.feedbackId, input.outcome, input.message);
				return { content: [{ type: "text", text: `Recorded ${input.outcome} outcome for feedback ${input.feedbackId}.` }], details: { feedbackId: input.feedbackId, outcome: input.outcome } };
			}
			if (input.action === "send" || input.action === "ask") {
				throw new Error("Native pi-subagents intercom currently handles supervisor replies. Child agents initiate asks with contact_supervisor.");
			}
			throw new Error(`Unsupported intercom action: ${input.action}`);
		},
	};
}

export function createNativeSupervisorChannel(pi: ExtensionAPI, state: SubagentState): { start: () => void; dispose: () => void; pending: Map<string, PendingSupervisorRequest> } {
	const pending = new Map<string, PendingSupervisorRequest>();
	const seenFiles = new Set<string>();
	let poller: ReturnType<typeof setInterval> | undefined;
	let lastStaleCleanupAt = 0;

	const registerParentTools = (): void => {
		if (!hasTool(pi, NATIVE_SUPERVISOR_TOOL_NAME)) pi.registerTool(buildParentIntercomTool(pending, state, NATIVE_SUPERVISOR_TOOL_NAME));
		if (!hasTool(pi, "intercom")) pi.registerTool(buildParentIntercomTool(pending, state));
	};

	const cleanupStaleChannelsIfDue = (): void => {
		const nowMs = Date.now();
		if (nowMs - lastStaleCleanupAt < STALE_EMPTY_CHANNEL_CLEANUP_INTERVAL_MS) return;
		lastStaleCleanupAt = nowMs;
		try {
			cleanupStaleEmptySupervisorChannels(nowMs);
		} catch {
			// Supervisor delivery must not fail because best-effort temp cleanup failed.
		}
	};

	const poll = (): void => {
		cleanupStaleChannelsIfDue();
		const ctx = state.lastUiContext;
		if (!ctx) return;
		refreshPendingRequests(pending, state, ctx);
		const now = Date.now();
		for (const { channelDir, file } of listRequestFiles()) {
			if (seenFiles.has(file)) continue;
			const request = parseRequestFile(file, channelDir);
			if (!request || !requestMatchesContext(request, state, ctx)) continue;
			const lifecycle = requestLifecycle(request, state, undefined, now);
			if (lifecycle !== "pending") {
				seenFiles.add(file);
				cleanupRequestLifecycle(request, lifecycle);
				continue;
			}
			seenFiles.add(file);
			safeUpdateFeedbackRecord(request.id, (record) => ({ ...record, lifecycle: request.expectsReply ? "awaiting_reply" : "delivered" }));
			if (request.expectsReply) pending.set(request.id, request);
			else {
				removeRequestFile(request.requestFile);
			}
			pi.sendMessage({
				customType: "subagent_supervisor_request",
				content: requestVisibleText(request),
				display: true,
				details: {
					id: request.id,
					reason: request.reason,
					expectsReply: request.expectsReply,
					runId: request.runId,
					agent: request.agent,
					childIndex: request.childIndex,
				},
			});
			if (request.expectsReply) {
				(pi as { events?: IntercomEventBus }).events?.emit(INTERCOM_DETACH_REQUEST_EVENT, {
					requestId: request.id,
					runId: request.runId,
					agent: request.agent,
					childIndex: request.childIndex,
				});
			}
		}
	};

	return {
		start: () => {
			if (poller) return;
			registerParentTools();
			poll();
			poller = setInterval(poll, CHANNEL_POLL_MS);
			poller.unref?.();
		},
		dispose: () => {
			if (poller) clearInterval(poller);
			poller = undefined;
			pending.clear();
			seenFiles.clear();
		},
		pending,
	};
}
