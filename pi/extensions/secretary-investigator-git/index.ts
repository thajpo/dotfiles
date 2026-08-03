import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import path from "node:path";

const PROJECT_ID = /^[0-9a-f]{64}$/;

function environment(): { projectId: string; control: string } {
  const projectId = process.env.PI_SECRETARY_PROJECT_ID ?? "";
  const control = process.env.PI_SECRETARY_CONTROL ?? "";
  if (!PROJECT_ID.test(projectId) || process.env.PI_SECRETARY_READ_ONLY !== "1" ||
      !control.startsWith("/") || path.basename(control) !== "pi-secretary-control.py") {
    throw new Error("secretary investigator requires validated read-only project environment");
  }
  return { projectId, control };
}

export default function secretaryInvestigatorGit(pi: ExtensionAPI): void {
  if (process.env.PI_SECRETARY_READ_ONLY !== "1") return;
  pi.registerTool({
    name: "secretary_git",
    label: "Read Git",
    description: "Run a bounded read-only Git operation in the secretary's registered project.",
    parameters: Type.Object({
      operation: Type.Union([
        Type.Literal("status"), Type.Literal("log"), Type.Literal("diff"), Type.Literal("show"),
        Type.Literal("branch"), Type.Literal("rev-parse"), Type.Literal("remote"), Type.Literal("tag"), Type.Literal("worktree"),
      ]),
      args: Type.Optional(Type.Array(Type.String({ maxLength: 512 }), { maxItems: 32 })),
    }),
    async execute(_id, params, signal) {
      const { projectId, control } = environment();
      const result = await pi.exec("python3", [
        control, "git-read", "--project-id", projectId, "--operation", params.operation, "--", ...(params.args ?? []),
      ], { signal });
      if (result.code !== 0) throw new Error((result.stderr || result.stdout || "secretary Git inspection failed").trim());
      return { content: [{ type: "text", text: result.stdout.trim() }], details: {} };
    },
  });
}
