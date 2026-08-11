/** Semantic subagent requests. Child process and resource authority stays in the controller. */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const CHANNEL_SYMBOL = Symbol.for("pi.controllerChannel.v1");

type ChannelApi = {
	request(operation: string, payload: Record<string, unknown>, signal?: AbortSignal): Promise<unknown>;
};

export const SUBAGENT_ROLES = ["investigator", "reviewer", "scout"] as const;

function result(value: unknown) {
	return { content: [{ type: "text" as const, text: JSON.stringify(value) }], details: {} };
}

function channel(): ChannelApi {
	const value = (globalThis as unknown as Record<symbol, ChannelApi | undefined>)[CHANNEL_SYMBOL];
	if (!value) throw new Error("authenticated controller channel is unavailable");
	return value;
}

export default function controllerSubagentBroker(pi: ExtensionAPI): void {
	pi.registerTool({
		name: "subagent",
		label: "Subagent",
		description: "Request one controller-created read-only investigator, reviewer, or scout on an immutable snapshot. Mutable attempts require a separate workstream.",
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
}
