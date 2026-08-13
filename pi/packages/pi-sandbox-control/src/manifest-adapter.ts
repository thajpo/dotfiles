/** Side-effect-free validator for the canonical Pi run manifest. */

import { createHash } from "node:crypto";
import { lstatSync, readFileSync, realpathSync } from "node:fs";
import path from "node:path";

export const MANIFEST_SCHEMA_VERSION = 2;
const SHA256 = /^sha256:[0-9a-f]{64}$/;
const ID = /^(?:run|op|conv|prj|wc)_[a-f0-9]{32}$/;
const TOP = new Set(["schemaVersion", "runId", "operationId", "parentRunId", "conversation", "session", "project", "scope", "workingCopy", "installedBuild", "hostProcess", "toolRuntime", "channelBindingHash", "supervisorOwner", "createdAt", "expiresAt", "manifestDigest"]);
const PROFILES = {
	secretary: "host-read-only", investigator: "host-read-only", reviewer: "host-read-only",
	personal: "writer-container", workstream: "writer-container", integration: "writer-container",
} as const;
const TOOL_KEYS = new Set(["specVersion", "specHash", "imageReference", "imageConfigId", "registryDigest", "platform", "command", "uid", "gid", "workdir", "mounts", "readOnlyRoot", "tmpfs", "networkMode", "capDrop", "securityOpt", "environment", "labels", "resources"]);

export interface RuntimeWorkingCopy { workingCopyId: string; projectId: string; resourceVersion: number; hostPath: string; writerEpoch: number; [key: string]: unknown }
export interface ToolRuntime { specVersion: 2; specHash: string; imageReference: string; imageConfigId: string; registryDigest: string; platform: string; command: string[]; uid: number; gid: number; workdir: "/workspace"; mounts: Array<Record<string, unknown>>; readOnlyRoot: true; tmpfs: Record<string, string>; networkMode: "none"; capDrop: ["ALL"]; securityOpt: ["no-new-privileges:true"]; environment: Record<string, string>; labels: Record<string, string>; resources: { memoryBytes: number; nanoCpus: number; pidsLimit: number } }
export interface RuntimeManifest {
	schemaVersion: 2; runId: string; operationId: string; parentRunId: string | null;
	conversation: { conversationId: string; role: keyof typeof PROFILES; authorityProfile: "host-read-only" | "writer-container" };
	project: { projectId: string; resourceVersion: number; [key: string]: unknown };
	scope: { workingCopyId: string; workingCopyResourceVersion: number; rootPath: string; [key: string]: unknown };
	workingCopy: RuntimeWorkingCopy | null; toolRuntime: ToolRuntime | null; manifestDigest: string;
	[key: string]: unknown;
}

export class ManifestAdapterError extends Error { readonly code = "CP_RUNTIME_MANIFEST_INVALID"; }
function object(value: unknown, name: string): Record<string, unknown> { if (!value || typeof value !== "object" || Array.isArray(value)) throw new ManifestAdapterError(`${name} must be an object`); return value as Record<string, unknown>; }
function exact(value: Record<string, unknown>, keys: Set<string>, name: string): void { if (Object.keys(value).length !== keys.size || Object.keys(value).some((key) => !keys.has(key))) throw new ManifestAdapterError(`${name} contains unknown or missing fields`); }
function digest(value: unknown, name: string): string { if (typeof value !== "string" || !SHA256.test(value)) throw new ManifestAdapterError(`${name} is invalid`); return value; }
function canonical(value: unknown): string { if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`; if (value && typeof value === "object") { const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)); return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(",")}}`; } return JSON.stringify(value); }
export function manifestDigest(manifest: Record<string, unknown>): string { const copy = { ...manifest }; delete copy.manifestDigest; return `sha256:${createHash("sha256").update(canonical(copy)).digest("hex")}`; }
export function toolRuntimeSpecHash(tool: Record<string, unknown>): string { const copy = { ...tool }; delete copy.specHash; return `sha256:${createHash("sha256").update(canonical(copy)).digest("hex")}`; }

function validateToolRuntime(value: unknown): ToolRuntime {
	const tool = object(value, "toolRuntime"); exact(tool, TOOL_KEYS, "toolRuntime");
	if (tool.specVersion !== 2 || !Array.isArray(tool.command) || tool.command.length === 0 || tool.command.some((item) => typeof item !== "string" || !item || item.includes("\0"))) throw new ManifestAdapterError("toolRuntime command is invalid");
	if (typeof tool.imageReference !== "string" || !tool.imageReference.endsWith(`@${digest(tool.registryDigest, "registryDigest")}`)) throw new ManifestAdapterError("toolRuntime image is not digest-pinned");
	digest(tool.specHash, "specHash"); digest(tool.imageConfigId, "imageConfigId");
	if (tool.workdir !== "/workspace" || tool.readOnlyRoot !== true || tool.networkMode !== "none" || canonical(tool.capDrop) !== '["ALL"]' || canonical(tool.securityOpt) !== '["no-new-privileges:true"]') throw new ManifestAdapterError("toolRuntime isolation is incomplete");
	if (!Number.isSafeInteger(tool.uid) || !Number.isSafeInteger(tool.gid) || !Array.isArray(tool.mounts) || tool.mounts.length !== 3) throw new ManifestAdapterError("toolRuntime identity or mounts are invalid");
	const mounts = tool.mounts as Array<Record<string, unknown>>;
	if (mounts[0]?.target !== "/workspace" || mounts[1]?.target !== "/workspace/.git" || mounts[2]?.kind !== "package-environment" || mounts[2]?.target !== "/environments" || mounts[2]?.readOnly !== false) throw new ManifestAdapterError("toolRuntime package environment mount is invalid");
	for (const key of ["tmpfs", "environment", "labels", "resources"]) object(tool[key], `toolRuntime.${key}`);
	if (tool.specHash !== toolRuntimeSpecHash(tool)) throw new ManifestAdapterError("toolRuntime specHash is invalid");
	return tool as unknown as ToolRuntime;
}

export function validateRuntimeManifest(value: unknown): RuntimeManifest {
	const raw = object(value, "manifest"); exact(raw, TOP, "manifest");
	if (raw.schemaVersion !== MANIFEST_SCHEMA_VERSION || typeof raw.runId !== "string" || !ID.test(raw.runId) || typeof raw.operationId !== "string" || !ID.test(raw.operationId)) throw new ManifestAdapterError("manifest identity is invalid");
	const conversation = object(raw.conversation, "conversation");
	const role = conversation.role as keyof typeof PROFILES;
	if (set(conversation) !== "authorityProfile,conversationId,role" || !PROFILES[role] || conversation.authorityProfile !== PROFILES[role] || typeof conversation.conversationId !== "string" || !ID.test(conversation.conversationId)) throw new ManifestAdapterError("conversation authority is invalid");
	const project = object(raw.project, "project");
	const scope = object(raw.scope, "scope");
	const session = object(raw.session, "session");
	const build = object(raw.installedBuild, "installedBuild");
	const host = object(raw.hostProcess, "hostProcess");
	if (typeof project.projectId !== "string" || !ID.test(project.projectId) || scope.projectId !== project.projectId || scope.projectResourceVersion !== project.resourceVersion) throw new ManifestAdapterError("project scope is invalid");
	if (session.piSessionId !== `pi-${conversation.conversationId}` || typeof session.sessionPath !== "string" || !session.sessionPath.endsWith(`/sessions/${project.projectId}/${conversation.conversationId}.jsonl`)) throw new ManifestAdapterError("session binding is invalid");
	if (typeof build.piVersion !== "string" || !build.piVersion || build.piVersion === "unknown" || build.piVersion === "0.0.0") throw new ManifestAdapterError("installed build is invalid");
	if (typeof host.executableSha256 !== "string" || !SHA256.test(host.executableSha256) || host.toolProfile !== role || !Array.isArray(host.argv) || host.argv[0] !== host.executable) throw new ManifestAdapterError("host process is invalid");
	const working = raw.workingCopy === null ? null : object(raw.workingCopy, "workingCopy") as unknown as RuntimeWorkingCopy;
	const tool = raw.toolRuntime === null ? null : validateToolRuntime(raw.toolRuntime);
	if ((PROFILES[role] === "writer-container") !== Boolean(working && tool)) throw new ManifestAdapterError("writer manifest bindings are incomplete");
	if (working && (!Number.isSafeInteger(working.writerEpoch) || working.writerEpoch < 1)) throw new ManifestAdapterError("writer epoch is invalid");
	digest(raw.channelBindingHash, "channelBindingHash"); digest(raw.manifestDigest, "manifestDigest");
	if (raw.manifestDigest !== manifestDigest(raw)) throw new ManifestAdapterError("manifest digest does not match canonical content");
	return raw as unknown as RuntimeManifest;
}

function set(value: Record<string, unknown>): string { return Object.keys(value).sort().join(","); }

export function readRuntimeManifest(manifestPath: string): RuntimeManifest {
	if (!path.isAbsolute(manifestPath)) throw new ManifestAdapterError("manifest path must be absolute");
	const metadata = lstatSync(manifestPath);
	if (!metadata.isFile() || metadata.isSymbolicLink() || (metadata.mode & 0o7777) !== 0o600 || realpathSync(manifestPath) !== manifestPath) throw new ManifestAdapterError("manifest must be a canonical 0600 regular file");
	const body = readFileSync(manifestPath, "utf8");
	let parsed: unknown;
	try { parsed = JSON.parse(body); } catch { throw new ManifestAdapterError("manifest JSON is invalid"); }
	if (canonical(parsed) !== body) throw new ManifestAdapterError("manifest JSON is not canonical");
	return validateRuntimeManifest(parsed);
}
