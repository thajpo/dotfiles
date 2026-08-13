/** Credential-free deterministic provider that runs one tool call then stops. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai";

export default function scriptedStopProvider(pi: ExtensionAPI): void {
  const faux = fauxProvider({
    api: "scripted",
    provider: "scripted",
    models: [{ id: "scripted-1", name: "Installed Process Script" }],
    tokenSize: { min: 4096, max: 4096 },
  });
  faux.setResponses([
    () => fauxAssistantMessage(fauxToolCall("ls", { path: "." }, { id: "scripted-stop-ls" }), { stopReason: "toolUse" }),
    () => fauxAssistantMessage("SCRIPTED_STOP_FINAL", { stopReason: "stop" }),
  ]);
  pi.registerProvider(faux.provider);
}
