# Minimal Herdr coordinator test

This test uses the existing `JLighter/herdr-spawn` Herdr plugin. The
repository contributes only a stable `bin/spawn` adapter, a tiny compatibility
shim for concurrent same-kind agents, and primary-agent instructions; it does
not implement an orchestration service.

The operating model is one headful primary/project-owner conversation plus
headful worker conversations. The primary delegates an initial task packet,
then continues talking with each worker while reviewing its worktree. The
primary owns integration, conflict resolution, tests, and design decisions.

The launcher also reports display-only metadata through Herdr's supported pane
API. It does not rename a Space, move a worktree, or change Git topology.
Coordination roles are attached only to the exact source and child panes whose
native agent sessions are verified during a successful spawn.

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

The tracked sidebar table requires Herdr's `rows_with_display_agent` support.
Stock v0.8.2 supports only global and canonical-agent layouts; using the old
global layout degrades ordinary panes by removing their workspace row. Until
the conditional layout support is available in the binary being tested, keep
this table out of the live config.

For an isolated development build, merge `herdr/coordination-sidebar.toml`
into its separate config, preserving the rest of that config, then validate it
with the explicit development binary and config path. Unset inherited live
session/socket routing first:

```bash
dev_root=/path/to/herdr-development-checkout
dev_herdr="$dev_root/target/debug/herdr"
isolated_config="$dev_root/.local/isolated/config.toml"
env -u HERDR_SOCKET_PATH -u HERDR_CLIENT_SOCKET_PATH -u HERDR_SESSION \
  HERDR_CONFIG_PATH="$isolated_config" "$dev_herdr" config check
```

Only panes with active guarded `display-agent` metadata select the
`state_icon + agent` / `pane` layout. Coordinated panes therefore render as:

```text
● Dotfiles · coordinator
Dreamer

● Dotfiles · worker
capacity-profile-review
```

The status glyph remains Herdr-owned. An ordinary agent receives no metadata,
so it retains Herdr's useful built-in `state_icon + workspace + tab` / `agent`
fallback. Space rows are not configured or reported by this feature, so their
workspace names, branches, and Git status remain unchanged.

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
~/dotfiles/bin/spawn --base "$base_ref" \
  --cohort Dotfiles \
  --task capacity-profile-review \
  -k codex -b capacity-profile-review \
  "Make one focused change. Run the relevant test, commit the work, and report the commit hash, tests, blockers, and changed files."
```

The launcher resolves symlinks before locating its shim, requires
`HERDR_ENV=1`, and runs a preflight that reports the resolved base, branch,
pane, worktree, and any existing agents in the same Git repository. The
`--base` option is an adapter option; it is removed before calling the
installed plugin and applied through a temporary plugin-config overlay.
`--cohort` and `--task` are also adapter options and are never forwarded to the
plugin. Both accept spaces and punctuation; `--task` defaults to the resolved
worker branch when omitted. Requiring the cohort makes membership explicit
rather than guessing it from a Space, checkout, repository, or path.

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

Only after the plugin has returned success does the launcher re-read both exact
pane IDs and confirm that the source is still the same native agent session.
Because Herdr may report plugin success just before the child's native
`agent_session` reaches the API aggregate, the launcher polls only the returned
child pane for at most five seconds. It never substitutes a Space, workspace,
focused pane, or repository-wide identity. Once ready, it writes
`display-agent` and `title` with `--agent` and `--applies-to-source` guards. The
child is written first; if the coordinator write fails, the launcher attempts
to clear the child display metadata and reports separately whether that cleanup
succeeded. A failed worker start or readiness timeout writes no role metadata.

The coordinator's second line uses the first nonempty identity in this order:
its explicit Herdr agent name, stripped terminal title, canonical agent kind,
then native session UUID. This keeps an unnamed coordinator readable without
turning a terminal, Space, workspace, checkout, or repository label into cohort
membership; only the explicit `--cohort` value supplies that membership.

Herdr 0.8.2 does not offer a native-session-ID guard for presentation fields:
`--applies-to-source` guards the lifecycle authority (for example
`herdr:codex`), not the session UUID. The adapter therefore verifies session
UUIDs immediately before writing and records them in its result, while the
metadata remains pane-owned until replaced, cleared, or the pane closes. If an
operator deliberately replaces Codex with another Codex session in the same
pane, clear or replace the display metadata; do not interpret the old label as
membership of the Space. Custom `$token` rows were rejected because Herdr does
not apply agent/source guards to token patches.

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
