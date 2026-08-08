import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import path from "node:path";

function control(): string {
  const value = process.env.PI_SYSTEM_CONTROL;
  if (!value || !path.isAbsolute(value) || path.basename(value) !== "pi-control") throw new Error("Pi system controller path is missing");
  return value;
}

async function invoke(pi: ExtensionAPI, command: string, request: Record<string, unknown>, signal: AbortSignal): Promise<string> {
  const state = process.env.PI_SYSTEM_STATE_ROOT;
  const args = state ? ["--state-root", state, "dependency", command] : ["dependency", command];
  const result = await pi.exec("python3", [control(), ...args, "--request-json", JSON.stringify(request)], { signal, timeout: 30_000 });
  if (result.code !== 0) throw new Error((result.stderr || result.stdout || "dependency operation failed").trim().slice(0, 1024));
  return result.stdout.trim();
}

export default function dependencyReview(pi: ExtensionAPI): void {
  pi.registerTool({
    name: "record_dependency_disposition",
    label: "Record dependency disposition",
    description: "Record the controller's explicit disposition for a detected exact dependency candidate.",
    parameters: Type.Object({ dependencyChangeId: Type.String({ pattern: "^dep_[0-9a-f]{32}$" }), disposition: Type.Union([Type.Literal("standard"), Type.Literal("review-required"), Type.Literal("rejected")]) }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      const value = await invoke(pi, "disposition", params, signal);
      return { content: [{ type: "text", text: value }], details: {} };
    },
  });
}
