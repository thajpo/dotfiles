# Pi Greenfield Configuration

This directory contains the reproducible, non-secret source for the fresh Pi
system described by [`PI_GREENFIELD_IMPLEMENTATION_PLAN.md`](../PI_GREENFIELD_IMPLEMENTATION_PLAN.md).

Status: **component source exists; the complete installed product is not yet
accepted**.

## Product Boundary

- The controller owns projects, conversations, runs, workstreams, messages,
  reviews, and integration operations in a fresh database.
- Pi session JSONL owns conversation content, but the controller selects the
  exact file and binding.
- Coding agents run with one assigned working copy and one active writer.
- Secretaries, investigators, and reviewers receive controller-scoped read
  tools rather than general host shell access.
- Host and network commands require exact one-use approval.
- Tmux is presentation only and cannot create project or conversation identity.
- First-release operation does not depend on Herdr or remote publication.
- Pre-greenfield Pi registries, routes, sessions, and worktrees are not imported
  or consulted.

## Source Layout

```text
pi/extensions/                 controller-bound Pi tools
pi/packages/pi-sandbox-control/ first-party runtime boundary
pi/packages/pi-subagents-control/ first-party agent orchestration
pi/agents/                     role definitions
pi/prompts/                    bounded workflow prompts
scripts/pi_control/            host-local controller implementation
tests/system/                  installed-system acceptance infrastructure
```

The canonical contracts and current readiness boundary are indexed in
[`control-plane/README.md`](control-plane/README.md).

## Fresh State

The target paths are:

```text
~/.local/share/pi-system/
~/.local/state/pi-system/control.db
~/.local/share/pi-system-work/
```

Starting fresh means creating new controller projects and conversations. It
does not require classifying, importing, reconciling, or cleaning older Pi
state. Deleting older state is a separate manual choice and is never an
activation prerequisite.

## Installation Status

`bin/pi-install` and `scripts/pi_control/greenfield_install.py` implement the
staging shape, but installation is not accepted until the staged artifact is
self-contained and the final installed paths launch real Pi processes. Do not
treat a successful stage, direct `pi-control` invocation, or help command as a
release pass.

The required proof is:

1. Create fresh state.
2. Register a disposable project.
3. Launch a real controller-bound secretary and call a scoped read tool.
4. Launch a real coding conversation in its assigned runtime, edit, and test.
5. Stop and resume the exact fresh conversations.
6. Verify project, run, tool, container, and session identities.
7. Roll back the installed command generation without losing new work.

## Engineering Workflow

The parent chooses FAST, RIP, BUILD, or MAJOR from uncertainty and consequence,
with OFF, LIGHT, or DEEP learning selected independently. Children receive
fresh, bounded context by default. Contracts, tool authority, observable state,
and acceptance evidence constrain work; arbitrary turn or elapsed-time limits
do not substitute for those boundaries.

Harness feedback remains a separate user-scoped improvement feed. It does not
grant authority or become project lifecycle state.
