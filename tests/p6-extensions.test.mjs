import assert from "node:assert/strict";
import test from "node:test";
import { createExtensionJiti } from "./extension-jiti.mjs";

const jiti = createExtensionJiti(import.meta.url);
const modules = [
  await jiti.import("../pi/extensions/project-messages/index.ts"),
  await jiti.import("../pi/extensions/project-commands/index.ts"),
  await jiti.import("../pi/extensions/dependency-review/index.ts"),
];

test("P6 extensions declare only semantic controller operations", async () => {
  const declarations = new Map();
  const requests = [];
  globalThis[Symbol.for("pi.controllerChannel.v1")] = {
    registerTool(operation, definition) { declarations.set(operation, definition); },
    async request(operation, payload) { requests.push({ operation, payload }); return { ok: true }; },
  };
  for (const module of modules) module.default({});
  assert.deepEqual([...declarations].map(([operation]) => operation).sort(), [
    "command.request", "command.status", "dependency.disposition", "dependency.inventory",
    "message.acknowledge", "message.list", "message.post", "message.reply", "package-review.gate",
    "package-review.record", "package.request", "package.status",
  ]);
  assert.equal([...declarations.values()].some((tool) => /approve|authorize|execute|integrate/i.test(tool.name)), false);
  await declarations.get("command.request").execute("id", { operation: "host.controller-status", purpose: "test" }, new AbortController().signal);
  assert.deepEqual(requests, [{ operation: "command.request", payload: { operation: "host.controller-status", purpose: "test" } }]);
  assert.equal(Object.keys(requests[0].payload).some((key) => /project|conversation|run|writer/i.test(key)), false);
});

test("P6 extensions contain no subprocess or ambient identity path", async () => {
  const { readFile } = await import("node:fs/promises");
  for (const relative of ["project-messages", "project-commands", "dependency-review"]) {
    const source = await readFile(new URL(`../pi/extensions/${relative}/index.ts`, import.meta.url), "utf8");
    for (const forbidden of ["pi.exec", "child_process", "PI_SYSTEM_PROJECT_ID", "PI_SYSTEM_RUN_ID", "PI_SYSTEM_WRITER_GENERATION", "pi-control"]) assert.equal(source.includes(forbidden), false, `${relative}: ${forbidden}`);
  }
});
