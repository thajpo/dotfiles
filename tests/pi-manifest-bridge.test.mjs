import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { createJiti } from "../pi/npm/node_modules/jiti/lib/jiti.mjs";

const root = process.cwd();
const jiti = createJiti(root, { interopDefault: true });
const adapter = await jiti.import("./pi/packages/pi-sandbox-control/src/manifest-adapter.ts");
const fixture = realpathSync(mkdtempSync(path.join(tmpdir(), "pi-harness-bridge-")));
const repo = path.join(fixture, "repo");
const state = path.join(fixture, "state");
mkdirSync(repo);
execFileSync("git", ["init", "-q", repo]);
writeFileSync(path.join(repo, "README"), "bridge\n");
execFileSync("git", ["-C", repo, "add", "README"]);
execFileSync("git", ["-C", repo, "-c", "user.name=bridge", "-c", "user.email=bridge@example.invalid", "commit", "-qm", "bridge"]);
const prepared = JSON.parse(execFileSync("python3", [
  path.join(root, "tests", "pi_bridge_prepare.py"), "--state-root", state, "--repository", repo,
], { encoding: "utf8" }));
const manifest = adapter.readRuntimeManifest(realpathSync(prepared.manifestPath));
assert.equal(manifest.project.projectId, prepared.projectId);
assert.equal(manifest.conversation.role, "secretary");
assert.equal(manifest.conversation.authorityProfile, "host-read-only");
assert.equal(manifest.toolRuntime, null);
console.log("Pi manifest bridge: ok");
