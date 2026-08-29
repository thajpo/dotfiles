import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { test } from "bun:test";
import { FIRST_MATE_PROMPT, SECRETARY_PROMPT, WORKER_PROMPT } from "./pisec-prompts";

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
    const chain = () => ({ min: chain, max: chain, optional: chain, int: chain, url: chain, regex: chain });
    const zod = { string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain };
    Object.assign(process.env, {
      PISEC_ROLE: "secretary",
      PISEC_RUNTIME_SOCKET: "/tmp/runtime.sock",
      PISEC_SECRETARY_SOCKET: "/tmp/secretary.sock",
      PISEC_RUNTIME_TOKEN: "t".repeat(48),
      PISEC_RUNTIME_GENERATION: "g".repeat(64),
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
      dataDirs: ["/approved/data"],
      pythonEnv: "/approved/python",
      importSource: { kind: "git_worktree", path: "/external/review", sourceCommitOid: "f".repeat(40), sourceTreeOid: "e".repeat(40), mergeBaseOid: "d".repeat(40), patchSha256: "c".repeat(64), changedPaths: ["src/main.ts"] },
      taskPacket: {
        outcome: "Produce the verified change",
        boundaries: ["/approved/src"],
        acceptance: ["run the phase checks"],
      },
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
    "pisec_list_attention",
    "pisec_inspect_attention",
    "pisec_project_activity",
    "pisec_list_issues",
    "pisec_inspect_issue",
    "pisec_add_issue_context",
    "pisec_escalate_issue",
    "pisec_verify_issue",
    "pisec_acknowledge_issue",
    "pisec_link_issue_remediation",
    "pisec_request_issue_verification",
    "pisec_resolve_issue",
    "pisec_project_status",
    "pisec_git_status",
    "pisec_push_branch",
    "pisec_inspect_workstream_changes",
    "pisec_prepare_workstream_acceptance",
    "pisec_accept_workstream",
    "pisec_list_workstreams",
    "pisec_inspect_workstream",
    "pisec_list_integrations",
    "pisec_inspect_integration",
    "pisec_prepare_workstream",
    "pisec_create_workstream",
    "pisec_focus_workstream",
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
    "intended outcome: Produce the verified change",
    "allowed paths and changes: /approved/src",
    "non-effects: push",
    "acceptance tests: run the phase checks",
    "external source: git_worktree /external/review; normalize the pinned clean commit into the worker",
    "harness/model: omp / configured default",
    "approved readable data: /approved/data, /approved/python",
    "warning: approved readable data and Python paths are user data and are not proven secret-free",
  ]) assert.ok(approvalReason.includes(line), line);
  for (const hidden of [
    "op_" + "a".repeat(32),
    "prj_" + "b".repeat(32),
    "ws_" + "a".repeat(32),
    "a".repeat(40),
    "/tmp/work",
  ]) assert.equal(approvalReason.includes(hidden), false, hidden);
  assert.equal(refused.isError, true);
  assert.match(refusalText, /interactive approval UI/);
  assert.ok(events.includes("session_shutdown"));
});

test("checkpoint schema contains only the v1 semantic phases and fields", async () => {
  const source = await Bun.file(new URL("./pisec.ts", import.meta.url)).text();
  assert.match(source, /phase: z\.enum\(\["investigating", "implementing", "verifying"\]\)/);
  assert.doesNotMatch(source, /phase: z\.enum\(\["investigating", "implementing", "verifying", "ready_review"\]\)/);
  assert.match(source, /accepted target drift/);
  assert.match(source, /replacement completion packet/);
  assert.doesNotMatch(source, /needs_input|blocker_code|blockerCode/);
  assert.doesNotMatch(source, /nextAction: params\.next_action, \(\.\.\.params\.blocker/);
});

test("role prompt snapshots match their available tools and reporting contract", async () => {
  const source = await Bun.file(new URL("./pisec.ts", import.meta.url)).text();
  assert.match(FIRST_MATE_PROMPT, /medium-detail senior-engineering briefing/);
  assert.match(FIRST_MATE_PROMPT, /Goal and current position/);
  assert.doesNotMatch(FIRST_MATE_PROMPT, /pisec_prepare_workstream|pisec_create_workstream/);
  assert.match(SECRETARY_PROMPT, /Every worker proposal must describe the engineering outcome/);
  assert.match(WORKER_PROMPT, /Start the assigned engineering task immediately/);
  assert.match(WORKER_PROMPT, /pisec_submit_completion/);
  assert.match(WORKER_PROMPT, /accepted target drift/);
  assert.match(WORKER_PROMPT, /existing human acceptance/);
  assert.match(source, /authorized attention record and its typed source/);
  assert.match(source, /Do not pass an integration source ID to a coordination inspector/);
  assert.match(WORKER_PROMPT, /Remaining work, risks, and next action/);
  for (const tool of ["pisec_prepare_workstream", "pisec_create_workstream", "pisec_submit_completion"]) {
    assert.match(source, new RegExp(`(?:name: )?"${tool}"`), tool);
  }
  assert.doesNotMatch(source, /Default replies must fit a short screen/);
  assert.doesNotMatch(source, /use only Status, Needs attention, and Next action/);
});

test("first mate exposes the exact fleet surface", () => {
  const output = runProbe(`
    const records = { tools: [], events: [], labels: [] };
    const chain = () => ({ min: chain, max: chain, optional: chain, int: chain, url: chain, regex: chain });
    const zod = { string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain };
    Object.assign(process.env, {
      PISEC_ROLE: "first_mate",
      PISEC_RUNTIME_SOCKET: "/tmp/runtime.sock",
      PISEC_FLEET_SOCKET: "/tmp/fleet.sock",
      PISEC_RUNTIME_TOKEN: "t".repeat(48),
      PISEC_RUNTIME_GENERATION: "g".repeat(64),
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
    "pisec_fleet_list_attention",
    "pisec_fleet_inspect_attention",
    "pisec_fleet_list_issues",
    "pisec_fleet_inspect_issue",
    "pisec_fleet_add_issue_context",
    "pisec_fleet_acknowledge_issue",
    "pisec_fleet_resolve_issue",
    "pisec_fleet_status",
    "pisec_fleet_events",
    "pisec_fleet_list_workstreams",
    "pisec_fleet_inspect_workstream",
    "pisec_fleet_list_integrations",
    "pisec_fleet_inspect_integration",
     "pisec_fleet_git_changes",
    "pisec_fleet_request_issue_remediation",
    "pisec_fleet_request_issue_verification",
  ]);
  assert.equal(stringArray(output.labels)[0], "Pisec First Mate");
  assert.ok(stringArray(output.events).includes("session_shutdown"));
});

test("worker registers runtime handling without secretary tools", () => {
  const output = runProbe(`
    const records = { tools: [], events: [], labels: [] };
    const chain = () => ({ min: chain, max: chain, optional: chain, int: chain, url: chain, regex: chain });
    const zod = { string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain };
    Object.assign(process.env, {
      PISEC_ROLE: "worker",
      PISEC_RUNTIME_SOCKET: "/tmp/runtime.sock",
      PISEC_RUNTIME_TOKEN: "t".repeat(48),
      PISEC_RUNTIME_GENERATION: "g".repeat(64),
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
    "pisec_list_attention",
    "pisec_inspect_attention",
    "pisec_checkpoint_workstream",
    "pisec_submit_completion",
    "pisec_request_help",
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

test("prepared approval hashes apply the untouched acceptance and creation scopes", () => {
  const output = runProbe(`
    const { rm } = await import("node:fs/promises");
    const socketPath = "/tmp/pisec-extension-approval-" + process.pid + "-" + Date.now() + ".sock";
    await rm(socketPath, { force: true });
    const requests = [];
    const acceptanceScope = {
      kind: "workstream.accept",
      projectId: "prj_" + "b".repeat(32),
      workstreamId: "ws_" + "a".repeat(32),
      targetBranch: "main",
      completionPacketSha256: "c".repeat(64),
      taskPacketSha256: "d".repeat(64),
      candidatePatchSha256: "e".repeat(64),
      changedPaths: ["README.md"],
      acceptance: [{ criterion: "docs check", status: "passed" }],
      verification: [{ command: "git diff --check", result: "passed" }],
      conflictPolicy: "bounded-worker-reconciliation",
      effects: ["advance main"],
      nonEffects: ["no push"],
    };
    const creationScope = {
      operationId: "op_" + "f".repeat(32),
      projectId: "prj_" + "b".repeat(32),
      workstreamId: "ws_" + "9".repeat(32),
      harnessId: "codex",
      implementationModel: "gpt-5.6-codex-luna",
      taskPacket: { outcome: "Implement the approved parser fix.", boundaries: ["src/parser.ts"], acceptance: ["bun test"] },
      effects: ["create worker"],
      nonEffects: ["no push"],
    };
    const server = Bun.listen({
      unix: socketPath,
      socket: {
        data(socket, data) {
          const request = JSON.parse(data.toString().trim());
          requests.push(request);
          let result = { accepted: true };
          if (request.operation === "workstream.accept.prepare") result = { approvalScope: acceptanceScope };
          if (request.operation === "workstream.prepare") result = { approvalScope: creationScope };
          socket.write(JSON.stringify({ requestId: request.requestId, ok: true, result }) + "\\n");
          socket.end();
        },
      },
    });
    const records = { tools: [] };
    const chain = () => ({ min: chain, max: chain, optional: chain, int: chain, url: chain, regex: chain });
    const zod = { string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain };
    Object.assign(process.env, {
      PISEC_ROLE: "secretary",
      PISEC_RUNTIME_SOCKET: socketPath,
      PISEC_SECRETARY_SOCKET: socketPath,
      PISEC_RUNTIME_TOKEN: "t".repeat(48),
      PISEC_RUNTIME_GENERATION: "g".repeat(64),
      PISEC_WORKSTREAM_ID: "ws_" + "1".repeat(32),
      PISEC_RUNTIME_INSTANCE_ID: "instance",
      PISEC_SURFACE_ID: "w1:p1",
    });
    const pi = {
      zod,
      registerTool(value) { records.tools.push(value); },
      on() {},
      setLabel() {},
      setActiveTools() { return Promise.resolve(); },
    };
    const module = await import(${JSON.stringify(EXTENSION)} + "?approval=" + Date.now());
    module.default(pi);
    const prepareAcceptance = records.tools.find(tool => tool.name === "pisec_prepare_workstream_acceptance");
    const accept = records.tools.find(tool => tool.name === "pisec_accept_workstream");
    const prepareCreation = records.tools.find(tool => tool.name === "pisec_prepare_workstream");
    const create = records.tools.find(tool => tool.name === "pisec_create_workstream");
    const preparedAcceptance = await prepareAcceptance.execute("prepare-accept", { workstream_id: acceptanceScope.workstreamId });
    const acceptanceHash = preparedAcceptance.details.approvalScopeSha256;
    const acceptanceApproval = accept.approval({ approval_scope_sha256: acceptanceHash });
    const accepted = await accept.execute("accept", { approval_scope_sha256: acceptanceHash }, undefined, undefined, { hasUI: true });
    const replay = await accept.execute("accept-replay", { approval_scope_sha256: acceptanceHash }, undefined, undefined, { hasUI: true });
    const preparedCreation = await prepareCreation.execute("prepare-create", { title: "Parser", purpose: "Fix parser", brief: "Implement now", task_packet: { schemaVersion: 1, outcome: "Fix parser", boundaries: ["src/parser.ts"], acceptance: ["bun test"], openQuestions: [], evidence: [] } });
    const creationHash = preparedCreation.details.approvalScopeSha256;
    const creationApproval = create.approval({ approval_scope_sha256: creationHash });
    const created = await create.execute("create", { approval_scope_sha256: creationHash }, undefined, undefined, { hasUI: true });
    server.stop(true);
    await rm(socketPath, { force: true });
    console.log(JSON.stringify({
      acceptanceHash,
      creationHash,
      acceptanceReason: acceptanceApproval.reason,
      creationReason: creationApproval.reason,
      accepted,
      created,
      replay,
      operations: requests.map(request => request.operation),
      acceptanceApply: requests.find(request => request.operation === "workstream.accept.apply")?.payload.approvalScope,
      creationApply: requests.find(request => request.operation === "workstream.authorize_apply")?.payload.approvalScope,
    }));
  `);
  assert.match(stringValue(output, "acceptanceHash"), /^[0-9a-f]{64}$/);
  assert.match(stringValue(output, "creationHash"), /^[0-9a-f]{64}$/);
  assert.match(stringValue(output, "acceptanceReason"), /changed paths: README\.md/);
  assert.match(stringValue(output, "creationReason"), /intended outcome: Implement the approved parser fix\./);
  assert.deepEqual(output.operations, ["workstream.accept.prepare", "workstream.accept.apply", "workstream.prepare", "workstream.authorize_apply"]);
  assert.deepEqual(output.acceptanceApply, {
    kind: "workstream.accept",
    projectId: "prj_" + "b".repeat(32),
    workstreamId: "ws_" + "a".repeat(32),
    targetBranch: "main",
    completionPacketSha256: "c".repeat(64),
    taskPacketSha256: "d".repeat(64),
    candidatePatchSha256: "e".repeat(64),
    changedPaths: ["README.md"],
    acceptance: [{ criterion: "docs check", status: "passed" }],
    verification: [{ command: "git diff --check", result: "passed" }],
    conflictPolicy: "bounded-worker-reconciliation",
    effects: ["advance main"],
    nonEffects: ["no push"],
  });
  assert.equal(asRecord(output.replay).isError, true);
  assert.equal(asRecord(output.accepted).isError, undefined);
  assert.equal(asRecord(output.created).isError, undefined);
  assert.deepEqual(output.creationApply, {
    operationId: "op_" + "f".repeat(32),
    projectId: "prj_" + "b".repeat(32),
    workstreamId: "ws_" + "9".repeat(32),
    harnessId: "codex",
    implementationModel: "gpt-5.6-codex-luna",
    taskPacket: { outcome: "Implement the approved parser fix.", boundaries: ["src/parser.ts"], acceptance: ["bun test"] },
    effects: ["create worker"],
    nonEffects: ["no push"],
  });
});

test("worker consumes one typed bootstrap and receives the full packet on later turns", () => {
  const output = runProbe(`
    const { rm } = await import("node:fs/promises");
    const socketPath = "/tmp/pisec-extension-bootstrap-" + process.pid + "-" + Date.now() + ".sock";
    await rm(socketPath, { force: true });
    const requests = [];
    const messages = [];
    const activeTools = [];
    let prepared = 0;
    const taskPacket = { taskPacketId: "tp_1", packetSha256: "d".repeat(64), packet: { schemaVersion: 1, outcome: "Implement the assigned parser change.", boundaries: ["src/parser.ts"], acceptance: ["bun test"], openQuestions: [], evidence: [] } };
    const server = Bun.listen({
      unix: socketPath,
      socket: {
        data(socket, data) {
          const request = JSON.parse(data.toString().trim());
          requests.push(request);
          let result = { accepted: true };
          if (request.operation === "runtime.turn.prepare") {
            prepared += 1;
            result = { prepared: true, taskPacket, attention: [], bootstrap: prepared === 1 ? { eventType: "worker.bootstrap", sourceRecordId: "evt_boot", sourceRevision: 7, role: "worker" } : null };
          }
          socket.write(JSON.stringify({ requestId: request.requestId, ok: true, result }) + "\\n");
          socket.end();
        },
      },
    });
    const handlers = {};
    const chain = () => ({ min: chain, max: chain, optional: chain, int: chain, url: chain, regex: chain });
    const zod = { string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain };
    Object.assign(process.env, {
      PISEC_ROLE: "worker",
      PISEC_RUNTIME_SOCKET: socketPath,
      PISEC_RUNTIME_TOKEN: "t".repeat(48),
      PISEC_RUNTIME_GENERATION: "g".repeat(64),
      PISEC_WORKSTREAM_ID: "ws_" + "a".repeat(32),
      PISEC_RUNTIME_INSTANCE_ID: "instance",
      PISEC_SURFACE_ID: "w1:p1",
    });
    const pi = {
      zod,
      messageSink: messages,
      activeToolSource: ["read", "write", "shell"],
      registerTool(value) { (records.tools ??= []).push(value); },
      on(name, handler) { (handlers[name] ??= []).push(handler); },
      setLabel() {},
      // Real OMP ExtensionAPI methods read this.runtime. Keep this probe
      // receiver-sensitive so detached calls fail the same way as production.
      sendMessage(message, options) { this.messageSink.push({ message, options }); },
      getActiveTools() { return this.activeToolSource; },
      setActiveTools(value) { activeTools.push(value); return Promise.resolve(); },
    };
    const records = { tools: [] };
    const module = await import(${JSON.stringify(EXTENSION)} + "?bootstrap=" + Date.now());
    module.default(pi);
    const root = { hasUI: false, sessionManager: { getSessionFile() {} }, ui: { notify() {} } };
    await handlers.session_start[0]({}, root);
    const preparedTurn = await handlers.before_agent_start[0]({ systemPrompt: ["base"] }, root);
    const checkpoint = records.tools.find(tool => tool.name === "pisec_checkpoint_workstream");
    await checkpoint.execute("call-1", { phase: "investigating", summary: "Inspected the assigned parser.", next_action: "Implement the bounded change.", evidence: [] });
    server.stop(true);
    await rm(socketPath, { force: true });
    console.log(JSON.stringify({ operations: requests.map(request => request.operation), checkpoint: requests.find(request => request.operation === "workstream.checkpoint")?.payload, messages, activeTools, prompt: preparedTurn.systemPrompt.join("\\n") }));
  `);
  assert.deepEqual(output.operations, ["runtime.report", "runtime.turn.prepare", "runtime.bootstrap.ack", "runtime.turn.prepare", "workstream.checkpoint"]);
  assert.match(output.checkpoint.idempotencyKey, /^adapter:omp:[0-9a-f]{64}$/);
  assert.equal("idempotency_key" in output.checkpoint, false);
  assert.equal(output.messages.length, 1);
  assert.equal(output.messages[0].message.customType, "pisec");
  assert.equal(output.messages[0].message.details.source, "pisec");
  assert.equal(output.messages[0].message.details.sourceRecordId, "evt_boot");
  assert.equal(output.messages[0].message.details.sourceRevision, 7);
  assert.equal(output.messages[0].options.triggerTurn, true);
  assert.doesNotMatch(output.messages[0].message.content, /Implement the assigned parser/);
  assert.match(output.prompt, /IMMUTABLE_TASK_PACKET/);
  assert.match(output.prompt, /Implement the assigned parser change/);
});

test("worker blocks mutation when turn preparation fails even if the model continues", () => {
  const output = runProbe(`
    const { rm } = await import("node:fs/promises");
    const socketPath = "/tmp/pisec-extension-blocked-" + process.pid + "-" + Date.now() + ".sock";
    await rm(socketPath, { force: true });
    const requests = [];
    const messages = [];
    const activeTools = [];
    const server = Bun.listen({
      unix: socketPath,
      socket: {
        data(socket, data) {
          const request = JSON.parse(data.toString().trim());
          requests.push(request);
          socket.write(JSON.stringify({ requestId: request.requestId, ok: false, error: { message: "broker unavailable" } }) + "\\n");
          socket.end();
        },
      },
    });
    const handlers = {};
    const tools = [];
    const chain = () => ({ min: chain, max: chain, optional: chain, int: chain, url: chain, regex: chain });
    const zod = { string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain };
    Object.assign(process.env, {
      PISEC_ROLE: "worker",
      PISEC_RUNTIME_SOCKET: socketPath,
      PISEC_RUNTIME_TOKEN: "t".repeat(48),
      PISEC_RUNTIME_GENERATION: "g".repeat(64),
      PISEC_WORKSTREAM_ID: "ws_" + "a".repeat(32),
      PISEC_RUNTIME_INSTANCE_ID: "instance",
      PISEC_SURFACE_ID: "w1:p1",
    });
    const pi = {
      zod,
      messageSink: messages,
      activeToolSource: ["read", "write", "shell", "pisec_checkpoint_workstream"],
      registerTool(value) { tools.push(value); },
      on(name, handler) { (handlers[name] ??= []).push(handler); },
      setLabel() {},
      sendMessage(message, options) { this.messageSink.push({ message, options }); },
      getActiveTools() { return this.activeToolSource; },
      setActiveTools(value) { activeTools.push(value); return Promise.resolve(); },
    };
    const module = await import(${JSON.stringify(EXTENSION)} + "?blocked=" + Date.now());
    module.default(pi);
    const root = { hasUI: false, sessionManager: { getSessionFile() {} }, ui: { notify() {} } };
    const result = await handlers.before_agent_start[0]({ systemPrompt: ["base"] }, root);
    const checkpoint = tools.find(tool => tool.name === "pisec_checkpoint_workstream");
    const blockedCheckpoint = await checkpoint.execute("call", { phase: "investigating", summary: "x", next_action: "x", evidence: [] });
    server.stop(true);
    await rm(socketPath, { force: true });
    console.log(JSON.stringify({ operations: requests.map(request => request.operation), messages, activeTools, prompt: result.systemPrompt.join("\\n"), checkpoint: blockedCheckpoint }));
  `);
  assert.deepEqual(output.operations, ["runtime.turn.prepare", "runtime.report"]);
  assert.deepEqual(output.activeTools, [["pisec_list_attention", "pisec_inspect_attention", "pisec_show_task_packet", "pisec_list_coordination", "pisec_inspect_coordination", "pisec_list_issues", "pisec_inspect_issue", "pisec_check_secretary_research", "pisec_inspect_secretary_research", "pisec_request_help", "pisec_report_issue"]]);
  assert.equal(output.messages.length, 1);
  assert.equal(output.messages[0].message.details.sourceRecordId, "runtime.turn.prepare:ws_" + "a".repeat(32));
  assert.match(output.prompt, /PISEC_RUNTIME_BLOCKED/);
  assert.equal(output.checkpoint.isError, true);
  assert.match(output.checkpoint.content[0].text, /runtime is blocked/);
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
    const chain = () => ({ min: chain, max: chain, optional: chain, int: chain, url: chain, regex: chain });
    const zod = { string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain };
    Object.assign(process.env, {
      PISEC_ROLE: "secretary",
      PISEC_RUNTIME_SOCKET: socketPath,
      PISEC_SECRETARY_SOCKET: socketPath,
      PISEC_RUNTIME_TOKEN: "t".repeat(48),
      PISEC_RUNTIME_GENERATION: "g".repeat(64),
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
  assert.deepEqual(output.states, ["idle", null, "working", "idle"]);
  assert.deepEqual(output.events, ["session_start", null, "lifecycle", "lifecycle"]);
});

test("worker non-UI root session reports authenticated startup", () => {
  const output = runProbe(`
    const { rm } = await import("node:fs/promises");
    const socketPath = "/tmp/pisec-extension-worker-start-" + process.pid + "-" + Date.now() + ".sock";
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
    const chain = () => ({ min: chain, max: chain, optional: chain, int: chain, url: chain, regex: chain });
    const zod = { string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain };
    Object.assign(process.env, {
      PISEC_ROLE: "worker",
      PISEC_RUNTIME_SOCKET: socketPath,
      PISEC_RUNTIME_TOKEN: "t".repeat(48),
      PISEC_RUNTIME_GENERATION: "g".repeat(64),
      PISEC_WORKSTREAM_ID: "ws_" + "a".repeat(32),
      PISEC_RUNTIME_INSTANCE_ID: "instance",
      PISEC_SURFACE_ID: "w1:p2",
    });
    const pi = {
      zod,
      registerTool() {},
      on(name, handler) { (handlers[name] ??= []).push(handler); },
      setLabel() {},
      setActiveTools() { return Promise.resolve(); },
    };
    const module = await import(${JSON.stringify(EXTENSION)} + "?worker-start=" + Date.now());
    module.default(pi);
    const workerRoot = { hasUI: false, sessionManager: { getSessionFile() {} }, ui: { notify() {} } };
    await handlers.session_start[0]({}, workerRoot);
    server.stop(true);
    await rm(socketPath, { force: true });
    console.log(JSON.stringify({ states: reports.map(report => report.state), events: reports.map(report => report.event), report: reports[0] }));
  `);
  assert.deepEqual(output.states, ["idle", null]);
  assert.deepEqual(output.events, ["session_start", null]);
  const report = asRecord(output.report);
  assert.equal(stringValue(report, "workstreamId"), "ws_" + "a".repeat(32));
  assert.equal(stringValue(report, "runtimeInstanceId"), "instance");
  assert.equal(stringValue(report, "surfaceId"), "w1:p2");
  assert.equal(stringValue(report, "token"), "t".repeat(48));
  assert.equal(stringValue(report, "generation"), "g".repeat(64));
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
    const chain = () => ({ min: chain, max: chain, optional: chain, int: chain, url: chain, regex: chain });
    const zod = { string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain };
    Object.assign(process.env, {
      PISEC_ROLE: "worker",
      PISEC_RUNTIME_SOCKET: socketPath,
      PISEC_RUNTIME_TOKEN: "t".repeat(48),
      PISEC_RUNTIME_GENERATION: "g".repeat(64),
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
    console.log(JSON.stringify({ operations: requests.map(request => request.operation), failure: requests[2]?.payload }));
  `);
  assert.deepEqual(output.operations, ["runtime.report", "runtime.turn.prepare", "runtime.tool_failure"]);
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
    const chain = () => ({ min: chain, max: chain, optional: chain, int: chain, url: chain, regex: chain });
    const zod = { string: chain, enum: chain, any: chain, object: chain, literal: chain, array: chain, number: chain, boolean: chain };
    Object.assign(process.env, {
      PISEC_ROLE: "worker",
      PISEC_RUNTIME_SOCKET: "/tmp/runtime.sock",
      PISEC_RUNTIME_TOKEN: "t".repeat(48),
      PISEC_RUNTIME_GENERATION: "g".repeat(64),
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
