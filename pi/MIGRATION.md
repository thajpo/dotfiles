# Pi harness migration record

## Frozen baseline and recovery

- Source baseline: `549e57cb4951253b6f88c82de79c06af4035427c`
- Recovery ref created in the migration workspace:
  `refs/heads/rollback/pi-harness-pre-trusted-live-20260729`
- Existing `pi-sandbox/*`, recovery refs, commits, and containers are not deleted
  by this migration.
- Existing native macOS `.venv` directories are not copied into Linux tasks.
  Python dependency environments are keyed by exact project manifests and
  locked into Docker images when the graph is manifest-only.
- This change was prepared in the pre-existing isolated Pi container. Host
  activation and Docker boundary tests therefore remain an explicit `pi-host`
  step.
- Final global `AGENTS.md` SHA-256:
  `c9fc282049e8a7f9f88d07fad9f73a979531b02edd3abc7b7966c27dbd1cac5b`.
  Installation links `~/.pi/agent/AGENTS.md` to
  `$HOME/dotfiles/agent/AGENTS.md`; project-specific context files are not
  changed.

## Phase-0 inventory

Observed before modification:

- Checked-out migration branch: `pi-sandbox/ac954fb84584fc29`, clean.
- Host source branch represented in the clone: `chore/dotfiles-octo-sync` at
  `549e57c`.
- Pi version pin: `0.83.0`.
- Container Node/npm: `v22.23.1` / `10.9.8`; Bun was unavailable.
- Package pins: `pi-subagents@0.35.1`, `@kjrjay/pi-sandbox@0.2.0`,
  `pi-btw@0.4.1`, and the exact versions in `npm/package-lock.json`.
- Current execution container reported by the harness:
  `pi-dotfiles-pi-sandbox-ac954fb84584fc29-7dbea6cf124c91c6`.
- The host Pi executable, host package tree, host container list, host global
  symlinks, and host rollback directories were not visible from the private
  clone and are classified `UNKNOWN` until activation validation.

### KEEP

- Pi and all npm version pins and lockfile integrity values, including
  `pi-image-tools@1.4.0`.
- `pi-subagents` as the sole coding-agent orchestrator.
- Flat durable root sessions, an atomic root registry, exact session-file
  launch, and stable root worktrees. Legacy sessions are copied, never merged
  or deleted, until an explicit cleanup review.
- Luna Max for every Pi role, with child depth/concurrency caps.
- User-scoped child sessions and the no-project-artifact patch.
- Fail-closed tool routing and disabled host-side acceptance commands.
- `pi-btw` and its sandbox proxy design.
- Private clone, bundle validation, checkpoint import, and isolated branches.
- Docker loopback port publication remains opt-in; the default is no published
  port and no host gateway/environment pass-through.
- `scripts/pi-verify-change`.

### REWORK

- Workspace creation and task-container ownership.
- Child and BTW route identity.
- Docker UID/GID, mounts, cache, and hardening.
- Repository classification and protected-branch handling.
- Global `AGENTS.md`, launchers, installer, documentation, and acceptance tests.

### REMOVE

- `target: current` as a trusted-workspace substitute.
- Model/user-selectable publication targets in ordinary `pi`.
- Per-child competing target locks and per-child replacement containers.
- Host bind package caches.
- Legacy orchestrator installation and doctor guidance.
- Universal documentation claiming the host worktree never changes.

### NOT YET LOADED

- The files in this migration are not active merely because they are committed.
- The hardened Docker image, patched installed npm sources, global workflow
  symlink, root-session extension/registry, image package, scoped child roles,
  repository policy, launchers, and generated host context require the reviewed
  installer from explicit `pi-host`.

### UNKNOWN

- Host-global backup directories and symlink state.
- Stopped host containers not exposed to the old task container.
- Exact host Docker, GPU, Bun, tmux, and Neovim runtime state.

## Workflow and context layer

The parent selects FAST, RIP, BUILD, or MAJOR and an independent OFF, LIGHT, or
DEEP learning level. Top-level subagent execution is asynchronous by default;
successful completion notices wake the root model without displaying a child
transcript. Failures and attention events remain visible, and
`/subagents-fleet` remains an explicit inspection surface. Child roles start
fresh, do not inherit the parent transcript or context files, and receive applicable repository instructions in
their scoped assignment. Forking remains an explicit exception when the full
persisted parent history is the required source material.

`workflow-state` stores the current task packet as branch-aware custom entries
inside Pi's existing session JSONL. Old packet entries do not enter model
context; only the latest active packet is injected into the parent system
prompt. No packet entry is created until the parent calls `task_packet` with
`replace`, and `clear` appends a tombstone.

Context manifests are disabled by default. Setting
`PI_WORKFLOW_CONTEXT_AUDIT=1` records hashes and sizes beside the owning session;
`PI_WORKFLOW_CONTEXT_AUDIT_RAW=1` additionally records raw prompt/context data
and is intended only for disposable acceptance sessions.

## Final route contract

The launcher creates a user-owned `0600` JSON route outside the repository and
passes its path plus a separate random capability to host Pi processes. The
route records mode, canonical repository/worktree/Git paths, starting OID and
status, branch, task/session/container identity, owner PID and Linux start
identity, UID/GID, image, worktree root, policy hash, and parent ownership. Only
the capability hash is stored in the route.

Same-task children inherit this route and reuse its container. A task-specific,
read-only Git config contains only the effective `user.name` and `user.email`;
this makes ordinary commits work without mounting host Git configuration or
credentials. A missing, malformed, stale, wrong-owner, wrong-capability,
wrong-workspace, wrong-Git, or wrong-identity-resource route fails before tool
execution. Independent trusted candidates may derive a
separate container only for a linked worktree below the policy worktree root,
with the same Git common directory and a harness-created candidate branch.

## Root-session migration and acceptance

Before the first restart, inspect the legacy inventory without changing Git or
session files:

```bash
pi-root-session migrate --dry-run
```

Review every proposed repository/worktree and duplicate group. Apply the copy
only after the mapping is correct:

```bash
pi-root-session migrate
pi-root-session list
```

The migration forks each selected history into `sessions/root/<id>.jsonl`,
rewrites only its session header to the stable worktree, records the source in
`parentSession`, and places ambiguous duplicates below `sessions/root/archive`.
It never removes a legacy JSONL or Git worktree. After live resume acceptance,
review a dry cleanup and apply it only deliberately:

```bash
pi-root-session cleanup --repository /path/to/repository
pi-root-session cleanup --repository /path/to/repository --apply
```

Acceptance evidence must show: restarting a managed root opens the same exact
JSONL and worktree; `/resume` lists only direct root files; two direct roots have
different registry IDs; a depth-1 investigator runs under `sessions/subagent`;
success notices have `display: false` with `triggerTurn: true`; failed notices
remain visible; a pasted image is a native session content block and survives
resume/fork; and archiving does not leave a new prunable worktree.

## Activation and rollback

Activation is control-plane work:

```bash
pi-host
cd "$HOME/dotfiles"
./install.sh
```

Restart Pi only after installation and validation.

Rollback source and reinstall the previous pins:

```bash
pi-host
cd "$HOME/dotfiles"
git switch --detach refs/heads/rollback/pi-harness-pre-trusted-live-20260729
./install.sh
```

If the recovery ref was not promoted from the isolated migration workspace, use
its immutable OID directly:

```bash
git switch --detach 549e57cb4951253b6f88c82de79c06af4035427c
```

Before rollback, preserve task branches/worktrees and isolated branches needed
for review. Do not remove them automatically.

## Explicit cleanup

Pi-owned Docker resources now carry labels for task owner identity, runtime
environment key, skill manifest, and cache scope. Review a dry run first:

```bash
pi-sandbox-gc
pi-sandbox-gc --apply
```

The apply mode is deliberately conservative: dead trusted-live tasks can be
removed, while dead isolated tasks remain unless their checkpoint/recovery
state is explicitly marked clean. Unlabeled containers, images, volumes, and
global Docker build cache are never pruned.

Inspect before removing anything:

```bash
docker ps -a --filter label=pi.container-sandbox.managed=true
git worktree list
git branch --list 'pi/*' 'pi-sandbox/*'
```

Remove a completed candidate only by its exact recorded path and branch:

```bash
git worktree remove "$HOME/.local/share/pi/worktrees/<repo>/<task>"
git branch -d pi/<task-or-session>
```

Remove stopped task containers by their exact harness labels:

```bash
docker container prune --filter label=pi.container-sandbox.managed=true
```

Do not delete isolated or rollback branches until their review value has been
checked.
