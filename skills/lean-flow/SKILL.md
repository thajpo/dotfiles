---
name: lean-flow
description: "Turn a discussed feature into a concise GitHub issue and, only when implementation is imminent, a temporary implementation plan that is absorbed into the PR and deleted before merge. Use when the user invokes $lean-flow, asks to make a feature ready to implement, or asks to convert planning notes into issues/specs without maintaining a planning tracker."
---

# Lean Flow

## Purpose

Keep planning useful to a human reviewer without creating a second project
management system in Markdown.

Use this lifecycle:

```text
chat -> GitHub issue -> optional temporary plan -> PR -> merged code and docs
```

Do not create or maintain `current.md`, brainstorm registries, status mirrors,
or `Brainstormed`/`Specd` state machines.

## Core Rules

- Keep uncommitted ideas in chat unless they are worth remembering.
- Create one issue for one independently reviewable feature.
- Do not create a backlog of speculative issues without the user's request.
- Treat the issue as the planning document by default.
- Add a temporary repository plan only when imminent implementation needs more
  detail than the issue can comfortably hold.
- Delete a temporary plan before merge after its decisions and evidence are in
  the PR description.
- Keep durable documentation only for current behavior, architecture contracts,
  operational guidance, and research evidence.
- Prefer short prose and bullets. Avoid tables and process jargon.

## Workflow

### 1. Discuss

Clarify the problem, desired outcome, important tradeoffs, and what is out of
scope in chat. Do not write planning files during exploration.

If the user is only exploring, stop here.

### 2. Create A Short Issue

Create an issue only when the idea should survive the conversation or is likely
to be implemented.

Use this small structure:

```markdown
## Problem
Why this matters in one short paragraph.

## Outcome
What should be true when the work is done.

## Acceptance
- [ ] Observable result one
- [ ] Observable result two

## Testing
- [ ] Relevant automated or end-to-end proof

## Out of scope
- Explicit boundary, only when needed
```

Keep implementation guesses out unless they resolve a real architecture choice.

### 3. Make It Ready To Implement

When the user chooses the feature for implementation, expand the issue with the
minimum decisions needed to code safely:

- chosen behavior and architecture;
- acceptance criteria;
- testing and evidence;
- meaningful risks, migration, or rollback concerns;
- explicit non-goals when scope could drift.

The expanded issue is normally the complete implementation plan.

Create a temporary plan file on the feature branch only when the design is too
large for a readable issue. Typical reasons are a cross-cutting architecture
decision, a migration/rollout sequence, or repo-local diagrams and pseudocode
needed during implementation. Keep it focused on decisions, not a narrated
coding itinerary.

Explicit user approval in chat is enough to begin unless repository policy
requires stronger evidence. Do not invent an approval ceremony.

### 4. Implement Through One PR

Link the issue, isolate the branch/worktree, implement the accepted scope, and
validate in proportion to risk.

Once the PR exists, use its description and review thread as the active record.
Do not synchronize the same plan across an issue, tracker, plan file, and PR.

### 5. Finish And Delete Temporary Planning

Before merge:

- move the useful problem statement, decisions, tradeoffs, and validation into
  the PR description;
- delete any temporary plan file in the same PR;
- update durable docs only where actual system truth changed;
- leave research results and negative evidence intact.

After merge, close the issue. The PR, code, tests, and durable docs are the
record; no planning tombstone is required.

## Authority

- Before an issue exists: the latest explicit chat decision wins.
- Before a PR exists: the issue is the implementation contract.
- After a PR exists: the PR description and review thread are authoritative.
- After merge: code, tests, and durable documentation are authoritative.

When sources disagree, update the current authority instead of maintaining
parallel copies.

## Scope Judgment

- Feature or risky refactor: issue first, then implement.
- Small obvious fix: a direct focused PR is acceptable when the user asks for
  the fix and no product decision is hidden.
- Large feature: split only at independently testable and reviewable boundaries.
- Related steps that cannot deliver value independently belong in one issue.

When work may need splitting, tell the user how many issues and temporary specs
you recommend and why. Skip that ceremony for an obviously single feature.
Draft in chat unless the user explicitly asks to create or publish the issue;
an explicit create/publish request is sufficient authority to proceed.
