import { createHash } from "node:crypto";
import { fstatSync, readFileSync } from "node:fs";
import net from "node:net";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export const PROTOCOL_VERSION = 1;
export const MAX_FRAME_BYTES = 64 * 1024;
export const CHANNEL_ENVIRONMENT_KEY = "PI_CONTROLLER_CHANNEL_FD";
export const CHANNEL_SYMBOL = Symbol.for("pi.controllerChannel.v1");

type JsonObject = Record<string, unknown>;
type ToolDefinition = Parameters<ExtensionAPI["registerTool"]>[0];
export type ChannelApi = {
  registerTool(operation: string, definition: ToolDefinition): void;
  request(operation: string, payload: JsonObject, signal?: AbortSignal): Promise<unknown>;
};

export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const object = value as JsonObject;
  return `{${Object.keys(object).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`).join(",")}}`;
}

export function validateChallenge(value: unknown): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("controller challenge must be an object");
  const challenge = value as JsonObject;
  if (challenge.protocolVersion !== PROTOCOL_VERSION || challenge.type !== "challenge") throw new Error("controller challenge protocol is invalid");
  for (const key of ["runId", "manifestDigest", "childStartIdentity", "role", "sessionId", "sessionPath"]) {
    if (typeof challenge[key] !== "string" || challenge[key] === "") throw new Error(`controller challenge ${key} is invalid`);
  }
  if (challenge.childPid !== process.pid || !Array.isArray(challenge.resources) || !Array.isArray(challenge.activeTools) || !Array.isArray(challenge.toolSources) || !Array.isArray(challenge.allowedOperations) || challenge.allowedOperations.some((item) => typeof item !== "string")) {
    throw new Error("controller challenge process or resources are invalid");
  }
  return challenge;
}

function startIdentity(): string {
  const raw = readFileSync("/proc/self/stat", "utf8");
  const fields = raw.slice(raw.lastIndexOf(")") + 2).split(/\s+/);
  const boot = readFileSync("/proc/sys/kernel/random/boot_id", "ascii").trim();
  return `linux:${boot}:${fields[19]}`;
}

function digest(path: string): string {
  return `sha256:${createHash("sha256").update(readFileSync(path)).digest("hex")}`;
}

class FramedChannel {
  private buffer = Buffer.alloc(0);
  private pending: Array<{ resolve(value: JsonObject): void; reject(error: Error): void }> = [];
  private closedError: Error | undefined;

  constructor(private socket: net.Socket) {
    socket.on("data", (chunk: Buffer) => this.onData(chunk));
    socket.on("error", (error) => this.close(error));
    socket.on("end", () => this.close(new Error("controller channel closed")));
  }

  private close(error: Error): void {
    this.closedError = error;
    for (const waiter of this.pending.splice(0)) waiter.reject(error);
  }

  private onData(chunk: Buffer): void {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    if (this.buffer.length > MAX_FRAME_BYTES + 1) return this.close(new Error("controller channel frame exceeds its bound"));
    const newline = this.buffer.indexOf(0x0a);
    if (newline < 0) return;
    const body = this.buffer.subarray(0, newline).toString("utf8");
    this.buffer = this.buffer.subarray(newline + 1);
    let value: unknown;
    try { value = JSON.parse(body); } catch { return this.close(new Error("controller channel frame is invalid JSON")); }
    if (!value || typeof value !== "object" || Array.isArray(value) || canonicalJson(value) !== body) return this.close(new Error("controller channel frame is not canonical JSON"));
    const waiter = this.pending.shift();
    if (!waiter) return this.close(new Error("controller channel sent an unsolicited frame"));
    waiter.resolve(value as JsonObject);
  }

  read(): Promise<JsonObject> {
    if (this.closedError) return Promise.reject(this.closedError);
    return new Promise((resolve, reject) => this.pending.push({ resolve, reject }));
  }

  write(value: JsonObject): Promise<void> {
    const body = Buffer.from(`${canonicalJson(value)}\n`);
    if (body.length - 1 > MAX_FRAME_BYTES) return Promise.reject(new Error("controller channel frame exceeds its bound"));
    return new Promise((resolve, reject) => this.socket.write(body, (error) => error ? reject(error) : resolve()));
  }

  close(): void {
    this.socket.end();
  }
}

export default function controllerChannel(pi: ExtensionAPI): void {
  const rawFd = process.env[CHANNEL_ENVIRONMENT_KEY];
  if (!rawFd || !/^[0-9]+$/.test(rawFd) || Number(rawFd) < 3) throw new Error("missing or invalid inherited controller channel FD");
  const descriptor = Number(rawFd);
  try {
    if (!fstatSync(descriptor).isSocket()) throw new Error("not a socket");
  } catch {
    throw new Error("missing or invalid inherited controller channel FD");
  }
  const channel = new FramedChannel(new net.Socket({ fd: descriptor, readable: true, writable: true }));
  const challengePromise = channel.read().then(validateChallenge);
  let requestCounter = 0;
  let ready: Promise<void>;
  let grants: Set<string> | undefined;
  const declarations = new Map<string, ToolDefinition>();
  const registered = new Set<string>();
  const registerGranted = (operation: string, definition: ToolDefinition): void => {
    if (!grants?.has(operation) || registered.has(operation)) return;
    pi.registerTool(definition);
    registered.add(operation);
  };
  const api: ChannelApi = {
    registerTool(operation, definition) {
      if (!operation || declarations.has(operation)) throw new Error(`duplicate or invalid controller operation declaration: ${operation}`);
      declarations.set(operation, definition);
      registerGranted(operation, definition);
    },
    async request(operation, payload, signal) {
      await ready;
      if (!grants?.has(operation)) throw new Error("controller operation is not granted to this authenticated role");
      const requestId = `request-${++requestCounter}`;
      await channel.write({ protocolVersion: PROTOCOL_VERSION, type: "request", requestId, operation, payload });
      const onAbort = () => { void channel.write({ protocolVersion: PROTOCOL_VERSION, type: "cancel", requestId }); };
      if (signal?.aborted) onAbort();
      else signal?.addEventListener("abort", onAbort, { once: true });
      const response = await channel.read();
      signal?.removeEventListener("abort", onAbort);
      if (response.protocolVersion !== PROTOCOL_VERSION || response.type !== "response" || response.requestId !== requestId || typeof response.ok !== "boolean") throw new Error("controller response identity is invalid");
      if (signal?.aborted) throw new Error("controller request was aborted");
      if (!response.ok) throw new Error(String(response.error || "controller request failed"));
      return response.result;
    },
  };
  (globalThis as unknown as Record<symbol, ChannelApi>)[CHANNEL_SYMBOL] = api;
  const grantsReady = challengePromise.then((challenge) => {
    grants = new Set(challenge.allowedOperations as string[]);
    for (const [operation, definition] of declarations) registerGranted(operation, definition);
  });
  ready = new Promise((resolve, reject) => {
    pi.on("session_start", async (_event, ctx) => {
      try {
        const challenge = await challengePromise;
        await grantsReady;
        const resources = (challenge.resources as Array<{ resourceId: string; path: string; digest: string }>).map((item) => ({ ...item, digest: digest(item.path) })).sort((a, b) => a.resourceId.localeCompare(b.resourceId));
        const activeTools = [...pi.getActiveTools()].sort();
        const allTools = pi.getAllTools();
        const toolSources = activeTools.map((name) => {
          const tool = allTools.find((item) => item.name === name);
          return { name, path: String(tool?.sourceInfo?.path || "") };
        });
        const handshake = {
          protocolVersion: PROTOCOL_VERSION, type: "startup", runId: challenge.runId, manifestDigest: challenge.manifestDigest,
          childPid: process.pid, childStartIdentity: startIdentity(), role: challenge.role,
          sessionId: ctx.sessionManager.getSessionId(), sessionPath: ctx.sessionManager.getSessionFile(),
          activeTools, toolSources, loadedResources: resources,
        };
        await channel.write(handshake);
        const reply = await channel.read();
        if (reply.protocolVersion !== PROTOCOL_VERSION || reply.type !== "startup-accepted" || reply.runId !== challenge.runId || reply.manifestDigest !== challenge.manifestDigest) throw new Error("controller startup reply is invalid");
        resolve();
      } catch (error) {
        reject(error);
        throw error;
      }
    });
  });
  pi.on("agent_end", () => channel.close());
}
