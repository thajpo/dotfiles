import { afterEach, describe, it } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { createExtensionJiti } from "./extension-jiti.mjs";

const jiti = createExtensionJiti(import.meta.url);
const { default: workflowStateExtension } = await jiti.import(
  "../pi/extensions/workflow-state/index.ts",
);

const ENV_NAMES = [
  "PI_SUBAGENT_CHILD",
  "PI_SUBAGENT_CHILD_AGENT",
  "PI_SUBAGENT_CHILD_INDEX",
  "PI_WORKFLOW_CONTEXT_AUDIT",
  "PI_WORKFLOW_CONTEXT_AUDIT_RAW",
];

function clearEnv() {
  for (const name of ENV_NAMES) delete process.env[name];
}

afterEach(clearEnv);

function fastPacket() {
  return {
    task_id: "fast-1",
    mode: "fast",
    learning: "off",
    goal: "Fix the fixture",
    constraints: ["No API changes"],
    acceptance: ["fixture test passes"],
  };
}

function fakePi() {
  const handlers = new Map();
  const tools = [];
  const entries = [];
  return {
    handlers,
    tools,
    entries,
    on(name, handler) {
      handlers.set(name, handler);
    },
    registerTool(tool) {
      tools.push(tool);
    },
    appendEntry(customType, data) {
      entries.push({ type: "custom", customType, data });
    },
    getActiveTools() {
      return ["read", "bash", "task_packet"];
    },
  };
}

function fakeContext(sessionFile, branch = []) {
  return {
    sessionManager: {
      getBranch: () => branch,
      getSessionFile: () => sessionFile,
      getSessionId: () => "session-1",
    },
    model: { provider: "test", id: "model" },
    thinkingLevel: "high",
    getContextUsage: () => ({ tokens: 123, contextWindow: 1000, percent: 12.3 }),
  };
}

describe("workflow-state extension runtime", () => {
  it("registers the parent tool and injects only the latest packet", async () => {
    clearEnv();
    const pi = fakePi();
    workflowStateExtension(pi);
    assert.equal(pi.tools.length, 1);
    assert.equal(pi.tools[0].name, "task_packet");

    const ctx = fakeContext(undefined);
    await pi.tools[0].execute("call", { action: "replace", packet: fastPacket() }, undefined, undefined, ctx);
    assert.equal(pi.entries[0].data.schema_version, 1);
    assert.deepEqual(pi.entries[0].data.packet, fastPacket());
    const result = await pi.handlers.get("before_agent_start")(
      {
        prompt: "continue",
        systemPrompt: "base",
        systemPromptOptions: { contextFiles: [], selectedTools: [], skills: [] },
      },
      ctx,
    );
    assert.match(result.systemPrompt, /TASK_PACKET/);
    assert.match(result.systemPrompt, /fast-1/);

    await pi.tools[0].execute("call", { action: "clear" }, undefined, undefined, ctx);
    const cleared = await pi.handlers.get("before_agent_start")(
      {
        prompt: "continue",
        systemPrompt: "base",
        systemPromptOptions: { contextFiles: [], selectedTools: [], skills: [] },
      },
      ctx,
    );
    assert.equal(cleared, undefined);
  });

  it("does not inject a corrupt persisted packet", async () => {
    clearEnv();
    const pi = fakePi();
    workflowStateExtension(pi);
    const corrupt = { ...fastPacket(), raw_transcript: "x".repeat(40_000) };
    const branch = [{
      type: "custom",
      customType: "workflow-task-packet",
      data: { schema_version: 1, packet: corrupt },
    }];
    const ctx = fakeContext(undefined, branch);
    await pi.handlers.get("session_start")({}, ctx);
    const result = await pi.handlers.get("before_agent_start")({
      prompt: "continue",
      systemPrompt: "base",
      systemPromptOptions: { contextFiles: [], selectedTools: [], skills: [] },
    }, ctx);
    assert.equal(result, undefined);
  });

  it("does not expose the tool or inject a forked parent packet in a child", async () => {
    clearEnv();
    process.env.PI_SUBAGENT_CHILD = "1";
    const pi = fakePi();
    workflowStateExtension(pi);
    assert.equal(pi.tools.length, 0);

    const branch = [{
      type: "custom",
      customType: "workflow-task-packet",
      data: { schema_version: 1, packet: fastPacket() },
    }];
    const result = await pi.handlers.get("before_agent_start")(
      {
        prompt: "child assignment",
        systemPrompt: "child-base",
        systemPromptOptions: { contextFiles: [], selectedTools: [], skills: [] },
      },
      fakeContext(undefined, branch),
    );
    assert.equal(result, undefined);
  });

  it("captures one non-raw manifest for the first context event only", async () => {
    clearEnv();
    process.env.PI_WORKFLOW_CONTEXT_AUDIT = "1";
    process.env.PI_SUBAGENT_CHILD = "1";
    process.env.PI_SUBAGENT_CHILD_AGENT = "scout";
    process.env.PI_SUBAGENT_CHILD_INDEX = "2";

    const temp = fs.mkdtempSync(path.join(os.tmpdir(), "workflow-extension-test-"));
    const sessionFile = path.join(temp, "session.jsonl");
    fs.writeFileSync(sessionFile, "{}\n", { mode: 0o600 });
    const preexistingArtifactDir = path.join(temp, "session", "workflow-artifacts");
    fs.mkdirSync(preexistingArtifactDir, { recursive: true });
    fs.chmodSync(preexistingArtifactDir, 0o777);
    const pi = fakePi();
    workflowStateExtension(pi);
    const ctx = fakeContext(sessionFile);

    await pi.handlers.get("before_agent_start")(
      {
        prompt: "map auth",
        systemPrompt: "child-base",
        systemPromptOptions: {
          contextFiles: [{ path: "/repo/AGENTS.md", content: "repo rules" }],
          selectedTools: ["read"],
          skills: [{ name: "example-skill" }],
        },
      },
      ctx,
    );
    await pi.handlers.get("context")({ messages: [{ role: "user", content: "map auth" }] }, ctx);
    await pi.handlers.get("context")({ messages: [{ role: "user", content: "second call" }] }, ctx);

    const artifactDir = path.join(temp, "session", "workflow-artifacts");
    const manifests = fs.readdirSync(artifactDir).filter((name) => name.endsWith(".json"));
    assert.equal(manifests.length, 1);
    const manifest = JSON.parse(fs.readFileSync(path.join(artifactDir, manifests[0]), "utf8"));
    assert.equal(manifest.is_child, true);
    assert.equal(manifest.child_agent, "scout");
    assert.equal(manifest.child_index, 2);
    assert.equal(manifest.submitted_prompt.chars, "map auth".length);
    assert.equal(manifest.submitted_prompt.text, undefined);
    assert.equal(manifest.context_files[0].path, "/repo/AGENTS.md");
    assert.deepEqual(manifest.skill_names, ["example-skill"]);
    assert.equal(manifest.messages[0].content, undefined);
    assert.equal(manifest.task_packet.present, false);
    assert.equal(fs.statSync(artifactDir).mode & 0o777, 0o700);
    assert.equal(fs.statSync(path.join(artifactDir, manifests[0])).mode & 0o777, 0o600);
    assert.equal(pi.entries.filter((entry) => entry.customType === "workflow-context-manifest").length, 0);
    fs.rmSync(temp, { recursive: true, force: true });
  });

  it("refuses a symlinked session artifact directory", async () => {
    clearEnv();
    process.env.PI_WORKFLOW_CONTEXT_AUDIT = "1";
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), "workflow-extension-parent-symlink-"));
    const sessionFile = path.join(temp, "session.jsonl");
    fs.writeFileSync(sessionFile, "{}\n", { mode: 0o600 });
    const outside = path.join(temp, "outside");
    fs.mkdirSync(outside, { mode: 0o700 });
    fs.symlinkSync(outside, path.join(temp, "session"));

    const pi = fakePi();
    workflowStateExtension(pi);
    const ctx = fakeContext(sessionFile);
    await pi.handlers.get("before_agent_start")({
      prompt: "audit",
      systemPrompt: "base",
      systemPromptOptions: { contextFiles: [], selectedTools: [], skills: [] },
    }, ctx);
    await pi.handlers.get("context")({ messages: [{ role: "user", content: "audit" }] }, ctx);
    assert.deepEqual(fs.readdirSync(outside), []);
    fs.rmSync(temp, { recursive: true, force: true });
  });

  it("refuses a symlinked workflow-artifacts directory", async () => {
    clearEnv();
    process.env.PI_WORKFLOW_CONTEXT_AUDIT = "1";
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), "workflow-extension-symlink-"));
    const sessionFile = path.join(temp, "session.jsonl");
    fs.writeFileSync(sessionFile, "{}\n", { mode: 0o600 });
    const sessionArtifactRoot = path.join(temp, "session");
    const outside = path.join(temp, "outside");
    fs.mkdirSync(sessionArtifactRoot, { mode: 0o700 });
    fs.mkdirSync(outside, { mode: 0o700 });
    fs.symlinkSync(outside, path.join(sessionArtifactRoot, "workflow-artifacts"));

    const pi = fakePi();
    workflowStateExtension(pi);
    const ctx = fakeContext(sessionFile);
    await pi.handlers.get("before_agent_start")({
      prompt: "audit",
      systemPrompt: "base",
      systemPromptOptions: { contextFiles: [], selectedTools: [], skills: [] },
    }, ctx);
    await pi.handlers.get("context")({ messages: [{ role: "user", content: "audit" }] }, ctx);
    assert.deepEqual(fs.readdirSync(outside), []);
    fs.rmSync(temp, { recursive: true, force: true });
  });
});
