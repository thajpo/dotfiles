---
name: pi-subagents
description: |
  Delegate work to builtin or custom subagents with single-agent, chain,
  parallel, async, fresh-context, and intercom-coordinated workflows. Use
  for advisory review, implementation handoffs, and multi-step tasks where a
  single agent should stay in control while other agents contribute context,
  planning, or execution.
---

# Pi Subagents (parent orchestrator only)

This skill is for the main parent orchestrator only. Do not inject or follow it inside spawned child subagents. The parent session owns delegation, orchestration, review fanout, and final fix-worker launches; child subagents should receive concrete role-specific tasks. Ordinary children should not run their own subagent workflows.

## Core principles

- **Parent is sole decision and orchestration authority.** Children receive concrete task assignments and escalate decisions to the parent.
- **Fresh scoped children by default.** Use `context: "fresh"` for all children unless the child specifically needs the complete parent history. Forked context (`context: "fork"`) is explicit-only and should be rare.
- **One writer per active worktree.** Worker is the single writer. All other roles are report-only by policy (scout, context-builder, researcher, planner, reviewer, oracle, delegate). Because `bash` can mutate, `acceptanceRole: read-only` is not an access-control boundary; the parent must verify the actual worktree delta after children run.
- **Read-only fanout only when perspectives differ.** Do not fan out for simple single-angle review.
- **List available agents before executing** when unsure which agents to use: `subagent({ action: "list" })`.
- **Compact brief template.** Give subagents a compact brief: goal, context/evidence, success criteria, hard constraints, validation, output expectation, stop rules. Avoid long procedural scripts.

## Subagent roles

| Agent | Purpose | Model | Role |
|-------|---------|-------|------|
| `scout` | Fast codebase recon | Flash | read-only |
| `context-builder` | Requirements/codebase handoff | Flash | read-only |
| `researcher` | Web research brief | Flash | read-only |
| `planner` | Implementation plans | Luna | read-only |
| `reviewer` | Plan critic and final review | Luna | read-only |
| `oracle` | Decision-consistency advisory | Sol | read-only |
| `delegate` | Lightweight generic delegate | Luna | read-only |
| `worker` | Implementation and approved handoffs | Flash | writer |

## Running subagents

### Single agent — fresh context (default)
```typescript
subagent({
  agent: "oracle",
  task: "Review this direction. Evidence: ..."
})
```

### Forked context — explicit only when the full history is required
```typescript
subagent({
  agent: "delegate",
  task: "Extract the exact text requested from the inherited conversation; do not make decisions.",
  context: "fork"
})
```

Fork is not filtered context: it branches the complete persisted parent history. Do not use it merely because a worker follows earlier scouting; pass the accepted scoped brief instead.

### Parallel read-only fanout
```typescript
subagent({
  tasks: [
    { agent: "reviewer", task: "Review diff for correctness" },
    { agent: "reviewer", task: "Review diff for test quality" }
  ],
  concurrency: 2,
  context: "fresh"
})
```

### Chain
```typescript
subagent({
  chain: [
    { agent: "scout", task: "Map auth flow" },
    { agent: "planner", task: "Plan from {previous}" },
    { agent: "worker", task: "Implement from {previous}" }
  ]
})
```

### Async — preferred for genuinely long work, not universal dogma
```typescript
subagent({
  agent: "worker",
  task: "Run the full test suite",
  async: true
})
```

Use `async: true` for long-running work. For quick bounded tasks, synchronous is fine. In interactive sessions, normally yield and let Pi wake the parent; use `subagent_wait()` when the user asked for run-to-completion results in the current turn. Never poll with sleep loops.

### Subagent control
- `subagent({ action: "status" })` — inspect active runs
- `subagent({ action: "status", id: "..." })` — inspect one run
- `subagent_wait()` — block until next run completes
- `subagent_wait({ all: true })` — drain all runs
- `subagent({ action: "interrupt", id: "..." })` — soft-interrupt a run
- `subagent({ action: "steer", id: "...", message: "..." })` — send guidance to a live run
- `subagent({ action: "resume", id: "...", message: "..." })` — revive a completed/paused run
- `subagent({ action: "doctor" })` — diagnose setup issues

### Management
- `subagent({ action: "list" })` — list available agents and chains
- `subagent({ action: "create", config: {...} })` — create new agent
- `subagent({ action: "update", agent: "...", config: {...} })` — update agent
- `subagent({ action: "delete", agent: "..." })` — delete agent
- `subagent({ action: "eject", agent: "..." })` — copy builtin to user scope
- `subagent({ action: "disable", agent: "..." })` — hide agent
- `subagent({ action: "enable", agent: "..." })` — restore agent
- `subagent({ action: "reset", agent: "..." })` — restore builtin default

## Prompting subagents

Use this compact assignment shape and omit irrelevant headings:

```markdown
# Assignment
## Mode and role
## Goal or question
## Current accepted context
## Relevant repository instructions and boundaries
## Evidence or acceptance
## Stop and escalate conditions
## Output format
```

Include only accepted decisions and the evidence the role needs. Do not pass raw scout transcripts, unrelated reports, obsolete plans, or the whole task history. If a necessary repository instruction is not included, the child should escalate rather than assume.

## Artifacts and sessions

Artifacts and session files stay outside the project repo under the current patched configuration, in user-scoped session or temporary artifact directories. Keep ordinary final results concise. For large detail, set both `output` and `outputMode: "file-only"`; the parent receives a compact reference and pulls detail only when needed.

## Intercom / supervisor coordination

Children use the always-available `contact_supervisor` tool as the required parent-feedback channel; `intercom` is only a lower-level fallback for bridge/runtime failures. Parents reply with `subagent_supervisor({ action: "reply", message: "..." })`.

Every `agent-feedback.v1` interview and `AGENT_FEEDBACK` progress update is persisted in the central Pi-owned user storage under `~/.pi/agent/feedback/records/` (or the configured Pi agent directory), across all projects, with bounded normalized content and provenance. Every child also has the direct `harness_feedback` tool for one bounded non-blocking observation; subagents should actively report useful harness friction or improvement with `kind: "harness-improvement"`. Raw prompt content is opt-in via `PI_AGENT_FEEDBACK_RAW=1`. Parents can disposition non-blocking records with `subagent_supervisor({ action: "review", feedbackId, outcome: "accepted|rejected|deferred" })`; this does not automatically promote memory or ideas.

Use `contact_supervisor` with `reason: "need_decision"` when blocked. Use `reason: "progress_update"` for non-blocking checkpoints. Children must not decide unapproved scope, product, or architecture changes; they escalate.

## Worktree isolation

```typescript
subagent({
  tasks: [
    { agent: "worker", task: "Implement A" },
    { agent: "worker", task: "Implement B" }
  ],
  worktree: true
})
```

Use worktrees for intentionally independent writers. The parent shows an interactive approval prompt before creating them; approval is bound to the project open in the current Pi session and the exact request. Default is one writer per active worktree.

## Important constraints

- **Configured nesting depth is 2.** Ordinary children cannot launch descendants; explicit workers may use constrained read-only fanout.
- **Forked runs require a persisted parent session.** Use `context: "fresh"` when no session file exists.
- **No mandatory Fable/clarify/planner/review-loop ceremony.** Use what fits the task.
- **No mandatory clarification.** Use `interview` when genuine ambiguity exists; skip when the task is clear.
- **Async remains available** for genuinely long work; do not force it for every launch.

## Common workflow examples

### Recon → Plan → Implement
```typescript
subagent({
  chain: [
    { agent: "scout", task: "Map the auth flow" },
    { agent: "planner", task: "Plan migration from {previous}" },
    { agent: "worker", task: "Implement from {previous}" }
  ]
})
```

### Parallel review after implementation
```typescript
subagent({
  tasks: [
    { agent: "reviewer", task: "Review diff for correctness", output: false },
    { agent: "reviewer", task: "Review diff for test quality", output: false }
  ],
  concurrency: 2,
  context: "fresh",
  async: true
})
```

### Single oracle advisory
```typescript
subagent({
  agent: "oracle",
  task: "Review this approach. Evidence: ..."
})
```

### Isolated parallel writers
```typescript
subagent({
  tasks: [
    { agent: "worker", task: "Implement feature A in worktree" },
    { agent: "worker", task: "Implement feature B in worktree" }
  ],
  worktree: true
})
```
