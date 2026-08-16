import assert from "node:assert/strict";
import test from "node:test";
import { createExtensionJiti } from "./extension-jiti.mjs";

const jiti = createExtensionJiti(import.meta.url);
const module = await jiti.import("../pi/extensions/change-flow/index.ts");

test("change-flow declares only semantic controller operations", async () => {
  const declarations = new Map();
  const requests = [];
  globalThis[Symbol.for("pi.controllerChannel.v1")] = {
    registerTool(operation, definition) { declarations.set(operation, definition); },
    async request(operation, payload) { requests.push({ operation, payload }); return { ok: true }; },
  };
  module.default({});
  assert.deepEqual([...declarations].map(([operation]) => operation).sort(), [
    "change.list", "change.submit", "integration.analyze", "review.request",
  ]);
  assert.equal([...declarations.values()].some((tool) => /approve|authorize|integrate|execute/i.test(tool.name)), false);

  await declarations.get("change.submit").execute("id", {
    title: "fix", summary: "summary", targetRef: "refs/heads/main", captureMode: "dirty",
    selectedPaths: ["a.txt"], excludedPaths: [], idempotencyKey: "k1",
  }, new AbortController().signal);
  assert.equal(requests[0].operation, "change.submit");
  assert.equal(Object.keys(requests[0].payload).some((key) => /^(projectId|conversationId|runId)$/.test(key)), false);

  await declarations.get("change.list").execute("id", {}, new AbortController().signal);
  assert.equal(requests[1].operation, "change.list");

  await declarations.get("review.request").execute("id", { changeId: "chg_x", revision: 1 }, new AbortController().signal);
  assert.equal(requests[2].operation, "review.request");

  await declarations.get("integration.analyze").execute("id", { changeId: "chg_x", revision: 1, targetRef: "refs/heads/main" }, new AbortController().signal);
  assert.equal(requests[3].operation, "integration.analyze");
});

test("change-flow contains no subprocess or ambient identity path", async () => {
  const { readFile } = await import("node:fs/promises");
  const source = await readFile(new URL("../pi/extensions/change-flow/index.ts", import.meta.url), "utf8");
  for (const forbidden of ["pi.exec", "child_process", "PI_SYSTEM_PROJECT_ID", "PI_SYSTEM_RUN_ID", "PI_SYSTEM_WRITER_GENERATION", "pi-control"]) {
    assert.equal(source.includes(forbidden), false, forbidden);
  }
  assert.match(source, /pi\.controllerChannel\.v1/);
});

console.log("change-flow extension: ok");
