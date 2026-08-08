import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { createJiti } from "../pi/npm/node_modules/jiti/lib/jiti.mjs";

const jiti = createJiti(process.cwd(), { interopDefault: true });
const adapter = await jiti.import("./pi/packages/pi-sandbox-control/src/manifest-adapter.ts");
const digest = "sha256:" + "a".repeat(64);
const configId = "sha256:" + "b".repeat(64);
const manifest = {
  schemaVersion: 1,
  runId: "run_" + "1".repeat(32),
  operationId: "op_" + "2".repeat(32),
  taskId: null,
  conversationId: "conv_" + "3".repeat(32),
  piSessionId: "pi-package-test",
  parentRunId: null,
  project: { projectId: "prj_" + "4".repeat(32), resourceVersion: 1, objectFormat: "sha1", trustMode: "isolated", policyHash: digest },
  workingCopy: null,
  authority: "secretary",
  runtime: { runtimeSpecVersion: 1, runtimeSpecHash: digest, executionTarget: "host-read-only", platform: "host", imageReference: null, imageConfigId: null, registryDigest: null, controllerBuildId: "build_" + "5".repeat(32), piVersion: "test" },
  owner: { uid: 1000, gid: 1000, pid: 1, processStartIdentity: "fake:1" },
  capabilityHash: digest,
  attestationNonce: "package-attestation-nonce-abcdefghijklmnopqrstuvwxyz",
  createdAt: "2024-01-01T00:00:00Z",
  expiresAt: null,
  manifestDigest: "",
};
manifest.manifestDigest = adapter.manifestDigest(manifest);
const checked = adapter.validateRuntimeManifest(manifest);
assert.equal(checked.manifestDigest, manifest.manifestDigest);
assert.equal(adapter.runtimeContainerLabels(checked)["pi.control.run-id"], checked.runId);
const codingManifest = { ...manifest, authority: "writer", workingCopy: { workingCopyId: "wc_" + "6".repeat(32), resourceVersion: 1, kind: "worktree", purpose: "workstream", effectiveMode: "isolated", hostPath: "/workspace", gitCommonDir: "/workspace/.git", gitDir: "/workspace/.git", branchRef: "refs/heads/pi-system/test", headOid: null, treeOid: null, dirtyFingerprint: null, writerEpoch: 1 }, runtime: { runtimeSpecVersion: 1, runtimeSpecHash: digest, executionTarget: "container", platform: "linux/amd64", imageReference: `registry.example:5000/pi@${digest}`, imageConfigId: configId, registryDigest: digest, controllerBuildId: "build_" + "5".repeat(32), piVersion: "test" } };
codingManifest.manifestDigest = adapter.manifestDigest(codingManifest);
const codingChecked = adapter.validateRuntimeManifest(codingManifest);
const request = adapter.buildRuntimeCreateRequest(codingChecked, `registry.example:5000/pi@${digest}`, adapter.runtimeContainerName(codingChecked));
assert.equal(request.readiness, "attestation-required");
assert.equal(request.mounts.length, 1);
assert.throws(() => adapter.buildRuntimeCreateRequest(codingChecked, `registry.example:5000/pi@${digest}`, "pi-runtime-test"), adapter.ManifestAdapterError);
adapter.assertRouteBinding(checked, { projectId: checked.project.projectId, policyHash: checked.project.policyHash, containerPlatform: checked.runtime.platform });
assert.throws(() => adapter.validateRuntimeManifest({ ...manifest, unknown: true }), adapter.ManifestAdapterError);
assert.throws(() => adapter.buildRuntimeCreateRequest(checked, `registry.example/pi@${"sha256:" + "b".repeat(64)}`, "pi-runtime-test"), adapter.ManifestAdapterError);
const secretaryWithWorkingCopy = { ...manifest, workingCopy: { workingCopyId: "wc_" + "6".repeat(32), resourceVersion: 1, kind: "primary", purpose: "personal", effectiveMode: "isolated", hostPath: "/workspace", gitCommonDir: "/workspace/.git", gitDir: "/workspace/.git", branchRef: null, headOid: null, treeOid: null, dirtyFingerprint: null, writerEpoch: 0 } };
secretaryWithWorkingCopy.manifestDigest = adapter.manifestDigest(secretaryWithWorkingCopy);
assert.throws(() => adapter.validateRuntimeManifest(secretaryWithWorkingCopy), adapter.ManifestAdapterError);
const writerWithZeroEpoch = { ...codingManifest, workingCopy: { ...codingManifest.workingCopy, writerEpoch: 0 } };
writerWithZeroEpoch.manifestDigest = adapter.manifestDigest(writerWithZeroEpoch);
assert.throws(() => adapter.validateRuntimeManifest(writerWithZeroEpoch), adapter.ManifestAdapterError);
const observed = { id: "container-id", name: adapter.runtimeContainerName(codingChecked), imageId: configId, imageDigest: digest, platform: "linux/amd64", running: true, uid: 1000, gid: 1000, projectId: codingChecked.project.projectId, labels: adapter.runtimeContainerLabels(codingChecked), workingCopyId: codingChecked.workingCopy.workingCopyId, branchRef: codingChecked.workingCopy.branchRef, headOid: null, treeOid: null, gitCommonDir: codingChecked.workingCopy.gitCommonDir, gitDir: codingChecked.workingCopy.gitDir, writable: true, mounts: [{ source: "/workspace", target: "/workspace", mode: "rw", propagation: "rprivate", recursiveReadOnly: false }] };
adapter.assertContainerAttestation(codingChecked, observed);
assert.throws(() => adapter.assertContainerAttestation(codingChecked, { ...observed, imageId: "sha256:" + "c".repeat(64) }), adapter.ManifestAdapterError);
assert.throws(() => adapter.assertContainerAttestation(codingChecked, { ...observed, labels: { ...observed.labels, "pi.control.extra": "unexpected" } }), adapter.ManifestAdapterError);
assert.throws(() => adapter.assertContainerAttestation(codingChecked, { ...observed, platform: "linux/arm64" }), adapter.ManifestAdapterError);
const directory = mkdtempSync(path.join(tmpdir(), "pi-sandbox-control-test-"));
const manifestPath = path.join(realpathSync(directory), "manifest.json");
const sortKeys = (value) => Array.isArray(value) ? value.map(sortKeys) : value && typeof value === "object" ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([key, item]) => [key, sortKeys(item)])) : value;
writeFileSync(manifestPath, JSON.stringify(sortKeys(manifest)), { mode: 0o600 });
chmodSync(manifestPath, 0o600);
assert.equal(adapter.readRuntimeManifest(manifestPath).runId, checked.runId);
for (const mode of [0o400, 0o640, 0o700, 0o1600]) {
  chmodSync(manifestPath, mode);
  assert.throws(() => adapter.readRuntimeManifest(manifestPath), adapter.ManifestAdapterError);
}
chmodSync(manifestPath, 0o600);
console.log("pi-sandbox-control manifest adapter: ok");
