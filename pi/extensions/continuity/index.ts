import { createHash } from "node:crypto";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { latestActivePacket } from "../workflow-state/core.mjs";

const ENTRY = "conversation-continuity.v1";
const MAX_NOTE = 512;
const MAX_ITEMS = 24;

type Retained = {
  goal: string;
  currentSlice: string;
  decisions: string[];
  completed: string[];
  openQuestions: string[];
  risks: string[];
  firstKeptEntryId: string;
};

type Continuity = {
  schemaVersion: 1;
  sessionId: string;
  compactionEntryId: string;
  reason: "manual" | "threshold" | "overflow";
  createdAt: string;
  retained: Retained;
  summaryDigest: string;
  detailsAvailable: boolean;
};

function bounded(value: unknown, fallback = "unavailable"): string {
  return typeof value === "string" && value.trim() ? value.slice(0, MAX_NOTE) : fallback;
}

function list(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string").slice(0, MAX_ITEMS).map((item) => item.slice(0, MAX_NOTE)) : [];
}

function cards(ctx: ExtensionContext): Continuity[] {
  const result: Continuity[] = [];
  for (const entry of ctx.sessionManager.getBranch()) {
    if (entry.type !== "custom" || entry.customType !== ENTRY || !entry.data || typeof entry.data !== "object") continue;
    const value = entry.data as Partial<Continuity>;
    if (value.schemaVersion !== 1 || typeof value.sessionId !== "string" || typeof value.compactionEntryId !== "string" || !["manual", "threshold", "overflow"].includes(value.reason ?? "") || typeof value.createdAt !== "string" || typeof value.summaryDigest !== "string" || typeof value.detailsAvailable !== "boolean" || !value.retained || typeof value.retained !== "object") continue;
    const retained = value.retained as Partial<Retained>;
    if (typeof retained.goal !== "string" || typeof retained.currentSlice !== "string" || typeof retained.firstKeptEntryId !== "string") continue;
    result.push({
      schemaVersion: 1,
      sessionId: value.sessionId.slice(0, MAX_NOTE),
      compactionEntryId: value.compactionEntryId.slice(0, MAX_NOTE),
      reason: value.reason as Continuity["reason"],
      createdAt: value.createdAt.slice(0, MAX_NOTE),
      retained: {
        goal: retained.goal.slice(0, MAX_NOTE),
        currentSlice: retained.currentSlice.slice(0, MAX_NOTE),
        decisions: list(retained.decisions),
        completed: list(retained.completed),
        openQuestions: list(retained.openQuestions),
        risks: list(retained.risks),
        firstKeptEntryId: retained.firstKeptEntryId.slice(0, MAX_NOTE),
      },
      summaryDigest: value.summaryDigest.slice(0, MAX_NOTE),
      detailsAvailable: value.detailsAvailable,
    });
  }
  return result;
}

function latest(ctx: ExtensionContext, sessionId?: string): Continuity | undefined {
  return cards(ctx).reverse().find((value) => !sessionId || value.sessionId === sessionId);
}

function card(value: Continuity | undefined): string {
  if (!value) return "No continuity checkpoint recorded.";
  const open = value.retained.openQuestions.length;
  return `Conversation compacted (${value.reason}) · ${open} open decisions retained · ${value.detailsAvailable ? "run /continuity to inspect" : "continuity details unavailable"}`;
}

function makeCard(event: any, ctx: ExtensionContext): Continuity {
  const entry = event.compactionEntry && typeof event.compactionEntry === "object" ? event.compactionEntry : {};
  const sessionId = bounded(ctx.sessionManager.getSessionId(), "unknown");
  const fallbackEntryMaterial = JSON.stringify({ reason: event.reason, firstKeptEntryId: entry.firstKeptEntryId, summary: entry.summary });
  const fallbackEntryId = `compaction:${createHash("sha256").update(fallbackEntryMaterial).digest("hex").slice(0, 32)}`;
  const compactionEntryId = bounded(entry.id, fallbackEntryId);
  const packet = latestActivePacket(ctx.sessionManager.getBranch()) as Record<string, any> | null;
  const program = packet && packet.program && typeof packet.program === "object" ? packet.program : {};
  const retained: Retained = {
    goal: bounded(packet?.goal ?? program.desired_end_state),
    currentSlice: bounded(packet?.current_slice ?? program.current_slice),
    decisions: list(packet?.decisions),
    completed: list(program.completed_slices ?? packet?.completed),
    openQuestions: list(packet?.open_decisions),
    risks: list(packet?.remaining_uncertainty ?? packet?.risks),
    firstKeptEntryId: bounded(entry.firstKeptEntryId),
  };
  const rawSummary = typeof entry.summary === "string" ? entry.summary.slice(0, 128 * 1024) : "";
  const summary = rawSummary.slice(0, MAX_NOTE);
  const material = JSON.stringify({ schemaVersion: 1, sessionId, compactionEntryId, reason: event.reason, retained, summary: rawSummary });
  const summaryDigest = `sha256:${createHash("sha256").update(material).digest("hex")}`;
  return {
    schemaVersion: 1,
    sessionId,
    compactionEntryId,
    reason: event.reason === "threshold" || event.reason === "overflow" ? event.reason : "manual",
    createdAt: new Date().toISOString(),
    retained,
    summaryDigest,
    detailsAvailable: Boolean(summary && packet),
  };
}

export default function continuityExtension(pi: ExtensionAPI): void {
  pi.registerCommand("continuity", {
    description: "Inspect the latest durable conversation continuity checkpoint",
    handler: async (args, ctx) => {
      const value = latest(ctx, ctx.sessionManager.getSessionId() ?? undefined);
      const details = value ? [
        `Goal: ${value.retained.goal}`,
        `Current slice: ${value.retained.currentSlice}`,
        `Decisions retained: ${value.retained.decisions.join(" · ") || "none"}`,
        `Completed: ${value.retained.completed.join(" · ") || "none"}`,
        `Open: ${value.retained.openQuestions.join(" · ") || "none"}`,
        `Risks: ${value.retained.risks.join(" · ") || "none"}`,
        `Summary: ${value.summaryDigest}`,
      ].join("\n") : "";
      ctx.ui.notify(`${card(value)}${details ? `\n${details}` : ""}${args.trim() ? `\n${args.trim().slice(0, MAX_NOTE)}` : ""}`, "info");
    },
  });

  pi.on("session_compact", (event, ctx) => {
    const value = makeCard(event, ctx);
    const duplicate = cards(ctx).some((existing) => existing.sessionId === value.sessionId && existing.compactionEntryId === value.compactionEntryId);
    if (!duplicate) {
      try {
        pi.appendEntry(ENTRY, value);
      } catch (error) {
        ctx.ui.notify(`Continuity checkpoint could not be persisted: ${error instanceof Error ? error.message.slice(0, MAX_NOTE) : "unknown error"}`, "warning");
      }
    }
    ctx.ui.notify(card(value), "info");
  });

  pi.on("session_start", (_event, ctx) => {
    // Re-scan persisted entries after resume/branch navigation. Healthy state
    // stays silent; malformed or newer cards are simply unavailable.
    latest(ctx, ctx.sessionManager.getSessionId() ?? undefined);
  });
}
