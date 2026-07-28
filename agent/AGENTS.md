# Global Pi engineering workflow

## Core principle

Use agents to make understanding, implementation, and verification more thorough without turning the human into a reviewer of endless duplicated work.

> Diverge on understanding. Converge on the plan. Implement once. Diverge on verification.

> Durable intent, disposable attempts, severe evidence.

Spend human attention on meaning, model tokens on search, and machine compute on rejection.

## Models and roles

The main Pi session is the persistent Luna High parent and liaison.

- Parent and normal planner: `openai-codex/gpt-5.6-luna:high`
- Fast scout, context gathering, test discovery, and bounded worker: `deepseek/deepseek-v4-flash:high`
- Normal reviewer and integrator: `openai-codex/gpt-5.6-luna:high`
- Oracle, architecture, difficult debugging, research uncertainty, numerical decisions, concurrency, compatibility, security, and high-risk review: `openai-codex/gpt-5.6-sol:high`

Use `pi-subagents` as the only coding-agent orchestrator. Sol Max is not routine. `/btw` remains an interactive side-question channel and does not own implementation or final decisions unless explicitly assigned.

FirstMate, treehouse, pi-side-agents, and Workboard are dormant or removed. Do not install jj, swarm/team frameworks, vector memory, always-on autonomous review loops, or duplicate launcher families.

## Parallelism and workflow

Parallel research across projects is allowed; unrelated active integration frontiers are not. Normally use at most two investigative/review agents, one implementation writer, and three children total. Use multiple implementations only when alternatives are genuinely uncertain and evaluation is cheap and objective.

For nontrivial work:

1. Clarify intended behavior and what must remain unchanged.
2. Use fresh impact and minimum-change scouts when useful; add a test/risk mapper for risky work.
3. Luna reconciles reports and records the task contract.
4. Ask Sol about consequential architecture, security, compatibility, numerical, concurrency, or research decisions.
5. Run a fresh plan critic.
6. Normally use exactly one implementation worker.
7. Independently execute acceptance commands and inspect changed paths.
8. Use fresh reviewers for correctness, security, tests, scope, and unnecessary complexity.
9. The human reviews the final surviving diff.
10. Nothing pushes, publishes, deploys, merges a remote PR, or changes production without explicit user intent.

The contract covers intended behavior, unchanged behavior, interfaces and schemas, state and permissions, numerical and performance budgets, compatibility, allowed paths, failure behavior, acceptance commands, edge cases, unresolved decisions, and escalation conditions. If tests, traces, profilers, code, or candidate analyses disagree, investigate the disagreement and improve the contract or evidence rather than averaging it away.

## Agent autonomy

Inside the assigned sandbox, agents have broad normal engineering capabilities: read, write, edit, bash, grep, find, ls, tests, builds, package installation, diagnostics, profilers, formatters, local servers, and ordinary local Git including branch, commit, amend, rebase, merge, cherry-pick, reset, clean, reflog, and conflict resolution. Do not ask for routine command approvals.

Scouts may create reproducers, tests, instrumentation, and temporary diagnostic changes but must report every modification. Reviewers normally report findings before repairing them. Workers stop and contact the parent instead of guessing when a boundary, public interface, schema, migration, concurrency, security, numerical, compatibility, benchmark, or explicit non-goal decision is unresolved.

Remote push/force-push, PR publication/comment/approval/merge, deployment, production mutation, and sending source or credentials to a new external service require explicit user intent.

## Sandbox boundary

All model-callable file and shell operations execute inside the configured Docker sandbox. Sandbox failure is a hard failure; never silently fall back to host tools. The sandbox must not expose the host home, unrelated repositories, SSH/GPG files or agents, cloud credentials, browser profiles, Pi authentication, Docker/LXD sockets, host process control, broad host environment variables, or production credentials. Do not rely on command-name blacklists as the secret boundary; secrets are absent from the execution environment.

The active sandbox is `~/.pi/agent/extensions/pi-sandbox.json`, using the locally built pinned image and `target: sandbox`. It copies a local Git clone, keeps the trusted checked-out branch/worktree unchanged, publishes only to an isolated local sandbox branch, ignores host-untracked files, passes no host environment variables, and removes containers after checkpointing. Outbound network is allowed for ordinary dependency and documentation access. Docker is not a perfect hostile-code boundary. GPU access is not provided; do not silently run GPU work on the host.

## Reports

Use compact Markdown, not diaries or giant transcripts. Store full transcripts in session artifacts. Every difficult implementation produces a factual work log listing files, commands, tests, evidence, deviations, failed attempts, and remaining uncertainty.

### Impact mapper

- Current behavior
- Files and services involved
- Interfaces, state, and data flow
- Existing tests
- Risks, assumptions, remaining uncertainty

### Minimum-change mapper

- Current behavior
- Necessary changes
- Things that should not change
- Existing code to reuse
- Tests, assumptions, remaining uncertainty

### Test/risk mapper

- Success and failure cases
- Integration and regression risks
- Proposed tests
- Claims difficult to verify
- Remaining uncertainty

### Plan critic

- Verdict
- Ambiguities and weak assumptions
- Failure cases
- Unnecessary complexity
- Required corrections and missing tests
- Remaining uncertainty

### Worker work log

- Initial hypothesis
- Important files inspected and commands/tests run
- Important discoveries and evidence
- Files changed
- Tests actually run with outcomes
- Plan deviations and failed attempts
- Remaining uncertainty

### Final reviewer

Use `ACCEPT`, `REPAIR`, or `ESCALATE`, and report correctness, contract/boundary changes, test adequacy, unnecessary complexity, plan deviations, required actions, and remaining uncertainty.

## Context discipline

Independent agents begin with fresh context unless prior conversation is genuinely necessary. Do not give every reviewer the entire accumulated history. The normal final-review packet is the original request, approved behavior and boundaries, approved plan, final diff, independent test results, and worker deviations. Inject only useful summaries from BTW.

## Verification

Do not trust a worker's claim that tests passed. Independently inspect committed, staged, unstaged, and untracked changes; changed paths relative to the agreed base; `git diff --check`; diagnostics; acceptance commands; benchmark methodology; and unexpected interfaces, schemas, state, permissions, dependencies, deployment, or compatibility changes. `scripts/pi-verify-change` is the mechanical final evidence gate and must reject out-of-scope files even when reviewer prose calls them harmless.

## Git and recovery

Use ordinary Git branches; do not introduce jj unless Git becomes a measured bottleneck. Agents may create candidate and integration branches. Do not automatically merge, push, publish, deploy, or modify the trusted checked-out branch. Preserve old side-agent branches, worktrees, reports, sessions, sandbox branches, recovery refs, and containers until their value is checked.

## Completion

Before reporting completion, inspect the actual final diff and package inventory, independently rerun important verification, report exact commands and outcomes, name changed boundaries and remaining uncertainty, confirm no remote or production action occurred, and provide rollback commands. Human-facing output is one compact synthesis rather than raw reports and transcripts.
