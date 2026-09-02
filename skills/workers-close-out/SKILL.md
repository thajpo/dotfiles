---
name: workers-close-out
description: Integrate an approved batch of reviewed Herdr worker commits into one project-owner branch and, after separate explicit authorization, retire successful worker subspaces. Use only when the user explicitly asks to merge, integrate, or close out workers; ordinary inspection or review is not authorization.
---

# Workers Close Out

Close out a batch owned by one project Space. Bind authorization to reviewed commit SHAs, integrate serially, and leave durable evidence. Do not turn a global worker overview into a cross-project merge operation.

## Authorization and scope

Require `HERDR_ENV=1` and follow the repository's Herdr preflight. Run `~/dotfiles/bin/herdr-workers-state --workspace "$HERDR_WORKSPACE_ID"`, require `schema_version: 1` and `owner.is_project_owner: true`, and operate only on its returned workers. If it exits `3`, report the source workspace and stop. Any other incomplete identity snapshot also fails closed. This skill must run from that source/project-owner Space and its primary integration worktree, never from a worker subspace.

Operate on only one parent Space per close-out batch. If the user asks to close workers across multiple Spaces, have each project owner prepare a separate review/approval manifest and obtain explicit direction for each integration target.

A status or review request never authorizes mutation. Use the current-conversation approval manifest from `$workers-review` when it still names:

- The integration branch and its reviewed head
- The ordered worker identities
- Every worker's full reviewed commit SHA
- The merge strategy and intended workspace/cleanup effects

The presence of a manifest is not approval. Require an explicit user message sent after the manifest was shown that approves that exact target, ordered SHA set, strategy, and effects. If there is no approved current manifest, produce one and ask for one batch approval. Do not ask again when the user has already approved that exact manifest. A request made before the SHAs were shown does not authorize unknown future heads.

Integration authorization does not authorize closing workspaces, deleting branches, or removing worktrees. Cleanup is a separate action requested explicitly after successful integration.

## Readiness gate

Before the first mutation, verify that:

- The integration worktree is the intended branch and is clean. Preserve unrelated user changes; do not stash them automatically.
- Every selected worker belongs to this source Space, is settled with a final report, has a clean worktree, has committed work, and has no unresolved blocker or review finding that prevents integration.
- Worker branch heads still equal their approved full SHAs.
- The target head still equals the approved head.
- Required worker reports and test evidence are available.
- Cross-worker overlap and dependency order were reviewed.

Perform the worker-head checks in one helper invocation using every approved `--expect-sha WORKSPACE=SHA` pair. Exit `4`, a missing workspace, or any `expectation_match: false` invalidates the manifest. Repeat the same exact expectation check immediately before promotion.

If a head moved, re-review that worker and obtain approval for the new SHA. Never substitute the branch's new head silently. A worker ID or branch name locates work; it is not the authorized merge object.

## Validate serially, promote atomically

Follow the repository's established merge convention. If none exists, prefer merge commits so worker boundaries remain visible and revertible. Apply exact approved commit SHAs, not mutable branch names.

Keep the real target untouched while validating the batch. Create a uniquely named temporary integration branch and worktree from the approved target head. Record these as skill-created temporary artifacts so they cannot be confused with user or worker worktrees.

For each worker in the approved order:

1. Recheck its branch head and the temporary integration state.
2. Determine already-integrated state according to the approved strategy: ancestry for merge-based integration, or recorded resulting commits and patch/tree equivalence for squash or cherry-pick. If proof is inconclusive, stop rather than risk duplicate application.
3. Otherwise inspect the effective diff against the temporary branch; earlier integrations can change or eliminate later changes. Apply the worker using the approved strategy.
4. Resolve only mechanical conflicts whose intended result is clear. Abort and ask the user when a conflict exposes a product or design decision.
5. Run the relevant tests before finalizing that integration. Recheck the index and worktree afterward so generated test artifacts or unrelated edits cannot enter an integration commit.
6. Recompute readiness for the next worker rather than assuming the original batch diff is unchanged.

Run the aggregate suite on the completed temporary integration branch. On a conflict, test failure, SHA drift, or unexpected state, abort any in-progress operation and leave the real target unchanged. Preserve enough diagnostics to report the failure, then remove only skill-created temporary artifacts and proven test artifacts. Do not reset, discard unrelated changes, silently skip commits, or improvise a different history strategy.

After the temporary branch passes, recheck every worker head, the clean real target worktree, and that the real target branch still equals its approved head. Promote the already-tested temporary integration tip with a single fast-forward-only update. If it cannot fast-forward exactly, stop without changing the target and require a new review manifest. Record the strategy-specific mapping from every worker SHA to its resulting target commit or equivalence proof. After verifying that the target points to the tested tip, remove the skill-created temporary worktree and branch; if that cleanup fails, report their exact retained paths/names. This temporary-artifact cleanup is distinct from worker cleanup.

## Retire and clean up

After successful promotion, report the integration result and stop. Keep every worker available until the user gives a separate, explicit cleanup instruction after seeing that result.

On that later cleanup request, first prove each worktree is clean and each worker is integrated using the recorded strategy-specific mapping: ancestry for merges, or resulting commits plus patch/tree equivalence for squash and cherry-pick. Show the exact workspace, branch, and worktree deletion set, then use the repository's canonical cleanup mechanism. If proof is missing or ambiguous, retain the artifacts. Never force-remove an unmerged or dirty worker.

## Final report

Report every requested worker as `integrated`, `not integrated`, or `skipped`, including:

- Approved and actually merged SHA
- Resulting target commit
- Tests and outcomes
- Conflict, drift, or blocker details
- Workspace close state
- Whether branch/worktree artifacts were retained or explicitly removed

Keep `User:`, `Codex:`, `Tool:`, and `Worker:` provenance distinct. Worker reports are evidence, not authorization.
