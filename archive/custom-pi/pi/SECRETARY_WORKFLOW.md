# Pi Harness Secretary And Worker Workflow

> Archived—do not follow for current Pisec.

This document describes the intended first-release workflow. It is accepted
only when exercised through the installed Pi harness launchers and controller.

## Roles

### Project Secretary

Registration creates one durable secretary conversation for a project. The
secretary runs on the host with controller-scoped read, project-message, web,
and semantic control tools. It has no general write, edit, shell, arbitrary Git,
or unrelated-project access.

The secretary may start temporary read-only investigations, propose a
workstream, request review, and propose integration. Those operations create
controller resources; a tmux pane or model statement cannot create authority.

### Investigator

An investigator is temporary and read-only. It receives one project or working
copy, bounded local read/search tools, and web tools. Completion, failure,
interruption, and needs-user outcomes are durable controller state.

### Personal Or Workstream Coder

A coding conversation owns one assigned working copy, one controller run, one
writer generation, and one runtime. It may read, edit, run shell commands, and
test only inside that execution boundary. Host Git ref changes, host commands,
and external-network operations use explicit controller operations.

### Reviewer And Integration Agent

A reviewer inspects one exact submitted revision without editing it and returns
an immutable receipt. A later revision makes that receipt stale. Simple landing
requires an explicit safe fast-forward operation. Conflicts or combined changes
use a separate integration working copy and coding agent, followed by review.

## Typical Flow

1. Register a project in fresh controller state.
2. Open its controller-created secretary conversation.
3. Discuss the goal and inspect scoped project evidence.
4. Start bounded read-only investigations when they reduce uncertainty.
5. Approve an exact workstream request.
6. Let the controller create the working copy, conversation, run, runtime, and
   presentation assignment as one recoverable operation.
7. Work in the headful coding conversation and send durable progress or
   needs-user messages to the project.
8. Submit one exact revision for independent review.
9. Explicitly fast-forward it when safe, or create an integration agent.
10. Clean up only exact controller-owned resources after the work is accepted
    and no process or recovery dependency remains.

## Presentation

Tmux may display secretary and coding conversations, focus an existing one, or
restore a stopped one. Every pane must be derived from a controller conversation
and exact run. Pane titles, current directories, and restored shell commands are
observations only.

First-release acceptance does not depend on Herdr. Restart must stop or replace
only exact managed Pi processes and must preserve unrelated tmux sessions.

## Fresh Conversation Rule

The Pi harness creates new session JSONL under its own state boundary.
It does not inspect or adopt pre-controller root registries, secretary records,
routes, worktrees, or chat files. Existing old chats may be retained or manually
deleted, but neither choice affects Pi harness correctness.

## Acceptance

The workflow is not implemented merely because rows, worktrees, or panes exist.
Acceptance requires observing real Pi processes, exact extension/tool sets,
session persistence, scoped access, writer exclusion, runtime identity, restart,
review, integration, failure behavior, and rollback through installed paths.
