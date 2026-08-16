import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { createJiti } from "../pi/npm/node_modules/jiti/lib/jiti.mjs";

const jiti = createJiti(process.cwd(), { interopDefault: true });
const adapter = await jiti.import("./pi/packages/pi-sandbox-control/src/manifest-adapter.ts");
const fixtureBody = readFileSync("tests/fixtures/control-plane/run-manifest.v2.json", "utf8").trimEnd();
const fixture = JSON.parse(fixtureBody);
assert.equal(adapter.validateRuntimeManifest(fixture).manifestDigest, fixture.manifestDigest);

const clone = (value) => structuredClone(value);
const resign = (value) => { value.manifestDigest = adapter.manifestDigest(value); return value; };
const rejects = (mutate) => { const value = clone(fixture); mutate(value); resign(value); assert.throws(() => adapter.validateRuntimeManifest(value), adapter.ManifestAdapterError); };

rejects((value) => { value.conversation.role = "personal"; });
rejects((value) => { value.installedBuild.piVersion = "unknown"; });
rejects((value) => { value.session.piSessionId = "caller-session"; });
rejects((value) => { value.scope.projectId = "prj_" + "9".repeat(32); });
rejects((value) => { value.toolRuntime = { specVersion: 1 }; });
rejects((value) => { value.unknown = true; });
rejects((value) => { value.scope.projectResourceVersion = 2; });
rejects((value) => { value.hostProcess.executableSha256 = "sha256:not-a-digest"; });
const tampered = clone(fixture); tampered.hostProcess.argv.push("--tampered");
assert.throws(() => adapter.validateRuntimeManifest(tampered), adapter.ManifestAdapterError);
const executableTamper = clone(fixture);
executableTamper.hostProcess.executableSha256 = "sha256:" + "9".repeat(64);
assert.notEqual(adapter.manifestDigest(executableTamper), fixture.manifestDigest);
assert.throws(() => adapter.validateRuntimeManifest(executableTamper), adapter.ManifestAdapterError);
resign(executableTamper);
assert.equal(adapter.validateRuntimeManifest(executableTamper).hostProcess.executableSha256, executableTamper.hostProcess.executableSha256);

const imageDigest = "sha256:" + "e".repeat(64);
const imageConfigId = "sha256:" + "f".repeat(64);
const writer = clone(fixture);
writer.conversation = { ...writer.conversation, role: "personal", authorityProfile: "writer-container" };
writer.scope = { ...writer.scope, source: "assigned-working-copy" };
writer.workingCopy = { workingCopyId: writer.scope.workingCopyId, projectId: writer.project.projectId, resourceVersion: writer.scope.workingCopyResourceVersion, kind: "primary", purpose: "personal", effectiveMode: "isolated", hostPath: writer.scope.rootPath, gitDir: "/workspace/.git", writerEpoch: 1 };
writer.hostProcess = { ...writer.hostProcess, toolProfile: "personal" };
writer.toolRuntime = {
  specVersion: 2, specHash: "", platform: "linux/amd64", imageReference: `registry.invalid/pi@${imageDigest}`, imageConfigId, registryDigest: imageDigest,
  command: ["python3", "-c", "idle"], uid: 1000, gid: 1000, workdir: "/workspace", readOnlyRoot: true,
  mounts: [
    { kind: "working-copy", source: writer.scope.rootPath, target: "/workspace", readOnly: false, sourceDevice: 1, sourceInode: 2 },
    { kind: "git-mask", source: "/state/git-mask", target: "/workspace/.git", readOnly: true, sourceDevice: 1, sourceInode: 3 },
    { kind: "package-environment", target: "/environments", readOnly: false },
  ],
  tmpfs: { "/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777" }, networkMode: "none", capDrop: ["ALL"], securityOpt: ["no-new-privileges:true"],
  environment: { HOME: "/tmp" }, labels: { "pi.control.managed": "true" }, resources: { memoryBytes: 1, nanoCpus: 1, pidsLimit: 1 },
};
writer.toolRuntime.specHash = adapter.toolRuntimeSpecHash(writer.toolRuntime);
resign(writer);
const checkedWriter = adapter.validateRuntimeManifest(writer);
assert.equal(checkedWriter.toolRuntime.networkMode, "none");
assert.equal(checkedWriter.toolRuntime.mounts[1].target, "/workspace/.git");

const directory = mkdtempSync(path.join(tmpdir(), "pi-manifest-v2-"));
const manifestPath = path.join(realpathSync(directory), "manifest.json");
writeFileSync(manifestPath, fixtureBody, { mode: 0o600 });
assert.equal(adapter.readRuntimeManifest(manifestPath).runId, fixture.runId);
chmodSync(manifestPath, 0o640);
assert.throws(() => adapter.readRuntimeManifest(manifestPath), adapter.ManifestAdapterError);
console.log("pi-sandbox-control canonical manifest v2: ok");
