# Multi-agent project coordination

This file is intentionally shared by the primary and every worker because it
is loaded globally through `~/.codex/AGENTS.md`. Determine the role from the
launch context before acting:

- **Primary/project owner:** this is the user's main conversation in the main
  worktree. Follow the Primary workflow below.
- **Worker:** this session was launched by `bin/spawn` in an assigned worktree
  with an initial task packet. Follow the Worker contract below. Do not spawn
  other workers, merge branches, or edit the primary worktree.

The initial task packet and the current working directory are authoritative for
the worker's assignment. The primary workflow below is not a worker mandate.

## Primary workflow

- Treat this session as the project owner and coordinator. Keep the user-facing
  conversation here; workers are implementation partners, not autonomous
  merge owners.
- Keep the primary conversation in the main project worktree.
- Before using Herdr, confirm `HERDR_ENV=1`. Use the same Herdr session as the
  primary and prefer explicit IDs returned by Herdr JSON responses.
- The canonical launcher is `~/dotfiles/bin/spawn`; `~/.local/bin/spawn` is
  only its convenience symlink. Before spawning, run this exact preflight:

  ```bash
  export HERDR_ENV=1
  test "$HERDR_ENV" = 1
  ~/dotfiles/bin/spawn --doctor
  git rev-parse --show-toplevel
  git branch --show-current
  HERDR_ENV=1 herdr agent list
  ```

- Spawn one worker per independent task with
  `HERDR_ENV=1 ~/dotfiles/bin/spawn --base <ref> -k codex -b <worker-slug>
  "<task packet>"`. The launcher prints the resolved base, branch, pane, and
  worktree and checks for agents already attached to the same Git repository.
  Do not use `--here` for mutating work.
- Give each worker an initial task packet containing the objective, boundaries,
  relevant context, acceptance checks, and the commit/report contract. The
  worker is headful and remains available for follow-up; do not treat the
  initial packet as the end of the conversation or close the worker while its
  task is active.
- Use `herdr agent list`, `herdr agent read <pane-or-agent>`, and
  `herdr agent prompt <pane-or-agent> "..."` to inspect and communicate with
  workers.
- Use `git worktree list`, `git -C <worktree> status`, `git -C <worktree>
  diff`, and `git -C <worktree> log` as the durable project state.
- After a successful spawn, verify the durable identities with
  `HERDR_ENV=1 herdr agent list`, `git worktree list`, and
  `git -C <worker-worktree> status --short`; record the returned agent/pane,
  branch, worktree, base, tests, changed files, and blockers in the owner
  report.
- Review and test worker branches before merging them one at a time. Resolve
  mechanical conflicts when the intended result is clear; stop and ask the
  user when a conflict requires a product decision.
- Never silently discard a worker branch or uncommitted changes. Cleanup is a
  separate, explicit action after integration or abandonment.

When reporting project state to the user, summarize every active worker by
task, pane/agent, branch, worktree path, lifecycle state, latest commit,
tests, changed files, and blockers. Ask the user for direction when the
available worker output or a merge conflict leaves the intended design
ambiguous.

## Worker contract

Workers own only their assigned worktree. They should not merge other worker
branches or edit the primary worktree. Their durable handoff is a commit plus
a concise final report; live Herdr output is for status and questions, not the
only record of completed work.
- Workers should remain interactive after the initial packet. They must answer
  follow-up questions from the primary, explain their diff and tradeoffs, and
  revise their branch when the primary changes direction.
