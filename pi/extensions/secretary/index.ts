import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import fs from "node:fs";
import path from "node:path";

const PROJECT_ID = /^[0-9a-f]{64}$/;
const ALIAS = /^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$/;

function requiredEnvironment(): { projectId: string; alias: string } {
  const projectId = process.env.PI_SECRETARY_PROJECT_ID ?? "";
  const alias = process.env.PI_SECRETARY_ALIAS ?? "";
  if (!PROJECT_ID.test(projectId) || !ALIAS.test(alias) || process.env.PI_SECRETARY_READ_ONLY !== "1") {
    throw new Error("secretary requires validated host launch environment");
  }
  return { projectId, alias };
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

export default function secretary(pi: ExtensionAPI): void {
  pi.on("session_start", (_event, ctx) => {
    const { alias, projectId } = requiredEnvironment();
    if (typeof pi.setSessionName === "function") pi.setSessionName(`secretary-${alias}`);
    ctx.ui.setStatus("secretary", ctx.ui.theme.fg("accent", `read-only · ${alias} · ${projectId.slice(0, 12)}`));
  });

  pi.on("before_agent_start", (event) => {
    const { alias, projectId } = requiredEnvironment();
    return {
      systemPrompt: event.systemPrompt + `\n\nYou are the persistent read-only secretary for project ${alias} (project ID ${projectId}).\n` +
        "Switchboard boundary: inspect the validated project repository and persistent project-status records only. " +
        "Apply this secretary boundary over any skill suggestion to fan out: use this one session and never launch investigators. " +
        "You are not a coding agent and must not modify files, Git state, worktrees, tmux, processes, tasks, or delegated agents. " +
        "Never ask another agent to act, fabricate a user turn, or auto-send a prompt. The human controls all conversation turns. " +
        "Promotion, implementation, shell commands, and per-project Pi sessions are outside this secretary.\n\n" +
        "## Project-status skill\n" + projectStatusSkill() +
        "\n\nSecretary enforcement: do not fan out, invoke delegated agents, or use any tool other than the CLI read allowlist. Never mutate state.",
    };
  });
}
