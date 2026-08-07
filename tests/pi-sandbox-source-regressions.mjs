import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { test } from "node:test";

const root = path.resolve(import.meta.dirname, "..");
const packageRoot = process.env.PI_TEST_PACKAGE_ROOT || path.join(root, "pi/npm");
const sourcePath = path.join(packageRoot, "node_modules/@kjrjay/pi-sandbox/index.ts");
const source = fs.readFileSync(sourcePath, "utf8");

function section(start, end) {
  const startIndex = source.indexOf(start);
  assert.notEqual(startIndex, -1, `missing source marker: ${start}`);
  const endIndex = source.indexOf(end, startIndex + start.length);
  assert.notEqual(endIndex, -1, `missing source marker after ${start}: ${end}`);
  return source.slice(startIndex, endIndex);
}

function assertOrdered(text, labels) {
  let previous = -1;
  for (const label of labels) {
    const index = text.indexOf(label);
    assert.notEqual(index, -1, `missing ordered source fragment: ${label}`);
    assert.ok(index > previous, `source fragment is out of order: ${label}`);
    previous = index;
  }
}

test("pi-sandbox source remains valid TypeScript after cumulative patches", async () => {
  const jitiPath = path.join(packageRoot, "node_modules/jiti/lib/jiti.mjs");
  const { createJiti } = await import(pathToFileURL(jitiPath));
  const transformed = createJiti(sourcePath).transform({ source, filename: sourcePath, ts: true });
  assert.doesNotMatch(transformed, /exports\.__JITI_ERROR__/);
});

test("parent transition release parses JSON and always clears in-memory ownership", () => {
  const transition = section(
    "private beginChildLifecycleTransition(operation: string)",
    "private activeParentTransition()",
  );
  assert.match(source, /function readSandboxParentTransition[\s\S]*JSON\.parse\(readFileSync\(transitionPath, "utf8"\)\)/);
  assert.doesNotMatch(source, /parseSandboxParentTransition\(readFileSync/);
  assert.match(transition, /release: \(\) => \{[\s\S]*readSandboxParentTransition\(transition\.path\)[\s\S]*finally \{[\s\S]*this\.parentTransition = undefined;/);
  assert.match(transition, /if \(this\.parentTransition\) throw new Error/);
});

test("checkpoint and rebase initialize before acquiring non-reentrant transitions", () => {
  const checkpoint = section("private async checkpointGitRefUnlocked(", "async checkpointGitRef(");
  assertOrdered(checkpoint, [
    "await this.ensure(ctx);",
    'this.beginChildLifecycleTransition("checkpoint or move the sandbox ref")',
  ]);

  const rebase = section("async rebaseHost(", "async finalizePendingRebase(");
  assertOrdered(rebase, [
    "await this.ensure(ctx);",
    "await this.checkpointGitRef(ctx);",
    'this.beginChildLifecycleTransition("rebase the sandbox")',
  ]);
  assert.match(rebase, /startContainerRebase\(state, \{[\s\S]*oldSandboxTip,[\s\S]*expectedCommitCount/);
});

test("nested rebase completion reuses the held transition", () => {
  const start = section("private async startContainerRebase(", "async rebaseHost(");
  assert.match(start, /finalizePendingRebase\(ctx, true\)/);
  assert.match(start, /abortRebase\(ctx, true\)/);

  const finalize = section("async finalizePendingRebase(", "async rebaseStatus(");
  assertOrdered(finalize, [
    "if (!transitionHeld) await this.ensure(ctx);",
    'transitionHeld ? undefined : this.beginChildLifecycleTransition("finalize the sandbox rebase")',
  ]);
  const abort = section("async abortRebase(", "async assertReadyForAgentTurn(");
  assertOrdered(abort, [
    "if (!transitionHeld) await this.ensure(ctx);",
    'transitionHeld ? undefined : this.beginChildLifecycleTransition("abort the sandbox rebase")',
  ]);
});

test("unused shutdowns do not create markers and task-local environments are writable", () => {
  const shutdown = section("async shutdown(ctx?: ExtensionContext", "class SandboxLifecycleComponent");
  assertOrdered(shutdown, [
    "if (!containerToCleanup)",
    'this.beginChildLifecycleTransition("shut down or remove the sandbox container")',
  ]);
  assert.match(source, /mode === "derived-image" \? "\/opt\/pi\/env" : "\/tmp\/pi-home\/task-env"/);
});
