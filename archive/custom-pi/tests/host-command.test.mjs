import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {
  DEFAULT_TIMEOUT_MS,
  HOST_COMMAND_PROTOCOL_VERSION,
  createRequest,
  ensureRequestRoot,
  listRequestFiles,
  parseRequest,
  parseResponse,
  requestPath,
  responsePath,
  truncateOutput,
  writeRequest,
  writeResponse,
} from "../pi/extensions/host-command/core.mjs";

test("host command requests are bounded and atomically persisted", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pi-host-command-test-"));
  try {
    ensureRequestRoot(root);
    const request = createRequest({
      targetSessionId: "session-1",
      parentRuntimeId: "runtime-1",
      command: "xclip -selection clipboard -o",
      reason: "Read the copied message",
      description: "Retrieve the user's native text clipboard for inspection.",
      requester: { kind: "child", agent: "scout", runId: "run-1", childIndex: 0 },
    });
    writeRequest(root, request);
    const files = listRequestFiles(root);
    assert.equal(files.length, 1);
    assert.deepEqual(parseRequest(files[0]), request);
    assert.equal(request.timeoutMs, DEFAULT_TIMEOUT_MS);

    const response = {
      type: "pi.host-command.response",
      version: HOST_COMMAND_PROTOCOL_VERSION,
      id: request.id,
      createdAt: Date.now(),
      status: "approved",
      output: "copied text",
      exitCode: 0,
    };
    writeResponse(root, response);
    assert.deepEqual(parseResponse(responsePath(root, request.id)), response);
    assert.equal(requestPath(root, request.id).endsWith(`${request.id}.json`), true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("invalid request command text is rejected", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pi-host-command-invalid-"));
  try {
    ensureRequestRoot(root);
    const request = createRequest({
      targetSessionId: "session-1",
      parentRuntimeId: "runtime-1",
      command: "echo safe",
      reason: "reason",
      description: "description",
    });
    request.command = "printf '\u001b[31msecret'";
    writeRequest(root, request);
    assert.equal(parseRequest(requestPath(root, request.id)), undefined);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("host command output is bounded by bytes and lines", () => {
  const bounded = truncateOutput(`${"x".repeat(100)}\nsecond\nthird`, { maxBytes: 12, maxLines: 2 });
  assert.equal(bounded.truncated, true);
  assert.ok(Buffer.byteLength(bounded.text, "utf8") <= 12);
  assert.ok(bounded.text.split("\n").length <= 2);
});
