import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const CONTINUATION_MESSAGE =
  "Context compaction has completed. Continue the current task from the compacted conversation summary and preserved state; do not wait for another user message. If the task is unfinished, take the next concrete step. If it is complete, briefly report completion.";

/**
 * Pi intentionally leaves threshold compaction idle so a user can decide what
 * to do next. Managed coding sessions are different: compaction is an
 * implementation detail of the same task, so stopping here loses the task's
 * autonomous momentum. Queue a hidden follow-up after compaction has fully
 * unwound; overflow recovery that already retries must not be duplicated.
 */
export default function autoContinueAfterCompaction(pi: ExtensionAPI): void {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let shuttingDown = false;

  const cancel = () => {
    shuttingDown = true;
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
  };

  const schedule = (reason: string) => {
    if (shuttingDown) return;
    if (timer !== undefined) clearTimeout(timer);
    // Let the compaction event return first. This avoids starting a new agent
    // run while manual compaction is still reconnecting its agent listeners.
    timer = setTimeout(() => {
      timer = undefined;
      if (shuttingDown) return;
      try {
        pi.sendMessage(
          {
            customType: "pi-auto-continue",
            content: CONTINUATION_MESSAGE,
            display: false,
            details: { reason },
          },
          { deliverAs: "followUp", triggerTurn: true },
        );
      } catch (error) {
        // Session replacement or shutdown may invalidate this extension between
        // compaction and the deferred callback. It must not break Pi teardown.
        console.error("auto-continue: unable to resume after compaction:", error);
      }
    }, 0);
  };

  pi.on("session_compact", (event) => {
    if (event.willRetry) return;
    schedule(event.reason);
  });

  // If a process was replaced immediately after saving compaction, there is no
  // live session_compact event left to handle. Resume from that durable marker.
  pi.on("session_start", (_event, ctx) => {
    try {
      const entries = ctx.sessionManager.getBranch();
      if (entries.at(-1)?.type === "compaction") schedule("resume");
    } catch {
      // A session may be ephemeral or still loading; the live event remains
      // authoritative when available.
    }
  });

  pi.on("session_shutdown", cancel);
}
