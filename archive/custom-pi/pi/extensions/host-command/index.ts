import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import type { ExtensionAPI, ExtensionContext, ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import {
  DEFAULT_REQUEST_TTL_MS,
  DEFAULT_TIMEOUT_MS,
  HOST_COMMAND_PARENT_RUNTIME_ENV,
  HOST_COMMAND_PROTOCOL_VERSION,
  MAX_COMMAND_BYTES,
  MAX_DESCRIPTION_BYTES,
  MAX_OUTPUT_BYTES,
  MAX_OUTPUT_LINES,
  MAX_REASON_BYTES,
  MAX_TIMEOUT_MS,
  createRequest,
  displayText,
  ensureRequestRoot,
  isExpired,
  listRequestFiles,
  listResponseFiles,
  parseRequest,
  parseResponse,
  removeFile,
  requestPath,
  requestRoot,
  responsePath,
  truncateOutput,
  writeRequest,
  writeResponse,
} from "./core.mjs";

type HostCommandParams = {
  command: string;
  reason: string;
  description: string;
  timeoutMs?: number;
};

type Requester = {
  kind: "child";
  runId?: string;
  agent?: string;
  childIndex?: number;
};

type HostResponse = {
  type: "pi.host-command.response";
  version: number;
  id: string;
  createdAt: number;
  status: "approved" | "rejected" | "failed";
  output: string;
  truncated?: boolean;
  exitCode?: number;
  signal?: string;
  message?: string;
};

const CHILD_ENV = "PI_SUBAGENT_CHILD";
const PARENT_SESSION_ENV = "PI_SUBAGENT_ORCHESTRATOR_SESSION_ID";
const CHILD_RUN_ENV = "PI_SUBAGENT_RUN_ID";
const CHILD_AGENT_ENV = "PI_SUBAGENT_CHILD_AGENT";
const CHILD_INDEX_ENV = "PI_SUBAGENT_CHILD_INDEX";
const MAX_DISPLAY_COMMAND_BYTES = 6000;
const MAX_RESPONSE_AGE_MS = DEFAULT_REQUEST_TTL_MS + 60_000;
const EXTENSION_FILE = fileURLToPath(import.meta.url);

function isChildProcess(): boolean {
  return process.env[CHILD_ENV] === "1";
}

function currentSessionId(ctx: ExtensionContext): string | undefined {
  try {
    const value = ctx.sessionManager.getSessionId();
    return typeof value === "string" && value ? value : undefined;
  } catch {
    return undefined;
  }
}

function requesterFromEnvironment(): Requester {
  const rawIndex = process.env[CHILD_INDEX_ENV];
  const childIndex = rawIndex !== undefined && /^\d+$/.test(rawIndex) ? Number(rawIndex) : undefined;
  return {
    kind: "child",
    ...(process.env[CHILD_RUN_ENV]?.trim() ? { runId: process.env[CHILD_RUN_ENV]!.trim() } : {}),
    ...(process.env[CHILD_AGENT_ENV]?.trim() ? { agent: process.env[CHILD_AGENT_ENV]!.trim() } : {}),
    ...(childIndex !== undefined ? { childIndex } : {}),
  };
}

function requesterLabel(request: { requester?: Requester }): string {
  const requester = request.requester;
  if (!requester || requester.kind !== "child") return "parent model";
  const agent = requester.agent || "child";
  const run = requester.runId ? `, run ${requester.runId}` : "";
  const index = requester.childIndex === undefined ? "" : `, child ${requester.childIndex}`;
  return `${agent}${index}${run}`;
}

function approvalBody(params: { command: string; reason: string; description: string }, cwd: string, requester = "parent model"): string {
  return [
    "A model requested a command outside its sandbox.",
    "",
    `Requester: ${requester}`,
    `Reason: ${displayText(params.reason, MAX_REASON_BYTES)}`,
    `Description: ${displayText(params.description, MAX_DESCRIPTION_BYTES)}`,
    "",
    "Command:",
    displayText(params.command, MAX_DISPLAY_COMMAND_BYTES),
    "",
    `Host working directory: ${displayText(cwd, 1000)}`,
    "",
    "If approved, this runs as the host user and may access files, credentials, network, or the clipboard.",
  ].join("\n");
}

function hostEnvironment(): NodeJS.ProcessEnv {
  const names = [
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
    "LANG",
    "LC_ALL",
    "TERM",
  ];
  const env: NodeJS.ProcessEnv = {
    PATH: process.env.PATH || "/usr/local/bin:/usr/bin:/bin",
    HOME: process.env.HOME || os.homedir(),
    SHELL: "/bin/bash",
  };
  for (const name of names) {
    const value = process.env[name];
    if (value) env[name] = value;
  }
  return env;
}

function runHostCommand(command: string, cwd: string, timeoutMs: number, signal?: AbortSignal): Promise<HostResponse> {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn("/bin/bash", ["-lc", command], {
        cwd,
        env: hostEnvironment(),
        stdio: ["ignore", "pipe", "pipe"],
        detached: process.platform !== "win32",
      });
    } catch (error) {
      resolve({
        type: "pi.host-command.response",
        version: HOST_COMMAND_PROTOCOL_VERSION,
        id: "local",
        createdAt: Date.now(),
        status: "failed",
        output: "",
        message: error instanceof Error ? error.message : String(error),
      });
      return;
    }

    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let capturedBytes = 0;
    let truncated = false;
    let settled = false;
    let timedOut = false;
    let cancelled = false;
    let timer: NodeJS.Timeout | undefined;
    let killTimer: NodeJS.Timeout | undefined;

    const append = (target: Buffer[], chunk: Buffer): void => {
      if (capturedBytes >= MAX_OUTPUT_BYTES) {
        truncated = true;
        return;
      }
      const remaining = MAX_OUTPUT_BYTES - capturedBytes;
      const kept = chunk.subarray(0, remaining);
      target.push(kept);
      capturedBytes += kept.byteLength;
      if (kept.byteLength < chunk.byteLength) truncated = true;
    };

    const terminate = (): void => {
      try {
        if (process.platform !== "win32" && child.pid) process.kill(-child.pid, "SIGTERM");
        else child.kill("SIGTERM");
      } catch {
        try { child.kill("SIGTERM"); } catch {}
      }
      killTimer = setTimeout(() => {
        try {
          if (process.platform !== "win32" && child.pid) process.kill(-child.pid, "SIGKILL");
          else child.kill("SIGKILL");
        } catch {}
      }, 1000);
      killTimer.unref?.();
    };

    const finish = (response: HostResponse): void => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      if (killTimer) clearTimeout(killTimer);
      signal?.removeEventListener("abort", onAbort);
      resolve(response);
    };

    const onAbort = (): void => {
      cancelled = true;
      terminate();
    };

    child.stdout?.on("data", (chunk: Buffer | string) => append(stdout, Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)));
    child.stderr?.on("data", (chunk: Buffer | string) => append(stderr, Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)));
    child.once("error", (error) => {
      finish({
        type: "pi.host-command.response",
        version: HOST_COMMAND_PROTOCOL_VERSION,
        id: "local",
        createdAt: Date.now(),
        status: "failed",
        output: "",
        message: error instanceof Error ? error.message : String(error),
      });
    });
    child.once("close", (exitCode, closeSignal) => {
      const rawOutput = [
        Buffer.concat(stdout).toString("utf8"),
        Buffer.concat(stderr).toString("utf8"),
      ].filter(Boolean).join(Buffer.concat(stdout).length && Buffer.concat(stderr).length ? "\n[stderr]\n" : "");
      const bounded = truncateOutput(rawOutput, { maxBytes: MAX_OUTPUT_BYTES, maxLines: MAX_OUTPUT_LINES });
      const outputWasTruncated = truncated || bounded.truncated;
      finish({
        type: "pi.host-command.response",
        version: HOST_COMMAND_PROTOCOL_VERSION,
        id: "local",
        createdAt: Date.now(),
        status: timedOut || cancelled ? "failed" : "approved",
        output: bounded.text,
        ...(outputWasTruncated ? { truncated: true } : {}),
        ...(exitCode === null ? {} : { exitCode }),
        ...(closeSignal ? { signal: closeSignal } : {}),
        ...(timedOut ? { message: `Command timed out after ${timeoutMs} ms.` } : {}),
        ...(cancelled ? { message: "Command cancelled." } : {}),
      });
    });

    timer = setTimeout(() => {
      timedOut = true;
      terminate();
    }, timeoutMs);
    timer.unref?.();
    if (signal?.aborted) onAbort();
    else signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function responseForRejected(id: string, message: string): HostResponse {
  return {
    type: "pi.host-command.response",
    version: HOST_COMMAND_PROTOCOL_VERSION,
    id,
    createdAt: Date.now(),
    status: "rejected",
    output: "",
    message,
  };
}

function responseForFailed(id: string, message: string): HostResponse {
  return {
    type: "pi.host-command.response",
    version: HOST_COMMAND_PROTOCOL_VERSION,
    id,
    createdAt: Date.now(),
    status: "failed",
    output: "",
    message,
  };
}

function toolResult(response: HostResponse) {
  const status = response.status === "approved" ? "approved" : response.status;
  const output = response.output || "(no output)";
  const lines = [`Host command ${status}.`];
  if (response.message) lines.push(response.message);
  if (response.exitCode !== undefined) lines.push(`Exit code: ${response.exitCode}`);
  if (response.signal) lines.push(`Signal: ${response.signal}`);
  lines.push("", output);
  if (response.truncated) lines.push("", "[host command output truncated]");
  const failed = response.status !== "approved" || (response.exitCode !== undefined && response.exitCode !== 0);
  return {
    content: [{ type: "text" as const, text: lines.join("\n") }],
    ...(failed ? { isError: true } : {}),
    details: {
      hostCommand: true,
      status: response.status,
      ...(response.exitCode !== undefined ? { exitCode: response.exitCode } : {}),
      ...(response.truncated ? { truncated: true } : {}),
      sensitive: true,
    },
  };
}

async function waitForResponse(root: string, request: ReturnType<typeof createRequest>, signal?: AbortSignal): Promise<HostResponse> {
  const deadline = request.expiresAt;
  const responseFile = responsePath(root, request.id);
  while (Date.now() <= deadline) {
    if (signal?.aborted) {
      removeFile(requestPath(root, request.id));
      return responseForFailed(request.id, "Host command request cancelled.");
    }
    const response = parseResponse(responseFile);
    if (response && response.id === request.id) {
      removeFile(responseFile);
      return response;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  removeFile(requestPath(root, request.id));
  return responseForFailed(request.id, "Host command request expired without a response.");
}

async function requestFromChild(params: HostCommandParams, ctx: ExtensionContext, signal?: AbortSignal) {
  const targetSessionId = process.env[PARENT_SESSION_ENV]?.trim();
  const parentRuntimeId = process.env[HOST_COMMAND_PARENT_RUNTIME_ENV]?.trim();
  if (!targetSessionId || !parentRuntimeId) {
    return toolResult(responseForFailed("local", "No live parent session is available for host command approval."));
  }
  let root;
  try {
    root = ensureRequestRoot(requestRoot(EXTENSION_FILE, process.env));
  } catch (error) {
    return toolResult(responseForFailed("local", error instanceof Error ? error.message : String(error)));
  }
  const request = createRequest({
    targetSessionId,
    parentRuntimeId,
    command: params.command,
    reason: params.reason,
    description: params.description,
    timeoutMs: params.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    requester: requesterFromEnvironment(),
  });
  writeRequest(root, request);
  return toolResult(await waitForResponse(root, request, signal));
}

async function executeForParent(params: HostCommandParams, ctx: ExtensionContext, signal?: AbortSignal, requester = "parent model") {
  if (!ctx.hasUI) return toolResult(responseForRejected("local", "Host command approval requires an interactive Pi UI."));
  const cwd = ctx.cwd;
  try {
    const info = fs.statSync(cwd);
    if (!info.isDirectory()) throw new Error(`Host working directory is not a directory: ${cwd}`);
  } catch (error) {
    return toolResult(responseForFailed("local", error instanceof Error ? error.message : String(error)));
  }
  let approved = false;
  try {
    approved = await ctx.ui.confirm("Approve host command?", approvalBody(params, cwd, requester));
  } catch (error) {
    return toolResult(responseForRejected("local", `Approval prompt failed: ${error instanceof Error ? error.message : String(error)}`));
  }
  if (!approved) return toolResult(responseForRejected("local", "The user rejected this host command."));
  return toolResult(await runHostCommand(params.command, cwd, params.timeoutMs ?? DEFAULT_TIMEOUT_MS, signal));
}

function hostCommandTool(pi: ExtensionAPI): ToolDefinition<any, any> {
  return {
    name: "host_command",
    label: "Host Command",
    description: [
      "Request one shell command to run as the host user outside the Pi sandbox.",
      "The user sees the exact command, reason, description, working directory, and risk warning and must approve it for this request.",
      "Use this only when the sandbox cannot perform the task, such as reading the native clipboard.",
      "Always provide a precise reason and description. Never claim the command ran until its result is returned.",
      "Host command output may contain sensitive data and is returned to the requesting model.",
    ].join("\n"),
    parameters: Type.Object({
      command: Type.String({ minLength: 1, maxLength: MAX_COMMAND_BYTES, description: "Exact shell command to request." }),
      reason: Type.String({ minLength: 1, maxLength: MAX_REASON_BYTES, description: "Why this host command is needed." }),
      description: Type.String({ minLength: 1, maxLength: MAX_DESCRIPTION_BYTES, description: "What the command does and what result is expected." }),
      timeoutMs: Type.Optional(Type.Integer({ minimum: 1000, maximum: MAX_TIMEOUT_MS, description: `Command timeout in milliseconds; default ${DEFAULT_TIMEOUT_MS}.` })),
    }, { additionalProperties: false }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const input = params as HostCommandParams;
      if (isChildProcess()) return requestFromChild(input, ctx, signal);
      return executeForParent(input, ctx, signal);
    },
  };
}

export default function hostCommandExtension(pi: ExtensionAPI): void {
  const child = isChildProcess();
  pi.registerTool(hostCommandTool(pi));
  if (child) return;

  const root = ensureRequestRoot(requestRoot(EXTENSION_FILE, process.env));
  let currentContext: ExtensionContext | undefined;
  let currentSession: string | undefined;
  let runtimeId: string | undefined;
  let poller: NodeJS.Timeout | undefined;
  let disposed = false;
  const seen = new Set<string>();
  const inFlight = new Map<string, { request: any; file: string }>();

  const rejectRequest = (request: any, message: string): void => {
    try {
      writeResponse(root, responseForRejected(request.id, message));
      removeFile(requestPath(root, request.id));
    } catch {
      // Best effort. The request will be rejected by expiry on the next startup.
    }
  };

  const handleRequest = async (request: any, file: string, ctx: ExtensionContext): Promise<void> => {
    if (disposed || !runtimeId || request.parentRuntimeId !== runtimeId || !currentSession || request.targetSessionId !== currentSession) {
      rejectRequest(request, "The parent session is no longer active.");
      return;
    }
    if (!fs.existsSync(file)) return;
    let approved = false;
    try {
      approved = ctx.hasUI && await ctx.ui.confirm("Approve host command?", approvalBody(request, ctx.cwd, requesterLabel(request)));
    } catch (error) {
      rejectRequest(request, `Approval prompt failed: ${error instanceof Error ? error.message : String(error)}`);
      return;
    }
    if (disposed || !runtimeId || request.parentRuntimeId !== runtimeId || !fs.existsSync(file)) {
      rejectRequest(request, "The parent session changed before approval completed.");
      return;
    }
    if (!approved) {
      rejectRequest(request, "The user rejected this host command.");
      return;
    }
    const result = await runHostCommand(request.command, ctx.cwd, request.timeoutMs, ctx.signal);
    const response: HostResponse = { ...result, id: request.id };
    try {
      writeResponse(root, response);
      removeFile(file);
    } catch {
      // The child will time out if the response cannot be persisted.
    }
  };

  const poll = (): void => {
    if (disposed || !currentContext || !currentSession || !runtimeId) return;
    const now = Date.now();
    for (const file of listRequestFiles(root)) {
      const request = parseRequest(file);
      if (!request || seen.has(request.id) || inFlight.has(request.id)) continue;
      if (request.targetSessionId !== currentSession || request.parentRuntimeId !== runtimeId) continue;
      seen.add(request.id);
      if (isExpired(request, now)) {
        rejectRequest(request, "The host command request expired.");
        continue;
      }
      inFlight.set(request.id, { request, file });
      void handleRequest(request, file, currentContext).finally(() => inFlight.delete(request.id));
    }
    for (const file of listResponseFiles(root)) {
      try {
        const info = fs.statSync(file);
        if (now - info.mtimeMs > MAX_RESPONSE_AGE_MS) removeFile(file);
      } catch {
        // A response can disappear while the child consumes it.
      }
    }
  };

  const start = (ctx: ExtensionContext): void => {
    disposed = false;
    currentContext = ctx;
    currentSession = currentSessionId(ctx);
    runtimeId = randomUUID();
    process.env[HOST_COMMAND_PARENT_RUNTIME_ENV] = runtimeId;
    seen.clear();
    ensureRequestRoot(root);
    if (poller) clearInterval(poller);
    poller = setInterval(poll, 250);
    poller.unref?.();
    poll();
  };

  pi.on("session_start", (_event, ctx) => start(ctx));
  pi.on("session_shutdown", () => {
    disposed = true;
    if (poller) clearInterval(poller);
    poller = undefined;
    for (const { request } of inFlight.values()) rejectRequest(request, "The parent session shut down before approval completed.");
    inFlight.clear();
    currentContext = undefined;
    currentSession = undefined;
    runtimeId = undefined;
    if (process.env[HOST_COMMAND_PARENT_RUNTIME_ENV]) delete process.env[HOST_COMMAND_PARENT_RUNTIME_ENV];
  });
}