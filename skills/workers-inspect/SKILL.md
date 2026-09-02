---
name: workers-inspect
description: Inspect and monitor the worker subspaces owned by a Herdr project Space, including real activity, Git changes, tests, blockers, and observable model/session facts. Use for worker status, progress, readiness, or an all-project worker overview. Do not use to merge or modify worker branches.
---

# Workers Inspect

Build a current, evidence-backed view of workers without relying on a manually maintained worker ledger. Herdr's native project Space and worker-subspace topology determines ownership; terminal output, processes, Git, tests, and worker reports determine what is actually happening.

## Scope

Default to the current project-owner Space. The API calls this the source workspace:

1. Require `HERDR_ENV=1` and follow the repository's Herdr preflight instructions.
2. Resolve the calling pane and workspace explicitly. Do not use the UI-focused pane.
3. Run `herdr worktree list --workspace "$HERDR_WORKSPACE_ID"`. Treat `.result.source.source_workspace_id` as the parent Space and require it to equal `HERDR_WORKSPACE_ID` before coordinating workers.
4. Select returned linked worktrees that have an `open_workspace_id`, then join them to `herdr workspace list` and `herdr agent list`. These open linked-worktree subspaces are the local worker candidates.
5. Validate every candidate by workspace, worktree, repository identity, and live agent. Do not infer ownership from a branch prefix, path convention, sidebar order, or cohort label alone.

If that equality check fails, the caller is a linked worker subspace rather than the source workspace. Do not coordinate its siblings. Report the source workspace and direct the request to its project owner.

Selectors such as `all`, `ready`, `working`, `blocked`, a worker name, branch, workspace ID, or pane ID apply only inside that parent Space unless the user explicitly requests all Spaces.

## Inspect substantive evidence

For each selected worker, gather enough evidence to explain its state:

- `herdr agent get` and `herdr agent read --source recent-unwrapped` for lifecycle, recent actions, reports, questions, and test output.
- `herdr pane process-info --pane <id>` for the current foreground process.
- `git -C <worktree> status --short`, branch, exact `HEAD`, recent commits, changed files, and a diff against the project owner's integration branch.
- Tests observed in terminal output, commit/report text, or rerun read-only when proportionate. Distinguish observed passing tests from tests merely claimed or not run.
- The worker's stated objective and blockers. When the evidence is stale or incomplete and the worker is settled, prompt it for a concise status report. Do not interrupt active work merely to refresh a dashboard.

Never claim access to hidden reasoning. Report model information only when it is declared by launch instructions or observable in Herdr/session output, and label it `declared`, `observed`, or `unknown`. Agent kind alone is not an exact model identity.

## All-Spaces mode

When the user asks for every project or all existing workers, preserve the hierarchy:

1. Enumerate source/root project Spaces with `herdr workspace list` and native worktree topology.
2. Identify the project-owner agent in each source Space using exact pane/workspace IDs and coordinator state when available. If multiple agents make ownership ambiguous, surface the ambiguity instead of guessing.
3. Inspect the current owner's children locally. Prompt each other project owner once to run `$workers-inspect` for its own child subspaces and return the standard report.
4. Use bounded waits of at most 60 seconds with progress updates and a default aggregate deadline of five minutes unless the user requests continued monitoring. Return available reports and mark nonresponding owners `pending`; one unavailable owner must not block the overview.
5. Aggregate the owner reports. Do not bypass owners by prompting every child globally.

Keep provenance explicit in forwarded material: `User:` for the user's words, `Codex:` for coordinator requests, `Tool:` for machine evidence, and `Worker:` for worker or owner reports.

## Report

Lead with a compact board grouped into `ready for review`, `needs revision or a decision`, `working`, and `inactive/unknown`. For every worker include:

- Task and a plain-language account of current or completed work
- Agent/pane/session, workspace, branch, and worktree
- Lifecycle state and observed foreground activity
- Exact head SHA and clean/dirty/ahead state
- Semantic change summary and changed files
- Tests run and their results
- Blockers, questions, and recommended next action
- Declared/observed model facts, without speculation

Classify a worker as ready only when it is settled, its intended work is committed, its worktree is clean, its report has no unresolved blocker, and the reported test evidence is adequate for review. Keep weaker states visibly provisional.

Add a batch-level overlap section for workers touching the same files, symbols, migrations, interfaces, or tests. State when the snapshot was taken and make uncertainty visible.
