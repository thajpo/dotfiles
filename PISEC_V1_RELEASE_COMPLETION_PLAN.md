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
newly proven and repaired Phase 10 source blocker, and the ordered route from
that blocker to completion. In particular, it supersedes stale statements that the commits
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
  `7b1423d27f89ea36d17da174e974ff62219acb76` with tree
  `105bfeca338c9b48a25ce501e7cd8d5ccac6f480`. It contains the bounded
  harness-aware fleet usability repair, fail-closed binding-harness identity
  validation, protected OMP/Codex activation and retained-backup replacement
  parity, the role-aware OMP worker startup attestation repair, and the
  Codex launcher repair required to accept the sealed `CODEX_HOME` surface.
  It also rebases generated Codex MCP and lifecycle-hook paths when a staged
  profile is activated, so the immutable surface cannot retain deleted staging
  paths and a bounded parser allowance for legitimate large Herdr snapshots.
- Its clean source gate passed: Python 268 tests with 1 skipped, Bun 9/9,
  compileall, shell syntax, operation-catalogue parity, Bun build, and
  `git diff --check`. The focused Pisec regression cluster passed 51 tests
  with no skips for the latest Herdr/runtime subset, including the production
  Codex activation assertions and large-snapshot coverage.
- The final documentation/status descendant will be the new
  `finalV1Commit`; the corrected source-core commit above is the
  `bootstrapV1Commit`. The descendant's full OID and tree must be captured
  after the final documentation update and before live mutation.
- No local `pisec-v1.0.0` tag exists.

This plan file is tracked and preserved as the executable handoff. The
compact status journal remains the only status record and contains only the
required phase, commit, checks, and blocker fields.

### 3.2 Current deployment and retained recovery material

- The retained corrected-bootstrap deployment is
  `deploy-5bb1e21eee1f4aa28ce84faf3e6bb767`, sourced from
  `3c1034ff4334f605a8839690eaad0da36b4ebaa6` with tree
  `cbcf96bd784d627b18a3bbf450e3d630a4ec6363`.
- The currently installed deployment is that bootstrap candidate with a
  partially replayed state; it is not final release evidence and must be
  replaced by the new corrected candidates.
- Its deployment bundle SHA-256 is
  `cba0099ad8b8557e1b27d663c583387a3bb909fb6bc6c2f69e7ca710116b0930`.
- Stable updater SHA-256:
  `9dd93dd26ecd4635e888eec9ef63fa9480fd367066a11ed096d7448daaeb9846`.
- The installed stable updater identifies final-candidate source
  `921f1045126cf3d1b69144e91d3b0e510dc24ead`, tree
  `56ecf2a2402e031744cda163246decab0ac080ff`, and bundle
  `cba0099ad8b8557e1b27d663c583387a3bb909fb6bc6c2f69e7ca710116b0930`.
  It must be refreshed from the final documentation/status descendant after
  that candidate is committed and accepted.
- Database identity/version: `pisec-core-v1`, version `1`.
- Schema SHA-256:
  `8f844d7e1835ec966041c79c70545a4f1def83cbf4f9dae19f94496388c633c5`.
- The corrected-bootstrap archive is
  `/home/j/.local/state/pisec.archive-20260826T185553Z`; its owner-only
  manifest is
  `/home/j/.local/lib/pisec/archive-manifests/pisec.archive-20260826T185553Z.json`.
- The original pre-v1 archive and its reviewed inventory/runbook remain
  preserved at their previously recorded paths. A final cutover archive must
  also be preserved and referenced by the external acceptance record.
- Reviewed re-registration runbook:
  `/home/j/.local/lib/pisec/cutover-runbooks/pisec-v1-reregister-20260826T124752Z.json`.
- Pre-cutover inventory:
  `/home/j/.local/lib/pisec/cutover-inventories/pisec-v1-precutover-20260826T124752Z.json`.
- The corrected-bootstrap reset intentionally has no compatible last-known-good
  deployment. The ordinary update to the final documentation/status descendant
  must establish the LKG, after which the recovery drill runs.

### 3.3 Current fresh-v1 live state

- The current corrected-bootstrap replay is partial: fourteen reviewed
  repositories were registered, four active project opens completed and two
  additional opens completed after bounded timeout verification. The vla-infra
  open failed closed after Herdr snapshot parsing exceeded the global JSON item
  bound; eight OMP bindings are present and the state is not final evidence.
  No worker acceptance, integration, completion, or cleanup record exists.
  This partial state must be archived/reset through the verified updater after
  the new corrected candidates are installed.
- The final documentation/status candidate, clean descendant source
  acceptance, corrected deployment/LKG, recovery drill, 51-scenario evidence,
  external acceptance record, and local tag remain outstanding.

## 4. Resolved source blockers and remaining release work

The earlier deterministic scope, fleet-transition, and OMP worker-attestation
blockers were reproduced, covered by fail-first tests, repaired in bounded
source commits, and passed the clean source gate. The mandatory live Codex
worker probe exposed two bounded root defects. First, profile construction
seals `CODEX_HOME` as part of the immutable surface, while the Codex launcher
required that directory to be writable and also called `secure_file` with an
unsupported keyword. The fail-first production launcher test reproduced the
exit-126 preflight failure; the earlier corrected source commit
`b41efa5a687bba9132cd318490b7b8e95b3d94bc` fixed that boundary. Second,
activation rebased returned artifact paths and policy but not generated Codex
`config.toml` and `hooks.json` contents. The live surface therefore pointed
at deleted staging files and Codex exited before its hook could report
attestation. The fail-first path assertions and source repair are in
`3c1034ff4334f605a8839690eaad0da36b4ebaa6`. The corrected source gate is
green, and the host Codex dependency now reports the exact pinned `0.147.0`.
The remaining work is the final documentation/status source-acceptance
descendant, corrected deployment/recovery, archive/reset of the partial
failed-worker state, and complete live acceptance; it must not introduce
another source workaround.

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

The failed live probe created no completion, acceptance, integration, merge, or
cleanup record. Its single needs-attention worker operation is explicitly
included in the reviewed partial-state archive/reset scope.

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
