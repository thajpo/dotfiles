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

type Authorization = "record" | "promote" | "open" | "ack" | "review" | "land" | "integrate" | "cleanup";
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

function recordWasAuthorized(value: string): boolean {
  const verb = "(?:record|save|park|log|note|capture|document|write\\s+down)\\w*";
  const object = "(?:idea|this|guidance|lesson|constraint|finding|insight|recommendation|baseline|capacity|note|it)";
  const direct = new RegExp(`\\b${verb}\\b[\\s\\S]{0,120}\\b${object}\\b`).test(value);
  const evaluative = new RegExp(`\\b(?:worth|useful|valuable|important|helpful)\\b[\\s\\S]{0,80}\\b${verb}\\b`).test(value);
  const negative = new RegExp(`\\b(?:don't|do not|never|not|without)\\b[\\s\\S]{0,30}\\b${verb}\\b`).test(value);
  return !negative && (direct || evaluative);
}

function updateAuthorization(text: string, source: string): void {
  authorized = new Set();
  if (source === "extension") return;
  const value = text.toLowerCase();
  if (recordWasAuthorized(value)) authorized.add("record");
  if (/\b(spin|promote)\b.*\b(out|session|agent)|\b(new feature|create (an? )?agent|open (an? )?agent)\b/.test(value)) authorized.add("promote");
  if (/\b(open|resume|focus|switch to)\b/.test(value)) authorized.add("open");
  if (/\b(acknowledge|dismiss|clear)\b.*\b(attention|event|notification|this)\b/.test(value)) authorized.add("ack");
  if (/\b(review|reviewer)\b.*\b(create|start|assign|open|this|it)\b|\b(create|start|assign)\b.*\breviewer\b/.test(value)) authorized.add("review");
  if (/\b(land|fast-forward)\b.*\b(review|candidate|workstream|this|it)\b|\bmerge\b.*\b(reviewed|candidate|workstream|this|it)\b/.test(value)) authorized.add("land");
  if (/\b(integrat|integration)\w*\b.*\b(agent|workstream|create|start|this|it)\b|\b(create|start)\b.*\bintegration\b/.test(value)) authorized.add("integrate");
  if (/\b(clean up|cleanup|remove)\b.*\b(workstream|agent|resources|this|it)\b/.test(value)) authorized.add("cleanup");
  if (/^\s*(yes|yep|do it|go ahead|please do|sounds good)[.!\s]*$/i.test(text)) {
    authorized.add("record"); authorized.add("promote"); authorized.add("open"); authorized.add("ack"); authorized.add("review"); authorized.add("land"); authorized.add("integrate"); authorized.add("cleanup");
  }
}

function consume(kind: Authorization): void {
  if (!authorized.has(kind)) throw new Error(`Current user turn did not authorize secretary ${kind}`);
  authorized = new Set();
}

export default function secretary(pi: ExtensionAPI): void {
  // Installed globally but active only under the fixed secretary launcher.
  if (process.env.PI_SECRETARY_READ_ONLY !== "1") return;
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
        "Switchboard boundary: inspect project evidence, record bounded ideas, and create/open peer full agents only after the current natural-language user turn explicitly authorizes it. Natural-language requests to log, note, capture, document, save, record, or park guidance count as record authorization; do not require the user to repeat a particular keyword. " +
        "You may use the subagent tool for read-only investigation when parallel or specialized inspection would improve the answer. Choose the number and shape of investigators according to the work rather than following a fixed fanout recipe; use their existing report formats and synthesize the results. Investigation never needs a Git worktree. " +
        "You are not a coding agent and cannot modify repository files, Git, or run shell commands. A promoted full agent owns implementation, its task_packet, and direct technical discussion. Never fabricate a user turn or relay general agent chat. Use secretary_git for bounded read-only Git inspection; never claim Git is unavailable when that tool can answer.\n\n" +
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
    name: "secretary_git", label: "Read Git",
    description: "Run a bounded read-only Git operation in this registered project. This cannot modify the repository.",
    parameters: Type.Object({
      operation: Type.Union([
        Type.Literal("status"), Type.Literal("log"), Type.Literal("diff"), Type.Literal("show"),
        Type.Literal("branch"), Type.Literal("rev-parse"), Type.Literal("remote"), Type.Literal("tag"), Type.Literal("worktree"),
      ]),
      args: Type.Optional(Type.Array(Type.String({ maxLength: 512 }), { maxItems: 32 })),
    }),
    async execute(_id, params, signal) {
      const { projectId } = requiredEnvironment();
      const text = await invoke(["git-read", "--project-id", projectId, "--operation", params.operation, "--", ...(params.args ?? [])], signal);
      return { content: [{ type: "text", text }], details: {} };
    },
  });
  pi.registerTool({
    name: "secretary_record_idea", label: "Record idea",
    description: "Record a bounded idea after the user explicitly asks to log, note, capture, document, save, record, or park it.",
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
    name: "secretary_land_reviewed", label: "Land reviewed commit",
    description: "Human-origin fast-forward-only landing of an exact current ACCEPT receipt under the target lock.",
    parameters: Type.Object({ requestId: Type.String({ pattern: ID.source }) }),
    async execute(_id, params, signal) {
      consume("land"); const { projectId } = requiredEnvironment();
      const text = await invoke(["land-reviewed", "--project-id", projectId, "--request-id", params.requestId], signal);
      return { content: [{ type: "text", text }], details: {} };
    },
  });
  pi.registerTool({
    name: "secretary_create_integration", label: "Create integration agent",
    description: "Create a separate full integration agent for a reviewed candidate after explicit user instruction; never merges automatically.",
    parameters: Type.Object({ requestId: Type.String({ pattern: ID.source }) }),
    async execute(_id, params, signal) {
      consume("integrate"); const { projectId } = requiredEnvironment();
      const text = await invoke(["integration-create", "--project-id", projectId, "--request-id", params.requestId], signal);
      return { content: [{ type: "text", text }], details: {} };
    },
  });
  pi.registerTool({
    name: "secretary_cleanup_workstream", label: "Clean landed workstream",
    description: "Guarded cleanup of exact-owned landed resources after explicit user instruction. Refuses dirty, live, moved, uncertain, or unlanded state and never forces.",
    parameters: Type.Object({ workstreamId: Type.String({ pattern: ID.source }) }),
    async execute(_id, params, signal) {
      consume("cleanup"); const { projectId } = requiredEnvironment();
      const text = await invoke(["workstream-cleanup", "--project-id", projectId, "--workstream-id", params.workstreamId], signal);
      return { content: [{ type: "text", text }], details: {} };
    },
  });
  pi.registerTool({
    name: "secretary_create_reviewer", label: "Create exact-OID reviewer",
    description: "Create and focus a distinct read-only reviewer for an exact pending review event after explicit user instruction.",
    parameters: Type.Object({ eventId: Type.String({ pattern: ID.source }) }),
    async execute(_id, params, signal) {
      consume("review"); const { projectId } = requiredEnvironment();
      const text = await invoke(["review-create", "--project-id", projectId, "--event-id", params.eventId], signal);
      return { content: [{ type: "text", text }], details: {} };
    },
  });
  pi.registerTool({
    name: "secretary_list_attention", label: "List attention",
    description: "List unacknowledged bounded workstream attention and host process-exit events.",
    parameters: Type.Object({}),
    async execute(_id, _params, signal) {
      const { projectId } = requiredEnvironment();
      const text = await invoke(["events-list", "--project-id", projectId], signal);
      return { content: [{ type: "text", text }], details: {} };
    },
  });
  pi.registerTool({
    name: "secretary_acknowledge_attention", label: "Acknowledge attention",
    description: "Acknowledge one exact attention event after explicit user instruction or affirmation.",
    parameters: Type.Object({ eventId: Type.String({ pattern: ID.source }) }),
    async execute(_id, params, signal) {
      consume("ack"); const { projectId } = requiredEnvironment();
      const text = await invoke(["event-ack", "--project-id", projectId, "--event-id", params.eventId], signal);
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
