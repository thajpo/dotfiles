import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { createJiti } from "../../pi/npm/node_modules/jiti/lib/jiti.mjs";

const filename = path.resolve("pi/extensions/continuity/index.ts");
const source = readFileSync(filename, "utf8");
assert.match(source, /conversation-continuity\.v1/);
assert.match(source, /session_compact/);
assert.match(source, /registerCommand\("continuity"/);
assert.match(source, /pi\.appendEntry/);
assert.match(source, /compactionEntryId/);
assert.match(source, /summaryDigest/);
assert.match(source, /latestActivePacket/);
assert.doesNotMatch(source, /setStatus\("continuity"/);
const jiti = createJiti(process.cwd());
const transformed = jiti.transform({ source, filename, ts: true });
assert.ok(transformed.length > 0);
console.log("continuity extension: ok");
