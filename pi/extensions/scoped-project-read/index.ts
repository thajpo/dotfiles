import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

function requestRoot(): string {
  const value = process.env.PI_SYSTEM_CONTROL;
  if (!value || !value.startsWith("/")) throw new Error("Pi system control path is missing");
  return value;
}

function assignment(): { projectId: string; workingCopyId?: string } {
  const projectId = process.env.PI_SYSTEM_PROJECT_ID;
  const workingCopyId = process.env.PI_SYSTEM_WORKING_COPY_ID;
  if (!projectId || !/^prj_[0-9a-f]{32}$/.test(projectId)) throw new Error("invalid project scope");
  if (workingCopyId && !/^wc_[0-9a-f]{32}$/.test(workingCopyId)) throw new Error("invalid working-copy scope");
  return { projectId, ...(workingCopyId ? { workingCopyId } : {}) };
}

export default function scopedProjectRead(pi: ExtensionAPI): void {
  const assigned = assignment();
  const call = async (operation: string, request: Record<string, unknown>, signal: AbortSignal) => {
    const payload = { projectId: assigned.projectId, ...(assigned.workingCopyId ? { workingCopyId: assigned.workingCopyId } : {}), operation, ...request };
    const result = await pi.exec("python3", [requestRoot(), "scoped-read", "--request-json", JSON.stringify(payload)], { signal });
    if (result.code !== 0) throw new Error((result.stderr || result.stdout || "scoped read failed").trim());
    return { content: [{ type: "text", text: result.stdout.trim() }], details: {} };
  };
  pi.registerTool({ name: "read", label: "Read project file", description: "Read a bounded file in the assigned project working copy.", parameters: Type.Object({ path: Type.String({ maxLength: 4096 }), startLine: Type.Optional(Type.Integer({ minimum: 1 })), maxLines: Type.Optional(Type.Integer({ minimum: 1, maximum: 10000 })) }), async execute(_id, params, signal) { return call("read", params, signal); } });
  pi.registerTool({ name: "ls", label: "List project files", description: "List a bounded directory in the assigned project working copy.", parameters: Type.Object({ path: Type.Optional(Type.String({ maxLength: 4096 })), pattern: Type.Optional(Type.String({ maxLength: 256 })) }), async execute(_id, params, signal) { return call("list", params, signal); } });
  pi.registerTool({ name: "grep", label: "Search project files", description: "Search bounded text in the assigned project working copy.", parameters: Type.Object({ pattern: Type.String({ maxLength: 512 }), path: Type.Optional(Type.String({ maxLength: 4096 })) }), async execute(_id, params, signal) { return call("grep", params, signal); } });
}
