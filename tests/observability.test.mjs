import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  boundedText,
  cycleAgentIndex,
  extractActivePacket,
  explicitSessionMessage,
  formatTaskPacket,
  normalizeMessages,
  projectInspectorState,
  reduceInspectorKey,
  statusGlyph,
} from "../pi/extensions/observability/core.mjs";

function packetEntry(packet) {
  return {
    type: "custom",
    customType: "workflow-task-packet",
    data: { schema_version: 1, packet },
  };
}

function packet() {
  return {
    task_id: "observe-1",
    mode: "build",
    learning: "light",
    intended_behavior: "Show explicit agent state",
    unchanged_behavior: "Do not expose hidden reasoning",
    affected_surfaces: ["Pi TUI"],
    decisions: ["Read-only first"],
    acceptance: ["Inspector opens"],
    goal: "Implement the inspector",
  };
}

describe("observability core", () => {
  it("reconstructs the newest packet and fails closed on invalid state", () => {
    assert.equal(extractActivePacket([]), null);
    assert.equal(extractActivePacket([packetEntry(packet())])?.task_id, "observe-1");
    assert.equal(extractActivePacket([packetEntry(packet()), { type: "custom", customType: "workflow-task-packet-clear" }]), null);
    assert.equal(extractActivePacket([packetEntry(packet()), packetEntry({ task_id: "bad" })]), null);
  });

  it("bounds and redacts explicit display text", () => {
    const text = boundedText("Bearer abcdefghijklmnop secret=super-secret-value sk-test_123456789 npm_12345678901234567890");    assert.doesNotMatch(text, /abcdefghijklmnop/);
    assert.doesNotMatch(text, /super-secret-value/);
    assert.doesNotMatch(text, /sk-test_123456789/);
    assert.doesNotMatch(text, /npm_12345678901234567890/);
    assert.ok(boundedText("x".repeat(100), 20).length <= 20);
  });

  it("formats task packets without serializing unknown raw content", () => {
    const lines = formatTaskPacket(packet());
    assert.ok(lines.some((line) => line.includes("Mode: build")));
    assert.ok(lines.some((line) => line.includes("Acceptance:")));
    assert.ok(lines.every((line) => !line.includes("super-secret-value")));
  });

  it("projects every run step and nested child into a selectable fleet", () => {
    const state = projectInspectorState({
      packet: packet(),
      runs: [{
        id: "run-1",
        state: "running",
        mode: "parallel",
        startedAt: 10,
        lastUpdate: 20,
        steps: [
          { agent: "scout", status: "running", currentTool: "read", currentPath: "/repo/a.ts" },
          { agent: "reviewer", status: "failed", error: "test failed" },
        ],
        nestedChildren: [{
          id: "nested-1",
          parentRunId: "run-1",
          parentStepIndex: 0,
          depth: 1,
          state: "complete",
          agent: "worker",
          startedAt: 11,
          lastUpdate: 19,
        }],
      }],
      messages: [{
        id: "brief-0",
        kind: "instruction",
        source: "async",
        runId: "run-1",
        index: 0,
        agent: "scout",
        ts: 10,
        text: "Inspect the relevant files",
      }, {
        id: "result-1",
        kind: "result",
        source: "artifact",
        runId: "run-1",
        index: 0,
        agent: "scout",
        ts: 30,
        text: "Found the entry point",
      }],
    });
    assert.deepEqual(state.agents.map((agent) => agent.agent).sort(), ["reviewer", "scout", "worker"]);
    assert.equal(state.agents.find((agent) => agent.agent === "scout")?.task, "Inspect the relevant files");
    assert.equal(state.agents.find((agent) => agent.agent === "worker")?.parentRunId, "run-1");
    assert.ok(state.messages.some((message) => message.kind === "result" && message.text.includes("Found")));
    assert.ok(state.messages.some((message) => message.kind === "failure" && message.agent === "reviewer"));
  });

  it("filters session records to explicit non-reasoning content", () => {
    assert.equal(explicitSessionMessage({ message: { role: "system", content: "secret system prompt" } }), null);
    assert.deepEqual(explicitSessionMessage({ message: { role: "assistant", content: [{ type: "thinking", text: "private reasoning" }, { type: "text", text: "public result" }] } }), { role: "assistant", text: "public result" });
    assert.deepEqual(explicitSessionMessage({ role: "toolResult", toolName: "read", content: "secret tool output" }), { role: "tool", text: "[tool result: read]" });
    assert.deepEqual(explicitSessionMessage({ role: "assistant", content: [{ type: "text", text: "one" }, { type: "text", text: "two" }] }), { role: "assistant", text: "one\ntwo" });
  });

  it("handles malformed sources and keeps explicit messages bounded", () => {
    const state = projectInspectorState({ runs: [null, "bad", { id: "empty", steps: "bad" }], messages: [null, { text: "ok", kind: "message" }] });
    assert.equal(state.agents.length, 1);
    assert.equal(state.messages.filter((message) => message.text === "ok").length, 1);
    assert.deepEqual(projectInspectorState(null).agents, []);
  });

  it("supports inspector navigation and status glyphs", () => {
    assert.deepEqual(reduceInspectorKey("1"), { type: "tab", tab: "task" });
    assert.deepEqual(reduceInspectorKey("a"), { type: "all-messages" });
    assert.deepEqual(reduceInspectorKey("v"), { type: "transcript" });
    assert.deepEqual(reduceInspectorKey("escape"), { type: "close" });
    assert.equal(cycleAgentIndex([{ key: "a" }, { key: "b" }], 0, -1), 1);
    assert.equal(cycleAgentIndex([{ key: "a" }, { key: "b" }], 1, 1), 0);
    assert.equal(statusGlyph("running"), "●");
    assert.equal(statusGlyph("failed"), "✗");
    assert.equal(statusGlyph("unknown"), "?");
    assert.equal(normalizeMessages([{ id: "1", kind: "instruction", text: "brief" }, { id: "1", kind: "result", text: "duplicate" }]).length, 1);
  });
});
