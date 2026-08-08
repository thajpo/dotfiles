# Phased control-plane MVP implementation plan

Status: **historical phase plan plus target architecture; substantial component source exists, but the active harness is not fully controller-integrated or live. Use `COMPLETION_IMPLEMENTATION_PLAN.md` for canonical remaining work and do not infer completion from this document alone**.

Audience: implementation agents that may not retain the architecture discussion.
Each phase is intentionally explicit and bounded. Do not combine phases merely
because adjacent files overlap.

## 1. Desired end state

A thin walking skeleton proves both workflows:

```text
pi-personal in primary checkout
  -> exact run and child
  -> submit immutable local change
  -> secretary lists/analyzes it
  -> user authorizes integration
  -> target CAS update
  -> restart preserves state

secretary creates separate worktree/headful agent
  -> exact run and child
  -> same local change queue
  -> same integration path
```

The walking skeleton is complete only after process-level crash/restart tests
and installed-artifact canary evidence. Rich dashboards, automatic cleanup,
cross-run container reuse, and patch consolidation are later.

## 2. Global implementation rules

Every phase worker MUST:

1. read this phase plus referenced contract sections;
2. inspect current implementation before editing;
3. preserve unrelated dirty changes;
4. use one writer in the assigned worktree;
5. avoid remote operations, host activation, or live state mutation unless the
   phase explicitly reaches reviewed activation;
6. add deterministic tests before claiming behavior;
7. use failpoints for crash-sensitive code;
8. inspect final diff and run exact acceptance commands;
9. report changed system surfaces;
10. stop on a contract ambiguity rather than inventing a convenience rule.

No phase may silently modify existing user state under `~/.pi`, XDG runtime or
state directories, Docker, tmux, Herdr, sessions, refs, or installed packages
during repository tests. Use temporary HOME/XDG roots and disposable Git repos.

## 3. Program dependency graph

```text
Phase 0 Contracts accepted
        |
Phase 1 Test/failpoint substrate
        |
Phase 2 Secure SQLite core
        |
Phase 3 Project + working-copy inventory/reconciliation (read-only)
        |
Phase 4 Conversations + run manifests + writer fencing
        |
Phase 5 Runtime/sandbox attestation
        |
Phase 6 Exact child execution and artifact contract
        |
Phase 7 Local change submission
        |
Phase 8 Secretary + pi-personal clients
        |
Phase 9 Integration queue and CAS mutation
        |
Phase 10 Continuity/UX + complete walking skeleton
        |
Phase 11 Shadow migration, canary activation, rollback proof
        |
Later: consolidation, reuse, dashboards, cleanup automation
```

Phases 5 adapter subparts may be parallel after Phase 4 interfaces freeze.
Phases 7 UI-independent capture and Phase 10 continuity may be developed in
parallel only after their shared schema/API versions freeze. Integration occurs
once through the parent/secretary and reruns all cross-boundary tests.

### 3.1 File allowlist rule

Within each phase, **Expected new files** and **Expected surfaces** form the
initial write allowlist, not a suggestion. All unlisted repository files and all
host/runtime state are unchanged by default. If inspection proves another file
is required, the worker stops and requests a scoped allowlist amendment with
reason, dependency, and tests. It does not opportunistically edit adjacent
launchers/docs/patches.

Current-state docs (`pi/README.md`, `pi/SECRETARY_WORKFLOW.md`, `pi/MIGRATION.md`,
`pi/WORKFLOW_ACCEPTANCE.md`) remain unchanged until a phase explicitly reaches
activated behavior and names the exact update.

### 3.2 Phase-0 decision matrix

No implementation worker chooses these seams. Phase 0 records one decision and
evidence in the normative contract before the blocking phase starts.

| Decision | Recommended default | Blocks | Evidence required |
|---|---|---|---|
| DB path/store | `${XDG_STATE_HOME}/pi-control/control.db`; secure local SQLite | Phase 2+ | permission/locking review |
| Controller lifecycle | per-command Python library/CLI; no daemon | Phase 2+ | crash/reconcile design accepted |
| IDs/schema | `STATE_CONTRACT.md` schema v1 | Phase 2+ | DDL parses on supported SQLite; independent review |
| SQLite runtime | SQLite >=3.40.0 plus capability preflight | Phase 2+ | deployment Python/SQLite check |
| Project rebind proof | inode/path when available plus object/ref anchors and explicit decision when incomplete | Phase 3+ | replacement/copy/move fixtures |
| Personal default | explicit primary checkout; separate worktree creates separate conversation | Phase 8 | product confirmation |
| Direct-mount writer revocation | no new writer until old proxy/container write access proven gone | Phase 4+ | process/container fault model |
| Runtime adapter source | First-party `pi/packages/pi-sandbox-control/` package, derived once from the reviewed final patched `@kjrjay/pi-sandbox` source, retaining license/upstream provenance; installed as an exact local file dependency and thereafter changed as normal source, not a new patch chain | Phase 5+ | package API/version/hash/rollback review before extraction |
| Subagent adapter source | First-party `pi/packages/pi-subagents-control/` package, mechanically derived once from reviewed final patched `pi-subagents@0.35.1`, retaining provenance and exact source manifest | Phase 6B+ | package API/version/hash/rollback review before extraction |
| Read-only child mechanism | immutable snapshot plus read-only runtime/no mutation tools | Phase 6 | threat/tool-boundary review |
| Dirty path attribution | task baseline + task delta; ambiguous files require explicit selection; ignored excluded by default | Phase 7B+ | dirty fixture review |
| Change ref namespace | `refs/pi/changes/<id>/<revision>` immutable | Phase 7+ | Git/ref retention review |
| Review policy | risk/project-policy selected; never merge authority | Phase 9A+ | authority review |
| Non-fast-forward | separate integration worktree in MVP | Phase 9D+ | target safety review |
| Migration precedence | typed precedence in migration contract; no timestamps | Phase 11A+ | shadow contradiction fixtures |
| Live activation selector | host-owned per-project activation record (`legacy|shadow|controller`); no model/caller environment override; test-only env permitted in disposable fixtures | Phase 8/11 | security review |
| Continuity idempotency | `(sessionId, compactionEntryId)` persisted-entry reconciliation | Phase 10A | compaction crash test design |

A decision may be deferred only if every phase it blocks remains blocked.

### 3.3 Versioned interface freezes

Before parallel children edit adapters, the parent commits or records a reviewed
interface artifact containing:

```text
schema version and migration digest
controller JSON request/response schemas
stable error codes
run-manifest/runtime-spec/attestation versions
adapter observe/plan/apply protocol
snapshot/artifact manifests
change/revision/authorization schemas
control event envelope
```

An interface change creates a new reviewed version or coordinates all consumers
in one sequential slice. Parallel children do not edit shared interfaces during
first pass.

### 3.4 Parallel ownership matrix

| Surface | Primary phase owner | Later consumer; rule |
|---|---|---|
| `scripts/pi_control/schema.py`, `store.py`, `models.py` | Phase 2 | Later phases consume frozen API; schema changes return to owner slice |
| `git_adapter.py`, `project_policy.py`, `reconcile.py` | Phase 3 | Phases 7/9 add separate modules, not concurrent edits |
| `session_adapter.py`, `run_manifest.py`, `leases.py` | Phase 4 | Phases 5/6 consume frozen versions |
| selected sandbox package/source and `runtime_adapter.py` | Phase 5 | Phase 8 does not edit these concurrently |
| snapshot/child/artifact modules and subagent adapter | Phase 6 | Phase 7 reuses snapshot API without editing it |
| `changes.py`, control-plane submit extension | Phase 7 | Phase 8 client wiring waits for merge/freeze |
| `bin/pi`, personal/root clients | Phase 8P | secretary files excluded |
| secretary extension/control facade | Phase 8S, then 9 | 8S completes before Phase 9 edits |
| integration/review modules | Phase 9 | no concurrent secretary edits |
| continuity/observability UI | Phase 10A/B | may parallel only in disjoint files after event API freeze |
| migration/install/activation | Phase 11 | no adapter worker modifies install surfaces concurrently |

Any overlap not listed is sequential by default.

### 3.5 Phase handoff matrix

This matrix is copied into each worker brief. `CP` means this directory.

| Phase | Required contract sections | Frozen output/API | Minimum acceptance command |
|---|---|---|---|
| 0 | all CP docs | accepted decisions/contracts | `git diff --check -- pi/control-plane` plus independent reviews |
| 1 | Acceptance §§1-2,7; State §§9,12 | fixture/failpoint API v1 | `python3 -m unittest tests.control_plane.test_failpoints` |
| 2 | State §§4-9,14-15; Acceptance §4 | schema/store/operation/event v1 | controller store/schema/operation/event tests |
| 3 | State §§2,5-7,11-13; Migration §§2-6 | Git/project/observation adapter v1 | identity/trust/reconcile/legacy-inventory tests |
| 4 | State §§6.4-6.5,7-10; Execution §§3-4,11 | conversation/run/manifest/lock v1 | conversation/manifest/fencing/process tests |
| 5 | Execution §§4-7,10,13; Acceptance §8 | runtime-spec/attestation adapter v1 | runtime unit tests, then disposable Docker script |
| 6 | Execution §§8-9; Acceptance §9 | snapshot/child/artifact v1 | snapshot/child/artifact unit and process tests |
| 7 | Change §§4-10; State §6.6; Acceptance §10 | change/revision submit API v1 | clean then dirty submission/failpoint tests |
| 8 | Product §§2-6,8-11; Observability §§7-10 | client protocol/activation lookup v1 | personal/secretary client and extension tests |
| 9 | Change §§11-20; State §§6.7-6.8; Acceptance §12 | analysis/auth/integration v1 | analysis, auth, CAS, recovery tests by subphase |
| 10 | Product §§3,9-10; Observability; Acceptance §§13,17 | continuity/error/Inspector projections v1 | extension tests then walking-skeleton script |
| 11 | Migration; Acceptance §§14,17-19 | migration/build/activation records v1 | inventory/import/build tests; 11D reviewed runbook |

Before a command name exists, the phase adds the exact test module named in its
section and records its invocation. It does not claim this placeholder matrix
ran.

## Phase 0 — Contract review and decision freeze

### Goal

Accept or amend the documents in this directory before runtime implementation.

### Required decisions

- controller DB path and secure directory policy;
- Python library/CLI as MVP, no resident daemon;
- resource IDs and schema v1;
- project trust inheritance;
- personal primary-checkout versus separate-worktree semantics;
- one-writer scope and direct-mount limitations;
- dirty personal submission inclusion/selection policy;
- change-ref namespace;
- MVP non-fast-forward policy (integration worktree recommended);
- review requirement policy versus integration authorization;
- migration source precedence and no-dual-writer boundary;
- compaction continuity card contents/privacy.

### Deliverables

- reviewed contract diff;
- decision log embedded in contracts;
- unresolved items explicitly deferred without blocking Phase 1;
- one traceability pass against `CORRECTION_LEDGER.md`.

### Acceptance

- independent architecture review finds no contradictory authority;
- independent implementation-plan review can describe the walking skeleton and
  state transitions without hidden conversation context;
- current-behavior docs remain unmodified or clearly current-state;
- `git diff --check` passes.

### Stop conditions

- Git versus SQLite authority ambiguous;
- writer revocation over trusted-live mount falsely claimed;
- target mutation authorization underspecified;
- dirty-state capture cannot define included content;
- migration would require destructive cleanup.

## Phase 1 — Test harness, fixtures, and failpoint substrate

### Goal

Make state-machine and crash errors cheap to reproduce before controller code
has external side effects.

### Expected new files

```text
tests/control_plane/__init__.py
tests/control_plane/helpers.py
tests/control_plane/fake_clock.py
tests/control_plane/fake_adapters.py
tests/control_plane/test_failpoints.py
tests/fixtures/control-plane/README.md
```

Potential reusable helper command:

```text
scripts/pi-control-test-fixture.py
```

### Implementation steps

1. Add disposable environment helper creating:
   - temporary HOME, XDG_STATE_HOME, XDG_RUNTIME_DIR;
   - bare/normal SHA-1 Git fixture; optional SHA-256 when installed Git supports;
   - primary checkout and linked worktrees;
   - synthetic Pi session files;
   - fake process/runtime/presentation adapters.
2. Sanitize inherited `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, index, object
   directory, hooks, config, pager, editor, diff, and terminal-prompt variables.
3. Add deterministic RFC3339 and monotonic fake clock interfaces. Correctness
   may not rely on wall-clock expiry alone.
4. Add named failpoints usable immediately before and after every planned
   external side-effect boundary. API v1:

   ```python
   class FailpointController(Protocol):
       def hit(self, name: str, context: Mapping[str, str]) -> None: ...
   ```

   Production default is a no-op. Tests select exactly one action (`raise`,
   `os._exit(<code>)`, or callback) by constructor injection; no unrestricted
   environment-controlled failpoint is enabled in installed code. Names use
   `<operation>.<step>.before|after`, including:
   `operation.intent`, `lock.acquire`, `git.ref_update`, `worktree.create`,
   `manifest.write`, `runtime.create`, `runtime.stop`, `snapshot.ref`,
   `change.revision`, `integration.target_ref`, `integration.index`, and
   `event.commit`. Subprocess-kill tests use a child controller process and
   never kill an arbitrary host PID.
5. Add helpers to snapshot:
   - SQLite rows;
   - Git refs/OIDs/worktree list/status;
   - filesystem entries/permissions/inodes;
   - fake process/container observations;
   - emitted control events.
6. Add assertion that repository-under-test and real HOME remain untouched.
7. Add process helper that terminates a controller subprocess at one failpoint
   and invokes reconcile in a fresh process.

### Tests

- fixture creates and destroys without touching real repo/home;
- failpoint fires once and at named boundary;
- fake clock cannot make monotonic comparisons across process origin silently;
- Git environment sanitizer blocks config/hook injection;
- file permission assertions work on Linux/macOS-supported paths;
- kill/restart helper records exact before/after state.

### Acceptance command target

```bash
python3 -m unittest tests.control_plane.test_failpoints
python3 -m unittest discover -s tests/control_plane -p 'test_*.py'
```

Phase 1 report includes the full registered failpoint-name list and proves
production construction uses the no-op controller.

### Stop conditions

- tests require live Docker/real sessions for core state transitions;
- failure injection cannot distinguish before versus after side effect;
- tests mutate current worktree refs or host state.

## Phase 2 — Secure SQLite controller core

### Goal

Implement schema, transaction, resource-version, operation journal, control
event, and idempotency foundations with fake adapters only.

### Expected new files

```text
bin/pi-control
scripts/pi_control/__init__.py
scripts/pi_control/cli.py
scripts/pi_control/schema.py
scripts/pi_control/store.py
scripts/pi_control/models.py
scripts/pi_control/operations.py
scripts/pi_control/events.py
scripts/pi_control/errors.py
tests/control_plane/test_store.py
tests/control_plane/test_schema_migrations.py
tests/control_plane/test_operations.py
tests/control_plane/test_events.py
```

### CLI v1 read-only/admin surface

```text
pi-control schema status --json
pi-control project list --json
pi-control operation list --json
pi-control event list --after <sequence> --json
```

Mutation APIs remain library/test-only until later phases or require temporary
fixture state root.

### Implementation steps

1. Before creating application state, require SQLite >=3.40.0 and probe
   `STRICT`, `json_valid`, partial indexes, triggers, and foreign keys. Return
   `CP_SQLITE_UNSUPPORTED` without application schema/state mutation on failure.
2. Secure state-root creation and lstat/ownership/mode checks.
3. Set umask before SQLite open; initialize/verify pragmas.
4. Implement ordered migration modules/files such as
   `scripts/pi_control/migrations/v001_initial.py`; each exports version, name,
   source digest, and one `apply(connection)` that performs only SQLite work.
   Phase 2 creates exactly the tables/indexes/triggers shown in State §6:
   `control_meta`, `schema_migrations`, `installed_builds`, `projects`,
   `working_copies`, `conversations`, `runs`, `changes`, `change_revisions`,
   `change_revision_inputs`, `reviews`, `integration_attempts`,
   `authorizations`, `operations`, `control_events`, `event_consumers`,
   `attention`, `migration_runs`, and `migration_manifests`.
5. Implement schema exactly from `STATE_CONTRACT.md`, with checks/triggers for
   role and writer constraints.
6. Add explicit migration checksums and `user_version` handling.
7. Add transaction context:
   - `BEGIN IMMEDIATE` for mutation;
   - rollback on every exception;
   - no external adapter call while transaction open.
8. Add resource-version CAS helpers returning typed stale-resource errors.
9. Add idempotent operation creation:
   - same key + same canonical request returns existing operation;
   - same key + different digest errors.
10. Add state mutation plus control event in one transaction.
11. Add consumer cursor/dedup helpers.
12. Add error-code and bounded-detail types.
13. Add safe JSON canonicalization/schema checks; no arbitrary Python pickles.
14. Record controller build/schema metadata.
15. Make run creation transactionally reject a build not in `active` state;
    test the active-writer trigger/claim lifecycle.

### Tests

- database/WAL/SHM parent permissions and symlink rejection;
- SQLite <3.40 or missing capability fails before application state mutation;
- foreign keys and check constraints;
- migration request/manifest immutability and idempotency;
- concurrent `BEGIN IMMEDIATE` behavior and busy error translation;
- CAS success/stale conflict;
- idempotent retry/different-request rejection;
- transaction rollback leaves no event without state and no state without event;
- duplicate event consumption;
- unknown newer schema refusal;
- migration interruption and idempotent retry;
- no raw forbidden fixture secrets in DB bytes/rows;
- 100-resource metadata query benchmark baseline.

### Acceptance commands

```bash
python3 -m unittest \
  tests.control_plane.test_store \
  tests.control_plane.test_schema_migrations \
  tests.control_plane.test_operations \
  tests.control_plane.test_events
python3 -m unittest discover -s tests/control_plane -p 'test_*.py'
```

### Acceptance

- all Phase 1/2 tests pass repeatedly;
- database corruption/newer schema fails closed with actionable code;
- no external Git/process/container effect is implemented yet;
- code coverage includes each constraint/error path where inexpensive.

### Stop conditions

- side effects placed inside SQLite transaction;
- generic arbitrary operation kind/payload can reach a shell;
- event delivery assumed exactly-once;
- TTL alone used as live lease proof.

## Phase 3 — Project and working-copy adapters, read-only reconciliation

### Goal

Inventory and reconcile projects and Git working copies without changing Git or
legacy state. Correct path-derived trust in the controller model.

### Expected files

```text
scripts/pi_control/git_adapter.py
scripts/pi_control/project_policy.py
scripts/pi_control/reconcile.py
scripts/pi_control/legacy_inventory.py
scripts/pi_control/locks.py

tests/control_plane/test_project_identity.py
tests/control_plane/test_working_copy_reconcile.py
tests/control_plane/test_trust.py
tests/control_plane/test_legacy_inventory.py
```

Read-only inspections of existing code:

```text
scripts/pi-root-session.py
scripts/pi-workspace.py
scripts/pi-secretary-control.py
pi/repository-policy.json
```

Do not alter their write behavior yet.

### CLI v1

```text
pi-control project register --repository <path> --name <name> --state-root <fixture>
pi-control project inspect <project-id> --json
pi-control working-copy inventory <project-id> --json
pi-control inspect <project-id> --json
pi-control reconcile <project-id> --observe-only --json
pi-control legacy inventory --json
```

Real-state registration remains behind explicit fixture/test scope until
migration phase. `inspect` performs no DB or external mutation.
`reconcile --observe-only` may transactionally persist normalized observations
and events in the disposable/shadow controller DB but performs no external
repair. There is no ambiguous `--dry-run` mode.

Project-policy input schema in this phase is the currently supported host-owned
version-1 policy (`defaultMode`, trusted/isolated/control-plane roots, protected
branches, worktree root). Unknown fields/version fail closed. The adapter
returns a normalized policy hash; it never edits policy.

### Implementation steps

1. Sanitize every Git subprocess environment.
2. Validate canonical top-level, common dir, Git dir, object format, current
   branch/ref, HEAD/tree, worktree list, and status.
3. Assign stable project ID; paths remain locators.
4. Implement explicit repository rebind plan and proof; do not auto-rebind.
5. Load host policy and bind trust/policy hash to project.
6. Prove linked worktree belongs to project common dir before inheriting trust.
7. Permit effective mode only equal/more restrictive than project trust.
8. Inventory unmanaged Git worktrees as observations without adopting them.
9. Implement pure observation and classification:
   ready/drifted/missing/ambiguous/error.
10. Import legacy root/secretary/route records into a read-only inventory model
    with source path, digest, and contradictions; do not choose silently.
11. Persist observations/events, not mutation.
12. Add ordered project/working-copy lock helpers but use only shared/read
    observation locks in this phase.

### Tests

- primary and linked worktree identity;
- managed path outside trusted root inherits registered project trust;
- unrelated repo placed under managed root is rejected;
- explicit isolated working copy narrows trust;
- project move/rebind requires exact proof and increments version;
- SHA-1 and supported SHA-256 OID validation;
- dirty/staged/untracked/conflicted/detached/submodule observations;
- missing path versus adapter error versus ambiguous duplicate;
- malicious Git env/config/hooks cannot execute;
- symlink/path replacement failure;
- legacy registries disagreeing on worktree/OID produce contradiction report;
- pure `inspect` changes neither DB nor external state;
- `reconcile --observe-only` changes only shadow/controller observation/event
  rows and makes no filesystem/Git/session/process/container change.

### Acceptance commands

```bash
python3 -m unittest \
  tests.control_plane.test_project_identity \
  tests.control_plane.test_working_copy_reconcile \
  tests.control_plane.test_trust \
  tests.control_plane.test_legacy_inventory
```

### Acceptance

- no Git ref/worktree/session/legacy file mutation;
- trust result no longer depends on managed worktree storage path alone;
- all observations include freshness/provenance;
- unknown does not become missing/ready;
- correction ledger ID-002 is test-proven in controller layer only, not yet
  activated.

## Phase 4 — Conversations, runs, manifests, locks, and fencing

### Goal

Create controller-owned conversation bindings and immutable run manifests with
one writer per working copy. Continue using fake runtime adapter.

### Expected files

```text
scripts/pi_control/session_adapter.py
scripts/pi_control/run_manifest.py
scripts/pi_control/process_adapter.py
scripts/pi_control/leases.py

tests/control_plane/test_conversations.py
tests/control_plane/test_run_manifest.py
tests/control_plane/test_writer_fencing.py
tests/control_plane/test_process_identity.py
```

Compatibility design targets:

```text
scripts/pi-root-session.py
pi/extensions/root-session/index.ts
bin/pi
```

Do not cut them over yet.

### Implementation steps

1. Register/import conversation ID and exact session file; validate session
   header but do not derive binding from cwd.
2. Enforce role/working-copy constraints.
3. Create secure runtime lock directory and working-copy lock files.
4. Implement lifetime `flock` writer handle owned by launcher/controller child
   process.
5. Under lock and `BEGIN IMMEDIATE`, increment writer epoch and create run.
6. Generate capability secret/hash and immutable canonical manifest.
7. Record expected project/working-copy versions, branch, HEAD/tree/dirty
   fingerprint, runtime spec hash, and active build.
8. Verify manifest permissions/content digest.
9. Add per-tool/bridge fence-check API v1:
   `check_run_authority(run_id, working_copy_id, writer_epoch,
   expected_resource_version, operation_kind)`. Initial call sites are every
   controller Git mutation and the compatibility tool-proxy boundary; later
   runtime/subagent adapters consume the frozen API. The raw capability secret
   is held only in the secure manifest-launch handoff/process environment or a
   dedicated `0600` runtime file, never SQLite, session JSONL, or project
   source; only its hash persists.
10. Implement graceful run stop/release and lost-owner reconcile with
    PID+start identity.
11. Explicitly refuse new writer if old writable runtime/process access is
    unknown.
12. Add operation context/lock ordering; helpers never reacquire transition.
13. Add compatibility projection interface that can later produce current task
    route without changing identity.

### Tests

- secretary has project/no writable working copy;
- personal explicit primary assignment;
- workstream exact separate assignment;
- session cwd disagreement does not rebind;
- same working copy second writer blocked;
- PID reuse rejected;
- epoch advances only under lock;
- stale epoch cannot call mutation API;
- unknown old runtime blocks new writer;
- manifest deterministic digest, strict schema, secure file;
- separate run ID on restart, same conversation/working copy;
- route filename/task ID cannot alter manifest source;
- non-reentrant nested operation prevented structurally;
- kill at each lock/DB/manifest boundary and reconcile.

### Acceptance commands

```bash
python3 -m unittest \
  tests.control_plane.test_conversations \
  tests.control_plane.test_run_manifest \
  tests.control_plane.test_writer_fencing \
  tests.control_plane.test_process_identity
```

### Acceptance

- fake process/runtime walking test rejects stale MLRE-like expected/actual OID;
- no real Pi/Docker cutover;
- writer fencing limitations for direct mounts remain explicit;
- current marker regressions continue passing.

## Phase 5 — Runtime and sandbox attestation adapter

### Goal

Make `pi-sandbox` a manifest-driven runtime adapter. Prove UID/GID, image,
mount, Git, and source state before tools.

Execute sequentially:

- **5A — Pure runtime specification:** canonical schema/hash and fake
  attestation; no package or Docker mutation.
- **5B — Selected adapter implementation:** extract the reviewed final patched
  upstream source once into first-party `pi/packages/pi-sandbox-control/`,
  preserve license/provenance, define package/API version, add exact local file
  dependency/lock entry, and implement the manifest adapter there. The worker
  does not choose between patching and replacement.
- **5C — Disposable Docker proof:** real create/attest/tool/stop fixtures.
- **5D — Staged installed artifact:** package/image/final hashes and rollback in
  staging, not live activation.

Each subphase has its own diff/review. 5B cannot begin while runtime-adapter
source is open.

### Expected surfaces

5A writes only controller runtime-spec/test files. 5B's exact allowlist adds the
new first-party package, package/lock manifests, and a one-time extraction
provenance manifest. Existing installed/node_modules source is read-only input;
it is not edited as the maintained target. Transitional activation remains
hash-gated until cutover.

```text
scripts/pi_control/runtime_adapter.py
scripts/pi-runtime.py
scripts/pi-workspace.py                 # compatibility client, staged
pi/packages/pi-sandbox-control/package.json
pi/packages/pi-sandbox-control/LICENSE
pi/packages/pi-sandbox-control/UPSTREAM.md
pi/packages/pi-sandbox-control/src/**
pi/npm/package.json
pi/npm/package-lock.json
pi/sandbox/Dockerfile
tests/control_plane/test_runtime_spec.py
tests/control_plane/test_runtime_attestation.py
tests/pi-docker-control-plane-e2e.sh
```

### Implementation steps

1. Canonical runtime-spec serializer/hash.
2. Resolve image repository+digest and installed controller build.
3. Manifest-driven container create with labels as observations.
4. No cross-run container reuse in v1.
5. Mount exact controller paths/modes and required Git metadata.
6. Create runtime-private tmpfs/cache paths with expected UID/GID.
7. Run independent attestation inside and outside container.
8. Return attestation to controller; do not mark ready in sandbox extension.
9. Tool proxy checks run/epoch before every mutation-capable call.
10. Stop/reconcile old writable runtime before new writer.
11. Sandbox emits machine events; remove healthy container-name footer leakage.
12. Keep isolated mode and control-plane read-only resources within existing
    least-privilege boundaries.
13. Preserve no host Docker socket, credentials, home, or unrelated mounts.

### Deterministic tests

- runtime spec hash and unknown fields;
- image digest mismatch;
- valid repository/digest Dockerfile reference;
- UID/GID and private-path ownership;
- mount source/target/mode/propagation;
- wrong Git common dir/branch/HEAD/tree;
- stale container labels cannot satisfy attestation;
- wrong build ID/policy hash;
- symlink/TOCTOU mount source substitution;
- no tools before attestation;
- stale epoch tool call blocked;
- old container unknown prevents new writer;
- cancellation and graceful stop.

### Live disposable tests

- trusted primary checkout;
- trusted linked worktree outside trusted path;
- isolated external repository;
- read-only review view;
- clean and dirty exact-source cases;
- restart creates new run/runtime but same durable work;
- wrong permission fixture safely recreates private resource;
- current `pi-docker-*` regressions.

### Acceptance commands

```bash
python3 -m unittest \
  tests.control_plane.test_runtime_spec \
  tests.control_plane.test_runtime_attestation
./tests/pi-docker-control-plane-e2e.sh
```

5A runs only the Python tests. 5C adds the Docker command. 5D reruns both against
the staged package/image and verifies manifest hashes.

### Acceptance

- parent source state and container state exact at first tool;
- no fallback to older OID/path;
- no broad chmod/cleanup;
- no cross-run reuse;
- installed adapter hash/build included in evidence.

### Stop conditions

- sandbox still chooses ref/worktree/session;
- readiness based on container existence/name;
- direct writable old runtime cannot be proven stopped;
- activation attempted before package/image staging.

## Phase 6 — Exact children and artifacts

### Goal

Make read-only and writer children receive exact source and mechanical authority.

Execute sequentially:

- **6A — Immutable snapshot/ref creation:** clean/dirty capture and crash
  reconciliation, no child launch.
- **6B — Child execution:** exact lineage, mechanical read-only, separate
  writer working copies, parent-advance semantics.
- **6C — Artifacts and terminal reconciliation:** manifests, retention/privacy,
  success/failure/lost/attention behavior.

Freeze snapshot and artifact schemas before adapters consume them. Before 6B,
extract the reviewed final patched `pi-subagents@0.35.1` source once into the
first-party `pi/packages/pi-subagents-control/` package, retain license/upstream
and exact source manifest, install it as a local file dependency, and stop
adding architecture patches to the old installed package. The extraction is a
mechanical reviewed sub-slice; behavior changes follow separately.

### Expected surfaces

```text
scripts/pi_control/snapshot.py
scripts/pi_control/artifacts.py
pi/packages/pi-subagents-control/package.json
pi/packages/pi-subagents-control/LICENSE
pi/packages/pi-subagents-control/UPSTREAM.md
pi/packages/pi-subagents-control/src/**
pi/npm/package.json
pi/npm/package-lock.json
pi/extensions/secretary-subagents/
pi/extensions/workstream-channel/

tests/control_plane/test_snapshot.py
tests/control_plane/test_child_runs.py
tests/control_plane/test_artifacts.py
tests/pi-child-control-plane-e2e.mjs
```

### Implementation steps

1. Implement temp-index snapshot with path/ignored/size/symlink/submodule policy.
2. Create controller-owned immutable snapshot refs with CAS.
3. Read-only child gets read-only snapshot working copy/runtime or no mutable
   tool surface.
4. Writer child defaults to separate controller working copy and run.
5. Parent assignment includes exact source revision and controller IDs.
6. Child attestation includes parent lineage/authority.
7. Parent may advance after immutable read-only child launch; result records
   source revision.
8. Same-working-copy writer delegation remains disabled unless separately
   implemented/tested.
9. Artifact manifest with checksum, provenance, sensitivity, retention.
10. Child terminal record distinguishes result, submitted change, dirty state,
    failure, lost, and attention.
11. Controller verifies writer Git state/change submission; no automatic ref
    import based on descendant claim alone.
12. Preserve subagent orchestration/context policy; source-state selection is
    controller-owned.

### Tests

- exact clean snapshot;
- staged/unstaged/deleted/renamed/untracked;
- ignored file exclusion and explicit include;
- concurrent file mutation during capture;
- conflicts/submodules/symlinks/special file rejection;
- read-only child mutation mechanically denied;
- parent advances while child remains exact;
- independent writer gets distinct worktree/container;
- stale/wrong parent route rejected;
- child success with dirty/unrelated state not accepted as clean;
- artifact outside repo, checksum/provenance/permissions;
- crash before/after snapshot ref, child launch, result, artifact event;
- no child becomes root conversation;
- no unexpected parent/worktree diff from report-only child.

### Acceptance commands

```bash
python3 -m unittest \
  tests.control_plane.test_snapshot \
  tests.control_plane.test_child_runs \
  tests.control_plane.test_artifacts
NODE_PATH="$PWD/pi/npm/node_modules" node --test tests/pi-child-control-plane-e2e.mjs
```

6A runs snapshot tests, 6B adds child process tests, and 6C adds artifact/terminal
reconciliation. Each subphase reruns preceding tests.

### Acceptance

- process test reproducing old `868db28` child-base condition is rejected before
  child model/tool execution;
- read-only is mechanical;
- ambiguous child work retained;
- all existing child lease/permission regressions pass.

## Phase 7 — Local change submission

### Goal

Submit immutable Git revisions from personal primary, personal worktree,
secretary workstream, and integration result without mutating target/source
unexpectedly.

Execute sequentially:

- **7A — Clean branch-tip submission:** change/draft/revision/ref protocol and
  failpoint recovery.
- **7B — Dirty task-delta submission:** only after path attribution/ignored-file
  decisions are frozen; temporary index and explicit selection.
- **7C — Producer-neutral query/events:** verify every future client uses the
  same API.

### Expected files

```text
scripts/pi_control/changes.py
scripts/pi_control/git_snapshot.py or reuse snapshot.py
pi/extensions/control-plane/index.ts (submit tool/client)

tests/control_plane/test_change_submission.py
tests/control_plane/test_change_revisions.py
```

### Semantic controller commands

```text
pi-control change submit --request-json <file/stdin>
pi-control change list --project <id> --json
pi-control change show <id> --revision <n> --json
pi-control change close <id> --expected-version <n> ...
```

No arbitrary `git` arguments.

### Implementation steps

1. Implement change/revision rows and ref naming.
2. Branch-tip capture.
3. Temporary-index dirty personal capture using Phase 6 snapshot machinery.
4. Baseline/task-delta and explicit path-selection policy.
5. Excluded/pre-existing state summary.
6. Immutable revision ref from absent; verify commit/tree.
7. Idempotent retry around crash boundaries.
8. New revision supersedes projection without deleting history/reviews.
9. Bounded verification/provenance schema.
10. Emit change events/secretary attention through outbox.
11. Source branch/worktree/index remain unchanged.
12. Submission never creates review/integration automatically.

### Tests

- each producer mode;
- clean branch and dirty personal source;
- pre-existing personal changes excluded;
- ambiguous overlap needs selection;
- target missing/moved at submission recorded, not mutated;
- same idempotency key retry;
- crash before/after commit-tree, ref, DB revision/event;
- source unchanged byte/index/ref assertions;
- immutable old revision after new submission;
- malicious path/ref/title rejection;
- object-format support;
- DB contains no source content/secrets.

### Acceptance commands

```bash
python3 -m unittest \
  tests.control_plane.test_change_submission \
  tests.control_plane.test_change_revisions
```

7A filters branch-tip cases first; 7B adds every dirty/ambiguous capture case;
7C reruns both plus controller query/event tests.

### Acceptance

- full submit operation survives every failpoint with either one valid revision
  or no owned residual except reconciled operation;
- secretary can query change from controller even if source process exits;
- no integration capability yet.

## Phase 8 — Secretary and `pi-personal` controller clients

### Goal

Expose the shared controller model through the two distinct workflows without
making extensions authoritative.

Execute sequentially unless files are disjoint and the client protocol is
frozen:

- **8A — Controller client protocol:** bounded JSON, version negotiation,
  errors, and host-owned per-project activation lookup.
- **8P — Personal/root client:** explicit primary versus separate-worktree
  selection and submission.
- **8S — Secretary client:** project inventory, workstream creation/focus,
  attention, and change queue.
- **8C — Compatibility projection:** legacy route/root/secretary clients call
  controller or reject writes; no dual writer.

Live selection is stored by the host controller as `legacy`, `shadow`, or
`controller` per project. A model/caller environment variable cannot turn it
on. A test-only override is accepted only with disposable HOME/state roots.
Fallback from `controller` to legacy after a controller error is forbidden;
report and recover or explicitly roll back.

### Expected surfaces

```text
bin/pi
bin/pi-personal
bin/pi-secretary
scripts/pi-root-session.py              # compatibility/cutover client
scripts/pi-secretary-control.py         # secretary facade or extracted adapter
pi/extensions/root-session/index.ts
pi/extensions/secretary/index.ts
pi/extensions/workstream-channel/index.ts
pi/extensions/workstream-brief/index.ts
pi/extensions/control-plane/index.ts

tests/control_plane/test_personal_client.py
tests/control_plane/test_secretary_client.py
tests/control_plane/secretary-extension.test.mjs
```

### Implementation steps

1. Add controller client protocol with bounded JSON and error codes.
2. Personal launcher resolves controller project/conversation and explicit
   working-copy strategy; remove clean/dirty placement heuristic only at
   cutover flag.
3. Separate personal worktree creates separate conversation/session.
4. Root-session extension reports session observations; no cwd-derived rebind.
5. Secretary project status combines controller resources, fresh Git inventory,
   changes, and attention.
6. Secretary can create/focus controller workstream through semantic approval.
7. Workstream agent can submit change and notify attention.
8. Personal agent can submit change without secretary creation ceremony.
9. Unmanaged worktrees shown read-only; adoption explicit and initially may be
   inspect-only/not implemented.
10. Current review/landing tools remain compatibility paths until Phase 9.
11. Tmux/Herdr consume display labels but remain presentation-only.
12. Healthy footer contains no controller/sandbox jargon.
13. Do not edit current-state `pi/README.md` or `pi/SECRETARY_WORKFLOW.md` in
    Phase 8 source work. Their behavioral updates are a separately reviewed 11D
    activation-document step after canary evidence.

### Tests

- secretary bound project/no writable working copy;
- personal direct primary branch;
- personal separate worktree/session;
- secretary workstream exact worktree/session;
- restart exact conversation/working copy/new run;
- session header cwd tamper rejected/ignored;
- current project stats includes managed/unmanaged and unavailable observations;
- changes from every producer visible;
- approval scopes and generic yes rejection;
- presentation backend cannot change controller state;
- source/current docs updated only behind activated behavior.

### Acceptance commands

```bash
python3 -m unittest \
  tests.control_plane.test_personal_client \
  tests.control_plane.test_secretary_client
NODE_PATH="$PWD/pi/npm/node_modules" node --test \
  tests/control_plane/secretary-extension.test.mjs
```

Current launcher/root/secretary suites are added whenever their files enter the
subphase allowlist.

### Acceptance

- both producer workflows reach same change queue;
- no extension writes controller-like duplicate state;
- legacy/current semantics remain available only for projects whose host-owned
  activation record is `legacy`; `shadow` never mutates external state, and
  `controller` never silently falls back.

## Phase 9 — Integration analysis and CAS mutation

### Goal

Enable secretary/user to analyze and integrate exact change revisions with
candidate/target preservation.

Execute as separately reviewed subphases:

- **9A — Analysis and exact-revision review:** no target mutation.
- **9B — Authorization binding:** current request, revision, target OID,
  strategy, expiry, replay/staleness tests; no target mutation.
- **9C — Fast-forward/already-contained CAS:** rollback ref and crash recovery.
- **9D — Non-fast-forward integration worktree:** separate working copy,
  submitted integration result, original revisions preserved.

No subphase combines new analysis semantics and first target mutation in one
unreviewed diff.

### Expected files

```text
scripts/pi_control/integration.py
scripts/pi_control/reviews.py
pi/extensions/secretary/index.ts
pi/extensions/review-receipt/index.ts
scripts/pi-secretary-control.py compatibility facade

tests/control_plane/test_integration_analysis.py
tests/control_plane/test_integration_cas.py
tests/control_plane/test_integration_recovery.py
```

### Implementation steps

1. Deterministic merge-base/path/conflict analysis with temporary index/tree.
2. Analysis freshness bound to candidate and target OIDs.
3. Exact-revision review request/receipt migration.
4. Structured current-turn integration authorization.
5. Fast-forward/already-contained strategy under project/target locks.
6. Rollback ref before target mutation.
7. Target checked-out/dirty/in-use handling; never reset/stash/force.
8. Target CAS and independent observation.
9. Operation saga/recovery at every Git/index/ref boundary.
10. Target movement returns needs-resolution.
11. Integration worktree for non-fast-forward/conflict; result submits new
    change revision.
12. Change merged record/event only after target proof.
13. Cleanup remains separate and manual.

### Tests

- already-contained, fast-forward, target moved, non-ancestor;
- textual conflict and semantic-analysis handoff fixture;
- dirty/in-use target refusal;
- stale review/current revision;
- generic approval/replayed approval rejection;
- crash at rollback-ref/ref-update/index-update/DB-event boundaries;
- ambiguous external target mutation preserved;
- multiple changes reanalyzed after each target update;
- original candidate immutable through failed integration;
- no push/remote operation;
- current secretary authorization regressions;
- exact global integration lock order and concurrent inversion/deadlock
  rejection.

### Acceptance commands

```bash
python3 -m unittest \
  tests.control_plane.test_integration_analysis \
  tests.control_plane.test_integration_cas \
  tests.control_plane.test_integration_recovery
python3 -m unittest \
  tests.test_pi_secretary_authorization \
  tests.test_pi_secretary_control
```

9A runs analysis/review only; 9B adds authorization; 9C/9D add CAS/recovery and
existing secretary regressions. Mutation tests use disposable repositories.

### Acceptance

- no target mutation without exact user authorization;
- every failure leaves target/candidate recoverable and explained;
- secretary can complete the minimal integration workflow.

## Phase 10 — Continuity, plain UX, and walking-skeleton E2E

### Goal

Make the system understandable and prove the complete user workflow before
migration/activation.

Execute as:

- **10A — Continuity and error projection:** continuity idempotency, stable
  error translation, healthy silence.
- **10B — Read-only Inspector/presentation:** Control tab and footer cleanup;
  no mutation controls.
- **10C — Complete walking skeleton:** starts only after Phases 8 and 9 are
  accepted; fresh-fixture twice-run, restart/crash, privacy, and staged-build
  checks.

10A may be developed against frozen event schemas earlier; 10C cannot run in
parallel with unfinished submission/integration behavior.

### Expected surfaces

```text
pi/extensions/continuity/index.ts
pi/extensions/observability/* (Control tab, read-only)
pi/pi-statusline.json only if necessary; preserve model/activity division
pi/npm sandbox status emission

tests/control_plane/continuity-extension.test.mjs
tests/control_plane/test_error_messages.py
tests/control_plane/test_walking_skeleton.py
```

### Implementation steps

1. Visible `conversation-continuity.v1` card on compaction.
2. `/continuity` inspect command and resume reconstruction.
3. One aggregated control attention status; healthy silence.
4. Consequence-oriented error translator for stable codes.
5. Read-only Control Inspector data source.
6. Remove container names/opaque project IDs from normal footer status.
7. Build process-level walking fixture using real Pi test/fake provider and
   disposable Docker where required.
8. Capture timing baselines for controller operations.
9. Run personal and secretary paths through submission/integration/restart.
10. Verify source/install build IDs in staged fixture.

### Tests

All contract examples plus `ACCEPTANCE_PLAN.md` pre-activation matrix.

### Acceptance commands

```bash
NODE_PATH="$PWD/pi/npm/node_modules" node --test \
  tests/control_plane/continuity-extension.test.mjs
python3 -m unittest tests.control_plane.test_error_messages
python3 -m unittest tests.control_plane.test_walking_skeleton
./tests/pi-control-plane-integration.sh
```

10A runs continuity/error tests; 10B adds observability extension tests; 10C
runs the Python skeleton twice from fresh fixtures and then the disposable
integration script. Staged-build checks use Phase 11C artifacts and are not
claimed before they exist.

### Acceptance

- user can explain current project/run/change after compaction/restart from UI;
- original stale-state failures reproduce as clear blocked operations;
- walking skeleton passes twice from fresh fixtures;
- no unrelated jargon or raw sensitive content;
- no host activation yet.

## Phase 11 — Completion integration, typed migration, staged proof, and canary

### Goal and current classification

Move from current root/secretary/workspace/route authorities to the controller
without dual writers, prove the actual configured harness and installed bytes,
and preserve exact rollback.

The original 11A–11C component implementations are useful but are not the whole
phase:

- generic filesystem/Git inventory does not yet type every configured source;
- shadow import handles a project/primary-working-copy subset;
- active `bin/pi` and secretary launchers still use legacy state paths;
- controller workstream creation does not yet allocate and launch the complete
  worktree/session/runtime saga;
- current “walking skeleton” and extension checks include direct DB seeding or
  source reachability rather than the complete installed path;
- Docker rollback proof still stops with exit 77 when Docker is unavailable.

Therefore Phase 11 reopens cross-phase integration surfaces without discarding
accepted component code. `COMPLETION_IMPLEMENTATION_PLAN.md` is the normative
remaining-work plan; `IMPLEMENTATION_SLICE_BRIEFS.md` contains the exact
worker-sized handoffs; `SYSTEM_INTEGRATION_TEST_PLAN.md` defines complete action
coverage and evidence.

### Required slices

Execute sequentially unless the completion plan explicitly permits disjoint
read-only/test work:

1. **C0 — Contract/status/action freeze.** Add schema v7, activation latch,
   workstream, migration mapping, and complete action-catalog contracts before
   implementation.
2. **C1 — Schema v7 and semantic API.** Add `workstreams`,
   `presentation_assignments`, `project_activations`, and immutable
   `migration_resource_mappings`; no external side effects.
3. **C2 — Typed inventory adapters.** Explicit Git, root-session, secretary,
   route/lease, artifact, process, Docker, tmux, Herdr, installed-build, policy,
   and backup observations. Every adapter emits observed/unavailable/error/
   unsupported and every record remains visible.
4. **C3 — Resolution and shadow import.** Immutable inventory+resolution
   manifests, record dispositions, complete dependency-ordered import,
   failpoints, retries, and field/relationship comparison in disposable roots.
5. **C4 — Activation and exact binding.** Host-owned fail-closed activation
   latch; exact project/conversation/working-copy launch resolution; no
   controller-mode legacy fallback or cwd/session-header rebind.
6. **C5 — Workstream/presentation lifecycle.** One controller saga for
   create/focus/relaunch/retire; legacy secretary becomes a facade in controller
   mode; preserve unrelated tmux sessions and never kill a live secretary/worker
   during switch/restart.
7. **C6 — Installed launcher/runtime/package wiring.** Actual managed launchers
   use run manifests, locks/epochs, attestation, and the reviewed first-party
   sandbox/subagent packages. Legacy packages are not co-loaded.
8. **C7 — Complete personal/secretary/child/change/review/integration wiring.**
   Real launchers and semantic extensions traverse the shared controller path;
   no direct resource-row seeding in system evidence.
9. **C8 — Continuity/Inspector/configured-package behavior.** Behavioral event,
   resume, privacy, error, host-command, feedback, image, goal, BTW, and package
   tests—not source regex alone.
10. **C9 — Full deterministic system harness.** Implement the action manifest,
    tiered runners, complete journeys, fault/race matrix, and before/after
    evidence from `SYSTEM_INTEGRATION_TEST_PLAN.md`.
11. **C10 — Staged install and Docker rollback.** Exact loaded bytes and package
    roots, real pinned Pi, Docker runtime/attestation, interrupted installer,
    and disposable rollback.
12. **C11 — Write/review Phase 11D runbook only.** Name exact canary, commands,
    backup, evidence, stop criteria, and rollback. Do not execute it in an
    ordinary implementation slice.
13. **11D — Canary activation and rollback.** Separate explicit user intent,
    `pi-host`, reviewed runbook, one exact canary, and live evidence.
14. **General rollout.** Later per-project reviewed operations after accepted
    canary; never implicit in 11D.

### Required source and staging gates

After the slice-specific commands in `COMPLETION_IMPLEMENTATION_PLAN.md`:

```bash
bash tests/system/run-contract.sh
bash tests/system/run-component.sh
bash tests/system/run-process-fixture.sh
bash tests/system/run-source-gate.sh
bash tests/system/run-staged-installed.sh
bash tests/system/run-docker.sh
bash tests/system/run-presentation.sh
bash tests/system/run-staging-gate.sh
```

Until those runners exist, the current compatibility evidence remains:

```bash
python3 -m unittest discover -s tests/control_plane -p 'test_*.py'
bash tests/pi-control-plane-integration.sh
bash tests/run-control-plane-candidate-tests.sh
```

A required unavailable Docker, staged-install, or presentation prerequisite
returns STOP/77 and cannot be described as staging acceptance.

### Stop conditions

Stop on any unresolved canary-scope record, stale/divergent OID choice,
duplicate/unknown writer, ambiguous session/project/workstream binding,
controller/legacy dual write, controller-mode fallback, first-party/loaded
package mismatch, broad presentation kill, unproven build, unknown live process,
failed backup restore, untraceable user action, silently skipped required tier,
or destructive migration/cleanup proposal.

11D has no generic implementation command. Its reviewed runbook records exact
host commands, canary scope, inventory/resolution/migration/build IDs, backups,
loaded hashes, live tests, stop criteria, and rollback before asking for user
approval.

## Phase 12 and later — Post-MVP workstreams

After Phase 11 acceptance, the secretary may use the new workflow to create
parallel changes for:

- consolidation of remaining compatibility patches after the Phase 5/6
  sandbox/subagent package extractions;
- cross-run container reuse performance experiment;
- richer project/dependency dashboard;
- Herdr semantic label parity;
- automated retention/cleanup with dry-run policy;
- observability/benchmark expansion;
- outside-project-root context hardening;
- optional resident reconciliation service;
- remaining correction-ledger items.

Each workstream submits an immutable change and is integrated separately.

## 4. Cross-phase changed system surfaces

Every phase report explicitly addresses:

```text
public API / CLI
SQLite schema/migration
Git refs and working copies
session JSONL/bindings
state transitions and authority
transactions and locks
concurrency/fencing
retries/cancellation/reconciliation
error semantics
runtime/container/mounts/permissions
subagent source/authority
change/review/integration
observability/privacy/retention
performance
installation/activation/rollback
```

"No change" is a valid declaration but must be deliberate.

## 5. Weak-model phase brief template

```markdown
# Phase N — <name>

## Goal
<one bounded result>

## Required reading
- exact contract sections
- existing source paths

## Accepted decisions
- copied relevant decisions only

## Allowed files
- exact paths/globs

## Must remain unchanged
- exact boundaries

## Required behavior
1. numbered executable requirements

## Failure behavior
- stable error and retained state

## Tests to add first
- exact fixtures/cases

## Acceptance commands
- exact commands

## Stop and escalate
- ambiguity/security/data-loss conditions

## Report
- changed paths, commands/outcomes, surfaces, uncertainty, no remote action
```

A worker must not be told to consult the parent transcript as its primary
contract.
