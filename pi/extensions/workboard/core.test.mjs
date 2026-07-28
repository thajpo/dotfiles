import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  activeWorkers,
  atomicWrite,
  chooseTaskId,
  chooseWorkerLabel,
  dispatchMarker,
  emptyState,
  normalizeConfig,
  parseNewArgs,
  parseSendArgs,
  reconcileWorkers,
  resolveInside,
  sanitizeTaskId,
  taskArtifactPaths,
} from "./core.ts";

test("task ids are inferred, validated, and deduplicated", () => {
  assert.equal(chooseTaskId("AUTH-42 refresh sessions", undefined, new Set()), "AUTH-42");
  assert.equal(chooseTaskId("automatic session refresh", undefined, new Set()), "WORK-AUTOMATIC-SESSION-REFRESH");
  assert.equal(chooseTaskId("automatic session refresh", undefined, new Set(["WORK-AUTOMATIC-SESSION-REFRESH"])), "WORK-AUTOMATIC-SESSION-REFRESH-2");
  assert.equal(sanitizeTaskId("auth-42"), "AUTH-42");
  assert.throws(() => sanitizeTaskId("../escape"), /Task id/);
});

test("command argument parsing preserves descriptions and messages", () => {
  assert.deepEqual(parseNewArgs("--id AUTH-42 automatic session refresh"), {
    requestedId: "AUTH-42",
    description: "automatic session refresh",
  });
  assert.deepEqual(parseSendArgs("scout-frontend investigate the race exactly"), {
    worker: "scout-frontend",
    message: "investigate the race exactly",
  });
});

test("artifact paths stay project relative", () => {
  const config = normalizeConfig({ artifactRoot: ".agent/tasks" });
  assert.deepEqual(taskArtifactPaths(config, "AUTH-42"), {
    artifactDir: ".agent/tasks/AUTH-42",
    intentPath: ".agent/tasks/AUTH-42/intent.md",
    decisionsPath: ".agent/tasks/AUTH-42/decisions.md",
    contractPath: ".agent/tasks/AUTH-42/contract.yaml",
  });
  assert.throws(() => normalizeConfig({ artifactRoot: "../outside" }), /inside/);
  assert.throws(() => resolveInside("/tmp/project", "../../etc/passwd"), /inside/);
});

test("worker labels and dispatch markers are deterministic", () => {
  const workers = [];
  assert.equal(chooseWorkerLabel("scout", "investigate frontend refresh behavior", workers), "scout-investigate-frontend-refresh");
  workers.push({ label: "scout-investigate-frontend-refresh" });
  assert.equal(chooseWorkerLabel("scout", "investigate frontend refresh behavior", workers), "scout-investigate-frontend-refresh-2");
  assert.equal(dispatchMarker("AUTH-42", "abc"), "[workboard task=AUTH-42 dispatch=abc]");
});

test("registry reconciliation associates dispatches and terminal exits", () => {
  const timestamp = new Date().toISOString();
  const task = {
    id: "AUTH-42",
    description: "refresh",
    phase: "architecture",
    artifactDir: ".agent/tasks/AUTH-42",
    intentPath: ".agent/tasks/AUTH-42/intent.md",
    decisionsPath: ".agent/tasks/AUTH-42/decisions.md",
    contractPath: ".agent/tasks/AUTH-42/contract.yaml",
    openDecisions: [],
    createdAt: timestamp,
    updatedAt: timestamp,
    workers: [{
      dispatchId: "abc",
      label: "scout-refresh",
      role: "scout",
      description: "refresh",
      model: "deepseek/deepseek-v4-flash:high",
      status: "dispatching",
      startedAt: timestamp,
      updatedAt: timestamp,
    }],
  };
  assert.equal(reconcileWorkers(task, [{ id: "refresh-locking", task: `${dispatchMarker("AUTH-42", "abc")} investigate`, status: "running", branch: "side-agent/refresh-locking", tmuxWindowIndex: 3 }]), true);
  assert.equal(task.workers[0].agentId, "refresh-locking");
  assert.equal(task.workers[0].status, "running");
  assert.equal(activeWorkers(task).length, 1);
  assert.equal(reconcileWorkers(task, [], { "refresh-locking": 0 }), true);
  assert.equal(task.workers[0].status, "done");
  assert.equal(activeWorkers(task).length, 0);
});

test("atomic writes replace complete state", async () => {
  const dir = await mkdtemp(join(os.tmpdir(), "workboard-core-test-"));
  try {
    const path = join(dir, "nested", "state.json");
    await atomicWrite(path, `${JSON.stringify(emptyState())}\n`);
    assert.deepEqual(JSON.parse(await readFile(path, "utf8")), emptyState());
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
