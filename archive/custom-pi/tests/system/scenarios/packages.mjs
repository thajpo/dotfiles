import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
const root = path.resolve(import.meta.dirname, "../../..");
const settings = JSON.parse(fs.readFileSync(path.join(root, "pi/settings.json"), "utf8"));
assert.equal(settings.packages.length, 13);
console.log(JSON.stringify({ scenarioId: "packages", status: "PASS", packages: settings.packages.length }));
