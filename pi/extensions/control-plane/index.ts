import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

function controlPath(): string {
  const configured = process.env.PI_CONTROL_CLI ?? path.join(os.homedir(), ".local", "bin", "pi-control");
  if (!path.isAbsolute(configured) || path.basename(configured) !== "pi-control") throw new Error("invalid controller CLI path");
  return configured;
}

function stateArgs(): string[] {
  const state = process.env.PI_CONTROL_STATE_ROOT;
  if (state === undefined) return [];
  if (!path.isAbsolute(state) || state.includes("\0")) throw new Error("invalid controller state path");
  return ["--state-root", state];
}

function parseJson(text: string): Record<string, unknown> {
  const value: unknown = JSON.parse(text);
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error("controller returned a non-object response");
  return value as Record<string, unknown>;
}

async function invoke(pi: ExtensionAPI, args: string[], signal: AbortSignal): Promise<Record<string, unknown>> {
  const result = await pi.exec("python3", [controlPath(), ...stateArgs(), ...args], { signal, timeout: 60_000 });
  if (result.code !== 0) throw new Error((result.stderr || result.stdout || "controller request failed").trim().slice(0, 1024));
  return parseJson(result.stdout);
}

async function invokeRequest(pi: ExtensionAPI, command: string[], request: unknown, signal: AbortSignal): Promise<Record<string, unknown>> {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "pi-control-request-"));
  const file = path.join(directory, "request.json");
  try {
    fs.writeFileSync(file, JSON.stringify(request), { encoding: "utf8", mode: 0o600, flag: "wx" });
    return await invoke(pi, [...command, "--request-json", file], signal);
  } finally {
    try { fs.rmSync(directory, { recursive: true, force: true }); } catch { /* preserve controller result */ }
  }
}

export default function controlPlaneExtension(pi: ExtensionAPI): void {
  pi.registerTool({
    name: "controller_status",
    label: "Controller project status",
    description: "Refresh and inspect one exact controller project; presentation only.",
    parameters: Type.Object({ projectId: Type.String({ pattern: "^prj_[0-9a-f]{32}$" }), refresh: Type.Optional(Type.Boolean()) }),
    async execute(_id, params, signal) {
      return { content: [{ type: "text", text: JSON.stringify(await invoke(pi, ["status", params.projectId, ...(params.refresh === false ? ["--no-refresh"] : [])], signal)) }], details: {} };
    },
  });
  pi.registerTool({
    name: "controller_focus",
    label: "Focus exact controller resource",
    description: "Resolve one exact conversation, working copy, change, or run without writing focus state.",
    parameters: Type.Object({ projectId: Type.String({ pattern: "^prj_[0-9a-f]{32}$" }), resourceId: Type.String({ pattern: "^(conv|wc|chg|run)_[0-9a-f]{32}$" }), expectedVersion: Type.Optional(Type.Integer({ minimum: 1 })) }),
    async execute(_id, params, signal) {
      const args = ["focus", params.projectId, params.resourceId];
      if (params.expectedVersion !== undefined) args.push("--expected-version", String(params.expectedVersion));
      return { content: [{ type: "text", text: JSON.stringify(await invoke(pi, args, signal)) }], details: {} };
    },
  });
  pi.registerTool({
    name: "controller_submit_change",
    label: "Submit controller change",
    description: "Submit an explicitly selected clean or dirty change through the shared controller queue; never integrates it.",
    parameters: Type.Object({ request: Type.Record(Type.String(), Type.Unknown()) }),
    async execute(_id, params, signal) {
      return { content: [{ type: "text", text: JSON.stringify(await invokeRequest(pi, ["change", "submit"], params.request, signal)) }], details: {} };
    },
  });
  pi.registerTool({
    name: "controller_create_workstream",
    label: "Create controller workstream",
    description: "Create a workstream conversation only with exact semantic approval and an existing controller-owned separate working copy.",
    parameters: Type.Object({ request: Type.Record(Type.String(), Type.Unknown()) }),
    async execute(_id, params, signal) {
      return { content: [{ type: "text", text: JSON.stringify(await invokeRequest(pi, ["workstream", "create"], params.request, signal)) }], details: {} };
    },
  });
  pi.registerTool({
    name: "controller_request_review",
    label: "Request controller review",
    description: "Request an exact-revision review bound to a live read-only reviewer run and capability.",
    parameters: Type.Object({ request: Type.Record(Type.String(), Type.Unknown()) }),
    async execute(_id, params, signal) {
      return { content: [{ type: "text", text: JSON.stringify(await invokeRequest(pi, ["review", "request"], params.request, signal)) }], details: {} };
    },
  });
  pi.registerTool({
    name: "controller_submit_review",
    label: "Submit controller review",
    description: "Submit an authenticated review receipt bound to its exact reviewer run and source revision.",
    parameters: Type.Object({ request: Type.Record(Type.String(), Type.Unknown()) }),
    async execute(_id, params, signal) {
      return { content: [{ type: "text", text: JSON.stringify(await invokeRequest(pi, ["review", "submit"], params.request, signal)) }], details: {} };
    },
  });
  pi.registerTool({
    name: "controller_analyze_integration",
    label: "Analyze controller integration",
    description: "Create a deterministic read-before-write integration analysis for one exact change revision and target.",
    parameters: Type.Object({ request: Type.Record(Type.String(), Type.Unknown()) }),
    async execute(_id, params, signal) {
      return { content: [{ type: "text", text: JSON.stringify(await invokeRequest(pi, ["integration", "analyze"], params.request, signal)) }], details: {} };
    },
  });
  pi.registerTool({
    name: "controller_authorize_integration",
    label: "Authorize controller integration",
    description: "Issue an expiring single-use integration authorization only after exact accepted review evidence.",
    parameters: Type.Object({ request: Type.Record(Type.String(), Type.Unknown()) }),
    async execute(_id, params, signal) {
      return { content: [{ type: "text", text: JSON.stringify(await invokeRequest(pi, ["integration", "authorize"], params.request, signal)) }], details: {} };
    },
  });
  pi.registerTool({
    name: "controller_integrate",
    label: "Integrate controller change",
    description: "Consume one exact integration authorization and run the controller's CAS/recovery-gated integration.",
    parameters: Type.Object({ request: Type.Record(Type.String(), Type.Unknown()) }),
    async execute(_id, params, signal) {
      return { content: [{ type: "text", text: JSON.stringify(await invokeRequest(pi, ["integration", "integrate"], params.request, signal)) }], details: {} };
    },
  });
  pi.registerTool({
    name: "controller_recovery_status",
    label: "Inspect controller recovery status",
    description: "Read durable operation, event, attention, and integration evidence for one exact project without mutation.",
    parameters: Type.Object({ projectId: Type.String({ pattern: "^prj_[0-9a-f]{32}$" }) }),
    async execute(_id, params, signal) {
      return { content: [{ type: "text", text: JSON.stringify(await invoke(pi, ["recovery", "status", params.projectId], signal)) }], details: {} };
    },
  });
  pi.registerTool({
    name: "controller_technical_details",
    label: "Inspect controller technical details",
    description: "Read bounded technical evidence for one exact project-scoped controller resource; secrets and credentials are not returned.",
    parameters: Type.Object({ projectId: Type.String({ pattern: "^prj_[0-9a-f]{32}$" }), resourceType: Type.String(), resourceId: Type.String() }),
    async execute(_id, params, signal) {
      return { content: [{ type: "text", text: JSON.stringify(await invoke(pi, ["recovery", "details", params.projectId, params.resourceType, params.resourceId], signal)) }], details: {} };
    },
  });
}
