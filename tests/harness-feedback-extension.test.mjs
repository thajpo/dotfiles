import { afterEach, test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { createExtensionJiti } from "./extension-jiti.mjs";

const jiti = createExtensionJiti(import.meta.url);
const extension = await jiti.import("../pi/extensions/harness-feedback/index.ts");
const originalEnv = new Map();

function setEnv(name, value) {
  if (!originalEnv.has(name)) originalEnv.set(name, process.env[name]);
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}

afterEach(() => {
  for (const [name, value] of originalEnv) {
    if (value === undefined) delete process.env[name];
    else process.env[name] = value;
  }
  originalEnv.clear();
});

test("harness_feedback rejects non-harness feedback kinds", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pi-harness-feedback-kind-"));
  try {
    setEnv("PI_CODING_AGENT_DIR", path.join(root, "agent"));
    const tools = [];
    extension.default({ registerTool(tool) { tools.push(tool); } });
    const tool = tools.find((candidate) => candidate.name === "harness_feedback");
    await assert.rejects(
      tool.execute("call", { kind: "routine-status", title: "Done" }, undefined, undefined, { cwd: root }),
      /kind='harness-improvement'/,
    );
    assert.equal(fs.existsSync(path.join(root, "agent", "feedback")), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("harness_feedback writes one bounded normalized central record", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pi-harness-feedback-extension-"));
  try {
    const agentDir = path.join(root, "agent");
    const repository = path.join(root, "project");
    fs.mkdirSync(repository);
    setEnv("PI_CODING_AGENT_DIR", agentDir);
    setEnv("PI_HARNESS_PROJECT_ID", "a".repeat(64));
    setEnv("PI_HARNESS_REPOSITORY", repository);
    setEnv("PI_AGENT_FEEDBACK_RAW", undefined);

    const tools = [];
    extension.default({ registerTool(tool) { tools.push(tool); } });
    const tool = tools.find((candidate) => candidate.name === "harness_feedback");
    assert.ok(tool);
    const result = await tool.execute("call-1", {
      kind: "harness-improvement",
      title: "Repeated \u001b]52;c;Y2xpcGJvYXJk\u0007 setup ceremony",
      evidence: ["The same setup was repeated twice"],
      recommendation: "😀".repeat(5000),
      decision_needed: false,
    }, undefined, undefined, { cwd: repository });

    assert.match(result.content[0].text, /^Recorded harness feedback hfb-/);
    const files = fs.readdirSync(path.join(agentDir, "feedback", "records"));
    assert.equal(files.length, 1);
    const record = JSON.parse(fs.readFileSync(path.join(agentDir, "feedback", "records", files[0]), "utf8"));
    assert.equal(record.form.schema, "agent-feedback.v1");
    assert.equal(record.form.kind, "harness-improvement");
    assert.doesNotMatch(record.form.title, /[\u001b\u0007]/);
    assert.ok(Buffer.byteLength(record.form.recommendation, "utf8") <= 4096);
    assert.equal(record.source.projectId, "a".repeat(64));
    assert.equal(record.source.repository, repository);
    assert.equal(record.outcome, "unreviewed");
    assert.equal("raw" in record, false);

    const duplicate = await tool.execute("call-2", {
      kind: "harness-improvement",
      title: "A second observation",
    }, undefined, undefined, { cwd: repository });
    assert.equal(duplicate.details.duplicate, true);
    assert.equal(fs.readdirSync(path.join(agentDir, "feedback", "records")).length, 1);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
