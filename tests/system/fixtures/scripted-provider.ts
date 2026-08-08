/** Deterministic local provider transcript for installed-process tests. */
export type ProviderTurn = { prompt: string; tool: string; arguments: Record<string, unknown> };

export const SCRIPTED_TURNS: readonly ProviderTurn[] = [
  { prompt: "inspect the assigned project", tool: "read", arguments: { path: "README" } },
];

export function transcript(prompt: string): ProviderTurn {
  return SCRIPTED_TURNS.find((turn) => turn.prompt === prompt) ?? SCRIPTED_TURNS[0];
}

if (process.argv[1]?.endsWith("scripted-provider.ts")) {
  process.stdout.write(`${JSON.stringify(transcript(process.argv[2] ?? "inspect the assigned project"))}\n`);
}
