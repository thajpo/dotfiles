# Controller migration, activation, and rollback plan

Status: **non-live typed migration/shadow/reconciliation and rollback implementation exist; migration-ready selection, staged/Docker capability evidence, launcher cutover, and Phase 11D remain gated; this plan must not be executed live**.

## 1. Purpose

The implemented candidate currently supports bounded inventory, disposable
shadow import, staged build identity, and rollback proofs. It is not a live
migration authority and has no permission to mutate production/root session
state. Do not invoke cutover, cleanup, daemon, or dual-writer steps.

Migrate from multiple JSON/file/ref/runtime lifecycle authorities to the SQLite
controller without deleting recoverable work, allowing dual writers, or
confusing repository source with installed activation.

Migration is copy/import and cutover first. Cleanup is a separate later
operation.

## 2. Current sources to inventory

At minimum:

- `~/.pi/agent/root-registry.json`;
- `~/.pi/agent/sessions/root/*.jsonl`;
- `~/.pi/agent/sessions/secretary/**`;
- current root-session archive/legacy session locations;
- `~/.local/state/pi-secretary/**`;
- current task route files under runtime directories;
- parent-transition and child-lease records;
- subagent async status/events/session/artifacts;
- Git worktree inventory and all relevant refs:
  `pi/*`, `pi-sandbox/*`, `refs/pi/*`, rollback/snapshot refs;
- Docker containers/images/volumes with Pi labels;
- tmux managed sessions/windows/panes and process trees;
- Herdr named sessions/workspaces/panes and processes;
- host repository policy and hash;
- installed launchers, helpers, extensions, package sources, lockfiles, images,
  and symlinks;
- repository source commit/tree and dirty diff;
- existing patch backup/rollback directories.

Inventory is read-only and records source path, file type, owner/mode, size,
SHA-256, parser/schema result, and relevant normalized identities.

## 3. Source precedence during import

No source is universally newest. Use typed precedence:

### 3.1 Git content

1. Git object/ref observation is authority for commit/tree/ancestry.
2. Working tree/index observation is authority for current dirty content.
3. JSON `startingOid`, Docker label, route, or session header cannot override
   observed Git state.
4. Divergent refs are preserved and reported, not reduced by timestamp.

### 3.2 Conversation content

1. Valid Pi session JSONL is conversation authority.
2. Root registry provides intended binding/visibility only after identity
   validation.
3. Session header cwd is historical locator evidence, not workspace authority.
4. Duplicate conversation IDs/files are contradictions requiring explicit
   mapping.

### 3.3 Project/workstream intent

1. Valid secretary project/workstream records are recorded intent for resources
   they own.
2. Git worktree/ref observation verifies but does not infer purpose.
3. Unregistered worktrees import as unmanaged observations.
4. Presentation pane labels/processes do not create workstream ownership.

### 3.4 Live process/runtime

1. Kernel process existence plus start identity is observation.
2. Container inspect plus process state is observation.
3. route/lease/label claims are matched evidence, not proof alone.
4. unknown host visibility remains unknown.

### 3.5 Trust

1. host policy plus explicit registered project mapping;
2. verified Git common-directory relationship for working copies;
3. path alone never imports a broadened trust decision.

## 4. Migration modes

### 4.1 Inventory

Reads all sources and emits a report. No DB or source mutation except writing the
report to an explicitly selected secure state/staging path.

### 4.2 Shadow import

Creates a new database in a staging directory from inventory. No launcher reads
it; no legacy writer changes behavior.

### 4.3 Shadow reconciliation

Controller observes current resources and compares its model with legacy
outputs. It remains read-only and emits contradictions.

### 4.4 Final import

After managed writers stop and migration lock is held, inventory is refreshed
and imported into final controller DB.

### 4.5 Canary cutover

One selected disposable/canary project uses controller as sole lifecycle writer.
Legacy stores may be projected read-only for compatibility but cannot accept
writes for controller-owned resources.

### 4.6 General cutover

Additional projects/conversations migrate only after canary acceptance.

### 4.7 Rollback

Stops controller-owned writers, preserves new refs/working copies/DB, restores
installed artifacts and legacy writer routing, and verifies old behavior. It
does not delete controller data.

## 5. Migration record

Authoritative lifecycle state is stored in the `migration_runs` table defined
by `STATE_CONTRACT.md` §6.8 and linked one-to-one to a typed `operations` saga.
Immutable source/contradiction/backup/build/comparison/rollback-proof manifests
are stored under:

```text
${XDG_STATE_HOME:-~/.local/state}/pi-control/migrations/<migration-id>/
```

Each file is represented by an immutable `migration_manifests` row containing
kind, path, SHA-256, size, and creation time. The secure-root ownership,
non-symlink, and mode rules apply. The immutable request projection is:

```json
{
  "schemaVersion": 1,
  "migrationId": "mig_...",
  "operationId": "op_...",
  "idempotencyKey": "caller-stable-key",
  "controllerBuildId": "build_...",
  "requestDigest": "sha256:...",
  "sourceManifestDigest": "sha256:...",
  "mode": "inventory|shadow-import|final-import|canary-cutover|rollback",
  "createdAt": "..."
}
```

Lifecycle `state`, `step`, `result`, errors, and completion time advance by
resource-version CAS with transactional control events; they are not falsely
called immutable. Request identity and manifest rows/files never change. A
retry with the same idempotency key/request digest returns or resumes the same
operation. A changed source manifest/request digest requires a new migration ID
and idempotency key. After a crash, reconciliation verifies table rows, manifest
hashes, backups, build identity, and external observations before resuming or
raising attention; file existence is never treated as completion proof.

Inventory may precede application DB creation. In that case it writes only to a
new secure migration directory; shadow import creates the DB and atomically
imports/hash-verifies the inventory manifest before any lifecycle advance.

## 6. Import mapping

### 6.1 Projects

For each valid registered repository:

- canonical primary checkout/common dir/object format;
- host policy/trust decision/hash;
- existing secretary alias/display name;
- random stable controller project ID;
- source record digests/provenance.

Same Git common directory under multiple aliases requires explicit alias merge
or separate display mapping, not duplicate project rows.

### 6.2 Working copies

Import:

- primary checkout;
- valid root-session linked worktrees;
- valid secretary workstreams/reviews/integrations;
- isolated workspaces with recoverable refs;
- other Git worktrees as unmanaged observations.

For each validate path, Git dir/common dir, branch/detached state, HEAD/tree,
status, registration, controller ownership evidence, and live process use.

Contradictions:

- two controller-owned records claim same path;
- same branch checked out unexpectedly;
- path missing but registration present;
- worktree belongs to different common dir;
- recorded base not ancestor when contract requires;
- route points to different working copy;
- multiple active conversations claim writer ownership.

No contradiction is resolved by modified time alone.

### 6.3 Conversations

Import exact root/secretary/workstream/review session files. Preserve Pi session
ID and assign controller conversation ID. Validate role/project/working-copy
mapping.

Archive status imports as desired archived. Ambiguous duplicates remain
unmigrated with report; files are untouched.

### 6.4 Runs

Existing processes/routes import only as observations or compatibility runs.
Do not treat their ephemeral task IDs as durable conversation/working-copy IDs.

At cutover, legacy live runs must be gracefully stopped or explicitly excluded.
New controller run manifests begin with new run IDs/epochs.

### 6.5 Changes and reviews

Secretary review requests/receipts and landed operations may import when exact
Git objects and bindings validate. Existing feature/workstream branches do not
automatically become changes. Provide a dry-run adoption/submission plan.

Orphan sandbox refs remain recovery refs until explicitly classified. The MLRE
`970ea8e` versus `b296516` choice is a human recovery decision and migration
MUST preserve both.

## 7. No dual-writer rule

Shadow phases are read-only. During canary/general cutover:

- controller is sole writer for migrated lifecycle resources;
- legacy tools either call controller or reject mutation with migration message;
- no best-effort dual-write to SQLite and JSON;
- compatibility JSON/routes may be generated projections with controller
  resource version/build ID and are never accepted back as newer truth;
- a feature flag cannot permit both legacy and controller Git/worktree writers;
- rollback changes the writer boundary only after managed processes stop.

Dual-read comparison is allowed; dual lifecycle write is not.

## 8. Build and artifact manifest

Before staging, produce canonical manifest containing:

```text
repository commit/tree and dirty patch digest
all source files included in install and SHA-256
Pi version
npm package-lock SHA-256
package versions and final installed-source hashes
compatibility patch order and hashes
Python/controller schema/build ID
Dockerfile/base/runtime image references and digests
launcher/helper/extension hashes
configuration/policy hashes
expected symlink targets
acceptance commands and outcomes
```

The build ID is a digest of this canonical manifest. Installed files report or
can be independently mapped to the same build ID.

A dirty repository may be staged only when the manifest includes exact dirty
content digest and review explicitly accepts it. Prefer a reviewed immutable
source commit for activation.

## 9. Staging procedure

1. Create user-owned `0700` staging root outside live install.
2. Install Pi/package tree using exact lockfile and pins.
3. Apply transitional patches only from expected hashes.
4. Build images with valid immutable base references.
5. Install controller DB/schema into a disposable state root.
6. Run all repository/unit/integration/process tests against staging paths.
7. Run staged launcher with disposable HOME/repo/session.
8. Verify artifact manifest final hashes/build ID.
9. Produce staged acceptance report.
10. Do not alter live symlinks, launchers, packages, images selected by live
    routes, or controller DB.

## 10. Pre-cutover live inventory

From explicit host-maintenance mode after user intent:

- verify exact repository/source/build under review;
- inspect managed Pi processes and start identities;
- inspect active child/transition/lease/operation state;
- inspect all relevant Git worktrees/refs/status;
- inspect Docker and presentation state;
- run legacy migration dry runs;
- write secure inventory and backup plan;
- identify canary project/conversation;
- prove rollback artifact exists and is restorable.

Unknown visibility or active ambiguous writer stops cutover.

## 11. Quiescence boundary

Before final import/cutover:

1. ask managed agents to finish current tool/turn or stop gracefully;
2. wait for child runs and transition operations or explicitly preserve them;
3. stop exact verified Pi processes without SIGKILL;
4. prove writable containers/processes no longer access migrated working copies;
5. acquire global migration and project locks;
6. refresh inventory and compare with reviewed plan;
7. abort if any source digest/OID/process state changed unexpectedly.

Presentation shells may remain only if they cannot launch legacy writers during
cutover. Safer canary procedure stops/restarts the exact managed surface.

## 12. Backup contract

Backup directory example:

```text
~/.local/state/pi/patch-backups/<timestamp>-control-plane-v1/
```

Manifest includes:

- every replaced installed file/symlink with mode/owner/hash/target;
- installed package tree files affected;
- launcher/helper/config copies;
- prior image IDs/digests where retainable;
- policy copy/hash;
- legacy registries/state records;
- Git ref inventory (not destructive bundle unless needed);
- pre-cutover process/container/presentation inventory;
- restore commands and expected hashes;
- controller DB snapshot after import and before first live mutation.

Backup creation is atomic enough that incomplete backup fails activation. Test
restore into a disposable root before live cutover.

## 13. Canary cutover

Recommended canary is a disposable fixture first, then one explicitly selected
real project/conversation with recoverable work.

Steps:

1. install staged artifacts atomically/hash-gated;
2. initialize final controller DB from reviewed import;
3. mark canary resource ownership controller-v1;
4. configure legacy clients for that resource to call controller/reject writes;
5. start one exact conversation through controller;
6. verify build ID, manifest, attestation, project/workspace/session identity;
7. run read and mutation fixture task;
8. launch exact read-only child;
9. submit a local change;
10. secretary inventories/analyzes it;
11. integrate only in disposable/canary target after explicit approval;
12. restart presentation/Pi and repeat state checks;
13. inject one safe crash/recovery scenario;
14. record all evidence.

Do not migrate other projects while canary has unresolved drift/attention.

## 14. General cutover

For each project:

- fresh inventory/contradiction report;
- explicit project mapping and working-copy ownership;
- exact conversation mapping;
- no live writer;
- import transaction with source digests;
- controller start and attestation;
- basic read/write/child/restart test;
- secretary project/change view test;
- project marked migrated only after evidence.

Projects may remain legacy while others are controller-owned, but one resource
cannot have both writers. Shared installed code must support explicit ownership
lookup.

## 15. Activation verification

After live install:

```text
installed build ID equals staged accepted build ID
all replaced file hashes equal manifest
active Pi process loaded expected launcher/extensions/packages
active image digest equals manifest
controller DB schema/build expected
run manifest/attestation expected
legacy writer blocked for migrated resource
project/working-copy/conversation exact
no unexpected Git refs/worktrees/status changes
no unrelated container/session/process changes
```

Repository tests are rerun independently where feasible. Live behavior evidence
is recorded separately from source test evidence.

## 16. Interrupted activation recovery

Activation itself is a journaled operation with steps:

```text
backup-complete
files-staged
files-swapped
controller-imported
canary-started
canary-accepted
complete
```

On restart:

- observe exact hashes/symlinks/DB/build/process state;
- if no live controller writer and files partly swapped, restore from backup or
  finish exact swap according to recorded intent;
- if controller writer may be live, stop/verify before changing artifacts;
- if DB import complete but launchers old, keep controller inactive and restore
  or finish after proof;
- if launchers new but DB unavailable, fail closed with recovery instructions;
- never select newest timestamp as correct generation.

## 17. Rollback triggers

Immediate rollback/canary stop on:

- wrong project/working copy/session/source OID;
- duplicate writer or stale epoch accepted;
- tools before attestation;
- destructive or ambiguous reconcile;
- source/install build mismatch;
- target mutation without exact authorization;
- candidate/ref/worktree loss;
- controller DB corruption/newer incompatibility;
- permission/trust broadening;
- inability to restore backup;
- repeated unexplained runtime failure.

A non-critical UI defect may disable only the UI extension if controller safety
remains proven.

## 18. Rollback procedure

1. stop new managed launches;
2. gracefully stop exact controller-owned writers/children;
3. verify writable runtime access gone;
4. acquire migration/project locks;
5. export controller DB, operations, events, manifests, new refs/worktrees, and
   attention to secure rollback evidence;
6. preserve all open change refs and ambiguous work;
7. atomically restore installed files/symlinks/config from verified backup;
8. restore legacy writer routing;
9. leave controller DB intact but inactive/read-only;
10. restart only selected legacy canary conversation;
11. verify installed hashes, exact session/worktree, Git state, and no
    unintended cleanup;
12. record rollback outcome.

Rollback does not delete controller-created refs/worktrees/sessions. Cleanup is
reviewed later.

## 19. Schema rollback

Do not downgrade the only copy of a database in place. Preserve DB backup and
use old code against legacy stores or a separately exported compatible view.

Forward schema migrations must define whether they are:

- additive and old-reader compatible;
- cutover requiring new readers;
- irreversible without restoring backup.

Unknown/newer schema fails closed.

## 20. Legacy retirement and cleanup

Only after sustained acceptance:

- mark legacy records read-only/retired;
- retain source hashes and mapping provenance;
- dry-run candidate cleanup of obsolete projections/routes/markers;
- prove no live/reference/recovery dependency;
- obtain explicit apply authorization;
- remove exact owned resources without force;
- retain controller migration/rollback records.

Do not delete legacy sessions or Git recovery refs merely because they imported
successfully.

## 21. Migration acceptance artifacts

Required bundle:

```text
source/build manifest
repository test report
staged process test report
pre-cutover inventory and contradictions
resolved mapping decisions
backup manifest and disposable restore proof
final import report and DB digest
canary run/change/integration/restart evidence
fault-injection evidence
installed hash/build/image evidence
rollback drill report
post-cutover Git/process/container/session diff
remaining uncertainty and deferred cleanup
```

## 22. Explicit non-goals

- No automatic authoritative-head choice for divergent MLRE work.
- No dual lifecycle writers.
- No migration-time cleanup.
- No live backend process migration.
- No automatic remote push/deploy.
- No activation from ordinary sandboxed project mode.

## 23. Remaining implementation contract

`COMPLETION_IMPLEMENTATION_PLAN.md` is the canonical implementation sequence.
The migration portion is not complete until all requirements below hold.

### 23.1 Inventory completeness

The inventory manifest uses typed schema v2, not a generic recursive file list.
It invokes explicit adapters for:

```text
Git repositories/worktrees/refs/status
root registry and root/archive Pi session headers
secretary project/workstream/runtime/attention/review state
routes, transitions, child leases, async status/events
artifact manifests and exact files
kernel process/start/ancestry observation
Docker containers/images/mounts with exact Pi labels
tmux managed sessions/windows/panes/processes
Herdr named sessions/Spaces/tabs/panes/agents
installed launchers/settings/extensions/helpers/packages/images/build manifest
host policy/machine profile
installer and migration backup manifests
```

Every adapter records one of `observed`, `unavailable`, `error`, or
`unsupported`. A successful empty observation differs from unavailable. Every
record receives an immutable disposition:
`import|observe|unmigrated|exclude|requires-decision|contradiction`. The report
may remain blocked, but it may not silently omit a configured source/resource.

Runtime and presentation records import as observations only. Existing
capabilities, epochs, manifests, attestations, or authorizations are never
reconstructed. Active legacy processes are stopped or excluded before cutover;
new controller runs start with new IDs and epochs.

### 23.2 Mapping completeness

Inventory and a separate exact resolution manifest bind all imported resources.
The shadow DB records immutable `migration_resource_mappings` for each inventory
record. Legacy secretary IDs, including unprefixed 64-hex IDs, remain source
identities and map to newly allocated random controller `prj_...`/`ws_...` IDs;
they are never truncated/reformatted into controller IDs. Required mapping order
is:

```text
installed build and policy evidence
projects
working copies
conversations
workstreams and presentation assignments
artifact indexes
historical observations and attention
explicit change-adoption proposals
```

Routes/leases/processes/containers/panes do not create active runs or writer
claims. Legacy reviews are historical evidence unless they already meet the
controller's authenticated exact-revision schema. Candidate branches become
changes only through an explicit adoption decision. Duplicate conversations,
incomplete project-rebind proof, divergent refs, ambiguous dirty ownership, and
MLRE alternatives remain human decisions.

A selected canary scope is migration-ready only when it has no
`requires-decision`, `contradiction`, unknown writer, unavailable required
adapter, or unmigrated resource capable of changing that scope. Other projects
may remain explicitly legacy.

### 23.3 Activation selector

Per-project mode is a resource-versioned `project_activations` row and a secure
host-owned `activation.v1.json` boot latch. The latch is only a fail-closed
projection; SQLite remains lifecycle authority. Production launchers accept no
environment mode override.

- `legacy`: legacy is sole writer; controller does not mutate external state.
- `shadow`: legacy is sole writer; controller observes/imports/compares only.
- `controller`: controller is sole writer; compatibility clients are facades or
  reject mutation. Missing/mismatched controller state stops launch.

Direct `legacy -> controller` is forbidden. Rollback changes controller to
legacy only after exact controller writers are quiesced and evidence is
preserved.

### 23.4 Root and secretary convergence

Root registry and secretary state are migration inputs/projections, not parallel
controller-mode authorities. In controller mode:

- root launch resolves exact SQLite project/conversation/working-copy before
  opening Pi; session header `cwd` is observation only;
- `scripts/pi-secretary-control.py` delegates lifecycle operations to the
  controller and no longer writes its old project/workstream lifecycle store;
- active project ordering and backend/layout may remain presentation
  preferences, but every referenced controller ID is validated;
- workstream creation is one recoverable saga covering rows, Git worktree,
  session, presentation assignment, run, runtime, and ready event.

### 23.5 Required migration tests

Tests use disposable roots and show:

1. each adapter success/empty/unavailable/error/malformed/timeout path;
2. inventory determinism and no source/Git/process/container/presentation
   mutation;
3. relationship-aware contradictions without false conflicts for shared
   references;
4. immutable inventory and resolution manifests plus stale-resolution refusal;
5. idempotent crash recovery before/after every file/row/batch/event boundary;
6. source change/tamper and field/relationship mismatch detection;
7. no active run/writer/review authorization synthesized from legacy evidence;
8. exact root/session/project/workstream mapping and controller-mode no-fallback;
9. legacy/shadow/controller writer exclusivity;
10. complete canary-scope STOP logic;
11. staged final-import, cutover, restart, and rollback in disposable resources;
12. Docker and installed-build proof returning STOP/77 when unavailable.

The full action and execution-tier requirements are in
`SYSTEM_INTEGRATION_TEST_PLAN.md`. Passing project-only shadow tests is component
evidence, not migration completion.
