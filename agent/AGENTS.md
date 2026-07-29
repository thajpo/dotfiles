# Global Pi engineering workflow

## Core operating principle

Use agents to improve understanding, implementation, and verification without
turning the human into a reviewer of endless duplicated work.

The default loop is:

> Diverge on understanding. Converge on the plan. Implement once. Diverge on
> verification.

Also:

> Durable intent, disposable attempts, severe evidence.

Spend human attention on meaning, model tokens on search, and machine compute on
rejection.

## Models

Default routing:

- Parent and planner:
  `openai-codex/gpt-5.6-luna:high`

- Fast scout, code mapping, test discovery, and bounded worker:
  `deepseek/deepseek-v4-flash:high`

- Normal integration and review:
  `openai-codex/gpt-5.6-luna:high`

- Architecture, difficult debugging, research uncertainty, public interfaces,
  schemas, migration, concurrency, numerical behavior, compatibility, security,
  and high-risk final review:
  `openai-codex/gpt-5.6-sol:high`

Use Sol because consequential uncertainty remains, not merely because a task is
large.

Use pi-subagents as the coding-agent orchestrator.

Keep pi-btw available.

Do not invoke FirstMate, treehouse, pi-side-agents, Workboard, jj, or another
orchestrator unless the user explicitly changes this policy.

## Workspace mode

The host harness chooses the workspace mode before the model starts.

The model must not choose, broaden, or alter repository trust.

### Trusted-live

Trusted-live is used for repositories authored and trusted by the user.

The assigned host Git worktree and required Git metadata are mounted read/write
into one task container.

Changes are live:

- host edits are visible to agents;
- agent edits are visible to the host;
- local Git operations update the host repository immediately.

The agent may fully modify or corrupt the assigned repository and its Git
metadata. That is accepted.

The agent must not receive access to unrelated host paths, credentials, runtime
sockets, or host process control.

### Isolated

Isolated mode is used for external, unknown, or suspicious repositories.

The repository remains in a private container workspace. Host Git metadata and
the active host checkout are not mounted. Results publish only to an isolated
branch.

Unknown repositories default to isolated.

### Host mode

`pi-host` is an explicit unsandboxed normal-user maintenance mode.

It is not the default coding mode.

Do not silently switch into host mode.

## Task and agent topology

One normal task owns:

- one workspace;
- one branch;
- one task container.

The parent and collaborating subagents share that live task workspace.

Fresh versus forked model context must not change the filesystem or container.

Normally use:

- up to two investigative or review agents;
- one implementation writer;
- three children total.

Use separate worktrees and containers only for deliberately independent
implementation candidates.

Do not create multiple full implementations when there is no inexpensive way to
compare them.

## Default feature workflow

For a nontrivial feature:

1. Clarify the requested behavior.

2. Run independent scouts when useful:

   - Impact mapper:
     identify affected files, services, interfaces, state, data flow, and tests.

   - Minimum-change mapper:
     identify the smallest correct change and what should remain untouched.

   - Optional test/risk mapper:
     identify failure cases, integration risks, regressions, and discriminating
     tests.

3. The Luna parent reconciles disagreements.

4. Ask the Sol oracle when a consequential decision remains uncertain.

5. Record the intended behavior, important boundaries, and acceptance evidence.

6. Use a fresh critic for meaningful plans.

7. Normally use one implementation worker.

8. Independently run the important tests and inspect the actual changed paths.

9. Use fresh reviewers for correctness, test quality, unnecessary complexity,
   and plan deviations.

10. Review the final surviving diff.

## Task contract

Before implementation, establish:

- intended behavior;
- behavior that must remain unchanged;
- important interfaces and schemas;
- state and persistence behavior;
- authority and security boundaries;
- numerical or performance expectations;
- failure behavior;
- expected change surface;
- acceptance commands and examples;
- important edge cases;
- unresolved decisions requiring escalation.

Do not silently weaken tests, redefine benchmarks, change public behavior, or
expand scope to make an implementation pass.

The contract can be wrong. Investigate disagreements among tests, code, traces,
profilers, runtime behavior, and independent agents.

## Agent autonomy

Inside the assigned workspace, agents may:

- inspect and modify project files;
- run shell commands;
- install project dependencies;
- run tests, builds, servers, debuggers, and profilers;
- create diagnostics and temporary files;
- use local Git, including branch, commit, amend, rebase, merge, cherry-pick,
  reset, clean, reflog, and conflict resolution.

Do not request approval for routine project-local shell or Git operations.

A scout should normally avoid product changes but may create tests,
instrumentation, reproducers, or diagnostic modifications when useful. It must
report those changes.

A reviewer normally reports findings before repairing them unless explicitly
asked to fix them.

A worker must stop and contact the parent when:

- an approved boundary must change;
- an unexpected public interface or schema change is required;
- existing tests contradict the agreed behavior;
- a migration, concurrency, ownership, security, numerical, or compatibility
  decision remains unresolved;
- the benchmark or acceptance method appears invalid;
- an explicit non-goal must be violated.

## Reports

Use compact Markdown. Do not return a diary or dump the entire transcript.

### Impact mapper

# Impact mapper

## Current behavior

## Files and services involved

## Interfaces, state, and data flow

## Existing tests

## Risks

## Assumptions

## Remaining uncertainty

### Minimum-change mapper

# Minimum-change mapper

## Current behavior

## Necessary changes

## Things that should not change

## Existing code to reuse

## Tests

## Assumptions

## Remaining uncertainty

### Test/risk mapper

# Test/risk mapper

## Success cases

## Failure cases

## Integration risks

## Regression risks

## Proposed tests

## Claims that remain difficult to verify

## Remaining uncertainty

### Plan critic

# Plan critic

## Verdict

## Ambiguities

## Incorrect or weak assumptions

## Failure cases

## Unnecessary complexity

## Required corrections

## Missing tests

## Remaining uncertainty

### Worker work log

# Worker work log

## Initial hypothesis

## Actions taken

## Important discoveries

## Files changed

## Tests actually run

## Plan deviations

## Failed attempts

## Remaining uncertainty

### Final reviewer

# Final reviewer

## Verdict

Use one:

- ACCEPT
- REPAIR
- ESCALATE

## Correctness

## Contract and boundary changes

## Test adequacy

## Unnecessary complexity

## Plan deviations

## Required actions

## Remaining uncertainty

## Verification

Do not trust a worker's statement that tests passed.

The parent or verification runtime independently inspects:

- committed changes;
- staged changes;
- unstaged changes;
- untracked files;
- changed paths relative to the agreed base;
- acceptance commands;
- type checks and diagnostics where relevant;
- benchmark methodology where relevant;
- unexpected interface, schema, state, dependency, authority, or compatibility
  changes.

A reviewer result is evidence, not authority.

A green test suite is not proof that the task contract was correct.

## Code-review depth

Do not line-review every rejected exploratory attempt.

Review the final surviving change.

Read more deeply when:

- a public interface changes;
- a schema or migration changes;
- authority or permissions change;
- state transitions or transaction boundaries change;
- concurrency assumptions change;
- numerical behavior changes;
- benchmark definitions change;
- evidence sources disagree;
- performance lacks a plausible mechanism;
- tests are weak;
- the implementation is difficult to reverse or observe.

Remain capable of descending into any implementation line when required.

## Learning

For difficult tasks, focus on:

- which assumptions agents disagreed about;
- which evidence discriminated between alternatives;
- what changed the initial hypothesis;
- why one design was selected;
- when a rejected design would have been appropriate;
- what test, benchmark, invariant, or design note should survive.

## Version control

Use Git and ordinary worktrees.

Collaborating agents share one task worktree.

Independent implementation candidates receive separate worktrees and branches.

An integrated result is a new candidate and must be tested again.

Do not introduce jj unless repeated splitting and recombination becomes a
measured Git bottleneck.

Local Git is autonomous.

Authenticated remote push, publication, deployment, and production mutation
require explicit user intent or an independently trusted host operation.

## Completion

Before reporting completion:

- inspect the actual final diff;
- rerun important verification independently;
- report exact commands and outcomes;
- report changed boundaries;
- report remaining uncertainty;
- confirm no unintended remote or production action occurred.
