/** Semantic subagent requests. Child process and resource authority stays in the controller. */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const CHANNEL_SYMBOL = Symbol.for("pi.controllerChannel.v1");

type ChannelApi = {
	request(operation: string, payload: Record<string, unknown>, signal?: AbortSignal): Promise<unknown>;
};

export const SUBAGENT_ROLES = ["scout", "investigator", "researcher", "planner", "oracle", "delegate", "reviewer"] as const;

function result(value: unknown) {
	return { content: [{ type: "text" as const, text: JSON.stringify(value) }], details: {} };
}

function channel(): ChannelApi {
	const value = (globalThis as unknown as Record<symbol, ChannelApi | undefined>)[CHANNEL_SYMBOL];
	if (!value) throw new Error("authenticated controller channel is unavailable");
	return value;
}

const roleField = Type.Union(SUBAGENT_ROLES.map((role) => Type.Literal(role)) as never);

export default function controllerSubagentBroker(pi: ExtensionAPI): void {
	pi.registerTool({
		name: "subagent",
		label: "Subagent",
		description: "Request one controller-created read-only investigator, reviewer, scout, researcher, planner, oracle, or delegate on an immutable snapshot and wait for its durable result. Mutable attempts require a separate workstream or a headless worker.",
		parameters: {
			type: "object",
			additionalProperties: false,
			required: ["role", "task"],
			properties: {
				role: { type: "string", enum: [...SUBAGENT_ROLES] },
				task: { type: "string", minLength: 1, maxLength: 4096 },
				idempotencyKey: { type: "string", minLength: 1, maxLength: 200 },
			},
		} as never,
		async execute(id, params, signal) {
			if (signal.aborted) throw new Error("subagent request was aborted");
			return result(await channel().request("subagent.spawn", { role: params.role, task: params.task, idempotencyKey: params.idempotencyKey ?? id }, signal));
		},
	});
	pi.registerTool({
		name: "subagent_start",
		label: "Subagent (async)",
		description: "Start one controller-created read-only subagent and return immediately. The child runs detached under the controller; poll with subagent_status or block with subagent_wait. Preferred for genuinely long work; use subagent for quick bounded tasks.",
		parameters: {
			type: "object",
			additionalProperties: false,
			required: ["role", "task"],
			properties: {
				role: { type: "string", enum: [...SUBAGENT_ROLES] },
				task: { type: "string", minLength: 1, maxLength: 4096 },
				idempotencyKey: { type: "string", minLength: 1, maxLength: 200 },
			},
		} as never,
		async execute(id, params, signal) {
			if (signal.aborted) throw new Error("subagent start was aborted");
			return result(await channel().request("subagent.start", { role: params.role, task: params.task, idempotencyKey: params.idempotencyKey ?? id }, signal));
		},
	});
	pi.registerTool({
		name: "subagent_status",
		label: "Subagent status",
		description: "Inspect one child run: current state and the durable terminal record when finished. The child request id comes from subagent_start or subagent.",
		parameters: {
			type: "object",
			additionalProperties: false,
			required: ["childRequestId"],
			properties: { childRequestId: { type: "string", minLength: 1, maxLength: 200 } },
		} as never,
		async execute(_id, params, signal) {
			return result(await channel().request("subagent.status", { childRequestId: params.childRequestId }, signal));
		},
	});
	pi.registerTool({
		name: "subagent_wait",
		label: "Subagent wait",
		description: "Block until the named child run completes or the timeout elapses; returns the terminal record when finished. Never poll with sleep loops; use this tool instead.",
		parameters: {
			type: "object",
			additionalProperties: false,
			required: ["childRequestId"],
			properties: {
				childRequestId: { type: "string", minLength: 1, maxLength: 200 },
				timeoutSeconds: { type: "number", minimum: 1, maximum: 3600 },
			},
		} as never,
		async execute(_id, params, signal) {
			return result(await channel().request("subagent.wait", { childRequestId: params.childRequestId, timeoutSeconds: params.timeoutSeconds ?? 300 }, signal));
		},
	});
	pi.registerTool({
		name: "subagent_list",
		label: "Subagent fleet",
		description: "List the recent controller-created children of the authenticated run with their states.",
		parameters: { type: "object", additionalProperties: false, properties: {} } as never,
		async execute(_id, _params, signal) {
			return result(await channel().request("subagent.list", {}, signal));
		},
	});
	pi.registerTool({
		name: "subagent_interrupt",
		label: "Subagent interrupt",
		description: "Soft-interrupt one detached child: its run terminalizes as interrupted, the session and work are preserved, and subagent_resume can continue the same conversation later.",
		parameters: {
			type: "object",
			additionalProperties: false,
			required: ["childRequestId"],
			properties: { childRequestId: { type: "string", minLength: 1, maxLength: 200 } },
		} as never,
		async execute(_id, params, signal) {
			return result(await channel().request("subagent.interrupt", { childRequestId: params.childRequestId }, signal));
		},
	});
	pi.registerTool({
		name: "subagent_stop",
		label: "Subagent stop",
		description: "Stop one detached child terminally: its run terminalizes and the child is not resumable.",
		parameters: {
			type: "object",
			additionalProperties: false,
			required: ["childRequestId"],
			properties: { childRequestId: { type: "string", minLength: 1, maxLength: 200 } },
		} as never,
		async execute(_id, params, signal) {
			return result(await channel().request("subagent.stop", { childRequestId: params.childRequestId }, signal));
		},
	});
	pi.registerTool({
		name: "subagent_resume",
		label: "Subagent resume",
		description: "Resume one interrupted detached child with the same conversation and task; a new controller run continues the durable session.",
		parameters: {
			type: "object",
			additionalProperties: false,
			required: ["childRequestId"],
			properties: { childRequestId: { type: "string", minLength: 1, maxLength: 200 } },
		} as never,
		async execute(_id, params, signal) {
			return result(await channel().request("subagent.resume", { childRequestId: params.childRequestId }, signal));
		},
	});
	pi.registerTool({
		name: "subagent_steer",
		label: "Subagent steer",
		description: "Send one bounded guidance message to a running child. The child reads it from the durable project message inbox.",
		parameters: {
			type: "object",
			additionalProperties: false,
			required: ["childRequestId", "message"],
			properties: {
				childRequestId: { type: "string", minLength: 1, maxLength: 200 },
				message: { type: "string", minLength: 1, maxLength: 4096 },
			},
		} as never,
		async execute(_id, params, signal) {
			return result(await channel().request("subagent.steer", { childRequestId: params.childRequestId, message: params.message }, signal));
		},
	});
	pi.registerTool({
		name: "worker_start",
		label: "Headless worker (async)",
		description: "Start one mutable headless worker in its own controller-created working copy and writer container, then return immediately. Exactly one writer owns the worker copy; the worker can read, edit, run shell, and test only inside that boundary. Track it with subagent_status/subagent_wait.",
		parameters: {
			type: "object",
			additionalProperties: false,
			required: ["task"],
			properties: {
				task: { type: "string", minLength: 1, maxLength: 4096 },
				title: { type: "string", minLength: 1, maxLength: 200 },
				idempotencyKey: { type: "string", minLength: 1, maxLength: 200 },
			},
		} as never,
		async execute(id, params, signal) {
			if (signal.aborted) throw new Error("worker start was aborted");
			return result(await channel().request("worker.start", { task: params.task, title: params.title ?? "headless worker", idempotencyKey: params.idempotencyKey ?? id }, signal));
		},
	});
}
