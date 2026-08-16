import { createHash, randomBytes } from "node:crypto";
import { chmodSync, existsSync, lstatSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import net from "node:net";
import path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const PROTOCOL_VERSION = 2;
const MAX_FRAME_BYTES = 64 * 1024;
const MAX_SOCKET_BUFFER_BYTES = 256 * 1024;
const TEXT_LIMIT = 16 * 1024;
const IDEMPOTENCY_LIMIT = 128;
const ADMISSION_TIMEOUT_MS = 4000;
const MAX_EVENT_BUFFER = 256;
const MAX_RESULT_CACHE = 256;
const BRIDGE_ROOT_ENV = "PI_SYSTEM_STATE_ROOT";
const WEB_INPUT_ENTRY = "pi-web-input";

type JsonObject = Record<string, unknown>;
type Client = { socket: net.Socket; subscribed: boolean };
type StoredResult = {
  fingerprint: string;
  type: "accepted" | "uncertain" | "rejected";
  operation: string;
  deliveryState: string;
  acceptedEntryId?: string;
  error?: { code: string; message: string };
};
type PendingDelivery = {
  idempotencyKey: string;
  fingerprint: string;
  text: string;
  deliverAs?: "steer" | "followUp";
  resolve?: (accepted: boolean) => void;
  preflight?: boolean;
  error?: { code: string; message: string };
};
type WebExtensionAPI = ExtensionAPI & {
  sendUserMessage: (content: string, options?: {
    deliverAs?: "steer" | "followUp";
    expandPromptTemplates?: boolean;
    preflightResult?: (success: boolean) => void;
    queueId?: string;
  }) => void | Promise<void>;
};
type WebExtensionContext = ExtensionContext & {
  getQueuedMessages?: () => ReadonlyArray<{ id?: string; text: string; deliverAs: "steer" | "followUp" }>;
  removeQueuedMessage?: (id: string) => boolean;
};

function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  const object = value as JsonObject;
  return `{${Object.keys(object).sort().map((key) => `${JSON.stringify(key)}:${canonical(object[key])}`).join(",")}}`;
}

function digest(value: JsonObject): string {
  return createHash("sha256").update(canonical(value)).digest("hex");
}

function textOf(value: unknown, remaining = TEXT_LIMIT): string {
  if (remaining <= 0) return "";
  if (typeof value === "string") return value.slice(0, remaining);
  if (Array.isArray(value)) return value.map((item) => textOf(item, remaining)).join("").slice(0, remaining);
  if (value && typeof value === "object") {
    const object = value as JsonObject;
    for (const key of ["text", "content", "value", "message"]) {
      if (key in object) return textOf(object[key], remaining);
    }
  }
  return "";
}

function frame(socket: net.Socket, value: JsonObject): void {
  const body = Buffer.from(`${canonical(value)}\n`);
  if (body.length - 1 > MAX_FRAME_BYTES || socket.destroyed || socket.writableLength > MAX_SOCKET_BUFFER_BYTES) {
    socket.destroy(new Error("web session frame exceeds its bound"));
    return;
  }
  socket.write(body);
}

function safeEvent(event: JsonObject): JsonObject {
  const type = typeof event.type === "string" ? event.type : "unknown";
  if (type === "message_start" || type === "message_end") {
    const message = event.message as JsonObject | undefined;
    return { type, role: typeof message?.role === "string" ? message.role : "unknown", text: textOf(message) };
  }
  if (type === "message_update") {
    const message = event.message as JsonObject | undefined;
    return { type, role: typeof message?.role === "string" ? message.role : "assistant", text: textOf(message) };
  }
  if (type === "tool_execution_start" || type === "tool_execution_end") {
    return { type, toolName: typeof event.toolName === "string" ? event.toolName : "tool", isError: event.isError === true };
  }
  if (type === "agent_start" || type === "agent_end" || type === "agent_settled" || type === "session_compact") return { type };
  return { type };
}

function parseManifest(): JsonObject | undefined {
  const manifestPath = process.env.PI_RUNTIME_MANIFEST;
  if (!manifestPath) return undefined;
  try {
    const raw = readFileSync(manifestPath, "utf8");
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as JsonObject : undefined;
  } catch {
    return undefined;
  }
}

function childStartIdentity(): string {
  if (process.platform !== "linux") return `node:${process.pid}`;
  const raw = readFileSync(`/proc/${process.pid}/stat`, "utf8");
  const closing = raw.lastIndexOf(")");
  const fields = raw.slice(closing + 2).trim().split(/\s+/);
  if (closing < 0 || fields.length <= 19) throw new Error("child start identity is unavailable");
  let boot = "unknown-boot";
  try { boot = readFileSync("/proc/sys/kernel/random/boot_id", "ascii").trim(); } catch {}
  return `linux:${boot}:${fields[19]}`;
}

function requestError(requestId: string, code: string, message: string): JsonObject {
  return { protocolVersion: PROTOCOL_VERSION, type: "rejected", requestId, error: { code, message: message.slice(0, 512), detail: {} } };
}

function runtimeState(pi: ExtensionAPI, ctx: ExtensionContext): JsonObject {
  const model = ctx.model as (JsonObject & { provider?: string; id?: string; name?: string }) | undefined;
  const registry = ctx.modelRegistry as unknown as { getAvailable?: () => unknown[] };
  const availableModels = typeof registry.getAvailable === "function" ? registry.getAvailable().slice(0, 256).map((item) => {
    const value = item as JsonObject;
    return { provider: value.provider, id: value.id, name: value.name, reasoning: value.reasoning === true };
  }) : [];
  const commands = typeof pi.getCommands === "function" ? pi.getCommands().slice(0, 256).map((item) => {
    const value = item as JsonObject;
    return { name: value.name, description: value.description, source: value.source };
  }) : [];
  const webContext = ctx as WebExtensionContext;
  const queued = typeof webContext.getQueuedMessages === "function" ? webContext.getQueuedMessages().slice(0, 256).map((item) => ({
    id: typeof item.id === "string" ? item.id : null,
    preview: item.text.slice(0, 512),
    deliverAs: item.deliverAs,
    removable: typeof item.id === "string" && item.id.length > 0,
  })) : [];
  return {
    idle: ctx.isIdle(),
    pendingMessages: ctx.hasPendingMessages(),
    queued,
    model: model && typeof model.provider === "string" && typeof model.id === "string" ? { provider: model.provider, id: model.id, name: typeof model.name === "string" ? model.name : model.id } : null,
    thinkingLevel: typeof pi.getThinkingLevel === "function" ? pi.getThinkingLevel() : null,
    availableModels,
    commands,
  };
}

function storedResponse(requestId: string, stored: StoredResult): JsonObject {
  const response: JsonObject = {
    protocolVersion: PROTOCOL_VERSION,
    type: stored.type,
    requestId,
    operation: stored.operation,
    deliveryState: stored.deliveryState,
  };
  if (stored.acceptedEntryId) response.acceptedEntryId = stored.acceptedEntryId;
  if (stored.error) response.error = { ...stored.error, detail: {} };
  return response;
}

function promptExpansion(pi: ExtensionAPI, text: string): "safe" | "extension" | "none" {
  if (!text.startsWith("/")) return "none";
  const space = text.indexOf(" ");
  const name = space === -1 ? text.slice(1) : text.slice(1, space);
  const commands = typeof pi.getCommands === "function" ? pi.getCommands() : [];
  const command = commands.find((item) => (item as JsonObject).name === name) as JsonObject | undefined;
  if (!command) return "none";
  return command.source === "extension" ? "extension" : command.source === "prompt" || command.source === "skill" ? "safe" : "none";
}

export default function webSession(pi: ExtensionAPI): void {
  const webPi = pi as WebExtensionAPI;
  let server: net.Server | undefined;
  let descriptorPath: string | undefined;
  let socketPath: string | undefined;
  const clients = new Set<Client>();
  const results = new Map<string, StoredResult>();
  const pending = new Map<string, PendingDelivery>();
  const eventBuffer: Array<{ eventId: string; event: JsonObject }> = [];
  let eventSequence = 0;
  let context: ExtensionContext | undefined;
  let metadata: JsonObject | undefined;

  const rememberResult = (key: string, result: StoredResult): void => {
    results.delete(key);
    results.set(key, result);
    while (results.size > MAX_RESULT_CACHE) {
      const oldest = results.keys().next().value;
      if (typeof oldest !== "string") break;
      results.delete(oldest);
    }
  };

  const markerResult = (data: JsonObject): { key: string; result: StoredResult } | undefined => {
    const key = data.idempotencyKey;
    const fingerprint = data.fingerprint;
    const operation = data.operation;
    const state = data.state;
    if (typeof key !== "string" || typeof fingerprint !== "string" || typeof operation !== "string") return undefined;
    if (state === "accepted") {
      return { key, result: { fingerprint, type: "accepted", operation, deliveryState: typeof data.deliveryState === "string" ? data.deliveryState : "accepted", acceptedEntryId: typeof data.acceptedEntryId === "string" ? data.acceptedEntryId : undefined } };
    }
    if (state === "pending") {
      return { key, result: { fingerprint, type: "uncertain", operation, deliveryState: "delivery_uncertain", error: { code: "CP_DELIVERY_UNCERTAIN", message: "the prior run ended before browser input delivery was proven" } } };
    }
    if (state === "uncertain") {
      return { key, result: { fingerprint, type: "uncertain", operation, deliveryState: "delivery_uncertain", error: { code: "CP_DELIVERY_UNCERTAIN", message: "browser input delivery could not be proven" } } };
    }
    if (state === "rejected") {
      const error = data.error as JsonObject | undefined;
      if (typeof error?.code !== "string" || typeof error.message !== "string") return undefined;
      return { key, result: { fingerprint, type: "rejected", operation, deliveryState: "rejected", error: { code: error.code, message: error.message } } };
    }
    return undefined;
  };

  const appendMarker = (data: JsonObject): string | undefined => {
    pi.appendEntry(WEB_INPUT_ENTRY, data);
    const getLeafId = context?.sessionManager.getLeafId;
    const value = typeof getLeafId === "function" ? getLeafId.call(context?.sessionManager) : undefined;
    return typeof value === "string" ? value : undefined;
  };

  const restoreResults = (ctx: ExtensionContext): void => {
    results.clear();
    const entries = ctx.sessionManager.getEntries();
    for (const entry of entries) {
      const item = entry as unknown as JsonObject;
      if (item.type !== "custom" || item.customType !== WEB_INPUT_ENTRY || !item.data || typeof item.data !== "object") continue;
      const marker = markerResult(item.data as JsonObject);
      if (marker) rememberResult(marker.key, marker.result);
    }
  };

  const findStoredResult = (key: string): StoredResult | undefined => {
    const cached = results.get(key);
    if (cached) return cached;
    if (!context) return undefined;
    const entries = context.sessionManager.getEntries();
    for (let index = entries.length - 1; index >= 0; index -= 1) {
      const item = entries[index] as unknown as JsonObject;
      if (item.type !== "custom" || item.customType !== WEB_INPUT_ENTRY || !item.data || typeof item.data !== "object") continue;
      const marker = markerResult(item.data as JsonObject);
      if (marker?.key === key) {
        rememberResult(key, marker.result);
        return marker.result;
      }
    }
    return undefined;
  };

  const publish = (event: JsonObject): void => {
    const runId = typeof metadata?.runId === "string" ? metadata.runId : "run";
    const eventId = `${runId}:${++eventSequence}`;
    const item = { eventId, event: safeEvent(event) };
    eventBuffer.push(item);
    if (eventBuffer.length > MAX_EVENT_BUFFER) eventBuffer.shift();
    for (const client of clients) {
      if (!client.subscribed) continue;
      try { frame(client.socket, { protocolVersion: PROTOCOL_VERSION, type: "event", eventId, event: item.event }); }
      catch { clients.delete(client); client.socket.destroy(); }
    }
  };

  const replay = (socket: net.Socket, afterEventId: unknown): void => {
    if (typeof afterEventId !== "string" || !afterEventId) return;
    const separator = afterEventId.lastIndexOf(":");
    const runId = typeof metadata?.runId === "string" ? metadata.runId : "";
    const sequence = Number(afterEventId.slice(separator + 1));
    if (separator < 0 || afterEventId.slice(0, separator) !== runId || !Number.isSafeInteger(sequence)) return;
    for (const item of eventBuffer) {
      const itemSequence = Number(item.eventId.slice(item.eventId.lastIndexOf(":") + 1));
      if (itemSequence > sequence) frame(socket, { protocolVersion: PROTOCOL_VERSION, type: "event", eventId: item.eventId, event: item.event });
    }
  };

  const close = (): void => {
    for (const client of clients) client.socket.destroy();
    clients.clear();
    pending.clear();
    server?.close();
    server = undefined;
    if (descriptorPath) { try { unlinkSync(descriptorPath); } catch {} }
    if (socketPath) { try { unlinkSync(socketPath); } catch {} }
    descriptorPath = undefined;
    socketPath = undefined;
    metadata = undefined;
    context = undefined;
    eventBuffer.length = 0;
    eventSequence = 0;
  };

  const start = (_event: unknown, ctx: ExtensionContext): void => {
    close();
    context = ctx;
    restoreResults(ctx);
    const manifest = parseManifest();
    const stateRoot = process.env[BRIDGE_ROOT_ENV];
    const runId = typeof manifest?.runId === "string" ? manifest.runId : "";
    const conversation = manifest?.conversation as JsonObject | undefined;
    const project = manifest?.project as JsonObject | undefined;
    const installedBuild = manifest?.installedBuild as JsonObject | undefined;
    const controllerBuildId = process.env.PI_CONTROLLER_BUILD_ID || "";
    const restartEpoch = process.env.PI_CONTROLLER_RESTART_EPOCH || "";
    const runBuildId = typeof installedBuild?.buildId === "string" ? installedBuild.buildId : "";
    const manifestDigest = typeof manifest?.manifestDigest === "string" ? manifest.manifestDigest : "";
    if (!stateRoot || !runId || typeof conversation?.conversationId !== "string" || typeof project?.projectId !== "string" || !controllerBuildId || !restartEpoch || !runBuildId || !manifestDigest) return;
    let processIdentity: string;
    try { processIdentity = childStartIdentity(); } catch { return; }
    const root = path.resolve(stateRoot, "web-bridges");
    mkdirSync(root, { recursive: true, mode: 0o700 });
    chmodSync(root, 0o700);
    const capability = randomBytes(32).toString("base64url");
    socketPath = path.join(root, `${runId}.sock`);
    descriptorPath = path.join(root, `${runId}.json`);
    server = net.createServer((socket) => {
      const client: Client = { socket, subscribed: false };
      let buffer = Buffer.alloc(0);
      let connected = false;
      let processing = Promise.resolve();
      const onFrame = async (value: unknown): Promise<void> => {
        if (!value || typeof value !== "object" || Array.isArray(value)) return socket.destroy();
        const request = value as JsonObject;
        if (!connected) {
          const expected = metadata || {};
          const fields = ["runId", "conversationId", "projectId", "sessionId", "controllerBuildId", "runBuildId", "manifestDigest", "childPid", "childStartIdentity", "restartEpoch", "capability"];
          if (request.type !== "connect" || request.protocolVersion !== PROTOCOL_VERSION || fields.some((key) => request[key] !== expected[key])) return socket.destroy();
          connected = true;
          clients.add(client);
          frame(socket, { protocolVersion: PROTOCOL_VERSION, type: "connected", runId: expected.runId, conversationId: expected.conversationId, projectId: expected.projectId, sessionId: expected.sessionId, controllerBuildId: expected.controllerBuildId, runBuildId: expected.runBuildId, manifestDigest: expected.manifestDigest, childPid: expected.childPid, childStartIdentity: expected.childStartIdentity, restartEpoch: expected.restartEpoch });
          return;
        }
        const requestId = typeof request.requestId === "string" ? request.requestId : "";
        if (request.type === "subscribe") {
          client.subscribed = true;
          frame(socket, { protocolVersion: PROTOCOL_VERSION, type: "subscribed", requestId });
          replay(socket, request.afterEventId);
          return;
        }
        if (request.type !== "command" || !requestId || !context) return socket.destroy();
        const operation = typeof request.operation === "string" ? request.operation : "";
        if (request.type === "command" && operation === "state") {
          if (Object.keys(request).some((key) => !["protocolVersion", "type", "requestId", "operation"].includes(key))) {
            frame(socket, requestError(requestId, "CP_INVALID_REQUEST", "runtime state request has unexpected fields"));
            return;
          }
          frame(socket, { protocolVersion: PROTOCOL_VERSION, type: "state", requestId, state: runtimeState(pi, context) });
          return;
        }
        const idempotencyKey = typeof request.idempotencyKey === "string" ? request.idempotencyKey : "";
        if (!idempotencyKey || idempotencyKey.length > IDEMPOTENCY_LIMIT || idempotencyKey.includes("\0")) {
          frame(socket, requestError(requestId, "CP_INVALID_REQUEST", "web command idempotency key is invalid"));
          return;
        }
        const text = typeof request.text === "string" ? request.text.trim() : "";
        const deliverAs = request.deliverAs === "steer" ? "steer" : request.deliverAs === "followUp" ? "followUp" : undefined;
        const inputId = typeof request.inputId === "string" ? request.inputId : "";
        const fingerprint = digest({ operation, ...(operation === "prompt" ? { text, ...(deliverAs ? { deliverAs } : {}) } : {}), ...(operation === "removeQueued" ? { inputId } : {}), ...(operation === "setModel" ? { model: request.model } : {}), ...(operation === "setThinking" ? { thinkingLevel: request.thinkingLevel } : {}) });
        const existing = findStoredResult(idempotencyKey);
        if (existing) {
          if (existing.fingerprint !== fingerprint) frame(socket, requestError(requestId, "CP_IDEMPOTENCY_CONFLICT", "idempotency key is bound to a different web command"));
          else frame(socket, storedResponse(requestId, existing));
          return;
        }
        const inFlight = pending.get(idempotencyKey);
        if (inFlight) {
          if (inFlight.fingerprint !== fingerprint) frame(socket, requestError(requestId, "CP_IDEMPOTENCY_CONFLICT", "idempotency key is bound to a different web command"));
          else frame(socket, { protocolVersion: PROTOCOL_VERSION, type: "pending", requestId, operation, deliveryState: "pending" });
          return;
        }
        if (!context || !metadata || !["prompt", "removeQueued", "stop", "compact", "setModel", "setThinking"].includes(operation)) {
          frame(socket, requestError(requestId, "CP_INVALID_REQUEST", "web session operation is unsupported"));
          return;
        }
        if (operation === "prompt") {
          if (Object.keys(request).some((key) => !["protocolVersion", "type", "requestId", "idempotencyKey", "operation", "text", "deliverAs"].includes(key)) || !text || Buffer.byteLength(text, "utf8") > TEXT_LIMIT || (request.deliverAs !== undefined && !deliverAs)) {
            frame(socket, requestError(requestId, "CP_INVALID_REQUEST", "prompt or delivery mode is invalid"));
            return;
          }
          if (!deliverAs && !context.isIdle()) {
            frame(socket, requestError(requestId, "CP_INPUT_CONFLICT", "the conversation is working; choose steer or after current work"));
            return;
          }
          if (promptExpansion(pi, text) === "extension") {
            frame(socket, requestError(requestId, "CP_UNSUPPORTED", "browser dispatch of extension commands is not enabled for this run"));
            return;
          }
        } else if (operation === "removeQueued") {
          if (Object.keys(request).some((key) => !["protocolVersion", "type", "requestId", "idempotencyKey", "operation", "inputId"].includes(key)) || !/^[A-Za-z0-9._:-]{1,128}$/.test(inputId)) {
            frame(socket, requestError(requestId, "CP_INVALID_REQUEST", "queued input ID is invalid"));
            return;
          }
        } else if (operation === "setModel") {
          if (Object.keys(request).some((key) => !["protocolVersion", "type", "requestId", "idempotencyKey", "operation", "model"].includes(key)) || typeof request.model !== "string" || !/^[A-Za-z0-9._-]{1,64}\/[A-Za-z0-9._:-]{1,128}$/.test(request.model)) {
            frame(socket, requestError(requestId, "CP_INVALID_REQUEST", "model selection is invalid"));
            return;
          }
        } else if (operation === "setThinking") {
          if (Object.keys(request).some((key) => !["protocolVersion", "type", "requestId", "idempotencyKey", "operation", "thinkingLevel"].includes(key)) || !["off", "minimal", "low", "medium", "high", "xhigh", "max"].includes(String(request.thinkingLevel))) {
            frame(socket, requestError(requestId, "CP_INVALID_REQUEST", "thinking level is invalid"));
            return;
          }
        } else if (request.text !== undefined || request.deliverAs !== undefined || request.inputId !== undefined) {
          frame(socket, requestError(requestId, "CP_INVALID_REQUEST", "bridge operation has unexpected fields"));
          return;
        }
        const queuedDelivery = operation === "prompt" && Boolean(deliverAs) && !context.isIdle();
        let delivery: PendingDelivery | undefined;
        let admitted: Promise<boolean> | undefined;
        if (operation === "prompt") {
          delivery = { idempotencyKey, fingerprint, text, deliverAs };
          admitted = new Promise<boolean>((resolve) => {
            const timer = setTimeout(() => {
              if (pending.get(idempotencyKey) === delivery) pending.delete(idempotencyKey);
              resolve(false);
            }, ADMISSION_TIMEOUT_MS);
            delivery!.resolve = (accepted) => {
              clearTimeout(timer);
              if (pending.get(idempotencyKey) === delivery) pending.delete(idempotencyKey);
              resolve(accepted);
            };
          });
          pending.set(idempotencyKey, delivery);
        }
        try {
          appendMarker({ idempotencyKey, fingerprint, operation, state: "pending", deliveryState: "pending" });
          if (operation === "prompt") {
            if (!delivery || !admitted) throw new Error("web input admission was not initialized");
            const expansion = promptExpansion(pi, text);
            const dispatch = webPi.sendUserMessage(text, {
              ...(deliverAs ? { deliverAs } : {}),
              ...(expansion === "safe" ? { expandPromptTemplates: true } : {}),
              ...(queuedDelivery ? { queueId: idempotencyKey } : {}),
              preflightResult: (accepted) => {
                if (delivery) delivery.preflight = accepted;
                delivery?.resolve?.(accepted);
              },
            });
            if (dispatch && typeof (dispatch as Promise<void>).catch === "function") {
              void (dispatch as Promise<void>).catch((error) => {
                if (pending.get(idempotencyKey) !== delivery) return;
                delivery!.error = { code: "CP_RUNTIME_UNAVAILABLE", message: String(error instanceof Error ? error.message : error) };
                delivery!.resolve?.(false);
              });
            }
            if (!await admitted) {
              await Promise.resolve();
              if (delivery.preflight === false) {
                const error = { code: "CP_INPUT_REJECTED", message: delivery.error?.message ?? "Pi rejected the browser input before delivery" };
                appendMarker({ idempotencyKey, fingerprint, operation, state: "rejected", deliveryState: "rejected", error });
                const rejected: StoredResult = { fingerprint, type: "rejected", operation, deliveryState: "rejected", error };
                rememberResult(idempotencyKey, rejected);
                frame(socket, storedResponse(requestId, rejected));
                return;
              }
              if (delivery.error) {
                appendMarker({ idempotencyKey, fingerprint, operation, state: "rejected", deliveryState: "rejected", error: delivery.error });
                const rejected: StoredResult = { fingerprint, type: "rejected", operation, deliveryState: "rejected", error: delivery.error };
                rememberResult(idempotencyKey, rejected);
                frame(socket, storedResponse(requestId, rejected));
                return;
              }
              appendMarker({ idempotencyKey, fingerprint, operation, state: "uncertain", deliveryState: "delivery_uncertain" });
              const uncertain: StoredResult = { fingerprint, type: "uncertain", operation, deliveryState: "delivery_uncertain", error: { code: "CP_DELIVERY_UNCERTAIN", message: "browser input delivery could not be proven" } };
              rememberResult(idempotencyKey, uncertain);
              frame(socket, storedResponse(requestId, uncertain));
              return;
            }
          } else if (operation === "removeQueued") {
            const webContext = context as WebExtensionContext;
            if (typeof webContext.removeQueuedMessage !== "function" || !webContext.removeQueuedMessage(inputId)) {
              const error = { code: "CP_QUEUE_ITEM_NOT_FOUND", message: "the queued browser input is no longer removable" };
              appendMarker({ idempotencyKey, fingerprint, operation, state: "rejected", deliveryState: "rejected", inputId, error });
              const rejected: StoredResult = { fingerprint, type: "rejected", operation, deliveryState: "rejected", error };
              rememberResult(idempotencyKey, rejected);
              frame(socket, storedResponse(requestId, rejected));
              return;
            }
            const acceptedEntryId = appendMarker({ idempotencyKey, fingerprint, operation, state: "accepted", deliveryState: "removed", inputId });
            const accepted: StoredResult = { fingerprint, type: "accepted", operation, deliveryState: "removed", acceptedEntryId };
            rememberResult(idempotencyKey, accepted);
            frame(socket, storedResponse(requestId, accepted));
            return;
          } else if (operation === "setModel") {
            const separator = String(request.model).indexOf("/");
            const model = context.modelRegistry.find(String(request.model).slice(0, separator), String(request.model).slice(separator + 1));
            if (!model || !await pi.setModel(model)) {
              frame(socket, requestError(requestId, "CP_MODEL_UNAVAILABLE", "the selected model is unavailable or unauthenticated"));
              return;
            }
          } else if (operation === "setThinking") {
            pi.setThinkingLevel(String(request.thinkingLevel) as never);
          } else if (operation === "stop") {
            context.abort();
          } else {
            context.compact();
          }
          const acceptedEntryId = appendMarker({ idempotencyKey, fingerprint, operation, state: "accepted", deliveryState: queuedDelivery ? "queued" : "accepted" });
          const accepted: StoredResult = { fingerprint, type: "accepted", operation, deliveryState: queuedDelivery ? "queued" : "accepted", acceptedEntryId };
          rememberResult(idempotencyKey, accepted);
          frame(socket, storedResponse(requestId, accepted));
        } catch (error) {
          if (delivery && pending.get(idempotencyKey) === delivery) pending.delete(idempotencyKey);
          frame(socket, requestError(requestId, "CP_RUNTIME_UNAVAILABLE", String(error instanceof Error ? error.message : "web session command failed")));
        }
      };
      socket.on("data", (chunk: Buffer) => {
        buffer = Buffer.concat([buffer, chunk]);
        if (buffer.length > MAX_FRAME_BYTES + 1) return socket.destroy();
        let newline = buffer.indexOf(0x0a);
        while (newline >= 0) {
          const body = buffer.subarray(0, newline).toString("utf8");
          buffer = buffer.subarray(newline + 1);
          try {
            const parsed = JSON.parse(body) as unknown;
            if (!parsed || typeof parsed !== "object" || canonical(parsed) !== body) { socket.destroy(); return; }
            processing = processing.then(() => onFrame(parsed)).catch(() => socket.destroy());
          } catch { socket.destroy(); return; }
          newline = buffer.indexOf(0x0a);
        }
      });
      socket.on("close", () => clients.delete(client));
    });
    metadata = { protocolVersion: PROTOCOL_VERSION, runId, conversationId: conversation.conversationId, projectId: project.projectId, sessionId: ctx.sessionManager.getSessionId(), controllerBuildId, runBuildId, manifestDigest, childPid: process.pid, childStartIdentity: processIdentity, restartEpoch, capability, socketPath };
    server.on("error", () => close());
    if (existsSync(socketPath)) {
      try {
        if (!lstatSync(socketPath).isSocket()) { close(); return; }
        unlinkSync(socketPath);
      } catch { close(); return; }
    }
    if (existsSync(descriptorPath)) {
      try {
        const info = lstatSync(descriptorPath);
        if (!info.isFile() || info.uid !== process.getuid?.() || (info.mode & 0o077) !== 0) return;
        unlinkSync(descriptorPath);
      } catch { return; }
    }
    server.listen(socketPath, () => {
      try { chmodSync(socketPath!, 0o600); writeFileSync(descriptorPath!, `${canonical(metadata)}\n`, { mode: 0o600, flag: "wx" }); chmodSync(descriptorPath!, 0o600); }
      catch { close(); }
    });
    server.unref();
  };

  pi.on("session_start", start);
  pi.on("session_shutdown", close);
  const on = pi.on as unknown as (event: string, handler: (value: unknown) => void) => void;
  for (const event of ["agent_start", "agent_end", "agent_settled", "message_start", "message_update", "message_end", "tool_execution_start", "tool_execution_end", "session_compact"]) {
    on(event, (value) => publish(value as JsonObject));
  }
}
