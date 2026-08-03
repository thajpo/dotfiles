import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, existsSync, writeFileSync } from "node:fs";
import { hostname, tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { test } from "node:test";

const packageRoot = process.env.PI_TEST_PACKAGE_ROOT || path.join(process.cwd(), "pi/npm");
const source = path.join(packageRoot, "node_modules/pi-subagents/src/runs/shared/session-lease.ts");
const { createJiti } = await import(pathToFileURL(path.join(packageRoot, "node_modules/jiti/lib/jiti.mjs")));
const jiti = createJiti(source, { interopDefault: true });
const lease = await jiti.import(source);
const acquireSandboxChildLease = lease.acquireSandboxChildLease ?? lease.default?.acquireSandboxChildLease;

function routeFixture() {
  const root = mkdtempSync(path.join(tmpdir(), "pi-child-lease-test-"));
  const route = path.join(root, "route.json");
  writeFileSync(route, JSON.stringify({ session: "test-session" }) + "\n", { mode: 0o600 });
  mkdirSync(path.join(root, "subagent-sandbox-leases"), { mode: 0o700 });
  return { root, route };
}

test("child lease rejects malformed live parent transition without reclaiming it", { skip: typeof acquireSandboxChildLease !== "function" }, () => {
  const { root, route } = routeFixture();
  const transition = path.join(root, "subagent-sandbox-leases", "parent-transition.json");
  writeFileSync(transition, JSON.stringify({
    version: 1,
    token: "transition-token",
    routePath: route,
    operation: "checkpoint",
    pid: process.pid,
    hostname: hostname(),
    processStartIdentity: 1,
    acquiredAt: new Date().toISOString(),
    acquiredAtMs: Date.now(),
  }) + "\n", { mode: 0o600 });
  const previous = process.env.PI_TASK_ROUTE_FILE;
  process.env.PI_TASK_ROUTE_FILE = route;
  try {
    assert.throws(
      () => acquireSandboxChildLease({ runId: "malformed-transition", sessionId: "test-session", source: "async" }),
      /metadata is invalid/,
    );
    assert.equal(existsSync(transition), true);
  } finally {
    if (previous === undefined) delete process.env.PI_TASK_ROUTE_FILE;
    else process.env.PI_TASK_ROUTE_FILE = previous;
  }
});
