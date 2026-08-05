import { afterEach, describe, it } from "node:test";
import assert from "node:assert/strict";
import { createJiti } from "../pi/npm/node_modules/jiti/lib/jiti.mjs";

const jiti = createJiti(import.meta.url);
const { default: secretaryExtension } = await jiti.import(
  "../pi/extensions/secretary/index.ts",
);

const ENV_NAMES = [
  "PI_SECRETARY_READ_ONLY",
  "PI_SECRETARY_PROJECT_ID",
  "PI_SECRETARY_ALIAS",
  "PI_SECRETARY_CONTROL",
  "TMUX",
];
const originalEnv = Object.fromEntries(ENV_NAMES.map((name) => [name, process.env[name]]));

afterEach(() => {
  for (const name of ENV_NAMES) {
    if (originalEnv[name] === undefined) delete process.env[name];
    else process.env[name] = originalEnv[name];
  }
});

function fakePi() {
  const handlers = new Map();
  const tools = [];
  const calls = [];
  return {
    handlers,
    tools,
    calls,
    on(name, handler) { handlers.set(name, handler); },
    registerTool(tool) { tools.push(tool); },
    getAllTools: () => tools,
    async exec(command, args) {
      calls.push({ command, args });
      if (command === "tmux") return { code: 0, stdout: "", stderr: "" };
      return {
        code: 0,
        stdout: JSON.stringify({ backend: "tmux", tmuxSession: "pi-test-session", workstreamId: "ws-test" }),
        stderr: "",
      };
    },
  };
}

describe("secretary workstream handoff", () => {
  it("uses UI approval, launches the current project, and focuses the worker session", async () => {
    process.env.PI_SECRETARY_READ_ONLY = "1";
    process.env.PI_SECRETARY_PROJECT_ID = "b".repeat(64);
    process.env.PI_SECRETARY_ALIAS = "dotfiles";
    process.env.PI_SECRETARY_CONTROL = "/tmp/pi-secretary-control.py";
    process.env.TMUX = "/tmp/pi-test-tmux,123,1";
    const pi = fakePi();
    const approvals = [];
    secretaryExtension(pi);
    const tool = pi.tools.find((candidate) => candidate.name === "secretary_create_workstream");
    assert.ok(tool);
    const result = await tool.execute("call", {
      title: "Add feedback persistence",
      brief: "Persist bounded agent feedback outside project roots.",
      role: "feature",
    }, new AbortController().signal, undefined, {
      cwd: "/home/j/dotfiles",
      hasUI: true,
      ui: { confirm: async (title, body) => { approvals.push({ title, body }); return true; } },
    });
    assert.equal(approvals.length, 1);
    assert.equal(approvals[0].title, "Approve workstream creation?");
    assert.match(approvals[0].body, /dotfiles/);
    assert.match(approvals[0].body, /Add feedback persistence/);
    assert.deepEqual(pi.calls[0].args.slice(0, 2), ["/tmp/pi-secretary-control.py", "promote"]);
    assert.deepEqual(pi.calls[1], { command: "tmux", args: ["switch-client", "-t", "=pi-test-session"] });
    assert.match(result.content[0].text, /Focused worker session pi-test-session/);
  });

  it("rejects workstream creation without an interactive UI", async () => {
    process.env.PI_SECRETARY_READ_ONLY = "1";
    process.env.PI_SECRETARY_PROJECT_ID = "c".repeat(64);
    process.env.PI_SECRETARY_ALIAS = "dotfiles";
    process.env.PI_SECRETARY_CONTROL = "/tmp/pi-secretary-control.py";
    const pi = fakePi();
    secretaryExtension(pi);
    const tool = pi.tools.find((candidate) => candidate.name === "secretary_create_workstream");
    await assert.rejects(
      tool.execute("call", { title: "No UI", brief: "Do not create", role: "feature" }, new AbortController().signal, undefined, { cwd: "/repo", hasUI: false, ui: {} }),
      /interactive secretary session/,
    );
    assert.equal(pi.calls.length, 0);
  });
});
