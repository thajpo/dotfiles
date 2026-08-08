# Pi host-local control-plane target

Status: **non-live controller, C9 deterministic source/process harness, C10 staged/rollback implementation, and C11 blocked runbook are implemented and reviewed; staged/Docker/presentation capability gates and Phase 11D activation remain blocked as specified**.

This directory specifies the intended replacement for the current split state
held by root-session records, task-route files, sandbox refs, child leases,
Docker labels, secretary workstream records, tmux/Herdr process state, and
installed package patches. It is deliberately separate from `../README.md`,
`../SECRETARY_WORKFLOW.md`, and `../MIGRATION.md`, which describe the currently
checked-in or previously activated harness.

No document in this directory grants authority, changes repository trust,
activates code, migrates state, or makes the target behavior true. A phase is
implemented only after its repository tests, disposable process tests,
installed-artifact checks, migration evidence, and activation checks pass.

## Why this exists

The original investigation began with repository tools failing because a stale
`parent-transition.json` made the sandbox believe the parent was moving a Git
ref. Several real lifecycle and permission bugs were corrected, but a later
MLRE child still started from stale commit `868db28` while newer work survived
under another sandbox ref. The recurring pattern was split authority:

- a durable conversation, a fresh route, a worktree path, and a sandbox ref all
  described different notions of "the current work";
- managed linked worktrees inherited policy from their storage path instead of
  their registered repository;
- routes reused a fresh execution identifier as task and session identity;
- root registry, secretary state, sandbox metadata, subagents, Git refs, Docker,
  and presentation backends each retained partial lifecycle state;
- the installed control plane could differ from the repository generation;
- compaction retained a hidden summary for the model but did not give the user
  an equivalent continuity checkpoint.

This target applies conventional local distributed-systems and Git concepts:

- one transactional host-local controller;
- desired and observed state;
- idempotent reconciliation;
- immutable Git commits, trees, and refs for source content;
- resource versions and compare-and-swap;
- kernel locks and monotonically increasing fencing tokens;
- a durable operation journal and transactional outbox;
- immutable run manifests and runtime attestation;
- a local change queue analogous to pull requests or Gerrit patch sets.

It does **not** introduce a proprietary parking format, a network control plane,
a consensus system, or a second source-content store.

## Reading order

1. [`PRODUCT_CONTRACT.md`](PRODUCT_CONTRACT.md)
   — human roles, secretary and `pi-personal` workflows, vocabulary, and UX.
2. [`STATE_CONTRACT.md`](STATE_CONTRACT.md)
   — controller boundary, resources, SQLite schema, invariants, transactions,
   leases, fencing, operations, reconciliation, trust, and adapter ownership.
3. [`EXECUTION_CONTRACT.md`](EXECUTION_CONTRACT.md)
   — run manifests, sandbox/runtime behavior, permissions, exact child state,
   artifacts, cancellation, and restart behavior.
4. [`CHANGE_INTEGRATION_CONTRACT.md`](CHANGE_INTEGRATION_CONTRACT.md)
   — Git change submission, dirty-state capture, patch-set revisions, review,
   conflict analysis, integration, cleanup, and secretary authority.
5. [`OBSERVABILITY_CONTINUITY_CONTRACT.md`](OBSERVABILITY_CONTINUITY_CONTRACT.md)
   — state projection, plain-language failures, audit events, privacy, and
   user-visible compaction continuity.
6. [`CORRECTION_LEDGER.md`](CORRECTION_LEDGER.md)
   — traceability from the observed failures and requested features to the
   contract clauses, implementation phases, and acceptance scenarios.
7. [`MVP_IMPLEMENTATION_PLAN.md`](MVP_IMPLEMENTATION_PLAN.md)
   — original dependency-ordered phases and historical implementation shape.
8. [`COMPLETION_IMPLEMENTATION_PLAN.md`](COMPLETION_IMPLEMENTATION_PLAN.md)
   — canonical remaining-work classification, resolved integration boundaries,
   schema/API additions, and dependency-ordered completion slices.
9. [`IMPLEMENTATION_SLICE_BRIEFS.md`](IMPLEMENTATION_SLICE_BRIEFS.md)
   — worker-sized C0a–C11 handoffs with exact reading, file allowlists,
   algorithms, failures, tests, commands, and stop conditions.
10. [`SYSTEM_INTEGRATION_TEST_PLAN.md`](SYSTEM_INTEGRATION_TEST_PLAN.md)
   — complete configured-harness action catalog, deterministic journey/fault
   coverage, execution tiers, traceability, and evidence requirements.
11. [`MIGRATION_ACTIVATION_PLAN.md`](MIGRATION_ACTIVATION_PLAN.md)
   — inventory, shadow import, cutover, installed-build proof, rollback, and
   legacy-state retention.
12. [`ACCEPTANCE_PLAN.md`](ACCEPTANCE_PLAN.md)
   — component unit/integration/process/crash/concurrency matrices plus
   migration, performance, privacy, and live activation evidence.

## Normative language

The words **MUST**, **MUST NOT**, **REQUIRED**, **SHOULD**, **SHOULD NOT**, and
**MAY** are normative in these documents. If code, tests, and prose disagree,
the implementation phase must stop and the contract must be resolved rather
than selecting the most convenient interpretation.

## Authority summary

| Concern | Authority | Projections/adapters |
|---|---|---|
| Source content | Git objects and refs | working trees, review checkouts, containers |
| Lifecycle relationships and intent | Controller SQLite database | route manifests, labels, session headers, UI |
| Repository trust | Host-owned project policy bound to registered project ID | Pi project-trust hook, sandbox mode |
| Live writer exclusion | Kernel lock plus controller writer epoch | PID/start identity and container labels |
| Conversation content | Pi session JSONL | session selector and continuity view |
| Runtime isolation | Immutable run specification selected by controller | sandbox/container adapter |
| Integration decision | User plus secretary semantic operation | Git merge/update-ref adapter |
| Presentation | None; presentation is not authority | tmux and Herdr |

## Target implementation shape

The MVP uses a Python controller CLI/library and SQLite database under the
user-owned state directory. A resident daemon is explicitly out of the MVP.
Every launcher and extension calls the same controller API. Reconciliation runs
at launch, before consequential operations, after observed failures, and via an
explicit diagnostic command. A later daemon may call the same reconciliation
library without changing resource schemas or authority.

Current candidate source layout (staged but not live-activated):

```text
bin/pi-control
scripts/pi_control/
  __init__.py
  cli.py
  schema.py
  store.py
  models.py
  operations.py
  reconcile.py
  git_adapter.py
  process_adapter.py
  runtime_adapter.py
  migration.py
pi/extensions/control-plane/
pi/extensions/continuity/
pi/packages/pi-sandbox-control/   # first-party maintained fork/adapter
pi/packages/pi-subagents-control/ # first-party maintained orchestrator adapter
                                  # both retain upstream license/provenance
tests/control_plane/
```

Target state layout:

```text
${XDG_STATE_HOME:-~/.local/state}/pi-control/
  control.db
  locks/
  artifacts/
  migrations/
```

The existing `scripts/pi-secretary-control.py`, `scripts/pi-root-session.py`,
`scripts/pi-workspace.py`, and sandbox/subagent packages become compatibility
clients or adapters in staged phases. They must not remain independent writers
of overlapping lifecycle state after cutover.

## Non-goals for the MVP

- no network API, clustering, Raft, or distributed consensus;
- no remote Git hosting or automatic push;
- no automatic merge based only on a green review;
- no automatic cleanup of ambiguous branches, worktrees, containers, sessions,
  refs, or legacy JSON records;
- no tmux-to-Herdr live process migration;
- no general filesystem snapshot format outside Git;
- no arbitrary host shell or Git interface exposed to models;
- no complete semantic-conflict solver;
- no replacement for Pi session JSONL;
- no broad observability rewrite or remote telemetry exporter;
- no claim that a repository commit is active before installed-build checks.

## Implementation handoff rule

A weaker implementation model receives only one phase at a time. Its brief must
include:

- the exact phase goal and prerequisites;
- the relevant clauses from these contracts;
- allowed and unchanged file surfaces;
- schema and operation version under implementation;
- expected failure behavior;
- exact deterministic tests;
- disposable process tests where applicable;
- stop/escalation conditions;
- a requirement to inspect the final diff and preserve unrelated changes.

No worker should receive "implement the control plane" as one assignment.
