import { afterEach, describe, it } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { createJiti } from "../pi/npm/node_modules/jiti/lib/jiti.mjs";

const jiti = createJiti(import.meta.url);
const supervisor = await jiti.import(
  "../pi/npm/node_modules/pi-subagents/src/intercom/native-supervisor-channel.ts",
);

const ENV_NAMES = [
  "PI_CODING_AGENT_DIR",
  "PI_AGENT_FEEDBACK_RAW",
  "PI_SECRETARY_PROJECT_ID",
  "PI_WORKSTREAM_PROJECT_ID",
  "PI_WORKSTREAM_ID",
  "PI_SUBAGENT_SUPERVISOR_CHANNEL_DIR",
  "PI_SUBAGENT_RUN_ID",
  "PI_SUBAGENT_CHILD_AGENT",
  "PI_SUBAGENT_CHILD_INDEX",
  "PI_SUBAGENT_ORCHESTRATOR_SESSION_ID",
  "PI_SUBAGENT_ORCHESTRATOR_TARGET",
  "PI_SUBAGENT_INTERCOM_SESSION_NAME",
];
const originalEnv = Object.fromEntries(ENV_NAMES.map((name) => [name, process.env[name]]));
const temporaryRoots = [];

function restoreEnv() {
  for (const name of ENV_NAMES) {
    if (originalEnv[name] === undefined) delete process.env[name];
    else process.env[name] = originalEnv[name];
  }
  for (const root of temporaryRoots.splice(0)) fs.rmSync(root, { recursive: true, force: true });
}

afterEach(restoreEnv);

function fakePi() {
  const tools = [];
  const messages = [];
  return {
    tools,
    messages,
    events: { emit() {} },
    getAllTools: () => tools,
    registerTool(tool) { tools.push(tool); },
    sendMessage(message) { messages.push(message); },
  };
}

function parentState(ctx) {
  return {
    currentSessionId: "parent-session",
    lastUiContext: ctx,
    foregroundControls: new Map(),
    foregroundRuns: new Map(),
    asyncJobs: new Map(),
  };
}

function setChildMetadata(runId, agent, index, channelDir) {
  process.env.PI_SUBAGENT_SUPERVISOR_CHANNEL_DIR = channelDir;
  process.env.PI_SUBAGENT_RUN_ID = runId;
  process.env.PI_SUBAGENT_CHILD_AGENT = agent;
  process.env.PI_SUBAGENT_CHILD_INDEX = String(index);
  process.env.PI_SUBAGENT_ORCHESTRATOR_SESSION_ID = "parent-session";
  process.env.PI_SUBAGENT_ORCHESTRATOR_TARGET = "secretary";
  process.env.PI_SUBAGENT_INTERCOM_SESSION_NAME = `${agent}-${index}`;
}

function recordsRoot(agentDir) {
  return path.join(agentDir, "feedback", "records");
}

async function waitFor(predicate, message) {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  assert.fail(message);
}

describe("native supervisor feedback persistence", () => {
  it("persists progress feedback in Pi storage and records disposition", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "pi-feedback-runtime-"));
    temporaryRoots.push(root);
    const agentDir = path.join(root, "agent");
    const projectRoot = path.join(root, "project");
    fs.mkdirSync(projectRoot, { recursive: true });
    process.env.PI_CODING_AGENT_DIR = agentDir;
    process.env.PI_SECRETARY_PROJECT_ID = "a".repeat(64);

    const runId = `feedback-progress-${Date.now()}`;
    const channelDir = supervisor.resolveSupervisorChannelDir(runId, "scout", 0);
    setChildMetadata(runId, "scout", 0, channelDir);
    const child = fakePi();
    supervisor.registerNativeSupervisorClient(child);
    const contact = child.tools.find((tool) => tool.name === "contact_supervisor");
    assert.ok(contact);
    await contact.execute("call", {
      reason: "progress_update",
      message: 'AGENT_FEEDBACK {"AGENT_FEEDBACK":{"schema":"agent-feedback.v1","kind":"risk","title":"Fixture risk","evidence":["bounded evidence"]}}',
    }, new AbortController().signal);

    const files = fs.readdirSync(recordsRoot(agentDir));
    assert.equal(files.length, 1);
    const recordPath = path.join(recordsRoot(agentDir), files[0]);
    const initial = JSON.parse(fs.readFileSync(recordPath, "utf8"));
    assert.equal(initial.schemaVersion, 1);
    assert.equal(initial.reason, "progress_update");
    assert.equal(initial.form.schema, "agent-feedback.v1");
    assert.equal(initial.form.kind, "risk");
    assert.equal(initial.source.agent, "scout");
    assert.equal(initial.source.runId, runId);
    assert.equal(initial.source.projectId, "a".repeat(64));
    assert.equal(initial.raw, undefined);
    assert.equal(fs.statSync(path.dirname(recordPath)).mode & 0o777, 0o700);
    assert.equal(fs.statSync(recordPath).mode & 0o777, 0o600);
    assert.equal(fs.existsSync(path.join(projectRoot, "feedback")), false);

    const parent = fakePi();
    const parentContext = { sessionManager: { getSessionId: () => "parent-session" } };
    const channel = supervisor.createNativeSupervisorChannel(parent, parentState(parentContext));
    channel.start();
    assert.equal(parent.messages.length, 1);
    assert.match(parent.messages[0].content, /Feedback record:/);
    const recordId = parent.messages[0].details.id;
    const parentTool = parent.tools.find((tool) => tool.name === "subagent_supervisor");
    assert.ok(parentTool);
    await parentTool.execute("review", { action: "review", feedbackId: recordId, outcome: "accepted", message: "Useful risk." });
    const reviewed = JSON.parse(fs.readFileSync(recordPath, "utf8"));
    assert.equal(reviewed.lifecycle, "reviewed");
    assert.equal(reviewed.outcome, "accepted");
    channel.dispose();
  });

  it("persists an interview form and parent reply outcome", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "pi-feedback-interview-"));
    temporaryRoots.push(root);
    process.env.PI_CODING_AGENT_DIR = path.join(root, "agent");
    const runId = `feedback-interview-${Date.now()}`;
    const channelDir = supervisor.resolveSupervisorChannelDir(runId, "worker", 1);
    setChildMetadata(runId, "worker", 1, channelDir);
    const child = fakePi();
    supervisor.registerNativeSupervisorClient(child);
    const contact = child.tools.find((tool) => tool.name === "contact_supervisor");
    assert.ok(contact);

    const parent = fakePi();
    const parentContext = { sessionManager: { getSessionId: () => "parent-session" } };
    const channel = supervisor.createNativeSupervisorChannel(parent, parentState(parentContext));
    channel.start();
    const pending = contact.execute("call", {
      reason: "interview_request",
      message: "Need a decision.",
      interview: {
        schema: "agent-feedback.v1",
        kind: "decision-needed",
        title: "Choose a boundary",
        want: "Allow the bounded read",
        blocked_by: "No approved route",
        why: "Validation needs the artifact",
        evidence: ["fixture evidence"],
        options: [{ label: "Approve", tradeoff: "exposes one file" }],
        recommendation: "Approve only that file",
        decision_needed: true,
      },
    }, new AbortController().signal);
    await waitFor(() => parent.messages.length === 1, "parent did not receive interview request");
    const requestId = parent.messages[0].details.id;
    const parentTool = parent.tools.find((tool) => tool.name === "subagent_supervisor");
    assert.ok(parentTool);
    await parentTool.execute("reply", {
      action: "reply",
      replyTo: requestId,
      message: '{"approved":true}',
      outcome: "accepted",
    });
    const result = await pending;
    assert.equal(result.details.outcome, "accepted");
    const files = fs.readdirSync(recordsRoot(process.env.PI_CODING_AGENT_DIR));
    assert.equal(files.length, 1);
    const record = JSON.parse(fs.readFileSync(path.join(recordsRoot(process.env.PI_CODING_AGENT_DIR), files[0]), "utf8"));
    assert.equal(record.form.kind, "decision-needed");
    assert.equal(record.lifecycle, "reviewed");
    assert.equal(record.outcome, "accepted");
    assert.equal(record.response.message, '{"approved":true}');
    channel.dispose();
  });
});
