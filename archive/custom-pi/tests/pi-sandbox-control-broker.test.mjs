import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { createJiti } from "../pi/npm/node_modules/jiti/lib/jiti.mjs";

const jiti = createJiti(import.meta.url, { interopDefault: true });
const { default: broker } = await jiti.import("../pi/packages/pi-sandbox-control/src/index.ts");
const adapter = await jiti.import("../pi/packages/pi-sandbox-control/src/manifest-adapter.ts");
const symbol = Symbol.for("pi.controllerChannel.v1");

function pi() {
  const tools = [];
  return { tools, registerTool(tool) { tools.push(tool); } };
}

const originalManifest = process.env.PI_RUNTIME_MANIFEST;
const originalChannel = globalThis[symbol];
try {
  delete process.env.PI_RUNTIME_MANIFEST;
  delete globalThis[symbol];
  const unbound = pi();
  broker(unbound);
  assert.deepEqual(unbound.tools, []);

  const fixture = JSON.parse(readFileSync("tests/fixtures/control-plane/run-manifest.v2.json", "utf8"));
  fixture.conversation = { ...fixture.conversation, role: "personal", authorityProfile: "writer-container" };
  fixture.scope = { ...fixture.scope, source: "assigned-working-copy" };
  fixture.workingCopy = { workingCopyId: fixture.scope.workingCopyId, projectId: fixture.project.projectId, resourceVersion: fixture.scope.workingCopyResourceVersion, kind: "primary", purpose: "personal", effectiveMode: "isolated", hostPath: fixture.scope.rootPath, gitDir: "/git/worktrees/p5", writerEpoch: 1 };
  fixture.hostProcess = { ...fixture.hostProcess, toolProfile: "personal" };
  const imageDigest = "sha256:" + "e".repeat(64);
  fixture.toolRuntime = {
    specVersion: 2, specHash: "", platform: "linux/amd64", imageReference: `python@${imageDigest}`, imageConfigId: "sha256:" + "f".repeat(64), registryDigest: imageDigest,
    command: ["python3", "-c", "idle"], uid: 1000, gid: 1000, workdir: "/workspace", readOnlyRoot: true,
    mounts: [{ kind: "working-copy", source: fixture.scope.rootPath, target: "/workspace", readOnly: false, sourceDevice: 1, sourceInode: 2 }, { kind: "git-mask", source: "/state/mask", target: "/workspace/.git", readOnly: true, sourceDevice: 1, sourceInode: 3 }, { kind: "package-environment", source: "/state/environments/wc", target: "/environments", readOnly: false, sourceDevice: 1, sourceInode: 4 }],
    tmpfs: { "/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777" }, networkMode: "none", capDrop: ["ALL"], securityOpt: ["no-new-privileges:true"], environment: { HOME: "/tmp" }, labels: { "pi.control.managed": "true" }, resources: { memoryBytes: 1, nanoCpus: 1, pidsLimit: 1 },
  };
  fixture.toolRuntime.specHash = adapter.toolRuntimeSpecHash(fixture.toolRuntime);
  fixture.manifestDigest = adapter.manifestDigest(fixture);
  const directory = mkdtempSync(path.join(tmpdir(), "pi-broker-"));
  const manifest = path.join(directory, "manifest.json");
  writeFileSync(manifest, JSON.stringify(fixture, Object.keys(fixture).sort()), { mode: 0o600 });
  // The replacer above is intentionally unusable for nested objects; write the
  // adapter's canonical form by recreating its recursive ordering here.
  const canonical = (value) => Array.isArray(value) ? `[${value.map(canonical).join(",")}]` : value && typeof value === "object" ? `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}` : JSON.stringify(value);
  writeFileSync(manifest, canonical(fixture), { mode: 0o600 });
  chmodSync(manifest, 0o600);
  process.env.PI_RUNTIME_MANIFEST = manifest;
  const calls = [];
  globalThis[symbol] = { async request(operation, payload, signal) { calls.push({ operation, payload, signal }); return { ok: true }; } };
  const bound = pi();
  broker(bound);
  assert.deepEqual(bound.tools.map((tool) => tool.name).sort(), ["bash", "edit", "read", "write"]);
  const read = bound.tools.find((tool) => tool.name === "read");
  const signal = new AbortController().signal;
  const value = await read.execute("call-1", { path: "README" }, signal);
  assert.equal(value.content[0].text, '{"ok":true}');
  assert.deepEqual(calls[0].operation, "writer-tool");
  assert.deepEqual(calls[0].payload, { tool: "read", arguments: { path: "README" } });
  assert.equal(calls[0].signal, signal);

  delete globalThis[symbol];
  const noChannel = pi();
  broker(noChannel);
  assert.deepEqual(noChannel.tools, []);
} finally {
  if (originalManifest === undefined) delete process.env.PI_RUNTIME_MANIFEST; else process.env.PI_RUNTIME_MANIFEST = originalManifest;
  if (originalChannel === undefined) delete globalThis[symbol]; else globalThis[symbol] = originalChannel;
}

console.log("pi-sandbox-control broker-only channel tools: ok");
