# Pi control-plane post-implementation audit and remediation report

Status: **STOP — do not authorize Phase 11D**

Audit timestamp: **2026-08-06T19:04:04Z**

Repository head inspected: **`36a248f`**

## 1. Executive verdict

The accumulated implementation is not ready for a live canary. The current test
suite is green, but several acceptance claims are stronger than the behavior
that is actually implemented.

Release-blocking findings:

- a clean staged install cannot resolve the first-party local npm packages and
  still invokes a removed legacy sandbox patch target;
- the controller CLI/library is not installed at all, and the repository-local
  launcher would not work if copied to `~/.local/bin` unchanged;
- the expected-build-manifest gate compares fields that are absent from the
  manifest file, while staged-tree verification allows unexpected files;
- Phase 11 inventory/import implements only a small project/worktree subset of
  the documented migration contract and is not crash-resumable;
- review acceptance can be submitted without proving reviewer identity;
- writer-run idempotency creates a second run for the same idempotency key;
- important lifecycle mutations either emit no outbox event or are not atomic
  with the event;
- continuity, Inspector, and walking-skeleton behavior do not satisfy the
  normative Phase 10 contract.

**Required decision:** reopen source acceptance for the affected Phase 4/8/9/10/11
surfaces. Keep Phase 11D blocked until all P0/P1 findings below are repaired and
the replacement acceptance gate passes from a clean staged source tree.

## 2. Scope and method

Audited surfaces:

- SQLite schema/store, operations, leases, reconciliation, and events;
- snapshots, child runs, artifacts, changes, reviews, and integration;
- controller clients/CLI and Pi extensions;
- migration, staged build, installer, rollback, and candidate gates;
- contracts and tests used to claim Phase 1–11C acceptance.

Workspace condition matters to the audit:

- 43 tracked files are modified;
- 263 files are untracked;
- 80 untracked files are under `scripts/pi_control`, `tests/control_plane`, or
  `pi/control-plane`.

`git diff`, `git diff --stat`, and `git diff --check` do not review untracked
content. Therefore previous “final diff” evidence did not cover most of the new
control-plane implementation.

Evidence used:

- four independent read-only audit passes split across lifecycle, Git/change,
  clients/extensions, and migration/install;
- parent inspection of the actual source and contracts;
- disposable reproductions for installation, manifests, idempotency, outbox,
  migration parsing/recovery, and atomicity;
- the passing 186-test control-plane suite and candidate gate, treated as
  evidence of covered behavior rather than proof of uncovered behavior.

No live migration, deployment, remote publication, production mutation, or
Phase 11D action was performed during this audit.

## 3. Severity scale

- **P0 / Critical:** release or activation gate is invalid, or clean installation
  cannot produce the claimed system.
- **P1 / High:** authority, lifecycle, recovery, or contract behavior is
  incorrect and can produce false success, lost provenance, or manual recovery.
- **P2 / Medium:** important hardening, observability, or acceptance weakness
  that should be fixed before canary unless explicitly risk-accepted.
- **P3 / Low:** maintainability/documentation issue without immediate authority
  or data-integrity consequence.

## 4. Confirmed findings

### AUD-001 — P0 — clean staged npm installation is broken

**Locations**

- `pi/npm/package.json:5,15`
- `pi/npm/package-lock.json:1564-1570`
- `install.sh:156-160,205-206`
- `scripts/pi-patch-subagents` legacy `@kjrjay/pi-sandbox` stages
- `tests/run-control-plane-candidate-tests.sh:5-8`

**Evidence**

The npm manifest uses:

```json
"pi-sandbox-control": "file:../packages/pi-sandbox-control",
"pi-subagents": "file:../packages/pi-subagents-control"
```

The installer copies only `pi/npm/{package.json,package-lock.json}` into
`$STAGING_DIR/npm`; it never creates `$STAGING_DIR/packages`. A real clean
`npm ci` succeeds but creates broken links:

```text
node_modules/pi-subagents -> ../../packages/pi-subagents-control
node_modules/pi-sandbox-control -> ../../packages/pi-sandbox-control
pi-subagents package: BROKEN/MISSING
pi-sandbox-control package: BROKEN/MISSING
```

Even when the two package trees are supplied in a disposable fixture,
`PI_CODING_AGENT_DIR=$tmp scripts/pi-patch-subagents` exits with:

```text
pi-sandbox: package not installed: .../npm/node_modules/@kjrjay/pi-sandbox
```

The scoped candidate gate explicitly skips the legacy patch-chain runner because
of this skew. `tests/pi-installer-transaction.sh` uses a fake npm implementation,
so it does not exercise this clean-install path.

**Consequence**

A real clean installation cannot reach the staged runtime claimed by Phase 11C.
The green installer transaction test masks the failure.

**How to fix**

1. Choose one canonical packaging mechanism:
   - copy the exact first-party package trees to `$STAGING_DIR/packages` before
     `npm ci`, or
   - `npm pack` reviewed tarballs and install exact tarball SHA-256 values.
2. Split `pi-patch-subagents` so extracted first-party packages are never sent
   through removed `@kjrjay/pi-sandbox` compatibility stages.
3. Fail if either local dependency is a broken link, escaping link, or does not
   match its package/source provenance manifest.
4. Replace the fake-npm success path with at least one real clean `npm ci`
   installer test.

**Required tests**

- clean staging with no checkout `node_modules` reuse;
- exact package tree and package-lock verification;
- repeat install from the same immutable inputs;
- explicit rejection of the removed legacy package path.

---

### AUD-002 — P0 — the controller CLI/library is not an installed artifact

**Locations**

- `bin/pi-control:9-15`
- `install.sh:352-365,459-490`
- `pi/extensions/control-plane/index.ts:6-29`

**Evidence**

The installer does not copy `bin/pi-control` or `scripts/pi_control/**` into the
staged generation or activate either one. The control-plane extension defaults
to `~/.local/bin/pi-control`.

The launcher itself imports by treating its parent repository as Python source:

```python
_repo = Path(__file__).resolve().parents[1]
from scripts.pi_control.cli import main
```

If copied unchanged to `~/.local/bin/pi-control`, `_repo` becomes `~/.local`,
where `scripts/pi_control` is absent.

**Consequence**

The installed extension cannot invoke the controller. Repository-local tests
exercise a source checkout, not the installed system.

**How to fix**

1. Package `scripts.pi_control` as an exact staged Python artifact (wheel,
   zipapp, or immutable installed tree).
2. Install a launcher that imports only that artifact, not a repository-relative
   path.
3. Add the launcher and every Python source file to the build manifest.
4. Make `pi/extensions/control-plane` verify the managed launcher/build identity,
   ownership, mode, and non-symlink path before invocation.

**Required tests**

- isolated `HOME` invocation of installed `pi-control schema status`;
- installed extension status/focus/submit smoke tests;
- stale or symlinked launcher rejection with `CP_BUILD_MISMATCH` or
  `CP_PERMISSION_INVALID`.

---

### AUD-003 — P0 — build-manifest verification is ineffective

**Locations**

- `scripts/pi_control/staged_build.py:101-125,128-182`
- `install.sh:381-416,459`
- `tests/control_plane/test_build_manifest.py`

**Evidence**

`BuildManifest.as_dict()` adds `manifestDigest` and `buildId`, but
`write_build_manifest()` serializes only `manifest.payload`. The on-disk file
therefore has neither field. The installer compares only those absent fields:

```python
if actual.get("manifestDigest") != expected.get("manifestDigest") \
   or actual.get("buildId") != expected.get("buildId"):
    ...
```

Two ordinary generated manifest files both yield `None == None`, so the expected
manifest check passes without comparing file hashes, image identity, metadata,
or tests.

A disposable reproduction also added an unexpected file after manifest
creation; `BuildManifest.verify_files()` still passed because it checks only
listed entries and never compares the complete actual path set.

Additional inconsistencies:

- the installer never calls `verify_files()`;
- no post-activation installed-tree equality check exists;
- manifest generation silently allows missing repository metadata by returning
  `None`;
- a prior manifest inside the staged root is not explicitly excluded, making
  repeated generation self-referential/non-reproducible.

**Consequence**

The Phase 11C build identity and expected-manifest safety gate do not establish
that staged or installed bytes equal reviewed bytes.

**How to fix**

1. Define a strict manifest envelope that stores `manifestDigest` and `buildId`
   on disk without self-reference (digest the canonical payload, then wrap it).
2. Parse/validate the complete expected schema and recompute its digest; compare
   canonical payloads, not two caller-supplied identifier strings.
3. Enumerate the actual staged tree and compare exact `(path, kind, mode,
   SHA-256/symlink target)` sets; reject extra, missing, special, and duplicate
   paths.
4. Explicitly exclude only the final manifest and its same-directory temporary
   file from payload enumeration.
5. Repeat exact-tree verification after activation and before setting
   `ACTIVATION_COMMITTED=1`.
6. Make unavailable Git/source metadata a hard failure for an activatable build.

**Required tests**

- malformed/missing `buildId` and digest rejection;
- changed payload with copied identifiers rejection;
- extra/missing/symlink/special-file rejection;
- repeat generation in one staging root;
- post-activation full-tree equality and rollback on mismatch.

---

### AUD-004 — P1 — Phase 11 inventory/import is only a partial prototype

**Locations**

- `scripts/pi_control/migration.py:66-196,240-279,391-407,479-513`
- `pi/control-plane/MIGRATION_ACTIVATION_PLAN.md:15-81,163+`
- `tests/control_plane/test_migration_*`

**Evidence**

The contract requires typed inventory of registries/sessions/routes, Git refs and
worktrees, Docker, tmux, Herdr, processes, installed artifacts, policy, images,
and backups. The implementation recursively hashes explicit filesystem roots
and optionally observes a Git repository. It has no Docker, tmux, Herdr,
process-tree, installed-build, image, route/lease, or typed source-precedence
adapter.

Shadow import calls `_project_records()` and creates only:

- one project per observed Git common directory; and
- one primary working-copy row.

It does not import conversations, sessions, workstreams, runs, changes, reviews,
transitions, child leases, artifacts, unmanaged worktrees, aliases, policy
bindings, or process observations. `shadow_reconcile()` compares only the set of
project IDs.

The current repository itself cannot be inventoried because the recursive walk
includes `.git` and dependency trees and stops at 4096 entries:

```text
ValueError: inventory file count exceeds its bound
```

**Consequence**

The implementation cannot perform the documented 11A inventory or 11B shadow
comparison. Calling the migration “complete” risks treating omitted resources
as nonexistent.

**How to fix**

Reopen Phase 11A/11B as separate slices:

1. Define typed source records and per-source schemas/provenance.
2. Add bounded adapters for each mandatory source, with explicit unavailable /
   unknown states.
3. Inventory Git metadata through Git adapters rather than recursively hashing
   `.git`; inventory installed package trees through explicit manifests.
4. Implement typed precedence and contradiction rules; never use a generic
   repeated-ID heuristic.
5. Import every contract resource type or explicitly classify it as
   unmanaged/unmigrated/blocked.
6. Reconcile field-level identities and observations, not only project-ID sets.

**Required tests**

Use a fixture containing every required legacy source, duplicate/contradictory
records, unavailable adapters, >4096 irrelevant dependency files, divergent
refs, unmanaged worktrees, and both preserved MLRE heads.

---

### AUD-005 — P1 — migration contradiction and crash recovery are incorrect

**Locations**

- `scripts/pi_control/migration.py:66-107,152-169,343-368,429-503`
- `tests/control_plane/test_migration_import.py`

**Confirmed defects**

1. JSONL identities are stored as a list of lists (`records.append(identity)`),
   while `_contradictions()` accepts only dictionary entries. Two divergent
   JSONL files with the same `sessionId` produced:

   ```text
   identities: [[{"kind":"sessionId","value":"same"}]]
   contradictions: []
   ```

2. The generic duplicate rule treats any repeated ID in different JSON records
   as contradictory if whole-file hashes differ. Legitimate route, registry,
   and workstream records that reference the same project can therefore block
   import. This is not typed precedence.

3. Operation row creation, synthetic build registration, manifest-file writes,
   and migration rows occur in separate transactions. A simulated crash after
   writing `source-inventory.json` left one operation, no migration row, and no
   manifest row. Retry failed permanently with `FileExistsError`.

4. “Disposable” is not mechanically enforced. Any custom existing controller
   state root other than the conventional default is accepted. A disposable
   test pointed shadow import at an existing controller DB; it wrote a new
   operation before failing on the existing active-build constraint.

**Consequence**

Real duplicate sessions can be missed, valid cross-record references can be
falsely blocked, and crash recovery can strand unindexed files/operations or
mutate a non-disposable controller database.

**How to fix**

1. Flatten per-record identities and include source type plus record key.
2. Encode contradiction rules per source/field rather than whole-file hash.
3. Create an immutable migration intent first, then use deterministic manifest
   paths and an adopt/verify/reconcile state machine for every crash boundary.
4. Derive deterministic IDs before external writes and store each saga step.
5. Require a newly created empty shadow root or a controller-created
   `shadow-only` marker/build binding; reject all pre-existing unmarked DBs.
6. On retry, inspect and adopt exact files/rows or move to durable
   `needs_attention`; never fail merely because a deterministic file exists.

**Required tests**

- duplicate session JSONL detection;
- legitimate shared project references without false contradiction;
- process death after operation, build, source manifest, contradiction manifest,
  project import, and final event;
- retry/adoption from each state;
- existing custom controller root rejection before any write.

---

### AUD-006 — P1 — review acceptance is not bound to reviewer authority

**Locations**

- `scripts/pi_control/reviews.py:51-86,89-120`
- `scripts/pi_control/integration.py:268-292`
- `tests/control_plane/test_walking_skeleton.py:49-53`

**Evidence**

`request_review()` permits `reviewer_conversation_id=None`. `submit_review()`
accepts only a `review_id` and receipt fields; it receives and verifies no actor,
conversation, capability, read-only run, source attestation, or reviewer
ownership. Integration authorization accepts any submitted `accept` receipt
whose caller-supplied evidence matches integration/analysis/target values.

The walking-skeleton test demonstrates the bypass: it requests a review without
a reviewer conversation, immediately submits `accept`, and authorizes
integration from direct library calls.

**Consequence**

Any caller with a review ID and public analysis values can create the acceptance
evidence used to authorize integration.

**How to fix**

1. Require a non-null reviewer conversation/run selected by policy.
2. Make `submit_review()` require authenticated actor/run/capability context and
   match it to the requested reviewer.
3. Bind the receipt to exact change revision ref/OID/tree/base, target ref/OID,
   analysis digest, reviewer source snapshot, and read-only attestation.
4. Store reviewer identity in immutable receipt fields and reject reassignment.
5. Expose review operations only through semantic client commands that carry
   controller-verified context.

**Required tests**

- other conversation/actor cannot submit a receipt;
- no-reviewer request cannot authorize integration;
- stale revision/target/reviewer snapshot rejection;
- read-only reviewer capability enforcement.

---

### AUD-007 — P1 — writer-run idempotency creates duplicate runs

**Locations**

- `scripts/pi_control/leases.py:167-233`
- `scripts/pi_control/operations.py:20-58`

**Evidence**

`create_operation()` correctly returns an existing operation for the same key,
but `create_writer_run()` ignores the operation state/result and always creates
a fresh `run_id`. A disposable reproduction closed the first run and replayed
the same key:

```text
same run: False
operations with key: 1
runs: 2
operation state/result: ('planned', None)
```

A replay while the first run is active fails as `WriterUnknownError` before it
even examines the existing operation.

**Consequence**

The lifecycle API is not idempotent even though its operation row is. Retries
can create multiple runs/epochs for one intent and cannot explain which run the
operation owns.

**How to fix**

1. Include all authority-relevant inputs in the operation request digest.
2. Persist the chosen `run_id`, epoch, process identity, and manifest binding in
   operation state/result atomically with run creation and writer claim.
3. On replay, reacquire/verify the exact leases and return or reconcile the same
   run; never generate another run.
4. Because capability secrets must not be stored, require the caller to replay
   the same secret (hash in the request) or use a separately designed sealed
   capability handoff.
5. Terminal replay returns the recorded terminal result, not a new run.

**Required tests**

- replay while active and after terminalization;
- crashes before/after writer claim and operation completion;
- changed request with same key;
- capability mismatch and PID/start-identity mismatch.

---

### AUD-008 — P1 — lifecycle outbox coverage and atomicity are incomplete

**Locations**

- `scripts/pi_control/store.py:648-774`
- `scripts/pi_control/client.py:150-174`
- `pi/control-plane/STATE_CONTRACT.md` transactional outbox requirements

**Evidence**

`create_run()` and `terminalize_run()` mutate runs and writer claims without
adding any `control_events`. A disposable reproduction observed event counts
`0 -> 0 -> 0` across run creation and terminalization.

`ControllerClient.create_workstream()` inserts a conversation and then calls
`append_event_in_transaction()` without entering `store.transaction()`. SQLite
is in autocommit mode. Injecting an event failure left:

```text
workstream rows: 1
workstream events: 0
```

**Consequence**

Consumers cannot reconstruct key lifecycle transitions, and workstream state can
commit without its outbox event. This violates the single lifecycle authority
and transactional outbox contract.

**How to fix**

1. Define the required event for every consequential lifecycle transition.
2. Add run-created, writer-claimed, run-terminalized, and writer-claim-cleared
   events inside the same transaction as state changes.
3. Wrap workstream creation and event insertion in one transaction.
4. Include operation ID/resource versions and check every conditional update's
   row count before emitting success.
5. Add an outbox completeness invariant/test over all public mutators.

---

### AUD-009 — P1 — non-fast-forward integration cannot recover after result publication

**Locations**

- `scripts/pi_control/integration.py:340-352,443-467`

**Evidence**

The integration worktree path is a deterministic integration ID. After merge
commit, `_result_change()` publishes an immutable result ref and commits change
rows. Only then does the code remove the worktree and mark the integration
successful. A crash/failure between those steps leaves the result ref/rows and
worktree, while retry immediately rejects any existing worktree as requiring
attention. No recovery path verifies/adopts the already-published result.

The result change ID is randomly generated after the merge, so a retry cannot
reconstruct it from integration identity without querying/journaling it.

**Consequence**

A semantically successful merge result can become a permanently unresolved
integration requiring manual database/Git/worktree intervention.

**How to fix**

1. Journal deterministic integration steps before each external mutation:
   worktree planned/created, merge committed, result ref published, result rows
   committed, worktree removed, integration completed.
2. Persist or deterministically derive the result change ID before ref creation.
3. On retry, inspect exact worktree HEAD/status, result ref, change/revision/input
   rows, authorization, and integration version; resume only when all evidence
   agrees, otherwise durable `needs_attention`.
4. Make `_mark_resolution()` verify CAS row count before emitting an event.

**Required tests**

Crash after merge commit, result ref, result rows, worktree removal, and final
outbox commit; each retry must converge or produce exact durable attention.

---

### AUD-010 — P1 — continuity is not the contracted continuity mechanism

**Locations**

- `pi/extensions/continuity/index.ts:3-57`
- `tests/control_plane/continuity-extension.test.mjs`
- `pi/control-plane/OBSERVABILITY_CONTINUITY_CONTRACT.md:345-405`

**Evidence**

Persisted entries contain only session ID, reason, `willRetry`, and timestamp.
They omit required `compactionEntryId`, retained goal/slice/decisions/completed/
open questions/risks, `firstKeptEntryId`, `summaryDigest`, and
`detailsAvailable`.

Every compaction event blindly appends a new entry. There is no deduplication or
crash reconciliation by `(sessionId, compactionEntryId)`. `/continuity` cannot
show the retained state required by the contract. The extension also publishes
a persistent footer status on every later session start, despite the healthy
silence rule.

The test only regex-checks source strings and TypeScript transformation; it does
not execute event, persistence, deduplication, resume, or rendering behavior.

**How to fix**

Implement the exact versioned card from the actual compaction result plus the
bounded task packet, persist its digest and entry ID, scan/deduplicate before
append and after resume, render retained state through `/continuity`, and use a
transient card/notification rather than permanent healthy footer state.

**Required tests**

Manual/threshold/overflow events, duplicate delivery, crash after append,
resume/branch navigation, digest consistency, malformed/newer entries, privacy,
and healthy footer silence.

---

### AUD-011 — P1 — root-session JSONL cwd parsing is broken and authority remains ambiguous

**Location**

- `pi/extensions/root-session/index.ts:16-50` (pre-existing adjacent code)

**Evidence**

`sessionCwd()` uses `.split("\\n")`, which splits on the literal backslash-plus-n
sequence rather than real newlines. A multi-line JSONL file is parsed as one
invalid JSON value, so the function falls back to `ctx.cwd`.

Even after correcting the split, the migration/controller contract says a
session header cwd is historical locator evidence, not workspace authority.
Passing it directly as `--worktree` without checking the controller binding can
reintroduce the wrong-project overwrite described in correction `ID-007`.

**Consequence**

Session registration can bind to the current process cwd rather than the
recorded/controller-selected project and working copy.

**How to fix**

1. Correct newline parsing and bound the header read.
2. Treat header cwd only as evidence; resolve the exact controller conversation/
   working-copy binding and reject contradictions.
3. Validate helper path ownership/build provenance before execution.

**Required tests**

Multi-line JSONL where header cwd differs from `ctx.cwd`, malformed/oversized
header, moved path, and a controller-binding contradiction.

---

### AUD-012 — P1 — the claimed user workflow and Phase 10 walking skeleton are not wired

**Locations**

- `scripts/pi_control/cli.py:105-163,194-258`
- `pi/extensions/control-plane/index.ts`
- `tests/control_plane/test_walking_skeleton.py:20-55`
- `pi/control-plane/MVP_IMPLEMENTATION_PLAN.md:1132-1204`

**Evidence**

The CLI/extension exposes status, focus, change submit, workstream create, and
personal selection. It exposes no review request/receipt, integration analysis,
integration authorization, or integrate command. No secretary/control facade
calls those library APIs.

The walking-skeleton test invokes `request_review`, `submit_review`,
`authorize_integration`, and `integrate` directly in one Python process. It does
not exercise the secretary/client/CLI path, writable run and attestation,
read-only child, restart, process recovery, compaction, Inspector, rollback, or
staged installed artifacts. The required `tests/pi-control-plane-integration.sh`
command does not exist.

No control-plane data source/tab is present in the observability extension; its
“control” handlers refer to subagent control events, not SQLite controller state.

**Consequence**

Library behavior exists, but the documented user workflow cannot be completed
through installed semantic clients, and Phase 10 acceptance did not test its
contract.

**How to fix**

1. Add versioned semantic CLI/client operations for review, analysis,
   authorization, integration, recovery status, and technical details.
2. Wire secretary actions to those operations with exact current-turn target
   authorization and authenticated reviewer identity.
3. Add a read-only Inspector controller adapter with malformed/newer-schema
   degradation.
4. Replace the direct-library skeleton with process-level installed-client flows
   and the full acceptance runbook.

---

### AUD-013 — P2 — error projection does not satisfy the user-facing error contract

**Locations**

- `scripts/pi_control/error_messages.py`
- `pi/control-plane/OBSERVABILITY_CONTINUITY_CONTRACT.md:221-260`

**Evidence**

The projection returns only `code`, one sentence, and raw bounded detail. The
contract requires attempted action, observed disagreement, whether anything
changed, what was preserved, safe next actions, and a technical-details action.
Several generic messages assert “no write was started,” “stopped before
mutation,” or “source and target were preserved” without carrying operation
reconciliation evidence proving those claims.

**How to fix**

Make the projection consume a structured operation outcome/recovery proof, not
only an exception code. Render unknown/ambiguous when side effects are not
proven. Add contract-shape and false-reassurance tests for pre- and post-side-
effect failures.

---

### AUD-014 — P2 — secure path handling is inconsistent

**Locations**

- `scripts/pi_control/staged_build.py:156-181`
- `scripts/pi_control/leases.py:55-92`
- `scripts/pi_control/locks.py`
- analogous file-read helpers

**Evidence**

`write_build_manifest()` creates/chmods an existing parent without validating
ownership or rejecting symlink parent components. Lease/lock code validates a
path, then reopens it by pathname; `O_NOFOLLOW` protects only the final lock file,
not parent replacement. Several lstat-then-open read paths have the same same-UID
TOCTOU class.

**How to fix**

Centralize secure-root handling around retained directory FDs and
`openat`/`O_DIRECTORY|O_NOFOLLOW` where supported. Verify owner/mode/device/inode
before and after sensitive operations. Add deterministic parent-swap tests.

---

### AUD-015 — P2 — acceptance and documentation state are internally inconsistent

**Locations**

- `tests/run-control-plane-candidate-tests.sh:5-23`
- `pi/control-plane/CORRECTION_LEDGER.md`
- `pi/control-plane/MIGRATION_ACTIVATION_PLAN.md:1-4`
- current Git status

**Evidence**

- the candidate gate explicitly excludes the known legacy patch-chain skew;
- it does not run `tests/pi-staged-artifact-rollback.sh`;
- Phase 10's required process integration script is absent;
- most new implementation files are untracked and omitted by ordinary Git diff;
- the correction ledger still marks major lifecycle, continuity, migration, and
  test items open;
- the migration plan still says “target plan; not implemented or active.”

The disposable Docker rollback script passed separately, but it is not part of
the advertised gate and it does not repair the installer/manifest defects above.

**How to fix**

Create a clean reviewed source candidate containing every intended file, update
the correction ledger per finding with source/staged/live evidence states, and
make one authoritative gate run all non-live acceptance commands. A skipped
Docker/process prerequisite must produce STOP, not a green candidate result.

## 5. Findings considered but not promoted as confirmed defects

- `ControllerClient.status()` was alleged to observe Git before confirming the
  project. `reconcile_observe_only()` calls `_project_row()` first, so that exact
  allegation was not retained. Opening a writable store can still initialize a
  missing DB and deserves a separate API-side-effect decision.
- reconciliation updates do not check every SQL row count, but they run under
  `BEGIN IMMEDIATE`; no demonstrated concurrent lost update was reproduced.
  Add defensive checks, but do not treat the current code as a proven race from
  that fact alone.
- mutating Git helpers use broad subcommand allowlists. No direct exploit was
  demonstrated in current call sites, but strict per-command schemas should be
  added during hardening.

## 6. Remediation implementation order

### Slice 0 — freeze and create a reviewable candidate

1. Keep Phase 11D disabled.
2. Preserve unrelated/MLRE state and both unresolved heads.
3. Put all intended source under version control in one reviewable candidate;
   classify every other dirty/untracked file as included, excluded, or unrelated.
4. Add failing regression tests for AUD-001 through AUD-012 before fixes.

**Exit:** complete diff is reviewable; reproductions fail for the intended reason.

### Slice 1 — make staged installation and build identity real

Fix AUD-001, AUD-002, and AUD-003 together because package layout, controller
packaging, and manifest identity are one boundary.

**Exit evidence:** real clean npm install, installed controller smoke test, exact
manifest envelope/tree equality, interrupted install rollback, and staged Docker
rollback all pass from the same immutable candidate.

### Slice 2 — repair lifecycle authority

Fix AUD-006, AUD-007, and AUD-008. Freeze authenticated actor/run context and
outbox event schemas before changing clients.

**Exit evidence:** reviewer impersonation fails; writer replay returns one run;
every public lifecycle mutator has atomic state+event tests.

### Slice 3 — repair integration recovery and expose semantic APIs

Fix AUD-009 and the Phase 8/9 parts of AUD-012. Use deterministic saga IDs and
recovery before adding UI conveniences.

**Exit evidence:** every integration crash boundary converges; the secretary can
perform review/analyze/authorize/integrate only through semantic controller
commands.

### Slice 4 — reimplement migration 11A/11B

Fix AUD-004 and AUD-005 as a new typed migration slice, not incremental patches
to the generic file walker.

**Exit evidence:** complete fixture inventory, typed contradictions, full shadow
mapping, field-level reconciliation, crash recovery, and custom/live-root
refusal before any write.

### Slice 5 — implement Phase 10 contract

Fix AUD-010, AUD-011, AUD-012 Inspector/E2E work, and AUD-013.

**Exit evidence:** behavioral continuity tests, healthy silence, exact
root-session/controller binding, read-only Inspector, and twice-run process-level
walking skeleton against staged artifacts.

### Slice 6 — hardening and final non-live acceptance

Fix AUD-014/AUD-015, rerun all suites independently, inspect the surviving diff,
and update the correction ledger with exact evidence.

Only after a fresh independent review and oracle approval should the project
prepare a Phase 11D runbook for explicit user authorization.

## 7. Required final acceptance matrix

At minimum, the replacement non-live gate must include:

```text
real clean npm install from staged first-party package inputs
installed pi-control/extension smoke tests in isolated HOME
full Python control-plane suite
behavioral extension tests (not source regex only)
writer idempotency and complete transactional-outbox tests
reviewer impersonation/authorization tests
all integration crash-boundary recovery tests
full typed migration inventory/import/reconcile crash matrix
exact staged and installed build-manifest tree equality
installer interruption/rollback matrix
Docker staged artifact/image/filesystem rollback
process-level personal + secretary walking skeleton twice
privacy/forbidden-content scan
git diff --check over a complete tracked/staged candidate
```

A missing runtime prerequisite is a recorded STOP condition. It is not an
accepted skip for canary readiness.

## 8. Changed system surfaces requiring deep re-review

- **Public API/CLI:** missing review/integration operations; controller artifact
  absent from install.
- **Schema/lifecycle:** operation/run idempotency and outbox completeness.
- **Git/working copies:** integration recovery and cross-operation lock model.
- **Authority/security:** reviewer identity and controller CLI provenance.
- **Migration:** source inventory, typed precedence, full import mapping, crash
  reconciliation, and shadow-root proof.
- **Installation/rollback:** local package layout, manifest identity, exact-tree
  proof, and post-activation verification.
- **UX/observability:** continuity semantics, healthy silence, root binding,
  Inspector, and non-reassuring errors.
- **Tests:** clean installed paths, process-level E2E, crash boundaries, and
  complete candidate coverage.

## 9. Bottom line

The implementation contains useful foundations and many strong local tests, but
the current completion state overstates integration, installation, migration,
continuity, and recovery readiness. Treat this report as a new pre-canary defect
ledger. Do not proceed to Phase 11D until every P0/P1 item is closed with
executable evidence and one fresh independent review of the complete tracked
candidate.

## 10. Candidate remediation update (non-live)

The remediation candidate now closes the following boundaries in source and
executable tests: first-party staged package/controller installation and exact
manifest identity; lifecycle operation/result idempotency and transactional
control events; authenticated reviewer authority and immutable revision
provenance; deterministic integration-result recovery across ref/row/worktree
crash boundaries; semantic client/CLI/extension review, integration, recovery,
and technical-details reachability; bounded continuity cards with deduplication
and healthy-footer silence; newline-correct bounded root-session header parsing;
read-only Inspector Control projection; structured non-reassuring error
projection; and directory-FD/no-follow handling for staged manifests, leases,
observation locks, and integration locks.

Evidence recorded for this candidate:

- Fresh independent remediation review: **ACCEPT** after the integration lock
  symlink-escape repair.
- `python3 -m unittest discover -s tests/control_plane -p 'test_*.py'`:
  **209 tests OK**.
- `bash tests/run-control-plane-candidate-tests.sh` passed provenance,
  extension/child E2E, 209 control-plane tests, static/acceptance tests,
  installer transaction, and process/client/continuity/Inspector integration,
  then stopped with **exit 77** because the Docker daemon/image prerequisite
  for `tests/pi-staged-artifact-rollback.sh` was unavailable.
- `pytest` was not used because it is unavailable in this environment;
  repository policy names `unittest` as authoritative.

A follow-up four-perspective adversarial review found and repaired additional
candidate defects: terminal operation regression, future/unemitted event cursor
acknowledgement through both APIs, cross-project technical-detail disclosure,
null-to-repository root-session rebinding, parent-swap/symlink lock and
integration-worktree paths, mutable submitted review/authorization records,
malformed or expired runtime manifests, incomplete operation idempotency
bindings, and an authorization cancellation race after Git CAS. Schema v6
mechanically freezes submitted receipts, authorization scope/terminal state,
and terminal operation outcomes.

AUD-004/AUD-005 remain explicitly partial pending a fuller typed legacy field
mapping and crash-resumable reconciliation slice. AUD-011 remains partial
until root-session registration consumes an exact controller conversation /
working-copy binding rather than treating header `cwd` as authority. Phase
11D remains blocked; no live activation, migration, deployment, daemon, or
remote publication occurred.

## 11. Completion-plan audit update

A later configured-action and integration-path audit clarified that the accepted
209-test result is **component candidate evidence**, not complete harness or
staging acceptance. In particular:

- active `bin/pi`, root, workspace, and secretary paths still use legacy
  lifecycle stores while `scripts/pi_control` remains a separate candidate;
- workstream creation currently assumes pre-created resources rather than one
  complete controller worktree/session/run/runtime saga;
- several named E2E checks seed SQLite directly or test help/source transform
  reachability instead of launching the exact staged path;
- staged first-party package trees still require proof that real Pi loaded those
  trees without co-loading legacy packages;
- `pi-restart` currently uses broad `tmux kill-server` behavior that must be
  replaced with exact managed-session handling;
- legacy root migration currently selects duplicate histories by modified time,
  contrary to typed no-timestamp precedence;
- full action traceability, typed migration adapters/mappings, exact activation
  bootstrap, real presentation coverage, and Docker rollback remain open.

The canonical repair program is now:

- `COMPLETION_IMPLEMENTATION_PLAN.md` — goals, authority, schema/API/manifest
  contracts, resolved boundaries, and C0–C11 dependency order;
- `IMPLEMENTATION_SLICE_BRIEFS.md` — worker-sized exact handoffs;
- `SYSTEM_INTEGRATION_TEST_PLAN.md` — configured action catalog, tiers, journeys,
  faults, traceability, and evidence.

These findings do not reopen the independently accepted local invariants unless
an implementation slice changes them. They do prevent any claim that all phases
or Phase 11C staging acceptance are complete. Phase 11D remains STOP.
