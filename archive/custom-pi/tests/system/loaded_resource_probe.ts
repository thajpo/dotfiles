/** Persist Pi's own view of loaded tools, model, process, and session. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function loadedResourceProbe(pi: ExtensionAPI): void {
  pi.on("session_start", (_event, ctx) => {
    pi.appendEntry("installed-process-probe", {
      schemaVersion: 1,
      process: {
        pid: process.pid,
        parentPid: process.ppid,
        argv: process.argv,
        environment: Object.fromEntries(Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined)),
      },
      cwd: ctx.cwd,
      session: {
        id: ctx.sessionManager.getSessionId(),
        file: ctx.sessionManager.getSessionFile(),
      },
      model: ctx.model ? {
        provider: ctx.model.provider,
        id: ctx.model.id,
        api: ctx.model.api,
      } : null,
      activeTools: pi.getActiveTools(),
      tools: pi.getAllTools().map((tool) => ({
        name: tool.name,
        description: tool.description,
        parameters: tool.parameters,
        sourceInfo: tool.sourceInfo,
      })),
    });
  });
}
