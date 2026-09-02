---
name: workers-summarize
description: Summarize the current worker's assignment, completed work, divergences, evidence, and every substantive instruction or question as a chronological decision ledger. Use when the user or project owner asks a worker for a handoff, retrospective, decision audit, or complete account of what was requested and resolved. Do not coordinate siblings, merge, or modify files.
---

# Workers Summarize

Produce a worker-local, evidence-backed handoff. Answer four questions clearly: what was the task, what happened, what diverged, and for each substantive thing the user said, did we make a decision?

This skill runs inside the worker being summarized. Do not inspect or coordinate sibling workers, and do not change code merely to make the summary cleaner.

## Recover the complete input history

Use the conversation in context, including the initial task packet and later prompts. When running as Codex, also run the bundled `scripts/extract_user_messages.py` by its path relative to this file. It reads the current local rollout selected by `CODEX_THREAD_ID` or `CODEX_SESSION_ID` and returns only events classified as `user.text`; this recovers earlier inputs after context compaction while excluding injected AGENTS/environment messages, subagent notifications, and turn-abort notices. Treat its `M1`, `M2`, and later source references as stable only within this report.

Require `complete: true` before claiming full coverage. If extraction exits nonzero or reports any `partial_reasons`, retry once in case the active rollout was between writes. If it remains partial, continue only with an explicit completeness warning and name every affected category. Do not use `--allow-partial` to suppress that warning.

Read every output page. Save the first page's `snapshot_id` and pass it as `--snapshot HASH` on every later page and text continuation; restart if `snapshot_match` is false. Require valid JSON ending with `output_end: workers-summarize-extract-v1`, follow `page.next_start` until `page.has_more_messages` is false, and follow each message's `next_text_offset` with `--start N --limit 1 --text-offset OFFSET` until `text_has_more` is false. If output is truncated, reduce `--limit`; never equate a complete transcript scan with complete output retrieval.

Treat transport role and authorship separately:

- Text explicitly prefixed `User:` is user-authored intent.
- Text explicitly prefixed `Codex:` is coordinator-authored direction or context.
- An unlabeled task packet or prompt received by the worker is `Coordinator/unattributed`; do not claim the user personally said it.
- `Tool:` output and `Worker:` reports are evidence, not user decisions.

If the local rollout is unavailable, continue from visible context but disclose the missing interval and lower the completeness claim. Never reconstruct missing user statements from memory or implementation choices. Prefixes inside quotations or code examples are content, not provenance labels; mixed-source packets require manual segmentation.

The extractor is a local recovery aid, not report content. Do not paste its raw JSON, local paths, internal IDs, or timestamps into the handoff. Redact credentials, secrets, personal data, and unrelated private content while preserving the operative meaning; record the source reference and state that a redaction occurred.

## Verify the work

Inspect the worker's own durable state:

- Initial objective, boundaries, acceptance checks, and later scope changes
- Current branch, exact assigned-base SHA, exact `HEAD`, merge base, and commits
- Committed branch diff, staged diff, unstaged diff, and an inspected inventory of untracked files
- Changed files and user-visible or architectural behavior
- Tests/checks actually run and their outcomes, distinguishing evidence verified now from historical claims that can no longer be verified
- Open blockers, questions, review findings, and uncommitted work

Keep stated claims separate from verified Git or tool evidence. Do not expose hidden chain-of-thought; summarize only externally relevant decisions, tradeoffs, actions, and rationale.

## Build the input and decision ledger

Walk every recovered input chronologically. Include every substantive objective, constraint, preference, correction, question, approval, rejection, and change of direction. Omit only clearly non-task chatter or transport-only acknowledgments. Reconcile every recovered source reference: map it to one or more ledger rows, or list it in an **Excluded inputs** table with a close paraphrase and the exact exclusion reason. State the recovered, covered, and excluded message counts; they must reconcile.

Use one row per atomic item; split a message containing multiple requests. Include:

| Field | Meaning |
|---|---|
| ID | Stable atomic-item label such as `M3.1`; one source message may produce several rows |
| Source ref | Recovered message reference such as `M3` |
| Source | `User`, `Codex`, or `Coordinator/unattributed` |
| What was said | Close paraphrase or short quotation preserving the operative meaning |
| Decision needed? | `Yes`, `No`, or `Unclear` |
| Decision status | `Decided`, `Open`, `Superseded`, `Deferred`, `Not answered`, or `Not applicable` |
| Decided by / authority | The explicit decider and, for a delegated choice, the source reference granting that authority |
| Controlling answer | The final explicit decision, or `None` |
| Evidence | The later message/action that resolved or superseded it |
| Implementation effect | What changed—or did not change—because of it |

Do not infer that implementing one option means the user decided it. A user decision is `Decided` only when the user explicitly chose it, explicitly delegated the choice, or an instruction from someone operating within that explicit delegation unambiguously resolved it. A coordinator choice without documented user delegation may settle worker execution but remains open as a user/product decision; say both. If later user guidance conflicts, preserve both entries, mark the earlier one `Superseded`, and identify the current controlling instruction.

After the full ledger, list only the still-open decisions in a short checklist. This is the actionable decision backlog.

## Explain execution and divergence

Report:

1. **Assignment now** — original task, current controlling scope, and acceptance criteria.
2. **What I did** — behavior delivered, commits, important files, tests, and current worktree state.
3. **What diverged** — every material difference from the initial packet or later controlling instructions, including added scope, omitted scope, alternate implementation, test deviations, and worker-chosen assumptions. For each, say why, whether it was authorized, and its impact.
4. **Input and decision ledger** — the chronological table above.
5. **Coverage reconciliation** — recovered, covered, and excluded counts plus the excluded-input table.
6. **Open decisions** — unresolved items requiring the user or owner.
7. **Handoff** — exact branch/base/HEAD SHAs, readiness, risks, and the next recommended action.

Use `None` when a section genuinely has no items. Do not hide mistakes, incomplete work, failed tests, or unauthorized divergence. The report is read-only: do not edit, commit, merge, close the workspace, or delete artifacts.
