# Pi configuration

This directory contains the reproducible, non-secret Pi harness:

- Pi `0.83.0` and exact npm package pins;
- `@narumitw/pi-goal@0.43.0` for same-session `/goal` continuation;
- `pi-subagents` as the sole coding-agent orchestrator;
- deterministic trusted-live and isolated Docker workspaces;
- `pi-btw` routed through the active task execution plane;
- model/agent definitions, prompts, theme, and UI configuration;
- hash-verified compatibility patches;
- the source policy installed as `~/.config/pi/repository-policy.json`;
- the hardened Linux task image, lock-derived runtime images, and migration/rollback record.

Secrets, auth, sessions, runtime routes, generated host context, and live policy
state remain outside Git.

## Platform contract

The machine profile owns host paths and trust. The macOS profile trusts
`~/projects`; the Linux x86_64 profile trusts `~/Projects`. In both cases the
task still executes in Linux Docker. Trust means that the assigned worktree may
be mounted read/write; it does not permit Pi or a model to execute arbitrary
host commands.

The route records both `executionTarget: linux-container` and the detected
container platform. There is deliberately no implicit macOS fallback. MLX or
other host-only workloads fail closed until a separate, explicitly selected
host backend is implemented.

Python projects with an exact `pyproject.toml` plus `uv.lock` receive a shared,
immutable image keyed by the manifests, base image, uv version, and Linux
platform. The task container owns only its writable upper layer and cache
volume. A project or worktree dependency change creates a new key; it never
mutates an image already used by another task. Workspace/path dependencies and
projects without the lock pair use a task-local environment at
`/opt/pi/task-env`, still inside Linux Docker and never the host `.venv`.

Active `SKILL.md` directories are mounted read-only at their original absolute
paths so the model can load them without exposing the host home directory.
For an explicitly configured control-plane route, the host mounts the pinned
Pi core's `docs/` and `examples/` directories read-only at their original paths.
The host-owned `controlPlaneRepositories` policy list is the authority for
which repositories receive this capability. The route derives those two
directories from the resolved Pi executable, and
the sandbox revalidates canonical directories, ownership, permissions, contained
symlinks, fixed package names, read-only flags, and `rprivate` propagation both
when creating and reusing a container. Ordinary trusted repositories receive
neither resource; host sessions, credentials, `.local/bin`, sockets, and the
Docker socket remain outside the task container.

## Image attachments

`pi-image-tools@1.4.0` is installed as a pinned Pi package. Its clipboard handler
reads native clipboard bytes in the host Pi process and sends `ImageContent`
blocks, so images are persisted in the root session JSONL and do not depend on
host/container `/tmp` sharing. Pi's built-in image-paste binding is disabled in
`keybindings.json`; `Ctrl+V` is owned by the extension. The package's peer range
ends at Pi 0.80, so the acceptance suite includes a smoke test against the
pinned Pi 0.83 APIs; activation uses `--legacy-peer-deps` deliberately and
keeps the package pin explicit.

Automatic or manual compaction does not end an active task. The global
`auto-continue` extension queues a hidden follow-up with `triggerTurn: true`
after the compaction lifecycle unwinds; overflow recovery that Pi already
retries is not duplicated.

## Host command requests

All normal parent and child models may call `host_command` when the sandbox
cannot perform a needed operation. The request must include the exact shell
command, a reason, and a description. Pi shows the user the requester, command,
working directory, reason, description, and a warning that the command runs as
the host user. The command runs only after the user approves that specific
request; rejection is returned to the model as a failed tool result.

Host commands are one-shot approvals. Pi does not persist an allow rule, routes
child requests through a user-owned request directory, binds them to the live
parent session/runtime, expires them, and rejects stale requests after restart
or resume. Output is bounded and marked sensitive. For example:

```text
host_command({
  command: "xclip -selection clipboard -o",
  reason: "Inspect the message the user copied",
  description: "Read the host text clipboard so I can help with the message."
})
```

`pi-host` remains a separate explicit maintenance mode and does not use this
request mechanism.

## Workspace modes

`pi` reads the host-owned policy before Pi starts. It canonicalizes the current
repository and logs one deterministic mode. Repository files, project settings,
model text, and sandbox flags cannot change that decision.

### Trusted-live

The exact assigned worktree and only its required Git common/worktree metadata
are mounted read/write at their original absolute paths. Host and container
changes are immediately shared. No clone, checkpoint, bundle import, synthetic
commit, or sandbox publication target is involved.

The agent can damage the assigned repository, its hooks/config, branches, refs,
reflogs, and worktree. This is accepted for authored repositories. Unrelated
host paths, credentials, sockets, host processes, and devices remain absent.

Before launch, the host snapshots only the effective Git `user.name` and
`user.email` into a task-specific `0600` config. That minimal file is mounted
read-only and selected with `GIT_CONFIG_GLOBAL`, so ordinary commits work
without exposing credential helpers, signing keys, includes, URL rewrites, or
the host Git configuration. Isolated tasks receive the same minimal identity
resource without receiving host repository metadata.

Docker development ports are disabled by default. A project may opt into
loopback-only dynamic or fixed publication in its machine-controlled sandbox
configuration; stale Pi containers are then eligible for the labeled cleanup
command below.

A clean protected checkout receives a linked `pi/<session>` worktree below the
configured worktree root. A dirty protected checkout fails without stashing,
copying, resetting, or discarding anything, except for the exact configured
control-plane repository, which stays trusted-live so repairs cannot deadlock
behind the dirty checkout they are meant to repair.

### Isolated

Unknown and external repositories use the retained private clone/checkpoint
backend. No host source or Git metadata is mounted. The parent owns publication
to `pi-sandbox/<session-hash>`; the active host checkout stays unchanged.

### Host maintenance

`pi-host` is deliberately unsandboxed, normal-user, fresh-session maintenance.
It prints a warning and disables context files, extension discovery, skills, and
prompt templates, so autonomous subagents are not loaded. It may explicitly
load only the inert `auto-continue` lifecycle extension so a host task resumes
after compaction; no tools or subagent providers are enabled by that extension.
It may run commands that use interactive `sudo`, but the harness never stores
or bypasses authentication.

This mode is dangerous: it can read or exfiltrate user secrets, delete user
files, change startup configuration, use authenticated CLIs, and modify user
processes. Docker/LXD access may provide effective root authority. Interactive
sudo authentication may temporarily authorize root commands. This risk is
accepted only when the user explicitly launches `pi-host`; ordinary `pi` never
falls back to it.

## Durable root sessions

Visible root sessions are flat files in `~/.pi/agent/sessions/root/*.jsonl` and
are indexed by the atomic user-owned `~/.pi/agent/root-registry.json`. A root
record contains the immutable conversation ID, profile, exact session file,
canonical repository identity, stable worktree, branch, and active/archived
state. `bin/pi`, `pi-tmux-session`, and the secretary launcher pass the exact
session path; they never use cwd-based `--session-id` lookup. Private child
sessions use `~/.pi/agent/sessions/subagent/` and are therefore absent from the
root session selector.

A dirty existing checkout is recorded in place rather than stashed, reset, or
copied; clean repositories receive a private linked worktree. The migration
utility is `pi-root-session migrate --dry-run` followed by
`pi-root-session migrate`; it forks selected legacy histories into stable
worktrees, records `parentSession`, leaves source files untouched, and archives
ambiguous duplicates. Run `pi-root-session cleanup` only after reviewing the
migration; `--apply` is the only path that prunes stale Git metadata or obsolete
same-HEAD legacy branches.

## Task topology

One normal task has one route, workspace, branch, and container. Parent,
scouts, worker, reviewers, and BTW share it. Child context style does not alter
the execution plane. Children cannot publish or remove the task container. A
worker may spawn headless asynchronous investigators, but that nested fanout is
mechanically restricted to read-only agents with read/search/supervisor tools;
it cannot create worktrees, write, edit, run shell commands, or spawn again.

`/goal <objective>` uses pi-goal to continue the same session after a settled
turn until `goal_complete`, `goal_blocked`, pause, or a provider/runtime stop.
This harness configures no automatic response-count or no-progress turn limit. Use `/goal --tokens <budget> ...` only when an explicit
provider-token bound is desired.
Investigator count is selected from the task rather than a fixed fanout recipe;
report-only parallelism does not create Git worktrees. Secretary investigators
run asynchronously with only read/search and bounded read-only Git tools; they
inherit neither shell/write tools nor ordinary extensions. Interactive
sessions keep `subagent_wait` non-blocking: completion notifications and
`subagent({ action: "status" })` remain available without holding the parent
turn open. Secretary investigations impose no elapsed-time, assistant-turn,
token, or tool-call budget; their observed duration and token usage are appended to
`~/.pi/agent/secretary-stats.jsonl` (or the configured `PI_CODING_AGENT_DIR`).
Each record omits prompts, task text, paths, and outputs. Run
`pi-secretary-stats` (or add `--json`) to aggregate duration, tokens, turns,
tool calls, cost, agent, model, and project totals.

Explicit independent mutable candidates receive linked worktrees, branches, and
containers. They must commit before comparison; candidate worktrees and branches
remain until explicitly removed.

Parent sandbox ref moves, rebases, automatic checkpoints, and container teardown
are frozen while a child run is active. Child runners publish a private,
route-bound lease with PID start-identity metadata; dead owners are reclaimed only
when process death or PID reuse is demonstrable. `/sandbox status` reports the
active leases and recovery is intentionally wait/stop, verify/export artifacts,
then retry—not stale-ref cleanup during execution. If a completed child left a
clean descendant commit in the container, the parent imports it through a
bundle, verifies ancestry and object integrity, and updates the sandbox ref with
compare-and-swap; dirty, rebased, unrelated, or ambiguous state remains blocked.

### Secretary Git maintenance

See `SECRETARY_WORKFLOW.md` for the complete user-facing topology, tool
allowlists, worker lifecycle, session boundaries, and failure behavior.

The secretary is not given arbitrary shell or Git arguments. `secretary_git` is
read-only. `secretary_git_cleanup` accepts a structured plan containing only
exact `benchmark/*` or `side-agent/*` branch entries, `feature/*` rename
destinations, worktree paths under the host-managed worktree root, and exact
Pi-owned artifact files. A plan records expected Git OIDs and artifact hashes.
The secretary first asks the host controller for a dry-run inventory and receives
a canonical plan hash; an apply call must repeat the plan and hash after an
explicit current-turn cleanup authorization. The controller revalidates the
repository identity, OIDs, branch/worktree ownership, protected branches,
worktree cleanliness, active-session/process use, and artifact hashes while
holding the repository common-directory lock. It uses compare-and-swap Git ref
updates and non-forced removal of clean owned worktrees. `secretary_land_reviewed`
can materialize a reviewed candidate only after the secretary and user jointly
decide that it is acceptable and the user explicitly authorizes landing; a
reviewer receipt never triggers an automatic merge. Cleanup never accepts
arbitrary Git arguments, source paths, remote operations, pushes, or force
deletions, and this capability does not execute the requested cleanup by itself.

## Engineering workflow and context

The Luna parent selects one engineering mode—FAST, RIP, BUILD, or MAJOR—and an
independent learning level—OFF, LIGHT, or DEEP. These are conversation policy,
not launcher or settings variants.

The auto-discovered global `workflow-state` extension provides a branch-aware
`task_packet` tool. It stores versioned, schema-bounded task state in Pi's existing session JSONL and
injects only the latest active packet into the parent prompt. It creates no
packet state for idle or trivial sessions until `replace` is called.

Custom children start with fresh context and do not inherit global/project
context files. The parent supplies only the applicable repository instructions,
accepted decisions, boundaries, evidence, and stop conditions in the child
assignment. Detailed subagent sessions and artifacts stay outside repositories.
Forked context is an explicit exception because it contains the complete parent
history rather than a filtered brief.

### Model freedom and anti-slop contracts

Spawned agents and subagents are not artificially crippled. The checked-in
subagent configuration deliberately omits `timeoutMs`, `maxRuntimeMs`,
`turnBudget`, `toolBudget`, and token budgets: there is no automatic elapsed-time,
assistant-turn, provider-token, or tool-call cutoff. `0` for the cumulative spawn
and parallel task-count settings means unlimited; the normal scheduler may still
stage concurrency, which is a host resource policy rather than a model
completion limit. A user can explicitly interrupt or stop a run.

Freedom is constrained by meaning and authority, not arbitrary impatience:
every consequential worker brief carries a task packet, contract, relevant
boundaries, acceptance evidence, and stop/escalation conditions. Role/tool
allowlists, repository policy, child depth, worktree ownership, read-only
investigator boundaries, and acceptance checks remain enforced. Anti-slop means
better contracts and observable evidence—not premature timeout or turn caps.
The Inspector (`/observe` or `Ctrl+I`) makes those instructions, constraints,
status, and results visible without exposing hidden reasoning.

Report-only roles omit `write` and `edit`, but their sandboxed `bash` can still
mutate the worktree. `acceptanceRole: read-only` controls acceptance inference;
it is not an authority boundary. The parent therefore verifies the actual
changed paths after children run. Top-level child runs are asynchronous by
default and successful completion notices are private model messages; use
`/subagents-fleet` or `subagent({ action: "status", view: "fleet" })` for
explicit inspection. Failures, pauses, stops, and attention notices remain
visible.

Context measurement is opt-in:

```bash
PI_WORKFLOW_CONTEXT_AUDIT=1 pi
```

This records hashes and byte counts beside the owning session. Add
`PI_WORKFLOW_CONTEXT_AUDIT_RAW=1` only in disposable acceptance sessions when
raw prompts/messages are required for a boundary audit.

## Installation and activation

`install.sh` links configuration, installs exact packages, replays patches only
from expected source hashes, builds the hardened image, safely installs or
preserves the host policy, and links `pi` plus `pi-host`. Run it only after
review from explicit host mode. Restarting Pi is the activation boundary.

See `MIGRATION.md` for classification, root-session migration, hashes,
limitations, cleanup, and rollback. Do not restart the managed tmux grid until
`pi-root-session migrate --dry-run` has been reviewed.

## Resource cleanup

Inspect Pi-owned containers, derived images, and cache volumes without deleting:

```bash
pi-sandbox-gc
```

Apply only the printed, label-scoped retention policy after review:

```bash
pi-sandbox-gc --apply
```

The command never runs `docker system prune`, never touches unlabeled Docker
resources, retains unproven isolated task state for recovery, and uses a
30-day image/volume retention window while keeping the two newest runtime
images per project group.
