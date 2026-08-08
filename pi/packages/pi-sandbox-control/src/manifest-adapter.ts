/**
 * Manifest-driven runtime boundary for pi-sandbox-control.
 *
 * This module is intentionally side-effect free apart from reading one
 * controller-owned manifest. It does not select a project, worktree, branch,
 * session, or container. Docker execution and live activation are later gates.
 */

import { createHash } from "node:crypto";
import { lstatSync, readFileSync, realpathSync } from "node:fs";
import path from "node:path";

export const MANIFEST_SCHEMA_VERSION = 1;
export const RUNTIME_ATTESTATION_VERSION = 1;

const SHA256 = /^sha256:[0-9a-f]{64}$/;
const IMAGE_REF = /^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?(?::[0-9]+)?(?:\/[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)*@sha256:[0-9a-f]{64}$/;
const ID = /^(?:run|op|conv|prj|wc)_[a-f0-9]{32}$/;
const OID = /^[0-9a-f]+$/;
const TOP_KEYS = new Set([
	"schemaVersion", "runId", "operationId", "taskId", "conversationId", "piSessionId", "parentRunId",
	"project", "workingCopy", "authority", "runtime", "owner", "capabilityHash", "attestationNonce",
	"createdAt", "expiresAt", "manifestDigest",
]);
const PROJECT_KEYS = new Set(["projectId", "resourceVersion", "objectFormat", "trustMode", "policyHash"]);
const WORKING_KEYS = new Set([
	"workingCopyId", "resourceVersion", "kind", "purpose", "effectiveMode", "hostPath", "gitCommonDir", "gitDir",
	"branchRef", "headOid", "treeOid", "dirtyFingerprint", "writerEpoch",
]);
const RUNTIME_KEYS = new Set([
	"runtimeSpecVersion", "runtimeSpecHash", "executionTarget", "platform", "imageDigest", "controllerBuildId", "piVersion",
]);
const OWNER_KEYS = new Set(["uid", "gid", "pid", "processStartIdentity"]);

export interface RuntimeProject {
	projectId: string;
	resourceVersion: number;
	objectFormat: "sha1" | "sha256";
	trustMode: "trusted" | "isolated";
	policyHash: string;
}

export interface RuntimeWorkingCopy {
	workingCopyId: string;
	resourceVersion: number;
	kind: "primary" | "worktree" | "isolated" | "review";
	purpose: "personal" | "workstream" | "integration" | "review" | "recovery" | "other";
	effectiveMode: "trusted-live" | "isolated" | "read-only";
	hostPath: string;
	gitCommonDir: string;
	gitDir: string;
	branchRef: string | null;
	headOid: string | null;
	treeOid: string | null;
	dirtyFingerprint: string | null;
	writerEpoch: number | null;
}

export interface RuntimeManifest {
	schemaVersion: 1;
	runId: string;
	operationId: string;
	taskId: string | null;
	conversationId: string;
	piSessionId: string;
	parentRunId: string | null;
	project: RuntimeProject;
	workingCopy: RuntimeWorkingCopy | null;
	authority: "read-only" | "writer" | "secretary" | "host-maintenance";
	runtime: {
		runtimeSpecVersion: 1;
		runtimeSpecHash: string;
		executionTarget: string;
		platform: string;
		imageDigest: string;
		controllerBuildId: string;
		piVersion: string;
	};
	owner: { uid: number; gid: number; pid: number; processStartIdentity: string };
	capabilityHash: string;
	attestationNonce: string;
	createdAt: string;
	expiresAt: string | null;
	manifestDigest: string;
}

export interface RuntimeRouteBinding {
	runId?: string;
	session?: string;
	startingOid?: string;
	projectId?: string;
	workingCopyId?: string;
	worktree?: string;
	branch?: string;
	gitCommonDir?: string;
	gitDir?: string;
	policyHash?: string;
	containerPlatform?: string;
	runtimeHelper?: string;
	image?: string;
}

export interface RuntimeMountObservation {
	source: string;
	target: string;
	mode: "ro" | "rw";
	propagation: string;
	recursiveReadOnly: boolean;
}

export interface RuntimeContainerObservation {
	id: string;
	name: string;
	imageId: string;
	imageDigest: string;
	platform: string;
	running: boolean;
	uid: number;
	gid: number;
	projectId: string;
	workingCopyId: string | null;
	branchRef: string | null;
	headOid: string | null;
	treeOid: string | null;
	gitCommonDir: string | null;
	gitDir: string | null;
	writable: boolean;
	labels?: Record<string, string>;
	mounts: RuntimeMountObservation[];
}

export interface RuntimeCreateRequest {
	image: string;
	name: string;
	labels: Record<string, string>;
	environment: Record<string, string>;
	mounts: RuntimeMountObservation[];
	readiness: "attestation-required";
}

export class ManifestAdapterError extends Error {
	readonly code = "CP_RUNTIME_MANIFEST_INVALID";
}

function object(value: unknown, name: string): Record<string, unknown> {
	if (!value || typeof value !== "object" || Array.isArray(value)) throw new ManifestAdapterError(`${name} must be an object`);
	return value as Record<string, unknown>;
}

function exact(value: Record<string, unknown>, keys: Set<string>, name: string): void {
	if (Object.keys(value).length !== keys.size || Object.keys(value).some((key) => !keys.has(key))) {
		throw new ManifestAdapterError(`${name} contains unknown or missing fields`);
	}
}

function text(value: unknown, name: string, max = 4096): string {
	if (typeof value !== "string" || value.length === 0 || value.length > max || value.includes("\0")) throw new ManifestAdapterError(`${name} is invalid`);
	return value;
}

function digest(value: unknown, name: string): string {
	const result = text(value, name);
	if (!SHA256.test(result)) throw new ManifestAdapterError(`${name} is not a canonical SHA-256 digest`);
	return result;
}

function integer(value: unknown, name: string, minimum = 0): number {
	if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) throw new ManifestAdapterError(`${name} is invalid`);
	return value;
}

function id(value: unknown, prefix: string, name: string): string {
	const result = text(value, name);
	if (!ID.test(result) || !result.startsWith(`${prefix}_`)) throw new ManifestAdapterError(`${name} is invalid`);
	return result;
}

function absolutePath(value: unknown, name: string): string {
	const result = text(value, name);
	if (!path.isAbsolute(result) || result !== path.posix.normalize(result) || result.split("/").includes("..")) throw new ManifestAdapterError(`${name} is not a normalized absolute path`);
	return result;
}

function oid(value: unknown, format: "sha1" | "sha256", name: string): string | null {
	if (value === null) return null;
	const result = text(value, name).toLowerCase();
	const length = format === "sha1" ? 40 : 64;
	if (result.length !== length || !OID.test(result)) throw new ManifestAdapterError(`${name} is invalid for ${format}`);
	return result;
}

function canonical(value: unknown): string {
	if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
	if (value && typeof value === "object") {
		const entries = Object.entries(value as Record<string, unknown>).sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0);
		return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(",")}}`;
	}
	return JSON.stringify(value);
}

function withoutDigest(manifest: Record<string, unknown>): Record<string, unknown> {
	const copy = { ...manifest };
	delete copy.manifestDigest;
	return copy;
}

export function manifestDigest(manifest: Record<string, unknown>): string {
	return `sha256:${createHash("sha256").update(canonical(withoutDigest(manifest))).digest("hex")}`;
}

function validateWorkingCopy(value: unknown, project: RuntimeProject, authority: RuntimeManifest["authority"]): RuntimeWorkingCopy | null {
	if (value === null) {
		if (authority === "writer") throw new ManifestAdapterError("writer manifest requires a working copy");
		return null;
	}
	if (authority === "secretary" || authority === "host-maintenance") throw new ManifestAdapterError("this authority cannot bind a working copy");
	const raw = object(value, "workingCopy");
	exact(raw, WORKING_KEYS, "workingCopy");
	const result = {
		workingCopyId: id(raw.workingCopyId, "wc", "workingCopyId"),
		resourceVersion: integer(raw.resourceVersion, "workingCopy.resourceVersion", 1),
		kind: raw.kind, purpose: raw.purpose, effectiveMode: raw.effectiveMode,
		hostPath: absolutePath(raw.hostPath, "workingCopy.hostPath"),
		gitCommonDir: absolutePath(raw.gitCommonDir, "workingCopy.gitCommonDir"),
		gitDir: absolutePath(raw.gitDir, "workingCopy.gitDir"),
		branchRef: raw.branchRef, headOid: oid(raw.headOid, project.objectFormat, "workingCopy.headOid"),
		treeOid: oid(raw.treeOid, project.objectFormat, "workingCopy.treeOid"),
		dirtyFingerprint: raw.dirtyFingerprint === null ? null : text(raw.dirtyFingerprint, "workingCopy.dirtyFingerprint", 256),
		writerEpoch: raw.writerEpoch === null ? null : integer(raw.writerEpoch, "workingCopy.writerEpoch"),
	} as RuntimeWorkingCopy;
	if (result.kind !== "primary" && result.kind !== "worktree" && result.kind !== "isolated" && result.kind !== "review") throw new ManifestAdapterError("workingCopy.kind is invalid");
	if (!["personal", "workstream", "integration", "review", "recovery", "other"].includes(String(result.purpose))) throw new ManifestAdapterError("workingCopy.purpose is invalid");
	if (!["trusted-live", "isolated", "read-only"].includes(String(result.effectiveMode))) throw new ManifestAdapterError("workingCopy.effectiveMode is invalid");
	if (result.branchRef !== null && (typeof result.branchRef !== "string" || !result.branchRef.startsWith("refs/"))) throw new ManifestAdapterError("workingCopy.branchRef is invalid");
	if (authority === "writer" && (result.effectiveMode === "read-only" || result.writerEpoch === null || result.writerEpoch < 1)) throw new ManifestAdapterError("writer manifest requires a positive writer epoch and writable mode");
	return result;
}

export function validateRuntimeManifest(value: unknown): RuntimeManifest {
	const raw = object(value, "manifest");
	exact(raw, TOP_KEYS, "manifest");
	if (raw.schemaVersion !== MANIFEST_SCHEMA_VERSION) throw new ManifestAdapterError("unsupported manifest schema");
	const authority = raw.authority;
	if (authority !== "read-only" && authority !== "writer" && authority !== "secretary" && authority !== "host-maintenance") throw new ManifestAdapterError("authority is invalid");
	const projectRaw = object(raw.project, "project");
	exact(projectRaw, PROJECT_KEYS, "project");
	const project = {
		projectId: id(projectRaw.projectId, "prj", "projectId"), resourceVersion: integer(projectRaw.resourceVersion, "project.resourceVersion", 1),
		objectFormat: projectRaw.objectFormat, trustMode: projectRaw.trustMode, policyHash: digest(projectRaw.policyHash, "project.policyHash"),
	} as RuntimeProject;
	if (project.objectFormat !== "sha1" && project.objectFormat !== "sha256") throw new ManifestAdapterError("project.objectFormat is invalid");
	if (project.trustMode !== "trusted" && project.trustMode !== "isolated") throw new ManifestAdapterError("project.trustMode is invalid");
	const runtimeRaw = object(raw.runtime, "runtime");
	exact(runtimeRaw, RUNTIME_KEYS, "runtime");
	const runtime = {
		runtimeSpecVersion: runtimeRaw.runtimeSpecVersion, runtimeSpecHash: digest(runtimeRaw.runtimeSpecHash, "runtimeSpecHash"),
		executionTarget: text(runtimeRaw.executionTarget, "executionTarget", 256), platform: text(runtimeRaw.platform, "platform", 256),
		imageDigest: digest(runtimeRaw.imageDigest, "imageDigest"), controllerBuildId: text(runtimeRaw.controllerBuildId, "controllerBuildId", 512), piVersion: text(runtimeRaw.piVersion, "piVersion", 512),
	} as RuntimeManifest["runtime"];
	if (runtime.runtimeSpecVersion !== 1) throw new ManifestAdapterError("unsupported runtime specification");
	const ownerRaw = object(raw.owner, "owner");
	exact(ownerRaw, OWNER_KEYS, "owner");
	const owner = { uid: integer(ownerRaw.uid, "owner.uid"), gid: integer(ownerRaw.gid, "owner.gid"), pid: integer(ownerRaw.pid, "owner.pid", 1), processStartIdentity: text(ownerRaw.processStartIdentity, "owner.processStartIdentity", 256) };
	const result = {
		schemaVersion: 1 as const, runId: id(raw.runId, "run", "runId"), operationId: id(raw.operationId, "op", "operationId"),
		taskId: raw.taskId === null ? null : text(raw.taskId, "taskId", 256), conversationId: id(raw.conversationId, "conv", "conversationId"),
		piSessionId: text(raw.piSessionId, "piSessionId", 512), parentRunId: raw.parentRunId === null ? null : id(raw.parentRunId, "run", "parentRunId"),
		project, workingCopy: null as RuntimeWorkingCopy | null, authority, runtime, owner, capabilityHash: digest(raw.capabilityHash, "capabilityHash"),
		attestationNonce: text(raw.attestationNonce, "attestationNonce", 512), createdAt: text(raw.createdAt, "createdAt", 128),
		expiresAt: raw.expiresAt === null ? null : text(raw.expiresAt, "expiresAt", 128), manifestDigest: digest(raw.manifestDigest, "manifestDigest"),
	};
	result.workingCopy = validateWorkingCopy(raw.workingCopy, project, authority);
	if (result.manifestDigest !== manifestDigest(raw)) throw new ManifestAdapterError("manifest digest does not match canonical content");
	return result;
}

export function readRuntimeManifest(manifestPath: string): RuntimeManifest {
	const metadata = lstatSync(manifestPath);
	if (!metadata.isFile() || metadata.isSymbolicLink() || (metadata.mode & 0o7777) !== 0o600) throw new ManifestAdapterError("manifest must be an exact 0600 regular file");
	if (realpathSync(manifestPath) !== manifestPath) throw new ManifestAdapterError("manifest path must be canonical");
	let parsed: unknown;
	try { parsed = JSON.parse(readFileSync(manifestPath, "utf8")); } catch (error) { throw new ManifestAdapterError(`manifest JSON is invalid: ${String(error)}`); }
	if (canonical(parsed) !== readFileSync(manifestPath, "utf8")) throw new ManifestAdapterError("manifest JSON is not canonical");
	return validateRuntimeManifest(parsed);
}

export function assertRouteBinding(manifest: RuntimeManifest, route: RuntimeRouteBinding): void {
	if (route.runId !== undefined && route.runId !== manifest.runId) throw new ManifestAdapterError("route run identity differs from manifest");
	if (route.session !== undefined && route.session !== manifest.piSessionId) throw new ManifestAdapterError("route session differs from manifest");
	if (route.startingOid !== undefined && route.startingOid !== manifest.workingCopy?.headOid) throw new ManifestAdapterError("route starting OID differs from manifest");
	if (route.projectId !== undefined && route.projectId !== manifest.project.projectId) throw new ManifestAdapterError("route project differs from manifest");
	if (route.workingCopyId !== undefined && route.workingCopyId !== manifest.workingCopy?.workingCopyId) throw new ManifestAdapterError("route working copy differs from manifest");
	const working = manifest.workingCopy;
	if (route.worktree !== undefined && route.worktree !== working?.hostPath) throw new ManifestAdapterError("route worktree differs from manifest");
	if (route.branch !== undefined) {
		const normalizedBranch = route.branch.startsWith("refs/") ? route.branch : `refs/heads/${route.branch}`;
		if (normalizedBranch !== working?.branchRef) throw new ManifestAdapterError("route branch differs from manifest");
	}
	if (route.gitCommonDir !== undefined && route.gitCommonDir !== working?.gitCommonDir) throw new ManifestAdapterError("route Git common directory differs from manifest");
	if (route.gitDir !== undefined && route.gitDir !== working?.gitDir) throw new ManifestAdapterError("route Git directory differs from manifest");
	if (route.policyHash !== undefined && route.policyHash !== manifest.project.policyHash) throw new ManifestAdapterError("route policy differs from manifest");
	if (route.containerPlatform !== undefined && route.containerPlatform !== manifest.runtime.platform) throw new ManifestAdapterError("route platform differs from manifest");
	if (route.runtimeHelper !== undefined && route.runtimeHelper.length === 0) throw new ManifestAdapterError("route runtime helper is empty");
	if (route.image !== undefined && (!IMAGE_REF.test(route.image) || !route.image.endsWith(`@${manifest.runtime.imageDigest}`))) throw new ManifestAdapterError("route image must be an immutable manifest-pinned image");
}

export function runtimeContainerName(manifest: RuntimeManifest): string {
	return `pi-runtime-${createHash("sha256").update(manifest.runId).digest("hex").slice(0, 16)}`;
}

export function runtimeContainerLabels(manifest: RuntimeManifest): Record<string, string> {
	return {
		"pi.control.managed": "true",
		"pi.control.run-id": manifest.runId,
		"pi.control.manifest-digest": manifest.manifestDigest,
		"pi.control.project-id": manifest.project.projectId,
		"pi.control.policy-hash": manifest.project.policyHash,
		"pi.control.runtime-spec-hash": manifest.runtime.runtimeSpecHash,
		"pi.control.controller-build-id": manifest.runtime.controllerBuildId,
		...(manifest.workingCopy ? {
			"pi.control.working-copy-id": manifest.workingCopy.workingCopyId,
			"pi.control.writer-epoch": String(manifest.workingCopy.writerEpoch ?? 0),
		} : {}),
	};
}

export function buildRuntimeCreateRequest(manifest: RuntimeManifest, image: string, name: string): RuntimeCreateRequest {
	const separator = image.lastIndexOf("@");
	if (separator <= 0 || !IMAGE_REF.test(image) || image.slice(separator + 1) !== manifest.runtime.imageDigest) throw new ManifestAdapterError("runtime image must be repository@manifest-digest");
	if (name !== runtimeContainerName(manifest)) throw new ManifestAdapterError("runtime name must be derived from the manifest run identity");
	const working = manifest.workingCopy;
	const readOnly = manifest.authority !== "writer" || working?.effectiveMode === "read-only";
	return {
		image, name: text(name, "container name", 128), labels: runtimeContainerLabels(manifest),
		environment: {
			PI_RUN_ID: manifest.runId, PI_MANIFEST_DIGEST: manifest.manifestDigest, PI_ATTESTATION_NONCE: manifest.attestationNonce,
			PI_PROJECT_ID: manifest.project.projectId, ...(working ? { PI_WORKING_COPY_ID: working.workingCopyId } : {}),
		},
		mounts: working ? [{ source: working.hostPath, target: "/workspace", mode: readOnly ? "ro" : "rw", propagation: "rprivate", recursiveReadOnly: false }] : [],
		readiness: "attestation-required",
	};
}

export function assertContainerAttestation(manifest: RuntimeManifest, observation: RuntimeContainerObservation, expectedMounts?: RuntimeMountObservation[]): void {
	if (!observation.id || !observation.name || !observation.imageId) throw new ManifestAdapterError("runtime identity observation is incomplete");
	if (observation.name !== runtimeContainerName(manifest)) throw new ManifestAdapterError("runtime name does not match manifest run identity");
	const labels = observation.labels;
	const expectedLabels = runtimeContainerLabels(manifest);
	if (!labels || Object.entries(expectedLabels).some(([key, expected]) => labels[key] !== expected) || Object.keys(labels).some((key) => key.startsWith("pi.control.") && !(key in expectedLabels))) throw new ManifestAdapterError("runtime labels do not match manifest");
	if (!observation.running) throw new ManifestAdapterError("runtime is not running");
	if (observation.imageId !== manifest.runtime.imageDigest || observation.imageDigest !== manifest.runtime.imageDigest || observation.platform !== manifest.runtime.platform || observation.uid !== manifest.owner.uid || observation.gid !== manifest.owner.gid) throw new ManifestAdapterError("runtime image, platform, or identity differs from manifest");
	if (observation.projectId !== manifest.project.projectId) throw new ManifestAdapterError("runtime project differs from manifest");
	const working = manifest.workingCopy;
	if (observation.workingCopyId !== (working?.workingCopyId ?? null) || observation.branchRef !== (working?.branchRef ?? null) || observation.headOid !== (working?.headOid ?? null) || observation.treeOid !== (working?.treeOid ?? null) || observation.gitCommonDir !== (working?.gitCommonDir ?? null) || observation.gitDir !== (working?.gitDir ?? null)) throw new ManifestAdapterError("runtime source identity differs from manifest");
	const expectedWritable = working !== null && manifest.authority === "writer" && working.effectiveMode !== "read-only";
	if (observation.writable !== expectedWritable) throw new ManifestAdapterError("runtime writability differs from manifest");
	const mounts = expectedMounts ?? (working ? [{ source: working.hostPath, target: "/workspace", mode: expectedWritable ? "rw" : "ro", propagation: "rprivate", recursiveReadOnly: false }] : []);
	const sortMounts = (values: RuntimeMountObservation[]) => values.slice().sort((left, right) => left.target.localeCompare(right.target));
	if (canonical(sortMounts(observation.mounts)) !== canonical(sortMounts(mounts))) throw new ManifestAdapterError("runtime mounts differ from manifest");
}
