import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";
import { createExtensionJiti } from "./extension-jiti.mjs";

const jiti = createExtensionJiti(import.meta.url, {
  "@earendil-works/pi-tui": new URL("./pi-tui-stub.mjs", import.meta.url).pathname,
});
const { default: observabilityExtension } = await jiti.import("../pi/extensions/observability/index.ts");
const { ASYNC_DIR } = await jiti.import("../pi/npm/node_modules/pi-subagents/src/shared/types.ts");

function validPacketEntry() {
  return {
    type: "custom",
    customType: "workflow-task-packet",
    data: {
      schema_version: 1,
      packet: {
        task_id: "observe-runtime",
        mode: "fast",
        learning: "off",
        goal: "Inspect a deliberately long instruction that must remain visible instead of being clipped at the overlay edge",
        constraints: ["Keep the Inspector read-only"],
        acceptance: ["The complete instruction is visible"],
      },
    },
  };
}

function theme() {
  return {
    fg(_color, value) { return value; },
    bold(value) { return value; },
  };
}

function harness(mode = "tui", sessionId = "session-1") {
  const commands = new Map();
  const shortcuts = new Map();
  const lifecycle = new Map();
  const eventHandlers = new Map();
  const notifications = [];
  const rendered = [];
  let customCalls = 0;
  let unsubscribed = 0;
  const pi = {
    registerCommand(name, value) { commands.set(name, value); },
    registerShortcut(name, value) { shortcuts.set(name, value); },
    on(name, handler) { lifecycle.set(name, handler); },
    events: {
      on(name, handler) {
        eventHandlers.set(name, handler);
        return () => { unsubscribed += 1; eventHandlers.delete(name); };
      },
    },
  };
  const ctx = {
    mode,
    hasUI: true,
    sessionManager: {
      getSessionId() { return sessionId; },
      getSessionFile() { return undefined; },
      getBranch() { return [validPacketEntry()]; },
    },
    ui: {
      notify(message, level) { notifications.push({ message, level }); },
      async custom(factory, options) {
        customCalls += 1;
        let closed = false;
        const component = factory(
          { requestRender() {}, terminal: { rows: 40 } },
          theme(),
          {},
          () => { closed = true; },
        );
        component.handleInput?.("1");
        rendered.push(component.render(60));
        component.handleInput?.("2");
        rendered.push(component.render(60));
        component.dispose?.();
        assert.equal(options.overlay, true);
        assert.equal(closed, false);
      },
    },
  };
  observabilityExtension(pi);
  lifecycle.get("session_start")?.({}, ctx);
  return { commands, shortcuts, lifecycle, eventHandlers, notifications, rendered, ctx, get customCalls() { return customCalls; }, get unsubscribed() { return unsubscribed; } };
}

test("/observe and Ctrl-I register one TUI Inspector with wrapped task text", async () => {
  const state = harness();
  assert.ok(state.commands.has("observe"));
  assert.ok(state.shortcuts.has("ctrl+i"));
  await state.commands.get("observe").handler("", state.ctx);
  assert.equal(state.customCalls, 1);
  const taskLines = state.rendered[0];
  assert.ok(taskLines.some((line) => line.includes("deliberately long instruction")));
  assert.ok(taskLines.some((line) => line.includes("clipped at the overlay")));
  assert.ok(taskLines.some((line) => line.includes("│edge")));
  assert.ok(taskLines.every((line) => line.length <= 60));
});

test("grouped completion events retain every child in the fleet", async () => {
  const state = harness();
  state.eventHandlers.get("subagent:async-started")?.({
    id: "run-1",
    sessionId: "session-1",
    source: "async",
    agents: ["scout", "reviewer"],
    startedAt: Date.now() - 5_000,
  });
  state.eventHandlers.get("subagent:async-complete")?.({
    id: "run-1",
    sessionId: "session-1",
    source: "async",
    results: [
      { agent: "scout", index: 0, status: "completed", summary: "mapped" },
      { agent: "reviewer", index: 1, status: "failed", summary: "rejected" },
    ],
  });
  await state.commands.get("observe").handler("", state.ctx);
  const fleet = state.rendered[1].join("\n");
  assert.match(fleet, /scout.*complete/);
  assert.match(fleet, /reviewer.*failed/);
  const elapsed = fleet.match(/Elapsed: (\d+)s/);
  assert.ok(elapsed && Number(elapsed[1]) >= 4, fleet);
  assert.doesNotMatch(fleet, /async agent /);
});

test("opening the Inspector does not reconcile or rewrite persisted run state", async () => {
  const runDir = path.join(ASYNC_DIR, `inspector-read-only-${process.pid}-${Date.now()}`);
  const statusPath = path.join(runDir, "status.json");
  fs.mkdirSync(runDir, { recursive: true });
  const serialized = JSON.stringify({
    runId: path.basename(runDir),
    sessionId: "session-1",
    state: "running",
    mode: "single",
    startedAt: 1,
    lastUpdate: 2,
    steps: [{ agent: "scout", status: "running", startedAt: 1 }],
  });
  fs.writeFileSync(statusPath, serialized);
  try {
    const state = harness();
    await state.commands.get("observe").handler("", state.ctx);
    assert.equal(fs.readFileSync(statusPath, "utf8"), serialized);
  } finally {
    fs.rmSync(runDir, { recursive: true, force: true });
  }
});

test("the fleet cap keeps the most recent runs rather than directory order", async () => {
  const sessionId = `inspector-recency-${process.pid}-${Date.now()}`;
  const runDirs = [];
  try {
    for (let index = 0; index < 65; index += 1) {
      const newest = index === 64;
      const name = newest ? `zz-newest-${process.pid}` : `aa-old-${process.pid}-${String(index).padStart(2, "0")}`;
      const runDir = path.join(ASYNC_DIR, name);
      runDirs.push(runDir);
      fs.mkdirSync(runDir, { recursive: true });
      fs.writeFileSync(path.join(runDir, "status.json"), JSON.stringify({
        runId: name,
        sessionId,
        state: "complete",
        mode: "single",
        startedAt: index + 1,
        lastUpdate: index + 1,
        steps: [{ agent: newest ? "newest" : `old-${index}`, status: "complete", startedAt: index + 1, endedAt: index + 1 }],
      }));
    }
    const state = harness("tui", sessionId);
    await state.commands.get("observe").handler("", state.ctx);
    const fleet = state.rendered[1].join("\n");
    assert.match(fleet, /Agent: newest/);
    assert.match(fleet, /Agents: 64/);
    assert.doesNotMatch(fleet, /Agent: old-0\b/);
  } finally {
    for (const runDir of runDirs) fs.rmSync(runDir, { recursive: true, force: true });
  }
});

test("the Inspector bounds per-refresh step detail work to its visible fleet", async () => {
  const sessionId = `inspector-step-cap-${process.pid}-${Date.now()}`;
  const runDir = path.join(ASYNC_DIR, sessionId);
  fs.mkdirSync(runDir, { recursive: true });
  fs.writeFileSync(path.join(runDir, "status.json"), JSON.stringify({
    runId: sessionId,
    sessionId,
    state: "running",
    mode: "parallel",
    startedAt: 1,
    lastUpdate: 2,
    steps: Array.from({ length: 130 }, (_, index) => ({ agent: `agent-${index}`, status: "running", startedAt: 1 })),
  }));
  try {
    const state = harness("tui", sessionId);
    await state.commands.get("observe").handler("", state.ctx);
    assert.match(state.rendered[0].join("\n"), /Fleet detail reads limited to 128 agent steps/);
    assert.match(state.rendered[1].join("\n"), /Agents: 128/);
  } finally {
    fs.rmSync(runDir, { recursive: true, force: true });
  }
});

test("the fleet cap retains an older active run ahead of newer terminal runs", async () => {
  const sessionId = `inspector-active-${process.pid}-${Date.now()}`;
  const runDirs = [];
  try {
    for (let index = 0; index < 65; index += 1) {
      const active = index === 0;
      const name = `active-priority-${process.pid}-${String(index).padStart(2, "0")}`;
      const runDir = path.join(ASYNC_DIR, name);
      runDirs.push(runDir);
      fs.mkdirSync(runDir, { recursive: true });
      fs.writeFileSync(path.join(runDir, "status.json"), JSON.stringify({
        runId: name,
        sessionId,
        state: active ? "running" : "complete",
        mode: "single",
        startedAt: index + 1,
        lastUpdate: index + 1,
        steps: [{ agent: active ? "long-running" : `terminal-${index}`, status: active ? "running" : "complete", startedAt: index + 1 }],
      }));
    }
    const state = harness("tui", sessionId);
    await state.commands.get("observe").handler("", state.ctx);
    const fleet = state.rendered[1].join("\n");
    assert.match(fleet, /Agent: long-running/);
    assert.match(fleet, /Status: running/);
    assert.match(state.rendered[0].join("\n").replaceAll("│", " "), /active runs are\s+prioritized/);
  } finally {
    for (const runDir of runDirs) fs.rmSync(runDir, { recursive: true, force: true });
  }
});

test("/observe rejects RPC mode instead of invoking a TUI-only custom component", async () => {
  const state = harness("rpc");
  await state.commands.get("observe").handler("", state.ctx);
  assert.equal(state.customCalls, 0);
  assert.match(state.notifications.at(-1).message, /interactive Pi TUI/);
  state.lifecycle.get("session_shutdown")?.();
  assert.equal(state.unsubscribed, 7);
});
