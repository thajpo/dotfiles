import assert from "node:assert/strict";
import test from "node:test";

import { createExtensionJiti } from "./extension-jiti.mjs";

const jiti = createExtensionJiti(import.meta.url);
const channel = await jiti.import("../pi/extensions/controller-channel/index.ts");

test("canonical JSON is stable and recursively ordered", () => {
  assert.equal(channel.canonicalJson({ z: 1, a: { d: 2, c: [3, { b: 1, a: 0 }] } }), '{"a":{"c":[3,{"a":0,"b":1}],"d":2},"z":1}');
});

test("challenge validation binds the exact child PID and protocol", () => {
  const value = {
    protocolVersion: 1, type: "challenge", runId: "run_1", manifestDigest: "sha256:x",
    childPid: process.pid, childStartIdentity: "linux:x:1", role: "secretary",
    sessionId: "pi-conv_1", sessionPath: "/state/session", resources: [], activeTools: [], toolSources: [], allowedOperations: [],
  };
  assert.equal(channel.validateChallenge(value).childPid, process.pid);
  assert.throws(() => channel.validateChallenge({ ...value, childPid: process.pid + 1 }), /process or resources/);
  assert.throws(() => channel.validateChallenge({ ...value, protocolVersion: 2 }), /protocol/);
});

test("extension refuses missing and forged inherited descriptors", () => {
  const previous = process.env.PI_CONTROLLER_CHANNEL_FD;
  const fakePi = { on() {} };
  try {
    delete process.env.PI_CONTROLLER_CHANNEL_FD;
    assert.throws(() => channel.default(fakePi), /missing or invalid/);
    process.env.PI_CONTROLLER_CHANNEL_FD = "not-a-number";
    assert.throws(() => channel.default(fakePi), /missing or invalid/);
    process.env.PI_CONTROLLER_CHANNEL_FD = "999999";
    assert.throws(() => channel.default(fakePi));
  } finally {
    if (previous === undefined) delete process.env.PI_CONTROLLER_CHANNEL_FD;
    else process.env.PI_CONTROLLER_CHANNEL_FD = previous;
  }
});
