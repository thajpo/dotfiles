import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { test } from "bun:test";

const ROOT = new URL("../../", import.meta.url).pathname;
const EXTENSION = new URL("./pisec.ts", import.meta.url).href;
type JsonRecord = Record<string, unknown>;

function asRecord(value: unknown): JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError("probe result must be an object");
  return value as JsonRecord;
}

function stringValue(record: JsonRecord, key: string): string {
  const value = record[key];
  if (typeof value !== "string") throw new TypeError(`probe field ${key} must be a string`);
  return value;
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) throw new TypeError("probe field must be an array");
  return value.map(item => {
    if (typeof item !== "string") throw new TypeError("probe array item must be a string");
    return item;
  });
}

function runProbe(source: string): JsonRecord {
  const result = spawnSync("bun", ["-e", source], {
    cwd: ROOT,
    env: { ...process.env },
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return asRecord(JSON.parse(result.stdout) as unknown);
}

test("Pisec extension is inert without a Pisec role", () => {
  const output = runProbe(`
    const records = { tools: [], events: [] };
    for (const key of Object.keys(process.env)) if (key.startsWith("PISEC_")) delete process.env[key];
    const pi = {
      zod: {},
      registerTool(value) { records.tools.push(value.name); },
      on(value) { records.events.push(value); },
      setLabel() {},
      setActiveTools() { throw new Error("inactive extension activated tools"); },
    };
    // Child process isolates environment captured at module evaluation.
    const module = await import(${JSON.stringify(EXTENSION)});
    console.log(JSON.stringify(records));
  `);
  assert.deepEqual(output, { tools: [], events: [] });
});

test("secretary exposes the exact semantic surface and UI-bound approval", () => {
  const output = runProbe(`
    const records = { tools: [], events: [], labels: [] };
    const chain = () => ({ min: chain, max: chain, optional: chain, int: chain, url: chain });
    const zod = { string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain };
    Object.assign(process.env, {
      PISEC_ROLE: "secretary",
      PISEC_RUNTIME_SOCKET: "/tmp/runtime.sock",
      PISEC_SECRETARY_SOCKET: "/tmp/secretary.sock",
      PISEC_RUNTIME_TOKEN: "t".repeat(48),
      PISEC_WORKSTREAM_ID: "ws_" + "a".repeat(32),
      PISEC_RUNTIME_INSTANCE_ID: "instance",
      PISEC_SURFACE_ID: "w1:p1",
    });
    const pi = {
      zod,
      registerTool(value) { records.tools.push(value); },
      on(value) { records.events.push(value); },
      setLabel(value) { records.labels.push(value); },
      setActiveTools() { return Promise.resolve(); },
    };
    // Child process isolates environment captured at module evaluation.
    const module = await import(${JSON.stringify(EXTENSION)} + "?secretary=" + Date.now());
    module.default(pi);
    const create = records.tools.find(value => value.name === "pisec_create_workstream");
    const scope = {
      operationId: "op_" + "a".repeat(32),
      projectId: "prj_" + "b".repeat(32),
      workstreamId: "ws_" + "a".repeat(32),
      title: "Title",
      purpose: "Purpose",
      brief: "Full brief",
      harnessId: "omp",
      workspaceAdapterId: "herdr",
      executionProfile: "worker-default",
      targetRef: "main",
      baseCommitOid: "a".repeat(40),
      branchName: "pisec/ws_" + "a".repeat(32) + "/work",
      worktreePath: "/tmp/work",
      agentName: "pisec-agent",
      externalDomains: [],
      effects: ["create"],
      nonEffects: ["push"],
    };
    const refused = await create.execute("id", { approval_scope: scope }, undefined, undefined, { hasUI: false });
    console.log(JSON.stringify({
      tools: records.tools.map(value => value.name),
      events: records.events,
      label: records.labels[0],
      approval: create.approval(scope),
      refused,
    }));
  `);
  const tools = stringArray(output.tools);
  const events = stringArray(output.events);
  const approval = asRecord(output.approval);
  const refused = asRecord(output.refused);
  const content = refused.content;
  if (!Array.isArray(content) || content.length === 0) throw new TypeError("refusal content is missing");
  const refusalText = stringValue(asRecord(content[0]), "text");
  assert.deepEqual(tools, [
    "pisec_project_activity",
    "pisec_report_secretary_issue",
    "pisec_list_issues",
    "pisec_inspect_issue",
    "pisec_add_issue_context",
    "pisec_verify_issue",
    "pisec_project_status",
    "pisec_git_status",
    "pisec_push_branch",
    "pisec_inspect_workstream_changes",
    "pisec_prepare_workstream_merge",
    "pisec_merge_workstream",
    "pisec_list_workstreams",
    "pisec_inspect_workstream",
    "pisec_prepare_workstream",
    "pisec_create_workstream",
    "pisec_send_workstream",
    "pisec_focus_workstream",
    "pisec_complete_workstream",
    "pisec_retire_workstream",
    "pisec_list_decisions",
    "pisec_record_decision",
    "pisec_list_coordination_requests",
    "pisec_inspect_coordination_request",
    "pisec_answer_coordination_request",
    "pisec_resolve_decision",
    "pisec_list_worker_research_requests",
    "pisec_inspect_worker_research",
    "pisec_claim_worker_research",
    "pisec_request_worker_research_context",
    "pisec_answer_worker_research",
    "pisec_decline_worker_research",
  ]);
  assert.equal(stringValue(output, "label"), "Pisec Secretary");
  assert.equal(approval.tier, "exec");
  assert.equal(approval.policy, "prompt");
  const approvalReason = stringValue(approval, "reason");
  for (const line of [
    "operation id: op_" + "a".repeat(32),
    "project id: prj_" + "b".repeat(32),
    "workstream id: ws_" + "a".repeat(32),
    "title: Title",
    "purpose: Purpose",
    "full brief: Full brief",
    "harness adapter: omp",
    "workspace adapter: herdr",
    "execution profile: worker-default",
    "target ref: main",
    "base commit OID: " + "a".repeat(40),
    "branch: pisec/ws_" + "a".repeat(32) + "/work",
    "checkout path: /tmp/work",
    "agent name: pisec-agent",
    "exact external domains: (empty)",
    "effects: create",
    "non-effects: push",
  ]) assert.ok(approvalReason.includes(line), line);
  assert.equal(refused.isError, true);
  assert.match(refusalText, /interactive approval UI/);
  assert.ok(events.includes("session_shutdown"));
});

test("first mate exposes the exact fleet surface", () => {
  const output = runProbe(`
    const records = { tools: [], events: [], labels: [] };
    const chain = () => ({ min: chain, max: chain, optional: chain, int: chain, url: chain });
    const zod = { string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain };
    Object.assign(process.env, {
      PISEC_ROLE: "first_mate",
      PISEC_RUNTIME_SOCKET: "/tmp/runtime.sock",
      PISEC_FLEET_SOCKET: "/tmp/fleet.sock",
      PISEC_RUNTIME_TOKEN: "t".repeat(48),
      PISEC_WORKSTREAM_ID: "ws_" + "a".repeat(32),
      PISEC_RUNTIME_INSTANCE_ID: "instance",
      PISEC_SURFACE_ID: "w1:p1",
    });
    const pi = {
      zod,
      registerTool(value) { records.tools.push(value.name); },
      on(value) { records.events.push(value); },
      setLabel(value) { records.labels.push(value); },
      setActiveTools() { return Promise.resolve(); },
    };
    const module = await import(${JSON.stringify(EXTENSION)} + "?first-mate=" + Date.now());
    module.default(pi);
    console.log(JSON.stringify(records));
  `);
  assert.deepEqual(stringArray(output.tools), [
    "pisec_fleet_list_access_grants",
    "pisec_fleet_inspect_access_grant",
    "pisec_fleet_prepare_access_grant",
    "pisec_fleet_apply_access_grant",
    "pisec_fleet_prepare_access_revoke",
    "pisec_fleet_apply_access_revoke",
    "pisec_fleet_list_issues",
    "pisec_fleet_inspect_issue",
    "pisec_fleet_add_issue_context",
    "pisec_fleet_acknowledge_issue",
    "pisec_fleet_resolve_issue",
    "pisec_fleet_status",
    "pisec_fleet_events",
    "pisec_fleet_send_secretary",
    "pisec_fleet_list_workstreams",
    "pisec_fleet_inspect_workstream",
    "pisec_fleet_git_changes",
    "pisec_fleet_prepare_workstream",
    "pisec_fleet_create_worker",
    "pisec_fleet_prepare_merge",
    "pisec_fleet_merge_workstream",
  ]);
  assert.equal(stringArray(output.labels)[0], "Pisec First Mate");
  assert.ok(stringArray(output.events).includes("session_shutdown"));
});

test("worker registers runtime handling without secretary tools", () => {
  const output = runProbe(`
    const records = { tools: [], events: [], labels: [] };
    const chain = () => ({ min: chain, max: chain, optional: chain, int: chain, url: chain });
    const zod = { string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain };
    Object.assign(process.env, {
      PISEC_ROLE: "worker",
      PISEC_RUNTIME_SOCKET: "/tmp/runtime.sock",
      PISEC_RUNTIME_TOKEN: "t".repeat(48),
      PISEC_WORKSTREAM_ID: "ws_" + "a".repeat(32),
      PISEC_RUNTIME_INSTANCE_ID: "instance",
      PISEC_SURFACE_ID: "w1:p1",
    });
    const pi = {
      zod,
      registerTool(value) { records.tools.push(value.name); },
      on(value) { records.events.push(value); },
      setLabel(value) { records.labels.push(value); },
      setActiveTools() { return Promise.resolve(); },
    };
    // Child process isolates environment captured at module evaluation.
    const module = await import(${JSON.stringify(EXTENSION)} + "?worker=" + Date.now());
    module.default(pi);
    console.log(JSON.stringify(records));
  `);
  const tools = stringArray(output.tools);
  const events = stringArray(output.events);
  assert.deepEqual(tools, [
    "pisec_checkpoint_workstream",
    "pisec_request_help",
    "pisec_request_coordination",
    "pisec_list_coordination",
    "pisec_inspect_coordination",
    "pisec_report_issue",
    "pisec_list_issues",
    "pisec_inspect_issue",
    "pisec_add_issue_context",
    "pisec_verify_issue",
    "pisec_show_task_packet",
    "pisec_request_secretary_research",
    "pisec_check_secretary_research",
    "pisec_inspect_secretary_research",
    "pisec_add_secretary_research_context",
    "pisec_acknowledge_secretary_research",
  ]);
  const labels = stringArray(output.labels);
  assert.equal(labels[0], "Pisec Worker");
  assert.ok(events.includes("session_start"));
  assert.ok(events.includes("before_agent_start"));
  assert.ok(events.includes("agent_start"));
  assert.ok(events.includes("session_shutdown"));
});

test("only the root UI session reports idle-working-idle lifecycle", () => {
  const output = runProbe(`
    // The extension must load after the probe-specific environment is installed.
    const { rm } = await import("node:fs/promises");
    const socketPath = "/tmp/pisec-extension-lifecycle-" + process.pid + "-" + Date.now() + ".sock";
    await rm(socketPath, { force: true });
    const reports = [];
    const server = Bun.listen({
      unix: socketPath,
      socket: {
        data(socket, data) {
          const request = JSON.parse(data.toString().trim());
          reports.push(request.payload);
          socket.write(JSON.stringify({ requestId: request.requestId, ok: true, result: { accepted: true } }) + "\\n");
          socket.end();
        },
      },
    });
    const handlers = {};
    const chain = () => ({ min: chain, max: chain, optional: chain, int: chain, url: chain });
    const zod = { string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain };
    Object.assign(process.env, {
      PISEC_ROLE: "secretary",
      PISEC_RUNTIME_SOCKET: socketPath,
      PISEC_SECRETARY_SOCKET: socketPath,
      PISEC_RUNTIME_TOKEN: "t".repeat(48),
      PISEC_WORKSTREAM_ID: "ws_" + "a".repeat(32),
      PISEC_RUNTIME_INSTANCE_ID: "instance",
      PISEC_SURFACE_ID: "w1:p1",
    });
    const pi = {
      zod,
      registerTool() {},
      on(name, handler) { (handlers[name] ??= []).push(handler); },
      setLabel() {},
      setActiveTools() { return Promise.resolve(); },
    };
    const module = await import(${JSON.stringify(EXTENSION)} + "?lifecycle=" + Date.now());
    module.default(pi);
    const child = { hasUI: false, sessionManager: { getSessionFile() {} }, ui: { notify() {} } };
    await handlers.session_start[0]({}, child);
    await handlers.agent_start[0]({}, child);
    await handlers.agent_end[0]({}, child);
    const root = { hasUI: true, sessionManager: { getSessionFile() {} }, ui: { notify() {} } };
    await handlers.session_start[0]({}, root);
    await handlers.agent_start[0]({}, root);
    await handlers.agent_end[0]({}, root);
    server.stop(true);
    await rm(socketPath, { force: true });
    console.log(JSON.stringify({ states: reports.map(report => report.state), events: reports.map(report => report.event) }));
  `);
  assert.deepEqual(output.states, ["idle", "working", "idle"]);
  assert.deepEqual(output.events, ["session_start", "lifecycle", "lifecycle"]);
});

test("failed tool telemetry is bounded and excludes tool output", () => {
  const output = runProbe(`
    const { rm } = await import("node:fs/promises");
    const socketPath = "/tmp/pisec-extension-failure-" + process.pid + "-" + Date.now() + ".sock";
    await rm(socketPath, { force: true });
    const requests = [];
    const server = Bun.listen({
      unix: socketPath,
      socket: {
        data(socket, data) {
          const request = JSON.parse(data.toString().trim());
          requests.push(request);
          socket.write(JSON.stringify({ requestId: request.requestId, ok: true, result: { accepted: true } }) + "\\n");
          socket.end();
        },
      },
    });
    const handlers = {};
    const chain = () => ({ min: chain, max: chain, optional: chain, int: chain, url: chain });
    const zod = { string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain };
    Object.assign(process.env, {
      PISEC_ROLE: "worker",
      PISEC_RUNTIME_SOCKET: socketPath,
      PISEC_RUNTIME_TOKEN: "t".repeat(48),
      PISEC_WORKSTREAM_ID: "ws_" + "a".repeat(32),
      PISEC_RUNTIME_INSTANCE_ID: "instance",
      PISEC_SURFACE_ID: "w1:p1",
    });
    const pi = {
      zod,
      registerTool() {},
      on(name, handler) { (handlers[name] ??= []).push(handler); },
      setLabel() {},
      setActiveTools() { return Promise.resolve(); },
    };
    const module = await import(${JSON.stringify(EXTENSION)} + "?failure=" + Date.now());
    module.default(pi);
    const root = { hasUI: true, sessionManager: { getSessionFile() {} }, ui: { notify() {} } };
    await handlers.session_start[0]({}, root);
    await handlers.tool_execution_end[0]({ toolName: "pisec_request_help", isError: true, error: "secret output must not be sent" }, root);
    await handlers.tool_execution_end[0]({ toolName: "bad tool name", isError: true }, root);
    server.stop(true);
    await rm(socketPath, { force: true });
    console.log(JSON.stringify({ operations: requests.map(request => request.operation), failure: requests[1]?.payload }));
  `);
  assert.deepEqual(output.operations, ["runtime.report", "runtime.tool_failure"]);
  const failure = asRecord(output.failure);
  assert.equal(failure.toolName, "pisec_request_help");
  assert.equal(failure.failureCode, "tool_error");
  assert.equal("error" in failure, false);
  assert.equal("secret output must not be sent" in failure, false);
});

test("worker rejects a session resume target outside its owned session root", () => {
  const output = runProbe(`
    const { mkdtemp, mkdir, writeFile } = await import("node:fs/promises");
    const root = await mkdtemp("/tmp/pisec-owned-");
    await mkdir(root + "/sessions");
    const outside = root + "-outside.jsonl";
    await writeFile(outside, "outside\\n");
    const records = { events: [], handlers: {} };
    const chain = () => ({ min: chain, max: chain, optional: chain, int: chain, url: chain });
    const zod = { string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain };
    Object.assign(process.env, {
      PISEC_ROLE: "worker",
      PISEC_RUNTIME_SOCKET: "/tmp/runtime.sock",
      PISEC_RUNTIME_TOKEN: "t".repeat(48),
      PISEC_WORKSTREAM_ID: "ws_" + "a".repeat(32),
      PISEC_RUNTIME_INSTANCE_ID: "instance",
      PISEC_SURFACE_ID: "w1:p1",
      PI_CODING_AGENT_DIR: root,
    });
    const pi = {
      zod,
      registerTool() {},
      on(name, handler) { records.events.push(name); records.handlers[name] = handler; },
      setLabel() {},
      setActiveTools() { return Promise.resolve(); },
    };
    const module = await import(${JSON.stringify(EXTENSION)} + "?worker-target=" + Date.now());
    module.default(pi);
    const result = await records.handlers.session_before_switch(
      { reason: "resume", targetSessionFile: outside },
      { ui: { notify() {} } },
    );
    console.log(JSON.stringify({ rejected: result?.cancel === true, events: records.events }));
  `);
  assert.equal(output.rejected, true);
  assert.ok(stringArray(output.events).includes("session_before_switch"));
});
