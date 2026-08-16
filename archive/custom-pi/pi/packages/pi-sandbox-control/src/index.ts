/** Model-visible writer tools backed only by the inherited controller channel. */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { readRuntimeManifest } from "./manifest-adapter.ts";

const CHANNEL_SYMBOL = Symbol.for("pi.controllerChannel.v1");
const MANIFEST_ENVIRONMENT_KEY = "PI_RUNTIME_MANIFEST";

type ChannelApi = {
	request(operation: string, payload: Record<string, unknown>, signal?: AbortSignal): Promise<unknown>;
};

function result(value: unknown) {
	return { content: [{ type: "text" as const, text: JSON.stringify(value) }], details: {} };
}

export default function sandboxBroker(pi: ExtensionAPI): void {
	const channel = (globalThis as unknown as Record<symbol, ChannelApi | undefined>)[CHANNEL_SYMBOL];
	const manifestPath = process.env[MANIFEST_ENVIRONMENT_KEY];
	if (!channel || !manifestPath) return;

	let manifest;
	try {
		manifest = readRuntimeManifest(manifestPath);
	} catch {
		return;
	}
	if (manifest.conversation.authorityProfile !== "writer-container" || !manifest.workingCopy || !manifest.toolRuntime) return;

	const call = async (tool: string, payload: Record<string, unknown>, signal: AbortSignal) => {
		if (signal.aborted) throw new Error(`${tool} was aborted`);
		return result(await channel.request("writer-tool", { tool, arguments: payload }, signal));
	};

	pi.registerTool({
		name: "read",
		label: "Read file",
		description: "Read one bounded regular file relative to the controller-assigned working copy.",
		parameters: { type: "object", additionalProperties: false, required: ["path"], properties: {
			path: { type: "string", minLength: 1, maxLength: 4096 },
			offset: { type: "integer", minimum: 1, maximum: 1_000_000 },
			limit: { type: "integer", minimum: 1, maximum: 10_000 },
		} } as never,
		async execute(_id, params, signal) { return call("read", params, signal); },
	});
	pi.registerTool({
		name: "write",
		label: "Write file",
		description: "Atomically write one bounded regular file relative to the controller-assigned working copy.",
		parameters: { type: "object", additionalProperties: false, required: ["path", "content"], properties: {
			path: { type: "string", minLength: 1, maxLength: 4096 },
			content: { type: "string", maxLength: 48 * 1024 },
		} } as never,
		async execute(_id, params, signal) { return call("write", params, signal); },
	});
	pi.registerTool({
		name: "edit",
		label: "Edit file",
		description: "Replace one exact occurrence in a bounded regular file in the assigned working copy.",
		parameters: { type: "object", additionalProperties: false, required: ["path", "oldText", "newText"], properties: {
			path: { type: "string", minLength: 1, maxLength: 4096 },
			oldText: { type: "string", minLength: 1, maxLength: 24 * 1024 },
			newText: { type: "string", maxLength: 24 * 1024 },
		} } as never,
		async execute(_id, params, signal) { return call("edit", params, signal); },
	});
	pi.registerTool({
		name: "bash",
		label: "Run in tool container",
		description: "Run bounded shell text or an exact argv only in the controller-attested tool container.",
		parameters: { type: "object", additionalProperties: false, properties: {
			command: { type: "string", minLength: 1, maxLength: 16 * 1024 },
			argv: { type: "array", minItems: 1, maxItems: 128, items: { type: "string", minLength: 1, maxLength: 4096 } },
			timeout: { type: "integer", minimum: 1, maximum: 120 },
		} } as never,
		async execute(_id, params, signal) { return call("bash", params, signal); },
	});
}
