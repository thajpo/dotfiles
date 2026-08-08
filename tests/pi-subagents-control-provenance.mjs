import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { test } from "node:test";

const root = path.resolve(import.meta.dirname, "..");
const firstParty = path.join(root, "pi/packages/pi-subagents-control");
const installed = path.join(root, "pi/npm/node_modules/pi-subagents");
const lock = JSON.parse(fs.readFileSync(path.join(root, "pi/npm/package-lock.json"), "utf8"));
const settings = JSON.parse(fs.readFileSync(path.join(root, "pi/settings.json"), "utf8"));

function filesUnder(directory) {
  const result = [];
  function visit(current) {
    for (const entry of fs.readdirSync(current, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) visit(full);
      else if (entry.isFile()) result.push(path.relative(directory, full).split(path.sep).join("/"));
    }
  }
  visit(directory);
  return result;
}

function sha256(file) {
  return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

test("first-party package preserves the reviewed patched source byte-for-byte", () => {
  assert.ok(fs.existsSync(path.join(firstParty, "LICENSE")));
  assert.ok(fs.existsSync(path.join(firstParty, "UPSTREAM.md")));
  const extracted = filesUnder(firstParty);
  assert.ok(extracted.length >= 140);
  assert.equal(extracted.some((file) => file.endsWith(".orig")), false);
  for (const relative of extracted) {
    const source = path.join(firstParty, relative);
    const upstream = path.join(installed, relative);
    assert.ok(fs.existsSync(upstream), `missing upstream extraction input: ${relative}`);
    if (relative === "package.json") {
      const actual = JSON.parse(fs.readFileSync(source, "utf8"));
      const expected = JSON.parse(fs.readFileSync(upstream, "utf8"));
      actual.files = [...actual.files].sort();
      expected.files = [...expected.files].sort();
      assert.deepEqual(actual, expected, "package metadata differs beyond provenance file declarations");
    } else {
      assert.equal(sha256(source), sha256(upstream), `source drift: ${relative}`);
    }
  }
});

test("runtime package is materialized inside the npm dependency tree", () => {
  const info = fs.lstatSync(installed);
  assert.equal(info.isDirectory(), true);
  assert.equal(info.isSymbolicLink(), false);
  const require = createRequire(path.join(installed, "src/agents/agents.ts"));
  assert.doesNotThrow(() => require.resolve("yaml"));
});

test("local dependency remains restart-safe", () => {
  assert.equal(lock.packages[""].dependencies["pi-subagents"], "file:../packages/pi-subagents-control");
  assert.equal(lock.packages[""].dependencies.jiti, "2.7.0");
  assert.equal(lock.packages[""].dependencies.yaml, "2.8.3");
  assert.equal(lock.packages["node_modules/pi-subagents"].resolved, "file:../packages/pi-subagents-control");
  assert.equal(lock.packages["node_modules/pi-subagents"].link, undefined);
  assert.ok(settings.packages.includes("./npm/node_modules/pi-subagents"));
  assert.ok(settings.packages.includes("./npm/node_modules/pi-sandbox-control"));
  assert.equal(settings.packages.some((source) => source === "npm:pi-subagents@0.35.1"), false);
  assert.equal(settings.packages.some((source) => source === "npm:@kjrjay/pi-sandbox@0.2.0"), false);
});

console.log("pi-subagents-control provenance: ok");
