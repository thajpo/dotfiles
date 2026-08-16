import assert from "node:assert/strict";
import test from "node:test";
import { createExtensionJiti } from "./extension-jiti.mjs";

const jiti = createExtensionJiti(import.meta.url);
const module = await jiti.import("../pi/extensions/secretary-work/index.ts");

test("secretary-work declares only semantic secretary operations", async () => {
  const declarations = new Map();
  const requests = [];
  globalThis[Symbol.for("pi.controllerChannel.v1")] = {
    registerTool(operation, definition) { declarations.set(operation, definition); },
    async request(operation, payload) { requests.push({ operation, payload }); return { ok: true }; },
  };
  module.default({});
  assert.deepEqual([...declarations].map(([operation]) => operation).sort(), [
    "integration.propose", "investigation.start", "project.work-index", "review.propose", "workstream.approve", "workstream.propose",
  ]);
  assert.equal([...declarations.values()].some((tool) => /authorize|integrate|execute/i.test(tool.name)), false);
  assert.equal([...declarations.values()].some((tool) => /approve/i.test(tool.name)), true);
  assert.equal([...declarations.values()].some((tool) => /propose/i.test(tool.name)), true);

  await declarations.get("project.work-index").execute("id", {}, new AbortController().signal);
  assert.deepEqual(requests[0], { operation: "project.work-index", payload: {} });

  await declarations.get("investigation.start").execute("id", { purpose: "map the failure", idempotencyKey: "k1" }, new AbortController().signal);
  assert.deepEqual(requests[1], { operation: "subagent.spawn", payload: { role: "investigator", task: "map the failure", idempotencyKey: "k1" } });

  await declarations.get("workstream.propose").execute("id", { title: "fix cancel", purpose: "diagnose and fix", targetRef: "main", knownOverlap: "none", idempotencyKey: "k2" }, new AbortController().signal);
  assert.equal(requests[2].operation, "workstream.propose");
  assert.equal(requests[2].payload.title, "fix cancel");
  assert.equal(requests[2].payload.idempotencyKey, "k2");
  assert.deepEqual(requests[2].payload.targetRef, "main");

  await declarations.get("review.propose").execute("id", { changeId: "chg_x", revision: 2, idempotencyKey: "k3" }, new AbortController().signal);
  assert.equal(requests[3].operation, "message.post");
  assert.equal(requests[3].payload.payload.proposal, "review");
  assert.equal(requests[3].payload.idempotencyKey, "k3");

  await declarations.get("integration.propose").execute("id", { changeId: "chg_x", revision: 2, idempotencyKey: "k4" }, new AbortController().signal);
  assert.equal(requests[4].operation, "message.post");
  assert.equal(requests[4].payload.payload.proposal, "integration");
  for (const request of requests) {
    assert.equal(Object.keys(request.payload).some((key) => /^(projectId|conversationId|runId)$/.test(key)), false);
  }
});

test("secretary-work contains no subprocess or ambient identity path", async () => {
  const { readFile } = await import("node:fs/promises");
  const source = await readFile(new URL("../pi/extensions/secretary-work/index.ts", import.meta.url), "utf8");
  for (const forbidden of ["pi.exec", "child_process", "PI_SYSTEM_PROJECT_ID", "PI_SYSTEM_RUN_ID", "PI_SYSTEM_WRITER_GENERATION", "pi-control"]) {
    assert.equal(source.includes(forbidden), false, forbidden);
  }
  assert.match(source, /pi\.controllerChannel\.v1/);
});

console.log("secretary-work extension: ok");
