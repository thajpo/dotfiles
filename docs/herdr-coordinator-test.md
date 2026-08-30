# Minimal Herdr coordinator test

This test uses the existing `JLighter/herdr-spawn` Herdr plugin. The
repository contributes only a stable `bin/spawn` adapter, a tiny compatibility
shim for concurrent same-kind agents, and primary-agent instructions; it does
not implement an orchestration service.

The operating model is one headful primary/project-owner conversation plus
headful worker conversations. The primary delegates an initial task packet,
then continues talking with each worker while reviewing its worktree. The
primary owns integration, conflict resolution, tests, and design decisions.

## One-time setup

Install and configure the existing plugin:

```bash
export HERDR_ENV=1
herdr plugin install JLighter/herdr-spawn --yes
herdr plugin config-dir herdr-spawn
```

In the printed config file, set:

```bash
kind=codex
focus=false
```

Make sure Herdr's Codex integration is installed:

```bash
HERDR_ENV=1 herdr integration install codex
```

The global Codex profile is configured for the lower-risk automation mode:
`workspace-write` sandboxing, `on-request` approvals, and automatic review of
eligible approval requests. `/home/j/.herdr` is an additional writable root so
Herdr can create and retire worker worktrees. The local spawn shim repeats the
worker sandbox flags as defense in depth.

Start or attach to Herdr, then run Codex in the main project worktree. The
primary session must be able to reach the same Herdr socket as the spawned
workers.

## Spawn a worker

From the main project worktree:

```bash
export HERDR_ENV=1
~/dotfiles/bin/spawn --doctor
base_ref=$(git branch --show-current)
~/dotfiles/bin/spawn --base "$base_ref" -k codex -b test/herdr-worker \
  "Make one focused change. Run the relevant test, commit the work, and report the commit hash, tests, blockers, and changed files."
```

The launcher resolves symlinks before locating its shim, requires
`HERDR_ENV=1`, and runs a preflight that reports the resolved base, branch,
pane, worktree, and any existing agents in the same Git repository. The
`--base` option is an adapter option; it is removed before calling the
installed plugin and applied through a temporary plugin-config overlay.

The command should create a separate worktree and Herdr workspace, start
Codex, and submit the prompt. If Codex presents a repository trust prompt,
approve it once for the project repository.

The worker remains an interactive Codex session after the initial prompt. Use
the worker pane for follow-up questions, revised acceptance criteria, review
comments, or a request to explain the diff. `~/dotfiles/bin/spawn` does not use
`codex exec`; it starts the headful Codex integration in the Herdr pane.

`~/dotfiles/bin/spawn` preserves the upstream plugin but gives each Herdr agent
the requested branch/worker slug as its positional name while keeping
`--kind codex` separate. If that name is already taken, it retries with a
pane-qualified fallback.

## Inspect and communicate

Use the worker pane ID printed by `spawn`, or discover it with:

```bash
HERDR_ENV=1 herdr agent list
HERDR_ENV=1 herdr agent read <pane-or-agent> --source recent-unwrapped --lines 120
HERDR_ENV=1 herdr agent prompt <pane-or-agent> \
  "Report current status, changed files, tests, commit, and blockers."
git worktree list
git -C <worker-worktree> status --short
git -C <worker-worktree> diff main...HEAD
```

## Review and land

Review the worker diff and tests. When ready, merge the branch from the main
worktree:

```bash
git diff main...<worker-branch>
git merge --no-ff --no-commit <worker-branch>
# inspect the staged result and run tests
git commit -m "Merge <worker-branch>"
```

If Git reports conflicts, the primary may resolve them when the intended
result is unambiguous. Ask the user when the conflict requires a design
decision. Do not use `spawn done` until the branch is integrated or explicitly
abandoned.

## What reaches each session

- Codex reads the global `~/.codex/AGENTS.md`, which in this setup is a
  symlink to `agent/AGENTS.md`. Both primary and workers see this file; its
  explicit role dispatch tells the primary to coordinate and workers to stay
  within their assignment.
- Herdr supplies the pane working directory and session context, including the
  socket environment used by Herdr commands. It does not copy the primary
  conversation into a worker.
- Codex supplies the normal shell, file, MCP, and skill tools from its own
  configuration. `bin/spawn` is the shell entry point that calls the installed
  `herdr-spawn` plugin, while Git supplies branches, commits, diffs, and
  conflict detection.

If a Herdr service restart is ever necessary, Codex transcripts remain
resumable with `codex resume <session-id>`, but Herdr does not automatically
recreate every worker conversation. Record the worker session IDs before a
planned restart and resume each one in its worktree. A resumed Codex process
loads the current `AGENTS.md` and configuration when it starts.
