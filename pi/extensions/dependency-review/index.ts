import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const CHANNEL_SYMBOL = Symbol.for("pi.controllerChannel.v1");
type Channel = { registerTool(operation: string, definition: Parameters<ExtensionAPI["registerTool"]>[0]): void; request(operation: string, payload: Record<string, unknown>, signal?: AbortSignal): Promise<unknown> };
function channel(): Channel { const value = (globalThis as unknown as Record<symbol, Channel>)[CHANNEL_SYMBOL]; if (!value) throw new Error("authenticated controller channel is unavailable"); return value; }
function result(value: unknown) { return { content: [{ type: "text" as const, text: JSON.stringify(value) }], details: {} }; }

export default function dependencyReview(_pi: ExtensionAPI): void {
  const controller = channel();
  controller.registerTool("dependency.inventory", {
    name: "inventory_dependency_changes", label: "Inventory dependency changes", description: "Diff immutable base and candidate package trees selected by the controller.",
    parameters: Type.Object({ changeId: Type.String({ pattern: "^chg_[0-9a-f]{32}$" }), revision: Type.Integer({ minimum: 1 }), workerReason: Type.Optional(Type.Record(Type.String({ maxLength: 256 }), Type.String({ maxLength: 2048 }))) }, { additionalProperties: false }),
    async execute(_id, params, signal) { return result(await controller.request("dependency.inventory", params, signal)); },
  });
  controller.registerTool("dependency.disposition", {
    name: "record_dependency_disposition", label: "Record dependency disposition", description: "Record policy disposition for an exact dependency delta.",
    parameters: Type.Object({ dependencyChangeId: Type.String({ pattern: "^dep_[0-9a-f]{32}$" }), disposition: Type.Union([Type.Literal("standard"), Type.Literal("review-required"), Type.Literal("rejected")]) }, { additionalProperties: false }),
    async execute(_id, params, signal) { return result(await controller.request("dependency.disposition", params, signal)); },
  });
  controller.registerTool("package-review.record", {
    name: "record_package_security_review", label: "Record package security review", description: "Record a review bound by the controller to this real investigator run.",
    parameters: Type.Object({ dependencyChangeId: Type.String({ pattern: "^dep_[0-9a-f]{32}$" }), evidence: Type.Record(Type.String({ maxLength: 128 }), Type.Unknown()), riskLevel: Type.Union([Type.Literal("low"), Type.Literal("medium"), Type.Literal("high"), Type.Literal("unknown")]), recommendation: Type.String({ minLength: 1, maxLength: 2048 }) }, { additionalProperties: false }),
    async execute(_id, params, signal) { return result(await controller.request("package-review.record", params, signal)); },
  });
  controller.registerTool("package-review.gate", {
    name: "check_package_review_gate", label: "Check package review gate", description: "Check exact candidate package review readiness.",
    parameters: Type.Object({ changeId: Type.String({ pattern: "^chg_[0-9a-f]{32}$" }), revision: Type.Integer({ minimum: 1 }) }, { additionalProperties: false }),
    async execute(_id, params, signal) { return result(await controller.request("package-review.gate", params, signal)); },
  });
  controller.registerTool("package.request", {
    name: "request_package_operation", label: "Request package operation", description: "Request one locked add, remove, or sync operation with scripts disabled. Approval remains outside the model channel.",
    parameters: Type.Object({ changeId: Type.String({ pattern: "^chg_[0-9a-f]{32}$" }), revision: Type.Integer({ minimum: 1 }), ecosystem: Type.Union([Type.Literal("npm"), Type.Literal("python")]), action: Type.Union([Type.Literal("add"), Type.Literal("remove"), Type.Literal("sync")]), packageName: Type.Optional(Type.String({ minLength: 1, maxLength: 256 })), exactVersion: Type.Optional(Type.String({ minLength: 1, maxLength: 256 })) }, { additionalProperties: false }),
    async execute(_id, params, signal) { return result(await controller.request("package.request", params, signal)); },
  });
  controller.registerTool("package.status", {
    name: "package_operation_status", label: "Package operation status", description: "Read one package request status in the authenticated project.",
    parameters: Type.Object({ packageRequestId: Type.String({ pattern: "^pkreq_[0-9a-f]{32}$" }) }, { additionalProperties: false }),
    async execute(_id, params, signal) { return result(await controller.request("package.status", params, signal)); },
  });
}
