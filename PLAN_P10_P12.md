# Implementation Plan: P10-P12 + Audit Fixes

**Status tracking document for the plan agreed in plan mode. Check off items as
they are implemented. Every phase must end with:**
- `python3 -m unittest discover -s tests/control_plane -t .` passing
  (baseline: 309 tests, 1 pre-existing `test_runtime_spec` failure)
- `python3 tests/system/validate_plan_docs.py` printing `valid`

All work is in `/home/j/dotfiles`.

---

## Phase 0 — Audit Bug Fixes (do first; cheap, several affect P10-12 evidence)

### [x] Fix 0.1 — P8: review verdict gating: only pass if ALL reviews pass

- **File:** `scripts/pi_control/integration.py` (`authorize_integration`)
- **Bug:** `authorize_integration` (~line 307-315) accepts any single `accept`
  verdict even if a later `changes_requested` exists. No conflict resolution.
- **User decision:** multiple reviews per (change, revision) ARE allowed
  (scoped reviews of different parts of a commit are valid), but authorization
  requires ALL submitted reviews to be `accept`.
- **Fix:** gate becomes — at least one submitted review with
  `verdict='accept'` AND zero submitted reviews with any other verdict
  (`changes_requested` or `comment`) for the same `(change_id, revision)`.
  Any non-accept blocks.
- **Escape hatch:** after a `changes_requested`, submitting revision 2 resets
  the review set (staleness already modeled); the old blocking review no
  longer applies.
- **Interpretation note:** `comment` is treated as non-pass (blocks). If
  `comment` should be neutral/informational, that is a one-line change later.
- **Tests:** accept+accept -> authorize succeeds; accept+changes_requested ->
  blocked; changes_requested alone -> blocked; no reviews -> blocked (existing
  behavior); after new revision, fresh accept -> succeeds.

### [x] Fix 0.2 — P8: superseded revision can still be authorized/merged

- **File:** `scripts/pi_control/integration.py`
- **Bug:** `authorize_integration` and `integrate` never re-check that
  `analysis.revision` is still the change's current revision or that the
  change is open.
- **Fix:** In `authorize_integration`, after loading the integration attempt:
  `SELECT state,current_revision FROM changes WHERE change_id=?` — raise
  `ConstraintError` unless `state='open'` and `current_revision ==
  analysis.revision`. Same check at the top of `integrate`.
- **Test:** `test_integration_analysis.py`: analyze -> submit revision 2 via
  `submit_change_revision` -> assert `authorize_integration` raises.

### [x] Fix 0.3 — P8: reviews submittable for merged/closed changes

- **File:** `scripts/pi_control/reviews.py`, `request_review` (~line 142)
- **Fix:** Extend the revision query to also select `c.state`; raise
  `ConstraintError` unless `state IN ('open','draft')`.
- **Test:** submit change -> mark it merged via SQL -> assert `request_review`
  raises.

### [x] Fix 0.4 — P7: child run double-launch terminalizes live run

- **File:** `scripts/pi_control/subagents.py:184-209`
- **Bug:** Replay/concurrent `run_controller_child` creates a second run; the
  failed `bind_child_run` triggers `record_child_terminal` on the *first*
  (still-running) child, and the second run orphans.
- **Fix:** In the `except` block (~200-206), only terminalize if the request
  has no `child_run_id` yet AND no live run exists for the request's
  conversation. Specifically: before calling `record_child_terminal`, query
  `SELECT run_id,observed_state FROM runs WHERE conversation_id=? AND
  observed_state NOT IN ('stopped','failed','lost')`; if such a run exists,
  fail the new run instead (`fail_run`) and re-raise without touching the
  first. Also fix 203-205: when `child_run_id` is NULL after launch failure,
  include the original exception in the `SubagentError` message instead of
  masking it.
- **Test:** `test_p7_workstreams.py` or new `test_p7_child_launch_replay.py`:
  create child assignment, simulate a second launch for the same request,
  assert first run is not terminalized and no orphan run rows remain in
  non-terminal states.

### [x] Fix 0.5 — P6: package execution strands requests in `running`

- **File:** `scripts/pi_control/package_environment.py:347-375`
- **Bug:** Consume sets `state='running'` at 347; the immutable-input
  re-validation (354-358) raises *before* the try block at 362, leaving the
  request stuck `running` with its one-use approval burned.
- **Fix:** Move the re-validation (lines 354-358) inside the `try` so failures
  flow to the failure path that finalizes the request as `failed`. Match the
  pattern in `command_requests.py:259-283`.
- **Test:** new case in `test_p6_package_materialization.py`: tamper the input
  tree between approve and execute -> assert request ends `failed`, not
  `running`.

### Fix 0.6 — DROPPED (not a bug)

- P6 audit finding: message transitions not bound to owning conversation.
- **User decision:** agents can obviously talk across conversations within a
  project — shared inbox model is INTENDED. No change. Do not implement.

---

## [x] Phase 1 — P10: Restart/Cancellation/Reconciliation/Faults/Tmux (installed journey)

### Key design decision: fault injection mechanism

- Failpoints in this codebase are **constructor-injected Python objects**
  (`failpoint=...` params in `integration.py`, `changes.py`,
  `greenfield_workstreams.py`, `leases.py`). They cannot cross process
  boundaries; the staged build runs in separate processes.
- **User decision (approved):** the installed journey uses real
  process/container kills (SIGINT, SIGKILL, `docker kill`, controller
  restart) rather than code-level failpoints.
- The fine-grained interior failpoint matrix is already proven at source tier
  (`test_integration_cas.py`, `test_p7_workstreams.py`, `test_failpoints.py`).

### Files to create

**`tests/system/fixtures/installed-p10.py`** (~350 lines) — five scenarios in
one journey, modeled on `installed-p9.py`'s structure (tempdir, staged root
via `PI_SYSTEM_STAGED_ROOT`, `cli()` helper collecting commands, evidence via
`tests.system.evidence.Evidence`/`write_evidence`):

1. **Temporary-run interruption** (covers HA-003 `investigation-interrupt`,
   `staged-installed`):
   - Register build + project via `bin/pi-control`.
   - Launch `bin/pi-system-investigator` with scripted provider (reuse
     `tests/system/fixtures/scripted-provider.ts` and the `run_launcher`
     SIGINT pattern from `p7_installed_journey.py:72-97`).
   - SIGINT mid-run. Assert: run terminalized (`observed_state` in
     stopped/failed), conversation archived, no leaked Pi process (`pgrep` by
     session file path or state-root pattern), investigation row has
     interrupted terminal state.

2. **Restart recovery / durable session continuity** (covers HA-015/HA-016
   durability):
   - Run secretary launcher with scripted provider; let it complete.
   - Kill any lingering controller processes; re-invoke `bin/pi-control
     project status`.
   - Assert: conversation, messages, session file, and attention rows all
     survived; the resumed secretary session has contiguous history (reuse
     pattern from `installed-pi.py:268-273`).
   - Assert `controller_restart_epoch` in `control_meta` rotated vs. initial
     value (SQL query).

3. **One-writer under container kill** (covers HA-005 `second-writer-refused`,
   requires Docker; STOP/77 if unavailable — same guard as
   `p7_installed_journey.py:101-106`):
   - Start a personal writer run via `bin/pi-system-container-run` with the
     P7 scripted provider.
   - `docker kill` the managed container mid-run (find via `docker ps -q
     --filter label=pi.control.managed=true`).
   - Assert: a second writer request for the same working copy is refused;
     after reconciliation/cleanup, exactly one new writer can claim; no
     managed containers remain at the end.

4. **Integration crash recovery / preserved ambiguity** (covers HA-009
   robustness):
   - Reuse the P9 flow: change -> review -> analyze -> authorize.
   - Kill the `pi-control integration integrate` process mid-flight (start it
     via `subprocess.Popen`, SIGKILL after a short delay; assert it died).
   - Run `bin/pi-control project reconcile` and `recovery status`.
   - Assert: target ref is either unchanged OR integration resumes
     deterministically on retry with the same `integrationId` (idempotent
     recovery); never a half-updated ref without a rollback ref. Assert an
     `integration.needs_resolution` or success — never silent corruption.

5. **Unrelated tmux preservation**:
   - `tmux new-session -d -s p10-unrelated 'sleep 600'` before the journey
     (skip scenario if `tmux` absent — print SKIP to stderr, do not fail).
   - After all other scenarios: `tmux has-session -t p10-unrelated` must
     succeed; `tmux kill-session -t p10-unrelated` cleanup.

**`tests/system/run-p10-installed.sh`** (5 lines, same shape as
`run-p9-installed.sh`).

### Evidence

One envelope per scenario (or one envelope with all assertions — follow P9's
single-envelope pattern). `scenarioId`: reuse the matching existing scenario
IDs (`investigation-interrupt`, etc.); `actionIds` accordingly;
`tier: staged-installed`; validate with `evidence.schema.json` +
`validate_evidence`.

### Plan doc

Set P10 to `installed-passed` with build ID, evidence paths, and one-line
summary of the five scenarios.

---

## [x] Phase 2 — P11: Complete journey, strict evidence, OpenCode comparison

### [x] 2a — `bin/pi-activate` skeleton (flips HA-012 to `implemented-source`)

- **This unblocks P11's release verifier** (evidence.py rejects any `planned`
  action in release mode). The full activation *execution* with human
  approval is P12; P11 only needs the source implementation + source tests.
- **`bin/pi-activate`** (~20 lines): same wrapper shape as `bin/pi-authorize`
  (source-tree import via `scripts/pi_control`, fallback to
  `PI_SYSTEM_CONTROL_ROOT`).
- **`scripts/pi_control/activation_cli.py`** (~120 lines), modeled directly on
  `authorization_cli.py`:
  - Args: `--state-root`, `--staged-root`, `--data-root`,
    `--test-only-decision approve|reject`.
  - Build the display: exact `buildId`, `manifestDigest`, staged root path,
    data root path, rollback plan (from `greenfield_install` metadata).
  - TTY approval: reuse the `/dev/tty` pattern from
    `authorization_cli.py:57-74` verbatim.
  - **Noninteractive test path:** copy the `_test_fixture` gate but fix the
    known lexical bug (`authorization_cli.py:39` — flagged in P6 audit): use
    `root.resolve().is_relative_to(Path("/tmp").resolve())` and keep the
    marker-file + env-var + mode-0600 checks. Marker content:
    `P12-NONPRODUCTION-TEST-ONLY\n`, env `PI_ACTIVATE_TEST_FIXTURE=1`.
  - On approve: call `greenfield_install.activate(staged_root, data_root)`
    (existing function, `greenfield_install.py:715-739`), then
    `ensure_fresh_state`.
- **Source tests** `tests/control_plane/test_p12_activation_cli.py` (~150
  lines): TTY-absent rejection; test-fixture approve/reject; digest mismatch
  refusal; idempotency (double activate of same stage is safe/no-op); marker
  validation (wrong mode, symlink, wrong content all rejected).
- **Manifest:** flip HA-012 `status` to `implemented-source` in
  `tests/system/action-manifest.v1.json`. `python3 -m unittest
  tests.system.test_action_manifest` must pass.

### [x] 2b — Release evidence aggregator

- **`tests/system/p11_release_verify.py`** (~200 lines):
  - Accept `--evidence-dir` globs for every phase's evidence directories.
  - Load all envelopes; call `validate_release_evidence` from
    `tests/system/evidence.py` (existing, line ~90).
  - Additional checks: all 18 HA action IDs covered by at least one PASS
    envelope with `installedProductActionObserved=True`; every envelope's
    `buildId` matches; scenario/tier of each envelope is declared in the
    action manifest for that action; no envelope contains `status != PASS`.
  - Print a coverage table; nonzero exit on any gap.

### [x] 2c — OpenCode guard

- **`tests/system/fixtures/opencode_guard.py`** (~100 lines):
  - Snapshot (path, size, sha256, mtime) of `~/.config/opencode/` and
    `~/.opencode/` (excluding `node_modules`, caches) before the full journey
    and after.
  - Assert identical sets; assert `opencode --version` still exits 0.
  - Emits an assertion dict consumed by the P11 evidence envelope
    (`opencodeConfigUnchanged: true`, `opencodeLaunchable: true`).

### [x] 2d — Full-journey runner

- **`tests/system/run-p11-release.sh`** (~60 lines): runs the P5/P6/P7/P8/P9/P10
  installed journeys in sequence against ONE staged build (set
  `PI_SYSTEM_STAGED_ROOT` so all share one build), then runs `opencode_guard`
  + `p11_release_verify.py`. Runs the whole thing twice (fresh tempdirs) to
  satisfy "one artifact completes the journey twice."

### Plan doc

P11 -> `installed-passed` with the aggregated evidence listing.

---

## [x] Phase 3 — P12: Atomic cutover, exact-generation rollback, activation

### [x] 3a — Activation approval module

- **`scripts/pi_control/activation_approval.py`** (~120 lines): one-use,
  expiring approval records bound to `sha256(buildId + stagedRoot + dataRoot +
  rollbackPlan)`. Reuse the `authorizations` table
  (`kind='activate-build'`) if its schema supports it (check
  `greenfield_schema.py:285-295`); otherwise a small dedicated table. One-use
  consume via the same CAS pattern as `command_requests.py`.

### [x] 3b — Extend `greenfield_install.activate()`

In `scripts/pi_control/greenfield_install.py:715-739`, add (keeping the
existing verify/rename/fsync core untouched):
1. **Launch lock:** `fcntl.flock` on `<data_root>.activation.lock` for the
   whole activate/rollback operation; non-blocking acquire, fail if held.
2. **Bounded smoke:** after the rename, run the activated
   `bin/pi-control --version`-equivalent (or `build register` dry check) with
   a 30s timeout; on failure, auto-rollback and raise.
3. **Protected-surface check:** before rename, snapshot existence of OpenCode
   dirs (`~/.config/opencode`, `~/.opencode`) and unrelated tmux sessions
   (`tmux ls`); after smoke, re-verify unchanged. This is the enforcement of
   `opencode-protected-until-approval` (HA-012 assertion).

### [x] 3c — P12 installed journey

- **`tests/system/p12_activation_journey.py`** (~250 lines):
  1. Stage build A; `pi-activate` (test-fixture approval) into a disposable
     `data_root`; assert activation marker, fresh state initialized, smoke
     passed.
  2. Run a minimal journey against A (project register + secretary scripted
     run) to create real state.
  3. Stage build B; activate over A; assert A moved to `.rollback.*` sibling,
     B live.
  4. `bin/pi-install rollback`; assert B preserved as `.preserved.*`, A
     restored, A's state intact.
  5. Assert OpenCode guard clean and unrelated tmux session survived
     throughout.
  6. Evidence envelopes: HA-012 (`final-activation-approved`, tier
     `activation`) and HA-013 (`rollback-preserves-new-state`, tier
     `rollback`).
- **`tests/system/run-p12-activation.sh`** (5 lines).

### Plan doc

P12 -> `release-passed` ONLY after explicit human approval step (the real TTY
approval, not the test fixture) — the runbook's step 10. The journey above
proves the mechanics; the status flip to `release-passed` requires the user to
run `bin/pi-activate` for real. **User decision:** the implementer stops at
`installed-passed` mechanics and leaves the real activation to the user.

---

## Sequencing & parallelism

```
Phase 0 (fixes 0.1-0.6)     — sequential, small, each lands with its test
Phase 1 (P10 journey)       — can start immediately; independent files
Phase 2a (pi-activate src)  — can start immediately; independent files
Phase 2b/2c/2d (P11)        — needs Phase 1 + 2a evidence/status
Phase 3 (P12)               — needs Phase 2 complete
```

Phase 0, 1, and 2a can run concurrently in separate worktrees if desired;
merge order: Phase 0 -> 2a -> 1 -> 2bcd -> 3.

---

## Decisions locked in plan mode (verbatim intent)

1. **P10 fault injection:** approved process-kill-based faults for the
   installed journey (no new production failpoint plumbing).
2. **Message binding (Fix 0.6):** dropped — "yes. obviously agents can talk
   between file paths." Project-wide message resolution is intended.
3. **Review verdicts (Fix 0.1):** "this is a clear bug that has interesting
   dynamics... we only accept as pass if all reviews pass." Multiple scoped
   reviews allowed; ALL must be accept.
4. **P12 final approval:** "ok. sure." — implementer stops at installed-passed
   mechanics; real activation requires explicit user TTY approval.
