import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";

const root = path.resolve(import.meta.dirname, "..");

test("child control-plane e2e exercises exact snapshot lineage and workspace boundaries", () => {
  assert.ok(fs.existsSync(path.join(root, "scripts/pi_control/child_runs.py")));
  assert.ok(fs.existsSync(path.join(root, "scripts/pi_control/snapshot.py")));
  const environment = { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONPATH: root };
  execFileSync("python3", ["-m", "unittest", "tests.control_plane.test_child_runs", "tests.control_plane.test_artifacts", "-q"], {
    cwd: root,
    env: environment,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
});

console.log("pi child control-plane e2e: ok");
