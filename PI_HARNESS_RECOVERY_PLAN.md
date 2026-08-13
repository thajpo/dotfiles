# Pi Harness Recovery Plan

**Aligned Product Model**
The old interaction documentation is the product contract. The control plane exists to make those interactions continuous, safe, inspectable, and recoverable.

The migration made a category error: it treated control-plane safety as justification for narrowing the product.

The clearest example is literal:

- `bin/pi-personal:50-51` creates and launches a `workstream`.
- `scripts/pi-surface.py:342-351` says personal is backed by a workstream.
- `scripts/pi_control/role_profiles.py:23-24` gives personal and workstream effectively identical policy.
- The old contract explicitly says personal is not required to use workstream ceremony.

The same narrowing happened to subagents: the full headless orchestration system became one blocking read-only child call.

## Canonical Roles

### `pisec`
One durable, read-only secretary per project.

The secretary should:

- Understand project direction and current work.
- Display controller-derived work, changes, reviews, failures, and attention.
- Launch asynchronous read-only investigations.
- Propose and create headful workstreams after exact approval.
- Request exact-revision reviews.
- Coordinate integration under separate exact authorization.
- Never become an implementation writer.
- Never treat tmux state as product authority.

Its ordered active project set is independent from the personal grid.

### `pi-personal`
One durable direct coding conversation per selected project.

The personal agent should:

- Default to the registered primary checkout.
- Capture the exact pre-task baseline before mutation.
- Operate through controller-owned containerized tools.
- Preserve pre-existing user changes.
- Ask when task ownership becomes ambiguous.
- Launch headless readers and isolated headless workers.
- Submit immutable revisions.
- Never integrate its own result.
- Create a separate headful workstream only when isolation or sustained parallel work warrants it.

Its ordered active project set is independently configurable.

### Workstream
A named, durable, headful implementation conversation.

It should:

- Always use a separate controller-owned working copy.
- Be created from an explicit bounded brief.
- Support direct user discussion.
- Submit immutable revisions.
- Remain available for follow-up.
- Never self-integrate.

### Headless Subagent
A controller-supervised child, not a tmux pane or ordinary headful conversation.

Required capabilities:

- Read-only roles: scout, investigator, researcher, reviewer, planner, oracle, delegate/context builder.
- Mutable worker role in an isolated controller-owned working copy and container.
- Fresh bounded context by default; explicit full-history fork only when required.
- Parallel and asynchronous execution.
- Status, wait, interrupt, stop, steer, and resume.
- Child-to-parent escalation and progress reporting.
- Durable results and lifecycle records.
- Role-specific prompt and model policy.
- One writer per active working copy.
- No arbitrary time, token, turn, or tool limits.

## Control-Plane Mapping

| Product behavior | Control-plane enforcement |
|---|---|
| Durable secretary/personal/workstream | Stable conversation and Pi session identity |
| Personal edits primary checkout | Exact baseline, writer claim, epoch, container mount, per-call fencing |
| Workstream isolation | Controller-created worktree, conversation, run, container |
| Headless reader | Immutable snapshot, scoped adapters, no mutation tools |
| Headless worker | Separate working copy and writer container |
| Async child | Durable child request and run independent of parent tool-call lifetime |
| Child communication | Durable typed parent/child message records |
| Submission | Controller-created immutable commit and revision ref |
| Review | Exact detached revision and independent reviewer |
| Integration | Separate exact authorization and target compare-and-swap |
| Grid membership/order | Controller-owned presentation preference, never conversation identity |
| Restart | New process/run, same durable conversation and work |
| Unknown state | Attention, never guessed replacement or cleanup |

## Cleanup Boundary

### Remove after references are severed
- Old controller/store/CLI family inside `scripts/pi_control/`.
- Migration/import code for historical chats and state.
- Legacy route, lease, registry, root-session, and workspace authority.
- Legacy launcher implementations superseded by controller-backed equivalents.
- Legacy secretary control scripts and extension implementations after their useful behavior is reimplemented.
- Herdr-specific implementation and tests for this release.
- Stale tests that only assert deleted mechanisms.
- Duplicate legacy installer behavior.
- Installed rollback debris and old state after authentication is reprovisioned.

### Preserve as requirements or evidence
- Old `PRODUCT_CONTRACT.md`.
- Old personal and secretary workflow documentation.
- Old full-system action catalog and journeys.
- `pi-personal`, `pisec`, and restart tests that express desired behavior rather than obsolete mechanisms.
- Headless `pi-subagents` role definitions, skill, orchestration semantics, and lifecycle tests.
- Harness observability and feedback requirements.
- Primary-checkout baseline and task-delta semantics.
- Exact approval, review, integration, and recovery invariants.

### Keep operationally
- `pisec`, `pi-personal`, `pi-start`, `pi-restart`, and `pidev`.
- Harness feedback command and storage.
- Provider authentication or an explicit replacement authorization flow.
- Active install until the replacement generation passes acceptance.
- Controller state until the deliberate fresh reset.

## Repair Program

This should be treated as MAJOR work delivered in bounded slices.

### Slice 1: Freeze Canonical Contracts
Replace the shortened greenfield product documents with a reconciled contract based on the old behavior.

Decisions now settled:

- Personal and workstream remain distinct.
- Personal defaults to the primary checkout.
- Personal and secretary grids have independent configured active sets.
- Headless subagents include workers and readers.
- Tmux is the accepted presentation backend for now.
- The controller strengthens behavior; it does not redefine it.

Explicitly defer Herdr without deleting backend-neutral presentation concepts.

**Acceptance:** document and catalog validation proves every user action has one owner and one scenario.

### Slice 2: Finish Rename Without Semantic Changes
Complete the in-flight `greenfield_*` to `pi_*` rename:

- Modules and tests.
- Resource catalogs.
- Action-manifest entrypoints.
- Validators and system runners.
- Documentation paths.
- Installed launcher references.

Do not combine this with product repair or broad deletion.

**Acceptance:** all four P0 commands pass and no stale module path remains.

### Slice 3: Canonical Installed Surface
Make daily use resolve one active generation for the entire operation.

- Package `pisec`, `pi-personal`, `pi-start`, `pi-restart`, `pidev`, and `pi-surface.py`.
- Remove implicit rebuild-from-dirty-repository behavior.
- Add a separate explicit development staging command.
- Eliminate `~/.local/share/pi/control` fallbacks.
- Retire or replace `install.sh`.
- Ensure surface, controller, resources, and runtime all bind one build ID.

**Acceptance:** installed daily commands prove the same active build end to end.

### Slice 4: Restore `pisec`
Implement controller-backed secretary behavior:

- Independent ordered active project set.
- `register`, `list`, `activate`, `swap`, `open`, `launch`, and diagnostic information.
- Desktop grouping and mobile layout.
- One live secretary per active project.
- Repair dead panes without killing live thinking sessions.
- Secretary-specific prompt and tools.
- Work index and attention view.
- Asynchronous investigator launch.
- Workstream, review, and integration proposals through semantic controller operations.
- Exact approvals for consequential actions.

**Acceptance:** installed secretary journeys cover grid management, day-open status, investigation, workstream creation, review, integration proposal, restart, and dead-pane repair.

### Slice 5: Restore `pi-personal`
Stop routing personal through workstream creation.

- Use a real `personal` conversation.
- Bind it to the primary working copy by default.
- Capture baseline before first mutation.
- Run tools in a controller-owned writer container.
- Support safe primary-checkout Git metadata masking without granting direct Git mutation.
- Preserve pre-existing staged, unstaged, and untracked work.
- Require selection when attribution is ambiguous.
- Support explicit separate-worktree/headful-workstream creation.
- Restore independent configured grid ordering and desktop/mobile presentation.

**Acceptance:** reproduce the old personal-primary journey with pre-existing dirty work, task-only submission, independent review, integration, restart, and unchanged pre-existing files.

### Slice 6: Restore Headless Subagents
Expand the controller broker without restoring uncontrolled upstream process authority.

- Preserve controller ownership of child resources.
- Add the full semantic role catalog.
- Add asynchronous child requests.
- Decouple child lifetime from a blocking parent tool call.
- Add status, wait, parallel fanout, interrupt, stop, steer, and resume.
- Add typed supervisor communication.
- Add role-specific prompts and model routing.
- Add isolated mutable workers with controller-owned working copies and containers.
- Preserve fresh context by default and add explicit fork support.
- Keep ordinary children from uncontrolled nested fanout.
- Persist compact results and detailed artifacts outside repositories.

**Acceptance:** installed scenarios for async investigation, parallel review, supervisor escalation, isolated worker, role/model routing, cancellation, resume, restart, and one-writer enforcement.

### Slice 7: Reconnect Submission and Integration
Expose existing controller mechanisms through the real conversations:

- Personal/workstream submission.
- Secretary change queue.
- Exact-revision review.
- Revision staleness.
- Target movement.
- Integration worker.
- Exact local integration authorization.
- Rollback and recovery.
- No automatic merge, push, or cleanup.

**Acceptance:** complete personal and secretary journeys, including conflict integration.

### Slice 8: Observability and Continuity
Restore the interaction-quality layer:

- `/observe` Task, Fleet, and Messages views.
- Child status and parent-child communication.
- User-visible compaction continuity card.
- Provenance-backed timing/token/tool metrics.
- Day-open attention.
- Harness feedback pipeline.
- No hidden reasoning exposure.
- No arbitrary completion throttles.

**Acceptance:** installed observability and restart-continuity scenarios.

### Slice 9: Remove Legacy
Only after replacement journeys pass:

- Delete unreachable legacy controller families.
- Delete old launch mechanisms and extensions.
- Remove obsolete tests while retaining behavioral tests.
- Remove duplicate installer and fallback roots.
- Update tmux-resurrect commands to current installed launchers.
- Preserve or reprovision authentication.
- Reset current controller state as an explicit fresh-start operation.
- Delete installed historical Pi trees and rollback debris.

**Acceptance:** release reachability contains only the canonical product, while all preserved interaction journeys remain green.

### Slice 10: Release Acceptance
Run:

- Contract and source gates.
- Full controller suite.
- Installed surface journeys.
- Real tmux presentation tests.
- Real writer-container tests.
- Personal-primary dirty-checkout journey.
- Secretary coordination journey.
- Headless asynchronous orchestration journey.
- Review/integration/conflict journey.
- Restart and recovery fault matrix.
- Rollback then explicit activation.

No production deletion or activation should occur before this final slice receives explicit user approval.

## First Implementation Target

The first coding change should **not** be mass deletion. It should produce three durable artifacts:

1. A canonical reconciled product contract based on the old documentation.
2. A release-action catalog restoring `pisec`, `pi-personal`, and full headless subagent scenarios.
3. A mechanical legacy/current reachability inventory based on imports and installed entrypoints.

That creates the standard against which cleanup and repair can be judged and prevents another migration from passing while replacing the desired product with a narrower one.

## Implementation Handoff (2026-08-12)

Execution state at handoff: Slices 1-9 complete and verified. Slice 10
~90% done. Everything lives in the working tree (no commits).

Verified green: P0 gates; control-plane suite (169 tests, 1 documented
pre-existing failure in test_runtime_spec.py); 16 node extension tests; grid
tests; all 18 original HA actions with installed PASS evidence (P6/P7/P9/P10/
P12 + 5 user-scenario journeys). The repair journey (HA-021/024/027/028/029/
031/032) PASSES with evidence in /tmp/opencode/repair-evidence/.

Remaining steps:

1. Fix bin/pi-restart sanitization: preserve the config roots
   (PI_SYSTEM_DATA_ROOT, PI_SYSTEM_STATE_ROOT, PI_SYSTEM_SURFACE_STAGE,
   PI_SYSTEM_MODEL, PI_TOOL_IMAGE) in its PI_SYSTEM_ compgen-clear loop,
   exactly as bin/pi-start now does.
2. Re-run the surface journey (fresh stage each run):
   stage; export PI_SYSTEM_STAGED_ROOT=<stage>
   PI_SYSTEM_EVIDENCE_DIR=/tmp/opencode/surface-evidence;
   PYTHONDONTWRITEBYTECODE=1 python3 tests/system/fixtures/installed-surface-journey.py
3. Flip HA-019/020/021/022/024/027/028/029/031/032 to implemented-source in
   tests/system/action-manifest.v1.json; re-run validate_plan_docs.py +
   test_action_manifest.
4. Wire run-repair-installed.sh and run-repair-surface.sh into
   tests/system/run-p11-release.sh.
5. Run the full P11 pipeline (OPENCODE_BIN=/home/j/.opencode/bin/opencode).
6. Slice 10 finale (user approval required): live-state reset, legacy wipe
   (keep ~/.pi/agent/auth.json), fresh stage+activate, surface relaunch.

Critical gotchas:

- tmux: always env -u TMUX with fixture-scoped TMUX_TMPDIR; never kill-server
  without the guard (it killed the user's live server once).
- pi-install activate consumes (renames) the stage; use data_root after.
- Catalog consistency is exact-equality enforced (pi_install constants ==
  pi/pi-resources.v1.json == tests/system catalogs); regenerate via
  _expected_catalog after constant edits.
- Handshake requires profile tools == granted registered tool names; any new
  tool needs _ROLE_OPERATIONS + _TOOL_RESOURCE + profiles + catalog +
  sorted scripted-provider EXPECTED lists; extension channel-op keys must be
  unique.
- Feedback records land in the run-scoped agent dir (<state>/runtime/<run>/agent).
- Evidence envelopes: one declared scenarioId per envelope; release requires
  all catalog actions implemented-source.
- ~/.local/bin surface launchers are symlinks to the repo.

### Implementation Completion 2026-08-12

All Slice 10 handoff steps are implemented and verified; the P11 aggregate is
green end to end.

- Fix 1 (activation self-registration): `activate()` in
  scripts/pi_control/pi_install.py registers the activated generation into
  the fresh state root (paths into the data root), with the import bound
  before the stage rename; `verify_stage` tolerates activation.json + state/.
  Verified functionally (registered row + verify_registered_build on the
  activated root) and by tests.test_pi_install / test_pi_core / p12 suites.
- Surface journey PASSES (HA-019/020/022): fixture HOME with empty tmux.conf
  (host tmux config interference), per-window remain-on-exit in pisec /
  pi-personal, journey pty-kill tolerance, shared-stage copy before activate.
- Repair journey extended and PASSES (HA-021/023/024/025/026/027/028/029/031/
  032): async investigation child + completion notification, headless worker
  isolation + one-writer refusal, child escalation + durable reply; evidence
  scenarios use the declared manifest scenario ids.
- Product fixes: `worker.start` and `review.request` now thread the acceptance
  test resources into detached children (previously the review reviewer and
  worker could not run under the acceptance profile); the worker launches via
  pi-system-workstream-run (role-exact launcher).
- Harness fix: the failpoints child PYTHONPATH now includes the package
  parent so `control_plane.*` target imports resolve in a full discovery run.
- Action manifest: 31 actions, all implemented-source (HA-030 was a stale
  action removed during handoff reconciliation; the plan and validators
  accept 31). validate_plan_docs + test_action_manifest green.
- P11: `bash tests/system/run-p11-release.sh` with a fresh stage + OPENCODE_BIN
  passes ("all 31 actions covered by installed evidence"); both repair
  journeys wired in after p12, shared evidence root no longer wiped by the
  journeys, verifier opencode-subdir exclusion fixed.
- Full unit discovery: 232 tests, only the documented pre-existing
  test_writer_requires_complete_distinct_image_identities error.

Slice 10 finale COMPLETE (user-approved, 2026-08-12): live state reset,
legacy wipe (backups at ~/pi-finale-backups-20260812T210340; ~/.pi/agent keeps
only auth.json), fresh stage activated into ~/.local/share/pi-system
(build_e74e0f38), activated-root registration verified live
(verify_registered_build OK). User relaunches pi-restart / pisec /
pi-personal.
