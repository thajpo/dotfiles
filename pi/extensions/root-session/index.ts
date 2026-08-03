import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/**
 * Keep registry state aligned with Pi's own session lifecycle. Launchers create
 * the first durable record before startup; this hook registers files created by
 * /new, /fork, and other session switches without treating child sessions as
 * roots.
 */
export default function rootSessionExtension(pi: ExtensionAPI): void {
  if (process.env.PI_SUBAGENT_CHILD === "1") return;

  pi.on("session_start", (_event, ctx) => {
    const sessionFile = ctx.sessionManager.getSessionFile();
    if (!sessionFile || !path.isAbsolute(sessionFile)) return;
    const agentDir = process.env.PI_CODING_AGENT_DIR ?? path.join(os.homedir(), ".pi", "agent");
    const rootDir = path.resolve(agentDir, "sessions", "root");
    if (path.dirname(path.resolve(sessionFile)) !== rootDir) return;

    const helper = process.env.PI_ROOT_SESSION_HELPER ?? path.join(
      os.homedir(), ".local", "share", "pi", "control", "pi-root-session.py",
    );
    const profile = process.env.PI_ROOT_PROFILE ?? inferProfile(ctx.sessionManager.getSessionId() ?? "");
    const worktree = sessionCwd(sessionFile) ?? ctx.cwd;
    try {
      execFileSync("python3", [helper, "--agent-dir", agentDir, "register-existing",
        "--session-file", path.resolve(sessionFile), "--conversation-id", ctx.sessionManager.getSessionId() ?? "",
        "--profile", profile, "--worktree", worktree], {
        stdio: "ignore",
        timeout: 5_000,
      });
    } catch (error) {
      // Registry bookkeeping must not make an already-valid Pi session unusable.
      console.error(`root-session: failed to register ${sessionFile}:`, error);
    }
  });
}

function sessionCwd(sessionFile: string): string | undefined {
  try {
    const first = fs.readFileSync(sessionFile, "utf8").split("\\n").find((line) => line.trim());
    if (!first) return undefined;
    const value = JSON.parse(first).cwd;
    return typeof value === "string" && path.isAbsolute(value) && fs.statSync(value).isDirectory() ? value : undefined;
  } catch {
    return undefined;
  }
}

function inferProfile(sessionId: string): string {
  if (sessionId.startsWith("personal-")) return "personal";
  if (sessionId.startsWith("sec-") || sessionId.startsWith("secretary-")) return "secretary";
  return "root";
}
