import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const packageRoot = process.env.PI_TEST_PACKAGE_ROOT;
if (!packageRoot) throw new Error("PI_TEST_PACKAGE_ROOT is required");
const modulePath = path.join(packageRoot, "node_modules/pi-subagents/src/runs/shared/session-lease.ts");
const { acquireSandboxChildLease, acquireSessionLease, sessionLeaseDir } = await import(pathToFileURL(modulePath));

const temp = mkdtempSync(path.join(os.tmpdir(), "pi-session-lease-test-"));
try {
  const sessionFile = path.join(temp, "session.json");
  writeFileSync(sessionFile, "{}\n");
  const leaseRoot = path.join(temp, "leases");
  mkdirSync(leaseRoot, { mode: 0o700 });
  const first = acquireSessionLease(
    { sessionFile, runId: "old", sourceRunId: "source-old" },
    {
      rootDir: leaseRoot,
      token: () => "old-token",
      pid: 101,
      hostname: "lease-test-host",
      processStartIdentity: "old-start",
      isProcessAlive: () => false,
      getProcessStartIdentity: () => "old-start",
    },
  );
  const second = acquireSessionLease(
    { sessionFile, runId: "new", sourceRunId: "source-new" },
    {
      rootDir: leaseRoot,
      token: () => "new-token",
      pid: 202,
      hostname: "lease-test-host",
      processStartIdentity: "new-start",
      isProcessAlive: () => false,
      getProcessStartIdentity: () => "new-start",
    },
  );
  assert.equal(first.leaseDir, second.leaseDir);
  assert.equal(JSON.parse(readFileSync(path.join(second.leaseDir, "owner.json"), "utf8")).token, "new-token");
  assert.ok(readdirSync(leaseRoot).some((name) => name.startsWith(`${path.basename(first.leaseDir)}.stale-`)));
  first.release();
  assert.ok(existsSync(second.leaseDir), "old release must not remove successor lease");
  second.release();

  const routeA = path.join(temp, "route-a.json");
  const routeB = path.join(temp, "route-b.json");
  writeFileSync(routeA, "{}\n");
  writeFileSync(routeB, "{}\n");
  delete process.env.PI_SUBAGENT_CHILD;
  const childA = acquireSandboxChildLease({ runId: "same-run", source: "foreground", routePath: routeA });
  const childB = acquireSandboxChildLease({ runId: "same-run", source: "foreground", routePath: routeB });
  assert.ok(childA && childB);
  assert.notEqual(childA.leaseDir, childB.leaseDir, "route-specific lease roots must not collide");
  childA.release();
  childB.release();
} finally {
  // The test harness owns the temporary tree; production reclamation deliberately
  // retains stale tombstones for inspection.
  const { rmSync } = await import("node:fs");
  rmSync(temp, { recursive: true, force: true });
}

console.log("PASS session and child lease lifecycle");
