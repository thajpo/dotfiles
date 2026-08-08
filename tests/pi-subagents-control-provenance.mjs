import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";

const root = path.resolve(import.meta.dirname, "..");
const firstParty = path.join(root, "pi/packages/pi-subagents-control");
const installed = path.join(root, "pi/npm/node_modules/pi-subagents");
const lock = JSON.parse(fs.readFileSync(path.join(root, "pi/npm/package-lock.json"), "utf8"));

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
  const extracted = filesUnder(firstParty).filter((file) => file !== "LICENSE" && file !== "UPSTREAM.md");
  assert.ok(extracted.length >= 140);
  assert.equal(extracted.some((file) => file.endsWith(".orig")), false);
  for (const relative of extracted) {
    const source = path.join(firstParty, relative);
    const upstream = path.join(installed, relative);
    assert.ok(fs.existsSync(upstream), `missing upstream extraction input: ${relative}`);
    if (relative === "package.json") {
      const actual = JSON.parse(fs.readFileSync(source, "utf8"));
      const expected = JSON.parse(fs.readFileSync(upstream, "utf8"));
      actual.files = [...actual.files].filter((file) => file !== "LICENSE" && file !== "UPSTREAM.md").sort();
      expected.files = [...expected.files].sort();
      assert.deepEqual(actual, expected, "package metadata differs beyond provenance file declarations");
    } else {
      assert.equal(sha256(source), sha256(upstream), `source drift: ${relative}`);
    }
  }
});

test("local dependency keeps the original pi-subagents import path", () => {
  assert.equal(lock.packages[""].dependencies["pi-subagents"], "file:../packages/pi-subagents-control");
  assert.equal(lock.packages[""].dependencies.jiti, "2.7.0");
  assert.equal(lock.packages[""].dependencies.yaml, "2.8.3");
  assert.deepEqual(lock.packages["node_modules/pi-subagents"], { resolved: "../packages/pi-subagents-control", link: true });
});

console.log("pi-subagents-control provenance: ok");
