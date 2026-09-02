---
name: workers-review
description: Review one or a batch of Herdr worker subspaces owned by a project Space and produce a human-friendly merge-readiness board tied to exact commit SHAs. Use when the user wants to understand, compare, or review worker changes before integration. This skill is read-only and does not merge.
---

# Workers Review

Review the changes that would actually enter the project, not just worker metadata or branch names. The result should let a human understand a batch quickly and safely authorize exact commits for integration.

## Resolve the batch

Require `HERDR_ENV=1` and follow the repository's Herdr preflight. Resolve the current source workspace with `herdr worktree list --workspace "$HERDR_WORKSPACE_ID"` and require `.result.source.source_workspace_id` to equal `HERDR_WORKSPACE_ID`. Only then select open linked-worktree subspaces owned by that source Space. Join exact workspace, pane, agent, worktree, and branch identities from Herdr and Git.

If the workspace equality check fails, do not coordinate sibling workers; report the source workspace and stop. Run the review from the project-owner/source Space. Accept selectors such as `all`, `ready`, worker names, branches, workspace IDs, or pane IDs; never let an ambiguous selector silently widen the batch.

For an all-Spaces request, prompt each unambiguous project owner to review its own batch, then aggregate the owner reports. Use waits of at most 60 seconds with progress updates and a default five-minute aggregate deadline; return partial results and mark late owners `pending`. Never form one cross-project merge batch.

## Freeze the review target

For every candidate record:

- Worker workspace/pane and worktree path
- Branch name and exact full `HEAD` SHA
- Clean or dirty working-tree state
- Project integration branch and its exact head
- Merge base and commits/diff that would actually merge

A worker with uncommitted changes is not a merge candidate. Mark it incomplete and recommend `$workers-steer` if the user wants the worker asked to finish and commit; do not turn a read-only review into an implementation prompt. A branch name or worker ID is only a locator; the full commit SHA is the reviewed object.

## Understand and verify the change

Read the complete diff and enough surrounding code, callers, tests, configuration, and schema to explain behavior. Incorporate the worker's stated objective and final report, but verify claims against durable artifacts.

For each material change, perform an independent defect-first review when an appropriate read-only reviewer is available. Give that reviewer the exact base and head SHAs. Findings must be concrete, introduced by the change, and actionable; keep speculative risks separate.

Assess:

- What behavior changes and why
- Important implementation choices and invariants
- Test evidence and material test gaps
- Compatibility, migration, security, performance, and operational risk where relevant
- Dependencies, duplicate work, and overlap across workers
- Whether merge order changes the result

Read-only clarification questions may be sent to a settled worker. Do not ask it to revise files within this skill. Label its answer `Worker:` and treat it as evidence, not authorization or ground truth.

## Produce the review board

Group candidates into:

- `ready to merge`
- `needs worker revision`
- `needs user decision`
- `still working or incomplete`

For each candidate give the task, semantic summary, key files/symbols, tests, findings/risks, exact reviewed SHA, and a clear recommendation. Include a cross-worker overlap/dependency map and proposed serial merge order.

Finish with a compact approval manifest containing the target branch/head, ordered worker identities, each full reviewed SHA, the proposed merge strategy, and the intended workspace/cleanup effects. This manifest is the handoff to `$workers-close-out`.

Immediately re-resolve every branch head before reporting. If it no longer equals the reviewed SHA, mark that candidate `stale—review again`; do not silently update the manifest.

Do not merge, commit, close workspaces, delete branches, or remove worktrees.
