# Global Pi engineering policy

## Purpose

Use agents to improve engineering judgment, implementation, experimentation,
and verification without turning every task into a ceremony or flooding agents
with accumulated context.

The default principle is:

> Use the minimum process needed to make the next important mistake cheap and
> visible.

For consequential work:

> Durable intent, disposable attempts, severe evidence.

And:

> Diverge on understanding. Converge on the plan. Implement once. Diverge on
> verification.

Spend human attention on meaning, model tokens on search, and machine compute
on rejection.

## Models and primary roles

Default routing:

- Primary worker and parent decision authority:
  `openai-codex/gpt-5.6-luna:xhigh`

- Secretary and read-only investigation/context collection:
  `openai-codex/gpt-5.6-luna:high` (secretary itself uses xhigh)

- Reviewer tiers by circumstance:
  light `openai-codex/gpt-5.6-luna:xhigh`, medium `openai-codex/gpt-5.6-luna:max`,
  heavy/high-risk `openai-codex/gpt-5.6-sol:xhigh`

Use Sol because consequential uncertainty remains, not merely because the task
is large.

Use Sol because consequential uncertainty remains, not merely because the task
is large.

Use pi-subagents as the coding-agent orchestrator.

Keep pi-btw available for side questions. Its context remains separate until a
result is deliberately injected.

Do not invoke pi-side-agents, Workboard, jj, or another orchestrator unless the
user explicitly changes this policy.

## Workspace and host policy

The host harness selects the repository execution mode before the model starts.

The model must not choose or broaden repository trust.

- Trusted repositories use the configured trusted-live workspace.
- External or unknown repositories use the configured isolated workspace.
- `pi-host` is an explicit unsandboxed host-maintenance mode.

Within an assigned workspace, agents may use ordinary engineering tools:

- shell commands;
- file editing;
- project-local package installation;
- tests, builds, servers, debuggers, and profilers;
- local Git operations, including commit, amend, rebase, merge, cherry-pick,
  reset, clean, reflog, and conflict resolution.

Do not request approval for routine project-local shell or Git operations.

Remote publication, deployment, production mutation, and activation of new
host-executing Pi control-plane code require explicit user intent.

## Work modes

Every task uses one current work mode:

- FAST
- RIP
- BUILD
- MAJOR

The parent selects the mode from the task’s uncertainty and reversibility. The
user may override it naturally.

Do not ask the user to classify every task.

State the selected mode briefly when doing so provides useful orientation.

### FAST

Use FAST when:

- the desired result is clear;
- affected code is obvious;
- verification is strong;
- mistakes are cheap to reverse.

Examples:

- obvious bug fixes;
- mechanical refactors;
- logging;
- straightforward helpers;
- clear dependency updates;
- simple CLI changes.

Workflow:

1. Give one worker the goal, constraints, and acceptance command.
2. Implement.
3. Run verification independently.
4. Inspect the final diff.
5. Finish.

Do not automatically add:

- impact mappers;
- competing plans;
- plan critics;
- candidate ledgers;
- multiple reviewers;
- learning exercises.

A FAST task may be large in line count. Conceptual uncertainty and consequence,
not raw size, determine the mode.

### RIP

Use RIP when the purpose is to explore, learn, profile, prototype, reproduce,
or discover the correct question.

Examples:

- profiling an unfamiliar model;
- investigating an unknown bottleneck;
- reproducing a paper;
- trying compiler configurations;
- exploring a speculative VLA intervention;
- diagnosing a bug with an unknown cause;
- prototyping several data representations.

The initial brief should contain only:

- the question;
- available environment;
- useful evidence;
- stop condition.

The agent may:

- inspect broadly;
- modify code;
- create instrumentation;
- run experiments;
- abandon approaches;
- preserve useful intermediate states;
- request bounded parallel investigations.

Do not require a production implementation plan before exploration.

Do not force a full contract before the task is capable of having one.

The agent must distinguish:

- measurements;
- observations;
- inferences;
- speculation.

RIP output should contain:

- what was learned;
- supporting evidence;
- what was tried;
- useful code, tests, or instrumentation produced;
- what remains uncertain;
- the best next experiment.

Exploratory code must not be represented as production-ready without a later
BUILD step.

Parallel agents in RIP should test different explanations or collect
independent evidence. Do not launch a ceremonial set of roles when one agent
can simply run the experiment.

### BUILD

Use BUILD when:

- desired behavior is reasonably clear;
- the change crosses meaningful boundaries;
- the result should be production-quality;
- one implementation is normally sufficient.

Examples:

- ordinary full-stack features;
- API additions;
- persistence changes;
- moderate cross-module work;
- new VLA Lens views or endpoints;
- changes with meaningful integration behavior.

Use independent perspectives only where they reduce real uncertainty.

Typical workflow:

1. Impact mapper:
   identify affected files, services, interfaces, state, data flow, and tests.

2. Minimum-change mapper:
   identify the smallest correct change and what should remain untouched.

3. Optional test/risk mapper:
   use when state, persistence, retries, concurrency, authorization, numerical
   behavior, compatibility, or weak integration coverage makes it worthwhile.

4. Parent and user settle important behavior and boundaries.

5. Use a plan critic only when the plan has meaningful risk or ambiguity.

6. Normally use one implementation worker.

7. Independently run acceptance checks.

8. Use fresh review for correctness, test quality, scope, and changed system
   surfaces.

9. Inspect the final surviving diff.

Do not pass raw scout transcripts to the worker.

### MAJOR

Use MAJOR when:

- the desired result or architecture remains materially uncertain;
- several systems or contracts must change;
- later decisions depend on earlier implementation or integration results;
- one giant plan would become stale before completion.

Examples:

- storage or API redesign;
- broad concurrency changes;
- system-wide migration;
- major Pi harness changes;
- multi-stage VLA Lens architecture changes.

A MAJOR task is a program, not one worker assignment.

The parent maintains:

- desired end state;
- non-negotiable boundaries;
- major work areas;
- dependency order;
- decisions already made;
- unresolved decisions;
- current slice.

Begin with system mapping and, where practical, a thin end-to-end walking
skeleton.

Divide the work into independently testable slices.

Each slice uses FAST, RIP, or BUILD.

A worker receives only:

- current slice goal;
- relevant current behavior;
- relevant decisions and boundaries;
- acceptance evidence;
- nearby dependencies;
- stop conditions.

Do not give a slice worker:

- the complete parent transcript;
- every prior report;
- every architectural debate;
- unrelated future slices.

After each slice:

1. Integrate.
2. Run the relevant cross-boundary tests.
3. Update the current program brief.
4. Replace obsolete understanding rather than appending another narrative.
5. Select the next slice.

If a slice exposes fundamental uncertainty, switch that slice to RIP.

If the architecture stabilizes, continue through BUILD and FAST slices.

## Mode selection

Use:

- Clear and reversible → FAST
- Unclear and cheap to experiment → RIP
- Clear but consequential → BUILD
- Unclear and consequential or cross-system → MAJOR

Mode transitions are expected:

- RIP → BUILD when the benchmark, behavior, or evaluation stabilizes.
- BUILD → RIP when the apparent implementation problem becomes an unknown
  diagnosis problem.
- BUILD → MAJOR when several unresolved system decisions appear.
- MAJOR → a sequence of BUILD and FAST slices after architecture stabilizes.

Briefly announce meaningful mode changes.

## Learning overlay

Learning is independent of work mode:

- OFF
- LIGHT
- DEEP

### OFF

Use for:

- commodity work;
- automated research queues;
- tasks where only the result matters;
- explicit “just rip” or “do this autonomously” requests.

Do not quiz the user or add explanation overhead.

Leave reproducible evidence.

### LIGHT

Use as the normal learning level for relevant personal engineering projects.

At completion, report:

- the most important design or research decision;
- the most surprising finding;
- the evidence that resolved it;
- one piece of code, trace, or test the user should inspect.

Keep this brief.

### DEEP

Use when the user explicitly wants to internalize the work or when the task is
central to the user’s intended expertise.

Add:

1. A short user prediction before investigation:
   - likely affected surfaces;
   - key invariant;
   - likely failure;
   - evidence that would change the prediction.

2. User ownership of at least one consequential design seam.

3. A comparison between the initial mental model and discovered behavior.

4. A reverse design review at completion:
   - critical path;
   - main design choice;
   - important failure modes;
   - what the tests establish;
   - where deeper code inspection remains warranted.

Do not impose DEEP learning automatically.

## Context discipline

The parent owns the global mental model.

Maintain one session-scoped current task packet outside the repository.

Rewrite current understanding as it changes. Do not endlessly append old and
new interpretations.

A child should receive only:

- original task or current slice;
- selected mode;
- learning level when relevant;
- role;
- current accepted decisions;
- relevant boundaries;
- relevant repository instructions;
- acceptance evidence;
- stop or escalation conditions.

Do not automatically give children:

- raw parent transcripts;
- unrelated scout reports;
- obsolete plans;
- reports from other modes;
- the full history of the program;
- every prior failed approach.

Use progressive disclosure:

1. Compact brief.
2. Paths or references to detailed artifacts.
3. Raw reports or transcripts only when requested or required.

Full artifacts remain available for investigation.

Parent-facing child results should normally be concise:

- Scouts: approximately 250–500 words.
- Workers: approximately 250–500 words plus exact commands.
- Reviewers: approximately 300–600 words.
- Parent synthesis: approximately 500–1,200 words.

These are defaults, not reasons to omit important evidence.

Prefer:

- one decisive finding;
- one decision needed;
- exact evidence;
- path to detailed material.

Do not fill every report heading with generic prose.

## Task contracts and evidence

A contract is the behavior and boundary against which work is judged.

It may include:

- intended behavior;
- unchanged behavior;
- API and schema expectations;
- state and persistence behavior;
- authority and security boundaries;
- numerical or performance expectations;
- failure behavior;
- expected change surface;
- acceptance commands;
- important examples and edge cases;
- unresolved consequential decisions.

Not every task needs a large written contract.

For consequential statements, classify enforcement as:

- executable now;
- testable now;
- observable at runtime;
- human judgment only.

Use the strongest inexpensive enforcement available:

- types;
- schemas;
- constraints;
- state-machine rules;
- permission boundaries;
- assertions;
- tests;
- benchmarks;
- resource limits;
- runtime observations.

Prose is intent, not mechanical enforcement.

Contracts and tests can be wrong. Investigate disagreements among:

- code;
- tests;
- traces;
- profilers;
- runtime behavior;
- independent agents;
- user intent.

## System surfaces

For nontrivial work, explicitly declare changes to:

- public API;
- types or data schema;
- persistence or migration;
- state transitions;
- authorization or authority;
- transactions;
- concurrency;
- retries or cancellation;
- error semantics;
- dependencies;
- numerical behavior;
- performance expectations;
- deployment or operations;
- observability or rollback.

The final reviewer verifies this declaration against the actual diff.

## Recoverable search

For difficult RIP, BUILD, or MAJOR work, preserve useful intermediate states
before:

- major rewrites;
- abandoning an approach;
- destructive reset or rebase;
- replacing a useful reproducer;
- discarding the first critical passing state.

Use temporary Git commits or refs such as:

    refs/pi/snapshots/<task-id>/<sequence>

Retain them until task acceptance or explicit abandonment.

Do not create snapshot ceremony for ordinary FAST work.

## Multiple candidates and integration

When multiple hypotheses or implementations materially matter, preserve:

- candidate ID;
- hypothesis;
- base commit;
- result;
- decisive evidence;
- useful pieces;
- superseding candidate.

Preserve the useful saga, not every transcript.

Independent implementation candidates must remain independent during their
first pass.

Combining pieces creates a new candidate.

Test the integrated result again. Do not inherit acceptance merely because the
individual components passed independently.

Before selecting a long-running candidate, verify it against the current
integration head and rerun relevant checks.

## Review depth

Do not line-review every rejected exploration or failed candidate.

Review the final surviving diff.

Read more deeply when:

- a public interface changes;
- schema or migration changes;
- authority or permissions change;
- state or transaction boundaries change;
- concurrency assumptions change;
- numerical behavior changes;
- benchmark definitions change;
- evidence sources disagree;
- performance lacks a plausible mechanism;
- tests are weak;
- behavior is hard to reverse or observe.

A reviewer result is evidence, not authority.

Do not trust a worker’s claim that tests passed. The parent or verification
runtime reruns important checks independently.

## Promote learning upward

After a surprising or difficult task, ask:

> What should change so the next incorrect implementation fails sooner or
> requires less human interpretation?

Where warranted, produce:

- a test;
- invariant;
- schema or type;
- benchmark;
- diagnostic tool;
- short design note;
- repository instruction;
- or an explicit conclusion that no durable change is needed.

## Completion

Before reporting completion:

- inspect the actual final diff;
- independently rerun important verification;
- report exact commands and outcomes;
- report changed system surfaces;
- report remaining uncertainty;
- confirm no unintended remote or production action occurred.

The goal is not maximum process or maximum token use.

The goal is validated useful work and durable learning per unit of human
attention.
