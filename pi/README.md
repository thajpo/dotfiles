# Pi configuration

This directory contains the reproducible, non-secret Pi harness:

- Pi `0.82.1` and exact npm package pins;
- `pi-subagents` as the sole coding-agent orchestrator;
- deterministic trusted-live and isolated Docker workspaces;
- `pi-btw` routed through the active task execution plane;
- model/agent definitions, prompts, theme, and UI configuration;
- hash-verified compatibility patches;
- the source policy installed as `~/.config/pi/repository-policy.json`;
- the hardened task image and migration/rollback record.

Secrets, auth, sessions, runtime routes, generated host context, and live policy
state remain outside Git.

## Modes

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

A clean protected checkout receives a linked `pi/<session>` worktree below the
configured worktree root. A dirty protected checkout fails without stashing,
copying, resetting, or discarding anything.

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

Explicit independent candidates receive linked worktrees, branches, and
containers. They must commit before comparison; candidate worktrees and branches
remain until explicitly removed.

## Installation and activation

`install.sh` links configuration, installs exact packages, replays patches only
from expected source hashes, builds the hardened image, safely installs or
preserves the host policy, and links `pi` plus `pi-host`. Run it only after
review from explicit host mode. Restarting Pi is the activation boundary.

See `MIGRATION.md` for classification, hashes, limitations, cleanup, and rollback.
