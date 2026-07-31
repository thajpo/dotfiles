import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const OPENAI_PROVIDERS = new Set(["openai", "openai-codex"]);

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
  let enabled = false;

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
      enabled = next;
      ctx.ui.notify(status(enabled), "info");
    },
  });

  pi.on("before_provider_request", (event, ctx) => {
    if (!enabled || !isOpenAIModel(ctx)) return;
    if (!event.payload || typeof event.payload !== "object" || Array.isArray(event.payload)) return;
    const payload = event.payload as ProviderPayload;
    return { ...payload, service_tier: "priority" };
  });
}
