# Pisec v1 Release Completion Plan

Status: executable Phase 10 repair and release handoff

Prepared: 2026-08-25 (America/New_York)

Work mode: MAJOR

Learning overlay: OFF

## 1. Authority and precedence

This file is the compact execution index for finishing Pisec v1. It does not
replace the behavioral or architectural contract in
[`PISEC_V1_FINALIZATION_PLAN.md`](./PISEC_V1_FINALIZATION_PLAN.md).

The implementation agent must read this file first, then consult the referenced
sections of the authoritative plan before changing source or live state:

- Phase 10.1 for immutable source acceptance;
- Phase 10.2 for the complete 51-scenario matrix;
- Phases 10.3 through 10.6 for inventory, cutover, live proof, evidence, and
  tagging;
- Section 15 for the Definition of Done; and
- the earlier contract sections implicated by a source change.

If this file and the authoritative plan differ about product behavior,
architecture, safety, or acceptance, the authoritative plan controls. This
file controls only the later factual handoff: current source/live state, the
newly proven Phase 10 release blocker, and the ordered route from that blocker
to completion. In particular, it supersedes stale statements that the commits
currently named as Phase 10 candidates are releasable without another source
correction.

Also maintain
[`PISEC_V1_IMPLEMENTATION_STATUS.md`](./PISEC_V1_IMPLEMENTATION_STATUS.md) in
the compact format already required: phase status, commit OID, checks and
results, and current blocker only. Do not turn it into a narrative log.

## 2. Mission and operating rules

Finish Phase 10 so the operator can use Pisec. Do not restart Phases 0 through
9, redesign the architecture, add compatibility layers, create a generic
framework, or admit parked work. Repair the root invariant, prove it through
public production paths, complete live acceptance, and create the local v1 tag.

Use MAJOR program control and BUILD discipline inside each bounded stage. For
every source stage:

1. establish fail-first evidence;
2. implement only the behavior specified here and in the authoritative plan;
3. run focused checks and relevant regressions;
4. obtain an independent, max-reasoning, read-only audit against the plan;
5. commit the coherent result; and
6. continue immediately.

No implementation worker is required. A worker created later in the live
acceptance sequence is a Pisec product-path test required by Phase 10.2 and
Phase 10.5, not implementation delegation.

The operator has already authorized wiping Pisec and Pisec-worker sessions if
normal recovery of the current partial fresh-v1 state cannot satisfy the
contract. Archive before reset; do not manually edit database rows, perform
ad-hoc path deletion, or touch unrelated user data. Do not push commits or
tags.

## 3. Current immutable evidence

### 3.1 Repository state before this handoff

- Earlier candidates and their live acceptance attempts are historical only;
  their identities must not be used for v1 release evidence.
- The corrected source-core commit is
  `3eb53de5764c40744499812388e3bdcef93b5860` with tree
  `786dc295def4da640b0feaf67b56560ecb675d50`. It contains the bounded
  harness-aware fleet usability repair, fail-closed binding-harness identity
  validation, and the protected OMP/Codex activation and retained-backup
  replacement parity repair.
- Its clean source gate passed: Python 266 tests with 1 skipped, Bun 8/8,
  compileall, shell syntax, operation-catalogue parity, Bun build, and
  `git diff --check`. The focused Phase 10 scope/adapter slice passed 15/15,
  including production OMP/Codex activation/replacement/sealing with sealed
  existing targets and retained cross-tree backups, the independent First
  Mate harness-mismatch guard, and fail-closed omitted harness validation.
- The final documentation/status descendant will be the new
  `finalV1Commit`; the corrected source-core commit above is the
  `bootstrapV1Commit`. The descendant's full OID and tree must be captured
  after the final documentation update and before live mutation.
- No local `pisec-v1.0.0` tag exists.

This plan file is tracked and preserved as the executable handoff. The
compact status journal remains the only status record and contains only the
required phase, commit, checks, and blocker fields.

### 3.2 Current deployment and retained recovery material

- The current intermediate deployment is
  `deploy-090c19bfeaa24881a72deb5e17e61c71`.
- Its deployed source is the reviewed pre-correction replay candidate
  `c4e116cd4e1487b7160841b3b94f4c541e2a6402`, tree
  `a5e1c11f01da4272effdb5310d83e3b050a8c513`.
- Its deployment bundle SHA-256 is
  `4671069e15bad3566b937b1418993ac25503228690c59f80104d71256fa98289`.
- Stable updater SHA-256:
  `59fbb73053947de7a53a9d419546d87e3e574b6b5b583e084985eea4669edda7`.
- The installed stable updater currently identifies the prior final
  documentation source `503f71379a45de36dc29a78ba57b67628fbc339f`, tree
  `1de067f2c3583d2f024ec956ccad714c1fb1346e`, and bundle
  `4671069e15bad3566b937b1418993ac25503228690c59f80104d71256fa98289`.
  It must be refreshed from the final descendant after this source-core
  correction is recorded.
- Database identity/version: `pisec-core-v1`, version `1`.
- Schema SHA-256:
  `8f844d7e1835ec966041c79c70545a4f1def83cbf4f9dae19f94496388c633c5`.
- The latest intermediate v1 archive is
  `/home/j/.local/state/pisec.archive-20260826T141551Z`; its owner-only
  manifest is
  `/home/j/.local/lib/pisec/archive-manifests/pisec.archive-20260826T141551Z.json`
  with manifest SHA-256
  `55a8c4a1f135a6ad50ee8e2fd43a7d39f0f5c939654123bdd3bf0250d22e129f`.
- The original pre-v1 archive and its reviewed inventory/runbook remain
  preserved at their previously recorded paths. A final cutover archive must
  also be preserved and referenced by the external acceptance record.
- Reviewed re-registration runbook:
  `/home/j/.local/lib/pisec/cutover-runbooks/pisec-v1-reregister-20260826T124752Z.json`.
- Pre-cutover inventory:
  `/home/j/.local/lib/pisec/cutover-inventories/pisec-v1-precutover-20260826T124752Z.json`.
- The intermediate reset intentionally has no compatible last-known-good
  deployment. The final corrected-bootstrap reset must establish the LKG during
  the ordinary update to `finalV1Commit`, after which the recovery drill runs.

### 3.3 Current fresh-v1 live state

- The intermediate reset is clean: all 14 existing repositories from the
  reviewed runbook are registered inactive in project mode; the absent
  `/tmp/pisec-live-acceptance-epoch3` placeholder is intentionally excluded.
- Nine intended-active projects are open in project mode with bound, idle,
  usable Secretaries; one canonical First Mate is bound and idle. No worker,
  reservation, or attention is present.
- Fleet transitions were intentionally attempted only as a fail-first probe
  on the older `4127ebb` replay and refused because that candidate lacks the
  corrected harness-aware usability path. No live mode was changed by that
  refusal.
- This is an intermediate replay, not final release evidence. The final
  corrected-bootstrap reset, fleet transitions, permissions, ordinary update,
  recovery drill, 51-scenario evidence, and tag remain outstanding.

## 4. Resolved source blockers and remaining release work

The earlier deterministic scope and fleet-transition blockers were reproduced,
covered by fail-first tests, repaired in bounded source commits, and passed the
clean source gate. The remaining work is live cutover and acceptance proof; it
must not introduce another source workaround.

Historical diagnosis (retained to explain the fail-first evidence):

1. Initial Secretary materialization obtained
   `externalDomains = ["*"]` from the existing
   `harness.profile_domains("secretary-project", ())` adapter contract.
2. The authoritative desired-generation payload hashed `externalDomains`.
3. `effective_runtime_scope` in `scripts/pisec/access.py` then replaced the
   role-composed domain scope with the raw project `external_domains`, which is
   currently `[]`.
4. Broker reconciliation called `mark_stale_bindings`, recomputed the desired
   generation from that different scope, and marks every binding stale.
5. For the affected bindings, the initial-materialization generation equaled
   the applied generation and artifact generation; the refresh-composed
   generation equals the current desired generation.
6. `_recover_start` correctly refused a stale applied generation, but a broad
   wrapper degrades the useful error into “secretary runtime identity is
   missing or mismatched.”

The usable-binding predicate correctly rejected the stale bindings. The repair
preserves that predicate, does not exempt supervisors, and does not assign
desired equal to applied without reproducing and attesting the exact
authoritative scope.

### 4.1 Why prior tests were green

`FixtureHarness.desired_generation` in `tests/pisec_fixture.py` hashes only
`executionProfile`. It omits production inputs including external domains,
data directories, model fields, and surface digest. The fixture therefore
made two different production scopes look identical.

The bounded repair also covered two adjacent source blockers:

- worker proposal scopes in `scripts/pisec/workstreams.py` omit
  `externalDomains` from `_FULL_SCOPE_FIELDS` and from the immutable proposed
  scope, despite production OMP/Codex worker Fence policy requiring it; and
- final supervisor activation can persist `bound`/`active`/`succeeded` after a
  local wait without a final authoritative usable-binding postcondition,
  leaving a reconcile race able to commit an already-stale success.

No live worker was created during the diagnosis, so that defect did not widen
live state.

## 5. Required source correction

Implement one role-aware composition rule using the existing adapter contract;
do not create a new policy subsystem.

### 5.1 Canonical runtime scope

- For Secretary and First Mate profiles, pass no project-added domains to
  `harness.profile_domains`; the adapter's role profile supplies the required
  wildcard domain scope.
- For worker profiles, pass the canonical project-added domains; the adapter
  returns its baseline domains plus those additions.
- Keep canonical project `dataDirs` in the runtime scope as required by the
  existing contract.
- Preserve all other generation inputs already required by production: exact
  harness/profile, model fields, runtime surface digest, and policy fields.
- Use the same composer and generation-payload function for initial proposal,
  materialization, desired-generation calculation, permission staging,
  refresh, reconciliation, retry, and binding repair.
- Permission replacement must compose each binding by role and harness. It
  must not overwrite one raw domain list across Secretary, First Mate, and
  worker bindings.

### 5.2 Worker authorization scope

- Add `externalDomains` to the complete immutable worker proposal and human
  authorization scope.
- The production OMP and Codex policies/materialized artifacts must exactly
  equal the approved scope.
- If project permissions change between proposal and authorization, reject or
  reprepare the proposal. Never silently widen an already approved worker.

### 5.3 Truthful supervisor completion and retry

Before committing `bound`, project `active`, or operation `succeeded`, re-read
authoritative state and prove all existing usable-binding conditions under the
existing lock or guarded final transaction:

- desired generation equals applied generation;
- no refresh reservation or ambiguous launch remains;
- exact runtime identity and session-start event match;
- attestation and artifact generation match;
- the expected project workspace identity matches; and
- no concurrent reconcile changed the generation being committed.

Normal public `project.open` retry must safely revalidate and recover the exact
current `needs_attention`/committed operation when effects now match. It must
not duplicate a binding, runtime, project, or operation. Preserve precise
errors such as “applied runtime generation is stale”; do not collapse a known
generation failure into a generic identity mismatch.

### 5.4 Bounded recent-code audit

Audit the recent Phase 10 reopening, saga-rewind, stale-binding, and attestation
patches against the corrected invariant. Classify each touched branch as:

- still required for a distinct crash/retry state;
- replaced by canonical role-aware scope composition or the guarded final
  postcondition; or
- an error-masking workaround.

Remove code only in the latter two categories when a fail-first test proves
the replacement and focused regressions prove the distinct crash states remain
recoverable. Do not revert recent commits wholesale, delete defensive checks
merely because they exposed the problem, or retain duplicated normalization as
a compatibility path.

## 6. Fail-first and regression contract

Add the tests before the repair and record their failure on the prior source.
Tests must use production generation/scope code wherever identity is under
test. Repair `FixtureHarness.desired_generation` so it models the complete
production payload; add a parity assertion so future fixture omissions fail
loudly.

### 6.1 Exact fail-first cases

1. Role/harness composition matrix:
   - OMP Secretary yields `externalDomains = ["*"]`;
   - OMP First Mate yields `externalDomains = ["*"]`;
   - OMP worker yields adapter baseline plus canonical project additions; and
   - Codex worker yields adapter baseline plus canonical project additions.
2. Materialize → bind → reconcile parity: artifact, desired, and applied
   generations are equal, and immediate reconcile makes no change.
3. One-project public path: register → open → reconcile → reopen ends usable,
   idempotent, and doctor-green.
4. Two-project public path: sequential open and reconcile leave both bindings
   usable; opening the second cannot stale the first.
5. Interrupted public retry: a committed/`needs_attention` open resumes through
   `project.open` with no duplicate binding, runtime, workspace, or operation.
6. Finalization race: force reconcile between attestation and final commit;
   the result may retry or report attention but can never be
   active/succeeded with a stale generation.
7. First Mate ensure → reconcile parity with the same usable-binding proof.
8. Production OMP and Codex worker proposal/materialization policy includes
   exact approved `externalDomains`, and permission drift before authorization
   rejects or reprepares rather than widens.
9. Multi-binding, multi-harness permission replacement preserves supervisor
   role domains, updates worker additions, and reaches one fully attested set or
   guarded compensation—never a usable mixed-policy fleet.
10. Doctor is negative for a deliberately stale binding and green after exact
    attested parity.
11. Error projection retains the precise stale-generation cause.
12. Fixture/production generation parity changes whenever any hashed scope
    field changes.

### 6.2 Larger system integration coverage

The following are required because fixture-only tests missed the current
defect. Exercise public dispatch/CLI boundaries and real production scope and
generation helpers, not direct database shortcuts:

- the complete one-project and two-project registration/open/reconcile/reopen
  data-and-action paths;
- OMP and Codex worker proposal → approval → materialization → authenticated
  action/data path, including exact Fence policy;
- permission replacement across supervisor and worker bindings;
- dropped in-memory attention hint plus broker restart, proving durable
  rediscovery and acknowledgement; and
- a multi-target permission operation and an interrupted operation resumed
  through its public idempotent command.

Retain the broad production-dispatch worker action/data test and all existing
refresh, attention, restart, and permission regressions. The focused source
suite must include at least:

```text
tests.test_pisec_secretary
tests.test_pisec_first_mate
tests.test_pisec_refresh
tests.test_pisec_project_policy
tests.test_pisec_workstreams
tests.test_pisec_protocol
tests.test_pisec_doctor
tests.test_pisec_adapters
tests.test_pisec_fence
```

## 7. Ordered execution

### Stage A — Protect the handoff and reproduce the root failure

1. Re-read this file, the authoritative sections it routes to, the compact
   status journal, recent Git history, status, and diff.
2. Preserve this plan as approved in-scope work and establish a clean committed
   base without modifying its content.
3. Capture sanitized current `pisec doctor --json` and update/deployment status.
   Do not mutate live state.
4. Inspect recent scope/retry/finalization changes and record the bounded
   classification in the commit description or temporary audit notes, not in
   the compact status journal.
5. Add the tests in Section 6 and run the focused suite against the unfixed
   implementation. Record failures that demonstrate scope divergence, fixture
   blindness, worker approval omission, or the finalization race.

### Stage B — Correct source and establish new release candidates

1. Implement only Section 5.
2. Run the focused suites and adjacent regressions until green.
3. Run an independent max-reasoning read-only audit of:
   - the authoritative scope/generation invariant at every call site;
   - worker authorization immutability;
   - final activation/reconcile race safety;
   - public retry idempotency and precise errors;
   - recent workaround removal; and
   - non-goals and diff scope.
4. Repair every in-scope audit finding and rerun the relevant checks.
5. Commit the corrected runtime core and record the full OID as the new
   `bootstrapV1Commit` candidate.
6. Update only final documentation/status truth needed for the corrected
   behavior, commit the compact descendant, and record its full OID as the new
   `finalV1Commit` candidate.
7. Run the exact Phase 10.1 gate from clean checkouts of both candidates where
   applicable, and the full gate below from the exact final candidate. Any
   tracked change invalidates acceptance and requires a new commit/OID and a
   complete rerun.
8. Obtain a final independent read-only source audit before live mutation.

### Stage C — Deploy and recover the current partial fresh-v1 state

1. Install the stable updater from the new `finalV1Commit` and verify its
   manifest and digest identify that exact source.
2. Deploy the corrected `bootstrapV1Commit` to the existing v1 database.
3. Attempt normal public recovery exactly once:
   - reconcile the five existing active projects;
   - reopen Dreamer³ through public `project.open`;
   - require every active binding to be usable with
     desired = applied = artifact generation; and
   - require doctor to be green.
4. If normal recovery cannot satisfy the contract without special-case or
   compatibility code, stop trying to preserve the partial fresh-v1 rows.
   Through the verified stable updater, archive and reset that partial Pisec
   state, including Pisec/Pisec-worker sessions, then replay the reviewed
   runbook through normal public operations. This reset is already authorized.
5. Preserve both the original pre-v1 archive and the newly created partial-v1
   archive. No manual row edit or ad-hoc file deletion is permitted.

### Stage D — Complete registration, supervision, and permissions

1. Validate the reviewed runbook and inventory are still owner-only and
   digest-matched.
2. Register all 15 canonical repositories idempotently. The reviewed cutover
   intent is to open each selected project initially in project mode; stop at
   the first Secretary attestation failure.
3. The reviewed intended fleet projects are `dotfiles`, `investing`,
   `jpo.github.io`, `mlre-transition`; the canonical control project is
   `dotfiles`. The only extra data directory is
   `/home/j/Projects/mlre-transition/tmp/jobos-data`.
4. Ensure First Mate only after the control project is active. Prove its usable
   binding immediately before every fleet transition.
5. Apply canonical permissions through the protected public operation. Require
   role-aware generation parity before reporting success.
6. Compare canonical repositories, active state, modes, control project, and
   permission arrays byte-for-byte with the reviewed runbook.
7. Require doctor green, no stale/reserved/ambiguous binding, and no unexplained
   `needs_attention` before proceeding.

### Stage E — Final deployment and recovery proof

1. Write the corrected bootstrap external verification record.
2. Deploy the new `finalV1Commit` through an ordinary update without resetting
   or re-registering state.
3. Verify `current` identifies the final commit and the corrected compatible
   bootstrap deployment becomes LKG.
4. Run `--recover-previous`, prove doctor/reconcile on bootstrap, then update
   back to the exact final commit and prove health again.
5. Refusal against the pre-v1 archive/incompatible marker must remain
   non-mutating.

### Stage F — Complete acceptance, evidence, and local tag

1. Execute all 51 scenarios in authoritative Phase 10.2. Reference them by
   number in evidence; do not copy or silently narrow the matrix.
2. Include the real project-mode worker lifecycle, real fleet remediation,
   dropped-hint/restart attention, permission replacement, independent worker
   Git, Reviewr/Collie separation, auth/Fence boundaries, updater/recovery, and
   interrupted re-registration.
3. Inventory all old Pisec and temporary Git worktrees before cleanup. Correlate
   each `/home/j/.local/share/pisec/worktrees/...` and `/tmp/pisec-*` path to an
   archived or current record. Never delete by broad path or age alone.
4. Run live Phase 10.5 proof, restart components in dependency order, rerun the
   source checks that depend on installed artifacts, and require a clean
   checkout still at `finalV1Commit`.
5. Write and canonical-JSON-validate the owner-only external acceptance record
   exactly as Phase 10.6 specifies.
6. Verify source/tree/bundle/schema/current/LKG/archive identities are mutually
   consistent, then create the annotated local `pisec-v1.0.0` tag at
   `finalV1Commit`.
7. Do not push the commit or tag. Preserve the pre-v1 archive.

## 8. Verification commands

The exact clean-source Phase 10.1 gate is:

```bash
git status --short
git diff --check
python3 -m compileall -q scripts tests
bash -n scripts/*.sh
python3 scripts/generate-pisec-operation-catalogue.py --check
bun build omp/extensions/pisec.ts --target bun --outdir <mktemp-directory>
python3 -m unittest discover -s tests
bun test omp/extensions/pisec.test.ts
```

Use `mktemp -d` for the build directory and record actual counts; never encode
test counts as assertions. Also run the focused modules in Section 6 after each
scope/retry change and `pisec doctor --json` at every live gate.

Before each commit and before every live transition, require at minimum:

```bash
git status --short
git diff --check
```

Inspect staged scope before committing. Do not stage unrelated user work.

## 9. Stop and decision rules

No new product decision is currently required. Continue automatically through
ordinary test failures, source repair, candidate replacement, normal recovery,
and—if recovery fails—the already authorized archive/reset and runbook replay.

Stop for user direction only if:

- existing dirty work cannot be preserved without overwriting or discarding it;
- a tested invariant is impossible or two authoritative requirements genuinely
  contradict;
- completion requires wider authority or product scope;
- an action would push, publish, alter a remote system, or delete unarchived
  user data; or
- new live evidence changes the affected-state inventory beyond the already
  reviewed and authorized Pisec/Pisec-worker reset boundary.

Do not stop because the task is large, a test fails, a safe mechanical choice
is needed, a current candidate OID must be replaced, or a normal public recovery
attempt fails. Do not add a compatibility layer to avoid the authorized clean
reset.

## 10. Completion gate

All of authoritative Section 15 remains mandatory. In addition, completion is
not claimable until all immediate release facts below are proven:

- the corrected core and final descendant have new exact clean OIDs and the
  full source gate is green;
- production and fixture generation payloads have enforced parity;
- every active Secretary and the global First Mate, when required, have exact
  desired/applied/artifact parity and pass the usable-binding predicate;
- the reviewed 15-project registration, intended active states, four fleet
  modes, control project, and permissions match the runbook exactly;
- doctor is green with no stale, reserved, startup-in-progress, or unexplained
  attention state;
- all 51 authoritative scenarios and the added whole-path integrations pass;
- `current` is the corrected final deployment and LKG is the corrected,
  recovery-proven bootstrap deployment;
- external evidence is owner-only and binds every required identity and result;
- local annotated tag `pisec-v1.0.0` points to the corrected final commit; and
- the original pre-v1 archive remains intact and recoverable.

Only then is Pisec v1 usable and complete.

## 11. Durable resume packet

After compaction, restart, or model handoff:

1. read this file;
2. read the relevant authoritative plan sections named in Section 1;
3. read the compact implementation status journal;
4. inspect Git history, current branch, status, diff, and candidate OIDs;
5. inspect current updater/deployment status and sanitized doctor output;
6. identify the first unchecked stage above; and
7. continue from there without repeating completed Phases 0 through 9 or any
   completed, evidence-backed Phase 10 stage.

Update the compact status journal immediately after each new source candidate
commit and whenever the current blocker changes. Keep detailed command output,
live inventories, and release evidence in their designated owner-only external
records rather than expanding the journal.
