# Pi Harness Configuration

This directory contains the reproducible, non-secret source for the Pi
system described by [`PI_IMPLEMENTATION_PLAN.md`](../PI_IMPLEMENTATION_PLAN.md).

Status: **installed and activated on this machine; release acceptance passes.**

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
- Pre-controller Pi registries, routes, sessions, and worktrees are not imported
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
state. A schema-format change may deliberately delete the controller state
through an explicitly approved activation reset; there is no automatic
migration or import path.

## Installed Status

The product is installed and activated at `~/.local/share/pi-system` with
controller state at `~/.local/state/pi-system`. Release acceptance runs
through `tests/system/run-p11-release.sh` against a fresh staged generation
(installed journeys, Docker, repair, and surface tests); evidence lands under
the configured `PI_SYSTEM_EVIDENCE_DIR`.

## Engineering Workflow

The parent chooses FAST, RIP, BUILD, or MAJOR from uncertainty and consequence,
with OFF, LIGHT, or DEEP learning selected independently. Children receive
fresh, bounded context by default. Contracts, tool authority, observable state,
and acceptance evidence constrain work; arbitrary turn or elapsed-time limits
do not substitute for those boundaries.

Harness feedback remains a separate user-scoped improvement feed. It does not
grant authority or become project lifecycle state.
