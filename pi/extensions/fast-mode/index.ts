import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const OPENAI_PROVIDERS = new Set(["openai", "openai-codex"]);
const STATE_ENTRY = "fast-mode-state";
const STATE_VERSION = 1;

type ProviderPayload = Record<string, unknown>;

function isOpenAIModel(ctx: ExtensionContext): boolean {
  const model = ctx.model;
  if (!model) return false;
  return OPENAI_PROVIDERS.has(model.provider);
}

function status(enabled: boolean): string {
  return enabled ? "Fast mode: ON (OpenAI priority service tier)" : "Fast mode: OFF";
}

export default function fastMode(pi: ExtensionAPI): void {
  // Priority service is the default for supported OpenAI models. `/fast off`
  // remains available for the occasional cost-sensitive turn.
  let enabled = true;

  const restore = (ctx: ExtensionContext): void => {
    enabled = true;
    for (const entry of ctx.sessionManager.getBranch()) {
      if (entry.type !== "custom" || entry.customType !== STATE_ENTRY) continue;
      const data = entry.data as { schemaVersion?: unknown; enabled?: unknown } | undefined;
      if (data?.schemaVersion === STATE_VERSION && typeof data.enabled === "boolean") enabled = data.enabled;
    }
  };

  pi.registerCommand("fast", {
    description: "Toggle OpenAI priority service tier: /fast [on|off|status]",
    async handler(args, ctx) {
      const requested = args.trim().toLowerCase();
      if (requested !== "" && requested !== "on" && requested !== "off" && requested !== "status") {
        ctx.ui.notify("Usage: /fast [on|off|status]", "error");
        return;
      }
      if (requested === "status") {
        ctx.ui.notify(status(enabled), "info");
        return;
      }

      const next = requested === "on" || (requested === "" && !enabled);
      if (next && !isOpenAIModel(ctx)) {
        ctx.ui.notify("Fast mode is available only for direct OpenAI/OpenAI Codex models.", "warning");
        return;
      }
      if (enabled !== next) {
        enabled = next;
        pi.appendEntry(STATE_ENTRY, { schemaVersion: STATE_VERSION, enabled });
      }
      ctx.ui.notify(status(enabled), "info");
    },
  });

  pi.on("session_start", (_event, ctx) => restore(ctx));
  pi.on("session_tree", (_event, ctx) => restore(ctx));

  pi.on("before_provider_request", (event, ctx) => {
    if (!enabled || !isOpenAIModel(ctx)) return;
    if (!event.payload || typeof event.payload !== "object" || Array.isArray(event.payload)) return;
    const payload = event.payload as ProviderPayload;
    return { ...payload, service_tier: "priority" };
  });
}
