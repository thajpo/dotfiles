import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";

const root = path.resolve(import.meta.dirname, "..");
const firstParty = path.join(root, "pi/packages/pi-subagents-control");
const metadata = JSON.parse(fs.readFileSync(path.join(firstParty, "package.json"), "utf8"));
const lock = JSON.parse(fs.readFileSync(path.join(root, "pi/npm/package-lock.json"), "utf8"));
const settings = JSON.parse(fs.readFileSync(path.join(root, "pi/settings.json"), "utf8"));

test("production package allowlists only the controller broker", () => {
  assert.deepEqual(metadata.files, ["index.ts", "src/controller-broker.ts", "README.md", "UPSTREAM.md", "LICENSE"]);
  assert.deepEqual(metadata.exports, { ".": "./index.ts" });
  assert.deepEqual(metadata.dependencies, undefined);
  const reachable = metadata.files.filter((file) => file.endsWith(".ts")).map((file) => fs.readFileSync(path.join(firstParty, file), "utf8")).join("\n");
  for (const forbidden of ["node:child_process", "...process.env", "PI_TASK_", "spawn(", "execFile(", "worktree", "intercom"]) {
    assert.equal(reachable.includes(forbidden), false, `production broker contains ${forbidden}`);
  }
  assert.match(reachable, /subagent\.spawn/);
});

test("local package identity remains restart-safe without direct runtime dependencies", () => {
  assert.equal(lock.packages[""].dependencies["pi-subagents"], "file:../packages/pi-subagents-control");
  assert.equal(lock.packages["node_modules/pi-subagents"].resolved, "file:../packages/pi-subagents-control");
  assert.ok(settings.packages.includes("./npm/node_modules/pi-subagents"));
  assert.equal(settings.packages.some((source) => source === "npm:pi-subagents@0.35.1"), false);
});

console.log("pi-subagents controller-broker provenance: ok");
