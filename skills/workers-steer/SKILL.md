---
name: workers-steer
description: Send scoped directions or status questions to one or a batch of workers owned by a Herdr project Space, then collect attributable responses. Use when the user wants to redirect, clarify, unblock, or query workers. Do not use for merging or direct edits in worker worktrees.
---

# Workers Steer

Communicate through the owning project Space while preserving target scope and message provenance.

## Resolve targets

Require `HERDR_ENV=1` and follow the repository's Herdr preflight. Resolve the source workspace through `herdr worktree list --workspace "$HERDR_WORKSPACE_ID"` and require `.result.source.source_workspace_id` to equal `HERDR_WORKSPACE_ID`. Select only its open linked-worktree worker subspaces, then resolve exact pane or unique agent targets from `herdr agent list`.

If the workspace equality check fails, do not steer siblings; report the source workspace and stop. Do not expand a worker, branch, status, or owner selector when it is ambiguous. A request naming `all` means all workers under the current owner, not all Herdr Spaces.

For an explicit all-Spaces request, send one prompt to each unambiguous project-owner agent. Each owner resolves and communicates with its own children, then returns an owner-level report. Do not broadcast directly to every worker across projects.

## Construct and send the prompt

Keep forwarded sources distinct:

- Prefix verbatim user-authored direction with `User:`.
- Prefix coordinator-added context, questions, acceptance checks, or report format with `Codex:`.
- Never present another worker's report or tool output as user instruction.

Include only the context needed for the selected worker's assignment. When asking for implementation, remind the worker to modify only its assigned worktree and to finish with a commit plus a concise report of tests, changed files, and blockers.

Use `herdr agent prompt <pane-or-agent> <text>` with exact resolved targets. For a settled worker, `--wait` may collect its response. For a working worker, avoid interruption: normally wait for it to settle, or submit one prompt without `--wait` when the user wants a time-sensitive course correction. Do not send interrupt keys unless the user explicitly requests interruption, and do not mistake completion of the worker's previous turn for acknowledgment of the new prompt. If a worker is blocked, inspect the prompt or approval UI and surface it to the user; never answer a product decision or permission request on the user's behalf.

For a large batch, submit independent prompts promptly, then monitor with waits of at most 60 seconds and regular user updates. Unless the user requests continued monitoring, use a default five-minute aggregate deadline, return available responses, and mark the rest `pending`. A timeout means `still running`, not failure. Inspect `herdr agent get` and `herdr agent read` after a stalled, blocked, or unknown result instead of blindly resending.

## Report responses

Return one row per target with the exact worker identity, delivery state, response summary, resulting lifecycle state, and any question or blocker. Label worker responses `Worker:`. Separate acknowledged direction from completed work, and verify completion later through Git and tests with `$workers-inspect` or `$workers-review`.

This skill sends messages only. It does not edit files, commit, merge, close workspaces, or delete worktrees.
