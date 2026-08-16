import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const CHANNEL_SYMBOL = Symbol.for("pi.controllerChannel.v1");
type ChannelApi = { request(operation: string, payload: Record<string, unknown>): Promise<unknown> };

export default function scopedProjectRead(pi: ExtensionAPI): void {
  const channel = (globalThis as unknown as Record<symbol, ChannelApi | undefined>)[CHANNEL_SYMBOL];
  if (!channel) return;
  const call = async (operation: string, request: Record<string, unknown>, signal: AbortSignal) => {
    if (signal.aborted) throw new Error("scoped read was aborted");
    const result = await channel.request("scoped-read", { ...request, operation });
    return { content: [{ type: "text" as const, text: JSON.stringify(result) }], details: {} };
  };
  pi.registerTool({ name: "read", label: "Read project file", description: "Read a bounded regular file relative to the authenticated controller scope.", parameters: Type.Object({ path: Type.String({ maxLength: 4096 }), startLine: Type.Optional(Type.Integer({ minimum: 1 })), maxLines: Type.Optional(Type.Integer({ minimum: 1, maximum: 10000 })) }), async execute(_id, params, signal) { return call("read", params, signal); } });
  pi.registerTool({ name: "ls", label: "List project files", description: "List one bounded directory relative to the authenticated controller scope.", parameters: Type.Object({ path: Type.Optional(Type.String({ maxLength: 4096 })), pattern: Type.Optional(Type.String({ maxLength: 256 })) }), async execute(_id, params, signal) { return call("list", params, signal); } });
  pi.registerTool({ name: "grep", label: "Search project files", description: "Search a bounded, non-symlink-following tree in the authenticated controller scope.", parameters: Type.Object({ pattern: Type.String({ maxLength: 512 }), path: Type.Optional(Type.String({ maxLength: 4096 })) }), async execute(_id, params, signal) { return call("grep", params, signal); } });
  pi.registerTool({ name: "git_read", label: "Inspect scoped Git", description: "Run one bounded status, diff, log, show, or rev-parse query at the controller-selected revision.", parameters: Type.Object({ query: Type.Union([Type.Literal("status"), Type.Literal("diff"), Type.Literal("log"), Type.Literal("show"), Type.Literal("rev-parse")]), path: Type.Optional(Type.String({ maxLength: 4096 })), mode: Type.Optional(Type.Union([Type.Literal("revision"), Type.Literal("working")])), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 100 })) }), async execute(_id, params, signal) { return call("git", params, signal); } });
}
