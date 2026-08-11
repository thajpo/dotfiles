## Objective
- Implement the remaining Pi spec: P10 (robustness), P11 (complete journey + evidence + OpenCode comparison), P12 (cutover/rollback/activation) per `PLAN_P10_P12.md` (all 15 items checked), plus fix all audit bugs found in P6-P8.
- **DONE:** All phases P1-P12 at `installed-passed` except P12 which stops at `installed-passed` mechanics — the real `bin/pi-activate` TTY approval on the live install is left to the user (by agreement).

## Important Details
- **Phase 0 — audit fixes (all landed + tested):**
  - 0.1 review verdict gating: authorize requires ALL submitted reviews accept (any changes_requested/comment blocks); multiple scoped reviews allowed.
  - 0.2 superseded-revision guard at authorize + integrate (change must be open at analyzed revision).
  - 0.3 request_review rejects merged/closed changes.
  - 0.4 child run double-launch: live bound run no longer terminalized on replay; error cause preserved.
  - 0.5 package execution post-consume failures finalize as `failed` (not stranded `running`).
  - 0.6 dropped (project-wide messaging is intended).
- **Phase 1 — P10:** `tests/system/fixtures/installed-p10.py` proves via real process kills: investigator SIGINT interruption terminalizes; secretary resume preserves contiguous session + conversation; integration killed mid-flight recovers deterministically (target advanced + rollback ref, never half-updated); unrelated tmux session survives; writer container killed with second writer refused + no managed containers remain.
- **Phase 2 — P11:** `bin/pi-activate` + `scripts/pi_control/activation_cli.py` (TTY gate + test-fixture gate with resolved-path fix); HA-012 → `implemented-source`; `tests/system/p11_release_verify.py` aggregator; `tests/system/fixtures/opencode_guard.py` (config snapshot + launchability); `tests/system/run-p11-release.sh` runs all journeys on one build.
- **Phase 3 — P12:** `activation_approval.py` one-use approvals (schema v3, `activation_approvals` table); `activate()` extended with launch lock (flock), bounded smoke, protected-surface verification, conditional fresh-state init; `tests/system/fixtures/installed-p12.py` proves A→B cutover→rollback with state intact.
- **Release verification PASSES:** all 18 HA actions have installed PASS evidence on single build `build_341120867dae530f75308795efbb1835`; OpenCode unchanged + launchable; evidence at `/tmp/pi-p11-release-evidence/`.
- **Resource catalog:** `bin/pi-activate` added to `pi/greenfield-resources.v1.json` launchers + `tests/system/launcher-surface.v1.json` releaseCanary; `activation_cli.py`/`activation_approval.py` added to RELEASE_FILES.

## Work State
### Completed
- All 15 plan items (PLAN_P10_P12.md fully checked).
- User-scenario journey suite (6 journeys, `tests/system/run-u-scenarios.sh`):
  coding-resume (HA-004), integration-agent-conflict (HA-009), multi-project
  (HA-001), review-exact-revision loop (HA-008), investigation-complete
  (HA-003/HA-017), real-TTY approval (HA-011/HA-007 via p6), plus dedicated
  envelopes for message threading (HA-006), locked-package-environment
  (HA-014), second-writer-refused (HA-004), subagent-isolation (HA-018),
  secretary-resume (HA-002), p2-controller-contract (HA-015).
- 24 of 25 declared scenarios now have installed PASS evidence on one build
  (`build_a38720edd263766e9384b8f254a66e27`); only host-startup-attestation-
  rejection remains source-only (inherently in-process race).
- **Product fix found by conflict journey**: `create_review_assignment` could
  not snapshot controller-created integration-result changes
  (`source_working_copy_id IS NULL`); now snapshots from the project primary
  repository (greenfield_review.py) + source test
  `test_integration_result_change_can_be_reviewed`.
- Control_plane suite: 332 tests, 1 pre-existing `test_runtime_spec` failure (failpoint host-isolation tests pass in isolation; flaky only under active opencode session writes to `~/.pi/agent/sessions/`).
- System tests: test_action_manifest, test_evidence, test_greenfield_docs, test_p5_source all pass; source gate passes; validate_plan_docs valid.

### Active
- (none — P12 `installed-passed`; real activation pending user TTY approval)

### Blocked
- P12 `release-passed` requires explicit user approval via `bin/pi-activate` on the real install.

## Next Move
- User runs `bin/pi-activate --staged-root <stage> --data-root <data>` at a controlling TTY, approves ACTIVATE, then plan doc P12 flips to `release-passed`.

## Relevant Files
- `PLAN_P10_P12.md`: the tracked plan (all checked).
- `tests/system/fixtures/installed-p10.py`, `run-p10-installed.sh`: P10 robustness journey.
- `tests/system/run-p11-release.sh`, `tests/system/p11_release_verify.py`, `tests/system/fixtures/opencode_guard.py`: P11 release pipeline.
- `bin/pi-activate`, `scripts/pi_control/activation_cli.py`, `scripts/pi_control/activation_approval.py`: activation CLI + approval.
- `scripts/pi_control/greenfield_install.py`: activate() launch lock + smoke + protected surfaces; RELEASE_FILES += activation modules.
- `scripts/pi_control/greenfield_schema.py`: v3 (`activation_approvals` table).
- `scripts/pi_control/reviews.py`, `integration.py`, `subagents.py`, `package_environment.py`: audit fixes.
- `pi/greenfield-resources.v1.json`, `tests/system/launcher-surface.v1.json`: pi-activate catalog entries.
- `PI_GREENFIELD_IMPLEMENTATION_PLAN.md`: P10/P11/P12 → `installed-passed` (P12 waits for real activation).
- Evidence: `/tmp/pi-p11-release-evidence/` (18 envelopes, build `build_341120867dae530f75308795efbb1835`).
