import assert from "node:assert/strict";
import test from "node:test";

import broker, { SUBAGENT_ROLES } from "../../src/controller-broker.ts";

test("broker exposes only semantic controller child requests", async () => {
	const calls: unknown[] = [];
	const symbol = Symbol.for("pi.controllerChannel.v1");
	const previous = (globalThis as any)[symbol];
	try {
		(globalThis as any)[symbol] = { request: async (...args: unknown[]) => { calls.push(args); return { terminal: "success" }; } };
		const tools: any[] = [];
		broker({ registerTool(tool: unknown) { tools.push(tool); } } as any);
		assert.deepEqual(SUBAGENT_ROLES, ["investigator", "reviewer", "scout"]);
		assert.deepEqual(tools.map((tool) => tool.name), ["subagent"]);
		const signal = new AbortController().signal;
		await tools[0].execute("call-1", { role: "reviewer", task: "inspect" }, signal);
		assert.deepEqual(calls, [["subagent.spawn", { role: "reviewer", task: "inspect", idempotencyKey: "call-1" }, signal]]);
	} finally {
		if (previous === undefined) delete (globalThis as any)[symbol];
		else (globalThis as any)[symbol] = previous;
	}
});

test("broker has no direct process execution or ambient environment spread", async () => {
	const source = await import("node:fs/promises").then((fs) => fs.readFile(new URL("../../src/controller-broker.ts", import.meta.url), "utf8"));
	for (const forbidden of ["node:child_process", "process.env", "spawn(", "execFile(", "...process.env"]) assert.equal(source.includes(forbidden), false, forbidden);
});
