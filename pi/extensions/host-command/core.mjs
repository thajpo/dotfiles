import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const HOST_COMMAND_PROTOCOL_VERSION = 1;
export const HOST_COMMAND_PARENT_RUNTIME_ENV = "PI_HOST_COMMAND_PARENT_RUNTIME";
export const HOST_COMMAND_REQUEST_ROOT_ENV = "PI_HOST_COMMAND_REQUEST_ROOT";
export const MAX_COMMAND_BYTES = 16 * 1024;
export const MAX_REASON_BYTES = 2 * 1024;
export const MAX_DESCRIPTION_BYTES = 4 * 1024;
export const MAX_OUTPUT_BYTES = 64 * 1024;
export const MAX_OUTPUT_LINES = 2000;
export const DEFAULT_REQUEST_TTL_MS = 10 * 60 * 1000;
export const DEFAULT_TIMEOUT_MS = 2 * 60 * 1000;
export const MAX_TIMEOUT_MS = 10 * 60 * 1000;

const REQUESTS_DIR = "requests";
const RESPONSES_DIR = "responses";
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export function extensionAgentDir(extensionFile) {
  return path.resolve(path.dirname(extensionFile), "..", "..");
}

export function requestRoot(extensionFile, env = process.env) {
  const configured = typeof env[HOST_COMMAND_REQUEST_ROOT_ENV] === "string"
    ? env[HOST_COMMAND_REQUEST_ROOT_ENV].trim()
    : "";
  return path.resolve(configured || path.join(extensionAgentDir(extensionFile), "host-command-requests"));
}

function assertDirectory(pathname, label) {
  const info = fs.lstatSync(pathname);
  if (!info.isDirectory() || info.isSymbolicLink()) {
    throw new Error(`${label} must be a regular directory`);
  }
  if (typeof process.getuid === "function" && info.uid !== process.getuid()) {
    throw new Error(`${label} must be owned by the invoking user`);
  }
  if ((info.mode & 0o077) !== 0) {
    throw new Error(`${label} must not be accessible by group or other users`);
  }
}

export function ensureRequestRoot(root) {
  const absolute = path.resolve(root);
  fs.mkdirSync(absolute, { recursive: true, mode: 0o700 });
  fs.chmodSync(absolute, 0o700);
  assertDirectory(absolute, "host command request root");
  for (const name of [REQUESTS_DIR, RESPONSES_DIR]) {
    const child = path.join(absolute, name);
    fs.mkdirSync(child, { recursive: true, mode: 0o700 });
    fs.chmodSync(child, 0o700);
    assertDirectory(child, `host command ${name} directory`);
  }
  return absolute;
}

export function safeId(value) {
  return typeof value === "string" && SAFE_ID.test(value);
}

export function createRequest(input, now = Date.now()) {
  const id = `hcr_${crypto.randomUUID().replaceAll("-", "")}`;
  const ttl = Number.isFinite(input.ttlMs) && input.ttlMs > 0
    ? Math.min(input.ttlMs, DEFAULT_REQUEST_TTL_MS)
    : DEFAULT_REQUEST_TTL_MS;
  return {
    type: "pi.host-command.request",
    version: HOST_COMMAND_PROTOCOL_VERSION,
    id,
    createdAt: now,
    expiresAt: now + ttl,
    targetSessionId: input.targetSessionId,
    parentRuntimeId: input.parentRuntimeId,
    command: input.command,
    reason: input.reason,
    description: input.description,
    timeoutMs: input.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    requester: input.requester ?? { kind: "child" },
  };
}

export function requestPath(root, id) {
  if (!safeId(id)) throw new Error("invalid host command request id");
  return path.join(root, REQUESTS_DIR, `${id}.json`);
}

export function responsePath(root, id) {
  if (!safeId(id)) throw new Error("invalid host command response id");
  return path.join(root, RESPONSES_DIR, `${id}.json`);
}

export function writeAtomicJson(file, value) {
  const directory = path.dirname(file);
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const temporary = path.join(directory, `.${path.basename(file)}.${process.pid}.${crypto.randomUUID()}.tmp`);
  try {
    fs.writeFileSync(temporary, `${JSON.stringify(value)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
    fs.chmodSync(temporary, 0o600);
    fs.renameSync(temporary, file);
    fs.chmodSync(file, 0o600);
  } finally {
    try {
      fs.unlinkSync(temporary);
    } catch {
      // The rename normally removed the temporary path.
    }
  }
}

export function writeRequest(root, request) {
  writeAtomicJson(requestPath(root, request.id), request);
}

export function writeResponse(root, response) {
  writeAtomicJson(responsePath(root, response.id), response);
}

export function removeFile(file) {
  try {
    fs.unlinkSync(file);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
}

export function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return undefined;
  }
}

function validText(value, maxBytes) {
  return typeof value === "string" && value.length > 0 && Buffer.byteLength(value, "utf8") <= maxBytes;
}

function safeUserText(value, maxBytes) {
  return validText(value, maxBytes) && !/[\u0000\u001b]/.test(value);
}

export function parseRequest(file) {
  const value = readJson(file);
  if (!value || value.type !== "pi.host-command.request" || value.version !== HOST_COMMAND_PROTOCOL_VERSION) return undefined;
  if (!safeId(value.id) || path.basename(file) !== `${value.id}.json`) return undefined;
  if (!safeUserText(value.targetSessionId, 256) || !safeUserText(value.parentRuntimeId, 256)) return undefined;
  if (!safeUserText(value.command, MAX_COMMAND_BYTES)) return undefined;
  if (!safeUserText(value.reason, MAX_REASON_BYTES)) return undefined;
  if (!safeUserText(value.description, MAX_DESCRIPTION_BYTES)) return undefined;
  if (!Number.isFinite(value.createdAt) || !Number.isFinite(value.expiresAt) || value.expiresAt < value.createdAt) return undefined;
  if (!Number.isInteger(value.timeoutMs) || value.timeoutMs < 1000 || value.timeoutMs > MAX_TIMEOUT_MS) return undefined;
  if (value.requester !== undefined && (typeof value.requester !== "object" || value.requester === null || Array.isArray(value.requester))) return undefined;
  return value;
}

export function listRequestFiles(root) {
  const directory = path.join(root, REQUESTS_DIR);
  let entries;
  try {
    entries = fs.readdirSync(directory, { withFileTypes: true });
  } catch (error) {
    if (error?.code === "ENOENT") return [];
    throw error;
  }
  return entries
    .filter((entry) => entry.isFile() && entry.name.endsWith(".json") && safeId(entry.name.slice(0, -5)))
    .map((entry) => path.join(directory, entry.name));
}

export function listResponseFiles(root) {
  const directory = path.join(root, RESPONSES_DIR);
  let entries;
  try {
    entries = fs.readdirSync(directory, { withFileTypes: true });
  } catch (error) {
    if (error?.code === "ENOENT") return [];
    throw error;
  }
  return entries
    .filter((entry) => entry.isFile() && entry.name.endsWith(".json") && safeId(entry.name.slice(0, -5)))
    .map((entry) => path.join(directory, entry.name));
}

export function parseResponse(file) {
  const value = readJson(file);
  if (!value || value.type !== "pi.host-command.response" || value.version !== HOST_COMMAND_PROTOCOL_VERSION) return undefined;
  if (!safeId(value.id) || path.basename(file) !== `${value.id}.json`) return undefined;
  if (value.status !== "approved" && value.status !== "rejected" && value.status !== "failed") return undefined;
  if (!Number.isFinite(value.createdAt)) return undefined;
  if (typeof value.output !== "string" || Buffer.byteLength(value.output, "utf8") > MAX_OUTPUT_BYTES * 2) return undefined;
  return value;
}

export function truncateOutput(value, { maxBytes = MAX_OUTPUT_BYTES, maxLines = MAX_OUTPUT_LINES } = {}) {
  let text = String(value ?? "");
  let truncated = false;
  const lines = text.split("\n");
  if (lines.length > maxLines) {
    text = lines.slice(0, maxLines).join("\n");
    truncated = true;
  }
  if (Buffer.byteLength(text, "utf8") > maxBytes) {
    text = Buffer.from(text, "utf8").subarray(0, maxBytes).toString("utf8");
    truncated = true;
  }
  return { text, truncated };
}

export function displayText(value, maxBytes = 6000) {
  const bounded = truncateOutput(value, { maxBytes, maxLines: 200 });
  return bounded.text
    .replace(/\u001b/g, "\\u001b")
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`);
}

export function isExpired(request, now = Date.now()) {
  return now > request.expiresAt;
}
