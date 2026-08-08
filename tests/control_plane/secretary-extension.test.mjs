import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { createJiti } from "../../pi/npm/node_modules/jiti/lib/jiti.mjs";

const sourcePath = path.resolve("pi/extensions/control-plane/index.ts");
const source = readFileSync(sourcePath, "utf8");
assert.match(source, /controller_status/);
assert.match(source, /controller_focus/);
assert.match(source, /controller_submit_change/);
assert.match(source, /controller_create_workstream/);
assert.match(source, /controller_request_review/);
assert.match(source, /controller_submit_review/);
assert.match(source, /controller_analyze_integration/);
assert.match(source, /controller_authorize_integration/);
assert.match(source, /controller_integrate/);
assert.match(source, /controller_recovery_status/);
assert.match(source, /controller_technical_details/);
assert.match(source, /--request-json/);
assert.doesNotMatch(source, /PI_CONTROL_ACTIVATION/);
assert.doesNotMatch(source, /tmux|herdr/i);

const jiti = createJiti(process.cwd());
const transformed = jiti.transform({ source, filename: sourcePath, ts: true });
assert.ok(typeof transformed === "string" && transformed.length > 0);
console.log("control-plane client extension: ok");
