import assert from "node:assert/strict";
import test from "node:test";
import { createExtensionJiti } from "./extension-jiti.mjs";

const jiti = createExtensionJiti(import.meta.url);
const module = await jiti.import("../pi/extensions/observability/index.ts");

test("observability declares only read-only controller projections", async () => {
  const declarations = new Map();
  const requests = [];
  globalThis[Symbol.for("pi.controllerChannel.v1")] = {
    registerTool(operation, definition) { declarations.set(operation, definition); },
    async request(operation, payload) { requests.push({ operation, payload }); return { ok: true }; },
  };
  module.default({});
  assert.deepEqual([...declarations].map(([operation]) => operation).sort(), [
    "observe.fleet", "observe.messages", "observe.queue", "observe.tasks",
  ]);
  assert.equal([...declarations.values()].some((tool) => /approve|authorize|integrate|submit|execute/i.test(tool.name)), false);

  await declarations.get("observe.tasks").execute("id", {}, new AbortController().signal);
  assert.equal(requests[0].operation, "project.work-index");
  await declarations.get("observe.fleet").execute("id", {}, new AbortController().signal);
  assert.equal(requests[1].operation, "subagent.list");
  await declarations.get("observe.messages").execute("id", { states: ["pending"] }, new AbortController().signal);
  assert.equal(requests[2].operation, "message.list");
  await declarations.get("observe.queue").execute("id", {}, new AbortController().signal);
  assert.equal(requests[3].operation, "change.list");
});

test("observability contains no subprocess or ambient identity path", async () => {
  const { readFile } = await import("node:fs/promises");
  const source = await readFile(new URL("../pi/extensions/observability/index.ts", import.meta.url), "utf8");
  for (const forbidden of ["pi.exec", "child_process", "PI_SYSTEM_PROJECT_ID", "PI_SYSTEM_RUN_ID", "PI_SYSTEM_WRITER_GENERATION", "pi-control"]) {
    assert.equal(source.includes(forbidden), false, forbidden);
  }
  assert.match(source, /pi\.controllerChannel\.v1/);
});

console.log("observability extension: ok");
