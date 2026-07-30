import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import fs from "node:fs";
import path from "node:path";

const PROJECT_ID = /^[0-9a-f]{64}$/;
const ALIAS = /^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$/;
const ID = /^[a-z0-9][a-z0-9-]{0,62}$/;
const ROLE = Type.Union([
  Type.Literal("feature"), Type.Literal("research"), Type.Literal("analysis"),
  Type.Literal("review"), Type.Literal("integration"),
]);

type Authorization = "record" | "promote" | "open";
let authorized = new Set<Authorization>();

function requiredEnvironment(): { projectId: string; alias: string; control: string } {
  const projectId = process.env.PI_SECRETARY_PROJECT_ID ?? "";
  const alias = process.env.PI_SECRETARY_ALIAS ?? "";
  const control = process.env.PI_SECRETARY_CONTROL ?? "";
  if (!PROJECT_ID.test(projectId) || !ALIAS.test(alias) || process.env.PI_SECRETARY_READ_ONLY !== "1" ||
      !control.startsWith("/") || path.basename(control) !== "pi-secretary-control.py") {
    throw new Error("secretary requires validated host launch environment");
  }
  return { projectId, alias, control };
}

function projectStatusSkill(): string {
  const agentDir = process.env.PI_CODING_AGENT_DIR ?? path.join(process.env.HOME ?? "", ".pi", "agent");
  const configured = process.env.PI_SECRETARY_SKILL_PATH;
  const skill = configured ?? path.join(agentDir, "skills", "project-status", "SKILL.md");
  if (path.basename(skill) !== "SKILL.md" || !skill.endsWith(path.join("skills", "project-status", "SKILL.md"))) {
    throw new Error("secretary skill path is not the fixed project-status skill");
  }
  try {
    const directory = path.dirname(skill);
    const sources = [
      ["SKILL.md", skill],
      ["references/investigation.md", path.join(directory, "references", "investigation.md")],
      ["references/output.md", path.join(directory, "references", "output.md")],
    ] as const;
    const value = sources.map(([label, source]) => `\n### ${label}\n${fs.readFileSync(source, "utf8")}`).join("\n");
    if (value.length > 64 * 1024) throw new Error("project-status skill is too large");
    return value;
  } catch (error) {
    throw new Error(`project-status skill unavailable: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function updateAuthorization(text: string, source: string): void {
  authorized = new Set();
  if (source === "extension") return;
  const value = text.toLowerCase();
  if (/\b(record|save|park)\b.*\b(idea|this)|\b(save|record) this\b/.test(value)) authorized.add("record");
  if (/\b(spin|promote)\b.*\b(out|session|agent)|\b(new feature|create (an? )?agent|open (an? )?agent)\b/.test(value)) authorized.add("promote");
  if (/\b(open|resume|focus|switch to)\b/.test(value)) authorized.add("open");
  if (/^\s*(yes|yep|do it|go ahead|please do|sounds good)[.!\s]*$/i.test(text)) {
    authorized.add("record"); authorized.add("promote"); authorized.add("open");
  }
}

function consume(kind: Authorization): void {
  if (!authorized.has(kind)) throw new Error(`Current user turn did not authorize secretary ${kind}`);
  authorized = new Set();
}

export default function secretary(pi: ExtensionAPI): void {
  pi.on("input", (event) => {
    updateAuthorization(event.text, event.source);
    return { action: "continue" };
  });

  pi.on("session_start", (_event, ctx) => {
    const { alias, projectId } = requiredEnvironment();
    if (typeof pi.setSessionName === "function") pi.setSessionName(`secretary-${alias}`);
    ctx.ui.setStatus("secretary", ctx.ui.theme.fg("accent", `read-only · ${alias} · ${projectId.slice(0, 12)}`));
  });

  pi.on("before_agent_start", (event) => {
    const { alias, projectId } = requiredEnvironment();
    return {
      systemPrompt: event.systemPrompt + `\n\nYou are the persistent secretary for project ${alias} (project ID ${projectId}).\n` +
        "Switchboard boundary: inspect project evidence, record bounded ideas, and create/open peer full agents only after the current natural-language user turn explicitly authorizes it. " +
        "Apply this boundary over any skill suggestion to fan out: never use subagents. You are not a coding agent and cannot modify repository files, Git, or run shell commands. " +
        "A promoted full agent owns implementation, its task_packet, direct technical discussion, and headless subagents. Never fabricate a user turn or relay general agent chat.\n\n" +
        "## Project-status skill\n" + projectStatusSkill() +
        "\n\nUse only the read allowlist and secretary semantic tools. User affirmation such as 'yes' is sufficient authorization; never ask for a second form or confirmation.",
    };
  });

  const invoke = async (args: string[], signal: AbortSignal) => {
    const { control } = requiredEnvironment();
    const result = await pi.exec("/usr/bin/python3", [control, ...args], { signal, timeout: 120_000 });
    if (result.code !== 0) throw new Error((result.stderr || result.stdout || "secretary operation failed").trim());
    return result.stdout.trim();
  };

  pi.registerTool({
    name: "secretary_record_idea", label: "Record idea",
    description: "Record a bounded idea after the user explicitly asks to save, record, or park it.",
    parameters: Type.Object({ title: Type.String({ maxLength: 200 }), brief: Type.String({ maxLength: 16384 }) }),
    async execute(_id, params, signal) {
      consume("record"); const { projectId } = requiredEnvironment();
      const text = await invoke(["record-idea", "--project-id", projectId, "--title", params.title, "--brief", params.brief], signal);
      return { content: [{ type: "text", text }], details: {} };
    },
  });
  pi.registerTool({
    name: "secretary_create_workstream", label: "Create full agent",
    description: "Create and focus a separate persistent full agent after explicit user instruction or affirmation.",
    parameters: Type.Object({ title: Type.String({ maxLength: 200 }), brief: Type.String({ maxLength: 16384 }), role: ROLE }),
    async execute(_id, params, signal) {
      consume("promote"); const { projectId } = requiredEnvironment();
      const text = await invoke(["promote", "--project-id", projectId, "--title", params.title, "--brief", params.brief, "--role", params.role], signal);
      return { content: [{ type: "text", text }], details: {} };
    },
  });
  pi.registerTool({
    name: "secretary_open_workstream", label: "Open full agent",
    description: "Open/focus an existing full agent after explicit user instruction.",
    parameters: Type.Object({ workstreamId: Type.String({ pattern: ID.source }) }),
    async execute(_id, params, signal) {
      consume("open"); const { projectId } = requiredEnvironment();
      const text = await invoke(["focus-workstream", "--project-id", projectId, "--workstream-id", params.workstreamId], signal);
      return { content: [{ type: "text", text }], details: {} };
    },
  });
  pi.registerTool({
    name: "secretary_list_workstreams", label: "List full agents",
    description: "List validated persistent workstreams for this project.",
    parameters: Type.Object({}),
    async execute(_id, _params, signal) {
      const { projectId } = requiredEnvironment();
      const text = await invoke(["project-workstreams", "--project-id", projectId], signal);
      return { content: [{ type: "text", text }], details: {} };
    },
  });
}
