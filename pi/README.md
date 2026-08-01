# Pi configuration

This directory contains the reproducible, non-secret Pi harness:

- Pi `0.82.1` and exact npm package pins;
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

The machine profile owns host paths and trust. On this Mac, `~/projects` is
trusted-live by policy, while the task still executes in Linux Docker. Trust
means that the assigned worktree may be mounted read/write; it does not permit
Pi or a model to execute arbitrary host commands.

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
It prints a warning and disables context files, extensions, skills, and prompt
templates, so autonomous subagents are not loaded. It may run commands that use
interactive `sudo`, but the harness never stores or bypasses authentication.

This mode is dangerous: it can read or exfiltrate user secrets, delete user
files, change startup configuration, use authenticated CLIs, and modify user
processes. Docker/LXD access may provide effective root authority. Interactive
sudo authentication may temporarily authorize root commands. This risk is
accepted only when the user explicitly launches `pi-host`; ordinary `pi` never
falls back to it.

## Task topology

One normal task has one route, workspace, branch, and container. Parent,
scouts, worker, reviewers, and BTW share it. Child context style does not alter
the execution plane. Children cannot publish or remove the task container.
Investigator count is selected from the task rather than a fixed fanout recipe;
report-only parallelism does not create Git worktrees. Secretary investigators
run in the foreground with only read/search and bounded read-only Git tools;
they inherit neither shell/write tools nor ordinary extensions.

Explicit independent mutable candidates receive linked worktrees, branches, and
containers. They must commit before comparison; candidate worktrees and branches
remain until explicitly removed.

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

Report-only roles omit `write` and `edit`, but their sandboxed `bash` can still
mutate the worktree. `acceptanceRole: read-only` controls acceptance inference;
it is not an authority boundary. The parent therefore verifies the actual
changed paths after children run. Long work opts into async explicitly; the
default is synchronous so FAST does not acquire an automatic reviewed gate.

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

See `MIGRATION.md` for classification, hashes, limitations, cleanup, and rollback.

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
