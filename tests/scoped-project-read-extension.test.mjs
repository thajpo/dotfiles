import assert from "node:assert/strict";
import test from "node:test";

import { createExtensionJiti } from "./extension-jiti.mjs";

const jiti = createExtensionJiti(import.meta.url);
const { default: scopedProjectRead } = await jiti.import("../pi/extensions/scoped-project-read/index.ts");
const channelSymbol = Symbol.for("pi.controllerChannel.v1");

function fakePi() {
  const tools = new Map();
  return { tools, api: { registerTool(tool) { tools.set(tool.name, tool); } } };
}

async function withChannel(callback) {
  const calls = [];
  globalThis[channelSymbol] = { async request(operation, payload) { calls.push({ operation, payload }); return { ok: true }; } };
  try { return await callback(calls); } finally { delete globalThis[channelSymbol]; }
}

test("is inert without the inherited authenticated controller channel", () => {
  const pi = fakePi();
  scopedProjectRead(pi.api);
  assert.deepEqual([...pi.tools], []);
});

test("registers only the four controller-scoped read tools", async () => {
  await withChannel(() => {
    const pi = fakePi();
    scopedProjectRead(pi.api);
    assert.deepEqual([...pi.tools.keys()].sort(), ["git_read", "grep", "ls", "read"]);
    for (const forbidden of ["bash", "write", "edit", "host_command", "subagent"]) assert.equal(pi.tools.has(forbidden), false);
  });
});

test("forces operation identity and supplies no ambient project assignment", async () => {
  await withChannel(async (calls) => {
    const pi = fakePi();
    scopedProjectRead(pi.api);
    await pi.tools.get("read").execute("call-1", { path: "README", operation: "write" }, new AbortController().signal);
    await pi.tools.get("git_read").execute("call-2", { query: "show", path: "README" }, new AbortController().signal);
    assert.deepEqual(calls, [
      { operation: "scoped-read", payload: { path: "README", operation: "read" } },
      { operation: "scoped-read", payload: { query: "show", path: "README", operation: "git" } },
    ]);
    assert.equal(JSON.stringify(calls).includes("projectId"), false);
    assert.equal(JSON.stringify(calls).includes("workingCopyId"), false);
  });
});
