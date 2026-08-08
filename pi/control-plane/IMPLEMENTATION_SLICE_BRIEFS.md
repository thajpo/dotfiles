# Pi control-plane weak-model implementation slice briefs

Status: **normative handoff companion to `COMPLETION_IMPLEMENTATION_PLAN.md`;
none of these briefs grants live authority**.

Use exactly one lettered brief per ordinary implementation worker. The parent
integrates it, reruns acceptance independently, reviews the surviving diff, and
then issues the next brief. A brief validator introduced in C0b rejects a brief
that omits a required heading.

## 1. Global rules imported by every brief

### Global unchanged boundary

Every brief inherits these prohibitions even when repeated below:

- no live HOME/XDG/controller/root/secretary/session/runtime mutation;
- no installed package/config/launcher activation, Docker/tmux/Herdr live
  mutation, remote operation, push, deployment, publication, cleanup apply,
  migration cutover, or Phase 11D execution;
- no edit to migrations v1–v6;
- no alternate lifecycle store, daemon, live process migration, same-copy writer
  child, cross-run container reuse, force Git operation, timestamp conflict
  resolution, controller-mode legacy fallback, or dual writer;
- no unrelated dirty/untracked work or MLRE recovery-state mutation.

Tests use disposable HOME/XDG/Git/state. Fakes/failpoints are constructor or
fixture injections, never production caller environment authority. Required
unavailable external prerequisites return STOP/77.

### Global operation/failure contract

Every mutating operation records durable intent before external effects, binds
its complete actor/resource/version/epoch/authorization/build/inventory/
resolution request into idempotency, uses ordered locks and CAS, and observes
external state before terminal success or retry. Exact replay returns the same
result. Changed replay returns `CP_IDEMPOTENCY_CONFLICT`. Terminal outcomes do
not regress. `needs_attention` is durable and not automatically retried.

### Global report

Report exact changed paths, commands/exits, changed system surfaces, remaining
uncertainty, and confirmation of no live/remote action.

## 2. C0 — Contract and traceability

### C0a — Status and normative contract freeze

#### Goal
Truthfully distinguish component, harness, migration, staging, canary, and
rollout evidence and freeze remaining authority decisions.

#### Prerequisites
Current component candidate and audit evidence are readable; no implementation
slice is active.

#### Required reading
All of:

```text
pi/control-plane/README.md
pi/control-plane/PRODUCT_CONTRACT.md
pi/control-plane/STATE_CONTRACT.md
pi/control-plane/EXECUTION_CONTRACT.md
pi/control-plane/CHANGE_INTEGRATION_CONTRACT.md
pi/control-plane/OBSERVABILITY_CONTINUITY_CONTRACT.md
pi/control-plane/MVP_IMPLEMENTATION_PLAN.md
pi/control-plane/COMPLETION_IMPLEMENTATION_PLAN.md
pi/control-plane/IMPLEMENTATION_SLICE_BRIEFS.md
pi/control-plane/SYSTEM_INTEGRATION_TEST_PLAN.md
pi/control-plane/MIGRATION_ACTIVATION_PLAN.md
pi/control-plane/ACCEPTANCE_PLAN.md
pi/control-plane/CORRECTION_LEDGER.md
```

#### Allowed files
Exactly the files above.

#### Must remain unchanged
All source, tests, current-state `pi/*.md`, and the global unchanged boundary.

#### Required behavior
1. Freeze schema-v7 completion resources and authorization vocabulary.
2. Freeze activation latch and legacy/shadow/controller ownership.
3. Freeze typed inventory/resolution/disposition and package-loading rules.
4. Make status headers evidence-specific and mutually consistent.
5. Preserve all genuine human decisions and remove implementation ambiguity.

#### Failure and retry behavior
A conflicting authority statement is a plan failure. Do not choose a convenient
interpretation; record the exact conflict and stop.

#### Tests to add first
None in C0a; C0b adds the documentation validator.

#### Acceptance commands

```bash
git diff --check -- pi/control-plane
python3 - <<'PY'
from pathlib import Path
import re
for p in Path('pi/control-plane').glob('*.md'):
    for target in re.findall(r'\[[^]]+\]\(([^)#]+\.md)', p.read_text()):
        assert (p.parent / target).is_file(), (p, target)
PY
```

#### Stop and escalate
Stop on duplicate authority, unclear activation fallback, missing workstream
identity, or a human decision not named in Completion §10.

### C0b — Action catalog, plan validator, and discovery contract

#### Goal
Create machine-readable current/planned action traceability and reject incomplete
weak-model briefs.

#### Prerequisites
C0a accepted.

#### Required reading
System Integration §§1–7; `pi/settings.json`; `bin/pi-help-custom`;
`bin/pi-start`; `bin/pi-secretary`; `scripts/pi_control/cli.py`; extension
registration sources named by launchers/settings.

#### Allowed files

```text
tests/system/__init__.py
tests/system/README.md
tests/system/action-manifest.schema.json
tests/system/action-manifest.v1.json
tests/system/launcher-surface.v1.json
tests/system/loaded-extensions.v1.json
tests/system/configured-packages.v1.json
tests/system/validate_plan_docs.py
tests/system/test_action_manifest.py
tests/system/test_slice_briefs.py
```

#### Must remain unchanged
Product/runtime source, settings, launchers, extensions, and global boundary.

#### Required behavior
1. Encode `supported|compatibility|planned|host-only|out-of-scope` distinctly.
2. Discover argparse actions, launcher actions/flags, literal `-e` extensions,
   tool/command registrations, settings packages, and host-only operations.
3. Require planned actions to name owning sub-slice; existing actions cannot hide
   as planned.
4. Require scenarios/assertions/tiers for supported actions and refusal scenarios
   for out-of-scope actions.
5. Parse this document and reject every lettered brief missing Goal,
   Prerequisites, Required reading, Allowed files, Must remain unchanged,
   Required behavior, Failure and retry behavior, Tests to add first,
   Acceptance commands, or Stop and escalate.

#### Failure and retry behavior
Unknown dynamic action/resource is a manifest failure unless explicitly listed
with provenance. Validation is read-only and repeatable.

#### Tests to add first
Synthetic missing CLI action, launcher flag, extension, package, scenario,
orphan manifest item, planned/current mismatch, and omitted brief heading.

#### Acceptance commands

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.system.test_action_manifest \
  tests.system.test_slice_briefs
python3 tests/system/validate_plan_docs.py
```

#### Stop and escalate
Stop if a surface cannot be discovered or allowlisted without executing live
code, or if current versus planned status is unclear.

## 3. C1 — Schema and protocol

### C1a — Schema-v7 DDL and migration

#### Goal
Add completion resources and authorization CHECKs without external effects.

#### Prerequisites
C0 accepted; current schema v6 checksum passes.

#### Required reading
State §§4–9, 16; Completion §5; existing schema/migration modules and tests.

#### Allowed files

```text
scripts/pi_control/schema.py
scripts/pi_control/migrations/__init__.py
scripts/pi_control/migrations/v007_completion_resources.py
scripts/pi_control/store.py
scripts/pi_control/models.py
tests/control_plane/test_schema_migrations.py
tests/control_plane/test_completion_schema.py
```

#### Must remain unchanged
Migrations v1–v6, external adapters/launchers, and global boundary.

#### Required behavior
1. Add workstreams, presentation assignments, project activations, and immutable
   migration mappings exactly as State §16.
2. Transactionally rebuild authorization kind CHECK without losing rows.
3. Add project/link/uniqueness/immutability constraints and migration checksum.
4. Make fresh-v7 and all v1…v6 upgrade results equivalent.
5. Keep external-state predicates in operations, not dishonest SQL.

#### Failure and retry behavior
Unknown newer schema → `CP_SCHEMA_NEWER`; unsupported SQLite →
`CP_SQLITE_UNSUPPORTED`; interrupted migration leaves old schema intact or full
v7. Never accept checksum disagreement.

#### Tests to add first
Fresh/upgrade equality, interruption, every CHECK/trigger/update/delete
violation, authorization-row preservation, unknown-newer refusal.

#### Acceptance commands

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.control_plane.test_schema_migrations \
  tests.control_plane.test_completion_schema
```

#### Stop and escalate
Stop on any need to edit old migrations or diverge from State §16 fields.

### C1b — Models, store, CAS, and operation states

#### Goal
Expose safe state-only APIs for new v7 resources.

#### Prerequisites
C1a accepted.

#### Required reading
State §§7–9, 14–16; Product §§2, 11; existing operation/event APIs.

#### Allowed files

```text
scripts/pi_control/models.py
scripts/pi_control/store.py
scripts/pi_control/operations.py
scripts/pi_control/events.py
scripts/pi_control/errors.py
scripts/pi_control/workstreams.py
tests/control_plane/test_workstreams.py
tests/control_plane/test_activation.py
tests/control_plane/test_migration_mappings.py
tests/control_plane/test_operations.py
tests/control_plane/test_events.py
```

#### Must remain unchanged
Schema/migration after C1a acceptance, all external adapters, and global boundary.

#### Required behavior
1. Validate random prefixed IDs and exact resource relationships.
2. Implement workstream/presentation/activation CAS transitions.
3. Validate build/migration/project activation predicates.
4. Insert immutable mappings and state+event atomically.
5. Bind complete authority metadata into idempotency.

#### Failure and retry behavior
Use `CP_RESOURCE_STALE`, `CP_IDEMPOTENCY_CONFLICT`,
`CP_WORKSTREAM_CONFLICT`, `CP_ACTIVATION_MISMATCH`, and
`CP_MIGRATION_UNRESOLVED`. Zero-row CAS never blindly retries.

#### Tests to add first
Exact replay, changed binding, concurrent CAS, cross-project link, illegal
activation, terminal regression, event rollback, mapping mutation/delete.

#### Acceptance commands

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
 tests.control_plane.test_workstreams \
 tests.control_plane.test_activation \
 tests.control_plane.test_migration_mappings \
 tests.control_plane.test_operations \
 tests.control_plane.test_events
```

#### Stop and escalate
Stop if an adapter/subprocess is needed inside a transaction.

### C1c — Protocol-v2 and CLI shapes with fake effects

#### Goal
Freeze bounded semantic request/response/error shapes before launcher work.

#### Prerequisites
C1b accepted.

#### Required reading
Completion §5.4; State §§13–15; Product §11; current client/CLI tests.

#### Allowed files

```text
scripts/pi_control/client.py
scripts/pi_control/cli.py
scripts/pi_control/error_messages.py
tests/control_plane/test_client_protocol_v2.py
tests/control_plane/test_cli_protocol_v2.py
tests/system/action-manifest.v1.json
```

#### Must remain unchanged
Adapters, launchers, extension tools, and global boundary.

#### Required behavior
1. Add exact-field 64-KiB protocol-v2 envelope and negotiation.
2. Define planned semantic operations from Completion §5.4 using fake effects.
3. Keep current CLI aliases mapped explicitly where names differ.
4. Keep host-only apply operations unavailable to ordinary clients/tools.
5. Return bounded six-part projected errors.

#### Failure and retry behavior
Malformed/unknown/oversized → `CP_INVALID_REQUEST`; version mismatch and
read-only mutation fail before state. No traceback or raw secret.

#### Tests to add first
Every schema, unknown fields, protocol negotiation, host-only refusal,
cross-project IDs, error redaction, action-manifest transition.

#### Acceptance commands

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
 tests.control_plane.test_client_protocol_v2 \
 tests.control_plane.test_cli_protocol_v2 \
 tests.system.test_action_manifest
```

#### Stop and escalate
Stop on arbitrary SQL/table/Git/shell payload or production mode override.

## 4. C2 — Typed inventory

Finite defaults: explicit roots; regular JSON/file 8 MiB; first 8 non-empty
JSONL header lines at 64 KiB each; subprocess output 4 MiB; Git/Docker/tmux/
Herdr timeout 10 s; process timeout 5 s; 100,000 records; 64-MiB manifest. Host
policy may lower, never model input raise.

### C2a — Root/session/secretary/route/artifact adapters

#### Goal
Produce pure typed records for file-backed harness sources.

#### Prerequisites
C1 accepted.

#### Required reading
Migration §§2–6, 23; Completion §§5.2, 6–7; session/artifact contracts.

#### Allowed files

```text
scripts/pi_control/migration_adapters/__init__.py
scripts/pi_control/migration_adapters/base.py
scripts/pi_control/migration_adapters/root_sessions.py
scripts/pi_control/migration_adapters/secretary.py
scripts/pi_control/migration_adapters/routes_leases.py
scripts/pi_control/migration_adapters/artifacts.py
tests/control_plane/migration_adapters/test_root_sessions.py
tests/control_plane/migration_adapters/test_secretary.py
tests/control_plane/migration_adapters/test_routes_leases.py
tests/control_plane/migration_adapters/test_artifacts.py
```

#### Must remain unchanged
Legacy files, session content, Git/runtime/presentation, importer, and global
boundary.

#### Required behavior
1. Observe only explicit roots with bounded no-follow reads.
2. Normalize identities/relationships/provenance without message bodies.
3. Redact capabilities/secrets/raw commands/environment.
4. Mark legacy run/writer/review authority as observation only.
5. Return observed/empty/unavailable/error distinctly.

#### Failure and retry behavior
Unavailable → `CP_ADAPTER_UNAVAILABLE` state, not empty. Malformed/oversized
becomes typed record/error; no partial success or source mutation.

#### Tests to add first
Valid/empty/unavailable/malformed/oversized/duplicate/symlink/parent-swap,
redaction, source hash unchanged.

#### Acceptance commands
Run the four exact adapter unittest modules listed in Allowed files.

#### Stop and escalate
Stop if raw conversation content or unbounded recursive scanning is required.

### C2b — Git/policy/installed-build/backup adapters

#### Goal
Observe source authority, host trust, loaded-build evidence, and backups.

#### Prerequisites
C1 accepted; C2 base protocol from C2a frozen.

#### Required reading
State §§2, 11, 14; Execution §§4–6; Migration §§2–3, 8, 12; Completion §6.

#### Allowed files

```text
scripts/pi_control/migration_adapters/git.py
scripts/pi_control/migration_adapters/policy.py
scripts/pi_control/migration_adapters/installed_build.py
scripts/pi_control/migration_adapters/backups.py
scripts/pi_control/git_adapter.py
tests/control_plane/migration_adapters/test_git.py
tests/control_plane/migration_adapters/test_policy.py
tests/control_plane/migration_adapters/test_installed_build.py
tests/control_plane/migration_adapters/test_backups.py
```

#### Must remain unchanged
Git refs/worktrees/index/config, policy, installed tree, backups, and global
boundary. `git_adapter.py` edits are read-only API additions only.

#### Required behavior
1. Run sanitized fixed Git observations and preserve source authority.
2. Hash/normalize host policy without permitting repository override.
3. Compare exact launcher/settings/extension/helper/package/build/image evidence.
4. Observe backup manifest and disposable-restore proof.
5. Import build only on full equality; otherwise mismatch record.

#### Failure and retry behavior
Timeout/unavailable is adapter state. Build disagreement is `CP_BUILD_MISMATCH`.
Never infer installed bytes from repository source.

#### Tests to add first
SHA formats, linked/unmanaged/divergent/dirty Git, malicious config/hooks,
moved/copied repo, policy tamper, missing/unexpected/symlinked install,
package-root mismatch, incomplete backup.

#### Acceptance commands
Run the four exact C2b adapter test modules; Git capability absence returns 77
only in system runner, while unit fakes remain mandatory.

#### Stop and escalate
Stop on Git mutation, recursive dependency crawl, or build proof requiring live
activation.

### C2c — Process/Docker/tmux/Herdr adapters

#### Goal
Observe live substrate/presentation without mutating or synthesizing authority.

#### Prerequisites
C1 accepted; C2 base protocol frozen.

#### Required reading
State §§2.4–2.5, 10, 12–14; Product §3; Migration §§3.4, 6.4, 23.

#### Allowed files

```text
scripts/pi_control/migration_adapters/processes.py
scripts/pi_control/migration_adapters/docker.py
scripts/pi_control/migration_adapters/tmux.py
scripts/pi_control/migration_adapters/herdr.py
tests/control_plane/migration_adapters/test_processes.py
tests/control_plane/migration_adapters/test_docker.py
tests/control_plane/migration_adapters/test_tmux.py
tests/control_plane/migration_adapters/test_herdr.py
```

#### Must remain unchanged
Processes, containers/images, tmux/Herdr state, runtime adapter, and global
boundary.

#### Required behavior
1. Use fixed allowlisted read commands and bounded parsers.
2. Record PID/start/ancestry and exact managed labels/locators.
3. Treat forged/unlabeled/unknown resources as observations only.
4. Never emit active run/writer/conversation identity.
5. Distinguish empty, unavailable, timeout, malformed, and inaccessible.

#### Failure and retry behavior
No automatic retry loop; one bounded call per adapter invocation. Unknown is not
stopped. Unavailable is `CP_ADAPTER_UNAVAILABLE` state.

#### Tests to add first
Strict fake command shapes; success/empty/unavailable/timeout/malformed/forged
labels/titles/PID reuse/inaccessible state.

#### Acceptance commands
Run all four C2c adapter modules. Do not invoke real Docker/tmux/Herdr in this
slice.

#### Stop and escalate
Stop if any stop/kill/create/remove command or arbitrary argument is needed.

### C2d — Inventory graph and canonical manifest-v2

#### Goal
Combine all adapter records into one immutable typed graph.

#### Prerequisites
C2a–C2c accepted.

#### Required reading
Completion §§5.2, 6–7; Migration §23; State §16.4.

#### Allowed files

```text
scripts/pi_control/migration.py
scripts/pi_control/migration_adapters/base.py
tests/control_plane/test_migration_inventory_v2.py
tests/control_plane/test_migration_contradictions.py
```

#### Must remain unchanged
Adapter semantics, logical resource IDs, source/runtime state, importer, and
global boundary.

#### Required behavior
1. Invoke finite adapter registry and include every adapter state once.
2. Build sources/records/relationships/contradictions/dispositions.
3. Derive only record IDs from canonical record identity; logical IDs remain
   random and unallocated.
4. Exclude observation timestamps from identity digest.
5. Write canonical manifest securely through descriptor-relative immutable I/O.

#### Failure and retry behavior
Adapter error remains in envelope; bound overflow produces typed failure and no
partial manifest. Existing exact immutable manifest may be adopted; mismatch is
attention/tamper.

#### Tests to add first
Determinism, shared references, divergent binding, record/manifest limits,
crash/tamper/symlink/parent swap, every adapter represented.

#### Acceptance commands

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
 tests.control_plane.test_migration_inventory_v2 \
 tests.control_plane.test_migration_contradictions
```

#### Stop and escalate
Stop if an adapter is silently omitted or timestamps choose identity.

## 5. C3 — Resolution and shadow migration

### C3a — Resolution manifest and random mapping allocation

#### Goal
Bind exact decisions to inventory and allocate stable random controller IDs.

#### Prerequisites
C2d and C1 mapping store accepted.

#### Required reading
Completion §§5.3, 7; State §§5, 6.8, 16.4; Migration §23.2.

#### Allowed files

```text
scripts/pi_control/migration_planner.py
scripts/pi_control/migration.py
tests/control_plane/test_migration_plan.py
tests/control_plane/test_migration_mappings.py
```

#### Must remain unchanged
Inventory manifest, source records, importer/external state, and global boundary.

#### Required behavior
1. Validate exact resolution schema/digest/scope.
2. Generate only contract-determined policy decisions; retain human decisions.
3. Refuse changed inventory/digest and duplicate/conflicting decisions.
4. In first import transaction allocate cryptographically random logical IDs.
5. Persist all record mappings before resource rows; replay reuses IDs.

#### Failure and retry behavior
Unresolved required record → `CP_MIGRATION_UNRESOLVED`; changed replay →
idempotency conflict; no mapping update/delete.

#### Tests to add first
Stale/duplicate/incomplete resolution, illegal observation import, random ID
shape/non-derivation/replay, legacy ID mapping without truncation.

#### Acceptance commands
Run migration plan and mapping unittest modules.

#### Stop and escalate
Stop for duplicate conversation, incomplete rebind, dirty ambiguity, change
adoption, divergent ref, or canary choice.

### C3b — Dependency-ordered shadow importer

#### Goal
Import complete safe resource relationships into a disposable shadow DB.

#### Prerequisites
C3a accepted.

#### Required reading
Completion C3; Migration §§4.2–4.3, 5–7, 23; State operation protocol.

#### Allowed files

```text
scripts/pi_control/migration_importer.py
scripts/pi_control/migration.py
tests/control_plane/test_migration_import_v2.py
```

#### Must remain unchanged
Default/live state root, legacy sources, external Git/runtime/presentation, and
global boundary.

#### Required behavior
1. Verify inventory/resolution and secure new/marked shadow root.
2. Transition planned→manifest-verified→mappings-allocated.
3. Import build/policy→projects→copies→conversations→workstreams/presentation→
   artifacts→historical observations.
4. Never create active runs/writers/review authorization.
5. Re-observe sources and terminalize complete or attention.

#### Failure and retry behavior
Malformed request before intent → failed; source/mapping/conflicting row after
intent → needs_attention; exact replay adopts matching rows and one event.

#### Tests to add first
Each batch/FK, crash before/after batch, exact row adoption, conflicting row,
source changed, unmarked/live root refusal.

#### Acceptance commands

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
 tests.control_plane.test_migration_import_v2
```

#### Stop and escalate
Stop on active legacy authority synthesis, cleanup, or live-root write.

### C3c — Field reconciliation, failpoints, and tamper recovery

#### Goal
Prove imported fields/relationships and recover every uncertain boundary.

#### Prerequisites
C3b accepted.

#### Required reading
State §§9, 12; Acceptance §§7, 14; Completion C3.

#### Allowed files

```text
scripts/pi_control/migration_reconcile.py
scripts/pi_control/migration.py
tests/control_plane/test_shadow_reconcile_v2.py
tests/control_plane/test_migration_failpoints.py
```

#### Must remain unchanged
Imported authority on mismatch, source/runtime/presentation, and global boundary.

#### Required behavior
1. Compare every field/link/disposition plus fresh source/Git evidence.
2. Inject before/after manifest/mapping/batch/event/terminal boundaries.
3. Observe before retry and classify old/desired/ambiguous.
4. Adopt only exact matching controller-owned effects.
5. Persist attention/evidence for tamper/ambiguity.

#### Failure and retry behavior
Neither old nor desired → needs_attention; same retry returns it until explicit
resolution. No destructive compensation of ambiguous resources.

#### Tests to add first
Wrong project/copy/session/workstream/policy/OID/build/disposition; manifest/file
Tamper; all failpoints; concurrent source change.

#### Acceptance commands
Run both C3c modules, then all `test_migration_*v2.py` and existing migration
component tests.

#### Stop and escalate
Stop on guessed repair, partial compare, or source mutation.

## 6. C4 — Activation and exact binding

### C4a — Activation latch secure I/O and planning

#### Goal
Create a fail-closed boot selector that cannot become lifecycle authority.

#### Prerequisites
C1 activation rows accepted.

#### Required reading
Completion §4.3; State §16.3; Migration §23.3.

#### Allowed files

```text
scripts/pi_control/activation.py
scripts/pi_control/client.py
scripts/pi_control/cli.py
tests/control_plane/test_activation_latch.py
tests/control_plane/test_activation.py
```

#### Must remain unchanged
Launchers, production state, activation rows except disposable tests, and global
boundary.

#### Required behavior
1. Exact latch fields/digests from Completion and C4a brief history.
2. Canonical JSON SHA-256 with manifestDigest omitted.
3. Descriptor-relative atomic `0600` write under `0700`, fsync file+directory.
4. Git identity lookup to one record and exact DB/build/migration/version match.
5. No environment-selected production mode/path.

#### Failure and retry behavior
Missing/unreadable/newer → `CP_ACTIVATION_UNAVAILABLE`; mismatch/duplicate/stale →
`CP_ACTIVATION_MISMATCH`; no legacy fallback or partial replace.

#### Tests to add first
Modes/transitions, canonical order/digest, symlink/parent swap, corrupt/newer,
copied/moved repo, stale DB/build/migration/project.

#### Acceptance commands
Run activation latch and activation unittest modules.

#### Stop and escalate
Stop if latch supplies working-copy/conversation/run/trust authority.

### C4b — Root/session controller projection

#### Goal
Bind root sessions to exact SQLite resources while preserving JSONL content.

#### Prerequisites
C4a and C3 accepted.

#### Required reading
State §§2.3, 6.4, 16.3; Product §§5, 11; Execution §11.

#### Allowed files

```text
scripts/pi_control/session_adapter.py
scripts/pi_control/client.py
scripts/pi-root-session.py
pi/extensions/root-session/index.ts
tests/test_pi_root_sessions.py
tests/control_plane/test_controller_session_binding.py
tests/control_plane/root-session-extension.test.mjs
```

#### Must remain unchanged
Session message bodies, Git work unless explicit disposable projection setup,
secretary facade, launchers, and global boundary.

#### Required behavior
1. Resolve exact project/conversation/wc/session in controller mode.
2. Emit registry projection with IDs, activation version, digest.
3. Treat header cwd as observation only.
4. Preserve all duplicate histories and require resolution; never mtime select.
5. Keep child sessions excluded and validate helper build/provenance.

#### Failure and retry behavior
Conflict/unavailable fails before registry/session/worktree mutation. Legacy mode
retains current behavior except unsafe duplicate auto-selection is removed in the
new typed path; shadow external writes remain legacy-only.

#### Tests to add first
Header bounds/drift, null/wrong repo, wrong project/wc/session, moved worktree,
child exclusion, projection tamper, duplicate histories, all modes.

#### Acceptance commands
Run root-session Python tests and controller binding/extension tests.

#### Stop and escalate
Stop if a duplicate mapping or repository rebind needs human choice.

### C4c — Launcher `launch.resolve` and no-fallback matrix

#### Goal
Make root launch consume one semantic exact launch plan.

#### Prerequisites
C4b and C1c accepted.

#### Required reading
Execution §§3–4, 11; Completion C4; current `bin/pi` and `pi-tmux-session`.

#### Allowed files

```text
scripts/pi_control/client.py
scripts/pi_control/cli.py
bin/pi
bin/pi-tmux-session
tests/control_plane/test_launcher_resolution.py
tests/system/scenarios/launchers.py
tests/system/action-manifest.v1.json
```

#### Must remain unchanged
Runtime creation (C6), secretary launchers, production mode env, and global
boundary.

#### Required behavior
1. Canonicalize repository and consult valid latch.
2. Call `launch.resolve` once for exact IDs/session/wc/build.
3. Consume response without row/header/cwd authority assembly.
4. Legacy/shadow/controller route exactly as contracted.
5. Controller error never invokes legacy mutation.

#### Failure and retry behavior
Activation/resource/version mismatch returns stable projected error before Pi or
workspace mutation. User explicitly retries after correction; launcher does not
choose another source.

#### Tests to add first
Hostile cwd/env, all modes, DB loss, stale versions, wrong session/worktree,
fake Pi not invoked on failure, no fallback call.

#### Acceptance commands
Run launcher-resolution unit test and its T2 fake-Pi scenario when C9a exists;
before C9a only component command is required.

#### Stop and escalate
Stop if current launcher behavior cannot be preserved in legacy mode without a
shared interface change.

## 7. C5 — Workstream and presentation

### C5a — Workstream create/focus/retire saga

#### Goal
Create complete workstream resources through one recoverable operation.

#### Prerequisites
C1, C3, and C4 accepted.

#### Required reading
Product §§2.4, 4.3; State §16.1; Completion C5; Change cleanup contract.

#### Allowed files

```text
scripts/pi_control/workstreams.py
scripts/pi_control/client.py
scripts/pi_control/cli.py
scripts/pi_control/git_adapter.py
tests/control_plane/test_workstream_lifecycle.py
tests/control_plane/test_workstream_recovery.py
```

#### Must remain unchanged
Presentation launchers, secretary legacy store, runtime implementation, and
global boundary.

#### Required behavior
1. Validate exact create authorization and preallocate desired rows.
2. Lock project/worktree in global order.
3. Create exact absent branch/worktree with CAS.
4. Create exact session and presentation/run intent.
5. Mark ready only after later fake adapter proof; focus is read-only.
6. Retire quiesces via fake run adapter; cleanup remains separate.

#### Failure and retry behavior
Remove only exact newly owned unexposed effects with proof; otherwise preserve
and needs_attention. Pre-existing path/ref/session is never deleted.

#### Tests to add first
Replay, concurrent create, every boundary crash, target move, collisions, exact
adoption, focus no mutation, active/unknown retire.

#### Acceptance commands
Run workstream lifecycle and recovery modules.

#### Stop and escalate
Stop if compensation ownership is ambiguous or runtime/presentation API must
change.

### C5b — Secretary compatibility facade

#### Goal
Eliminate legacy/controller lifecycle dual writes in controller mode.

#### Prerequisites
C5a and C4 accepted.

#### Required reading
Product §§2.2, 4, 11; Completion §4.1; current secretary workflow/control code.

#### Allowed files

```text
scripts/pi-secretary-control.py
pi/extensions/secretary/index.ts
pi/extensions/workstream-brief/index.ts
pi/extensions/workstream-channel/index.ts
bin/pi-secretary
tests/test_pi_secretary_control.py
tests/test_pi_secretary_authorization.py
tests/control_plane/test_secretary_controller_facade.py
tests/secretary-workstream-extension.test.mjs
```

#### Must remain unchanged
Presentation grid logic, controller core protocol, old legacy behavior in legacy
mode, and global boundary.

#### Required behavior
1. Controller mode delegates all lifecycle actions to protocol v2.
2. Old lifecycle records become read-only migration/projection inputs.
3. Active ordering and bounded notes remain non-authoritative only.
4. Map legacy 64-hex IDs to random prefixed IDs explicitly.
5. Load control-plane extension and exact safe tool list.

#### Failure and retry behavior
Controller failure is surfaced; no legacy fallback. Exact replay handled by
controller idempotency. Cross-project/capability mismatch fails before write.

#### Tests to add first
No old-file writes controller mode; legacy unchanged; shadow legacy-only writes;
ID mapping; capability/isolation; each semantic dispatch; failure no fallback.

#### Acceptance commands
Run existing secretary control/authorization, new facade, and extension tests.

#### Stop and escalate
Stop if a legacy operation lacks a semantic controller equivalent; return to C1c.

### C5c — Presentation assignment, restart, swap, and relaunch

#### Goal
Make tmux/Herdr mutation exact and preserve live/unrelated work.

#### Prerequisites
C5b accepted.

#### Required reading
Product §3; State §16.2; Completion §§4.5, C5; current launchers and backend docs.

#### Allowed files

```text
scripts/pi_control/presentation_adapter.py
bin/pi-start
bin/pi-restart
bin/pi-personal
bin/pi-personal-herdr
bin/pisec
bin/pi-secretary
bin/pi-secretary-herdr
bin/pi-herdr-workstream
tests/test_pi_restart.py
tests/test_pi_personal.py
tests/test_pi_personal_herdr.py
tests/test_pi_secretary_lifecycle.py
tests/test_pi_secretary_herdr_backend.py
tests/control_plane/test_presentation_lifecycle.py
```

#### Must remain unchanged
Controller identities, worker Git/session content, unrelated tmux/Herdr
resources, runtime package, and global boundary.

#### Required behavior
1. Observe exact managed backend resources and process identity.
2. Replace broad `tmux kill-server` with exact managed-session operations.
3. Gracefully stop/verify removed secretary or refuse swap.
4. Preserve/refuse around live workers; never automatic backend migration.
5. Relaunch only stopped exact workstream with same IDs.

#### Failure and retry behavior
Unknown observation → `CP_PRESENTATION_UNKNOWN` and no mutation. Partial stop is
attention and re-observed; no killing through uncertainty.

#### Tests to add first
Unrelated tmux survives; active secretary turn; live worker; partial stop;
concurrent restart; backend/layout matrix; stale locator; unknown backend.

#### Acceptance commands
Run all six listed Python modules; real backend T5 runs later and returns 77 when
required declared capability is absent.

#### Stop and escalate
Stop if exact process quiescence cannot be proven or a backend command would
affect an unowned resource.

## 8. C6 — Installed package, launch, and runtime

### C6a — Staged settings and first-party package resolution

#### Goal
Make staged Pi load reviewed local sandbox/subagent packages exclusively.

#### Prerequisites
C0/C1 accepted; first-party source/provenance available.

#### Required reading
Completion §§4.6, C6; System §7.5 package table; Pi package docs; installer.

#### Allowed files

```text
pi/settings.json
pi/npm/package.json
pi/npm/package-lock.json
pi/packages/pi-sandbox-control/**
pi/packages/pi-subagents-control/**
install.sh
scripts/pi-patch-subagents
tests/system/configured-packages.v1.json
tests/system/scenarios/packages.mjs
tests/pi-subagents-control-provenance.mjs
tests/pi-sandbox-control-manifest.test.mjs
```

#### Must remain unchanged
Runtime behavior, launchers, live settings/npm tree, unrelated package versions,
and global boundary.

#### Required behavior
1. Stage local `./packages/...` sources and exact lock.
2. Prevent co-load of legacy sandbox/subagents.
3. Verify copied trees/license/provenance and resolved paths.
4. Limit legacy patch path to explicit legacy generation.
5. Record loaded resources in package manifest.

#### Failure and retry behavior
Missing npm/Pi/local tree in staged gate → 77; mismatch/tamper/co-load → fail and
restore disposable prior generation. No live fallback.

#### Tests to add first
Clean npm, exact diagnostics/load path, legacy duplicate, tamper/unexpected file,
missing package, lock mismatch.

#### Acceptance commands
Run provenance/manifest Node tests and the C9a staged package scenario when it
exists. Do not run Docker in C6a.

#### Stop and escalate
Stop if Pi cannot load local package paths without changing package architecture.

### C6b — Launch manifest, writer lock, and run lifecycle

#### Goal
Bind actual launch to controller run/manifest/lock/epoch before runtime.

#### Prerequisites
C4c and C6a accepted.

#### Required reading
Execution §§3–4, 7, 10–11; State §§9–10; current run/lease/process code.

#### Allowed files

```text
bin/pi
scripts/pi_control/run_manifest.py
scripts/pi_control/leases.py
scripts/pi_control/process_adapter.py
scripts/pi_control/client.py
scripts/pi_control/cli.py
tests/control_plane/test_run_manifest.py
tests/control_plane/test_writer_fencing.py
tests/control_plane/test_process_identity.py
tests/control_plane/test_run_lifecycle.py
```

#### Must remain unchanged
Runtime adapter implementation, secretary/presentation, package settings, and
global boundary.

#### Required behavior
1. Resolve launch, record operation/run, create capability/manifest.
2. Acquire lifetime kernel lock and increment epoch under transaction.
3. Call fake runtime only after manifest verification.
4. Fence terminal/cancelled/stale mutation.
5. Gracefully stop/lost-reconcile and create new run on restart.

#### Failure and retry behavior
Lock busy/stale/unknown uses existing stable errors. Crash reconciliation observes
lock/process/manifest; never grants new writer from PID absence alone.

#### Tests to add first
Second writer, stale epoch/version/capability, PID reuse, unknown old access,
crash each boundary, expiry, restart, cancel.

#### Acceptance commands
Run four listed control-plane modules. External Docker absence is irrelevant to
this fake-runtime slice and must not skip tests.

#### Stop and escalate
Stop if launcher needs runtime details outside frozen spec/manifest API.

### C6c — Runtime preparation, attestation, tool fence, and stop

#### Goal
Prove exact execution and writable-access revocation.

#### Prerequisites
C6b accepted; C6a package load accepted.

#### Required reading
Execution §§4–7, 10, 13; Acceptance §8; runtime package API.

#### Allowed files

```text
scripts/pi_control/runtime_adapter.py
scripts/pi-runtime.py
scripts/pi-workspace.py
pi/packages/pi-sandbox-control/src/index.ts
pi/packages/pi-sandbox-control/src/manifest-adapter.ts
tests/control_plane/test_runtime_spec.py
tests/control_plane/test_runtime_attestation.py
tests/pi-docker-control-plane-e2e.sh
```

#### Must remain unchanged
Launch identity/schema, cross-run reuse policy, live Docker, and global boundary.

#### Required behavior
1. Canonical exact runtime spec and immutable image/build/mount policy.
2. Create/reconcile through operation context.
3. One-use nonce attestation before tools.
4. Per-mutation run/wc/epoch/runtime/cancel fence.
5. Track processes and gracefully prove writable access gone before handoff.

#### Failure and retry behavior
Wrong spec/attestation → `CP_RUN_ATTESTATION_FAILED`; unavailable runtime →
`CP_RUNTIME_UNAVAILABLE`; uncertain old access → `CP_WRITER_UNKNOWN`. Observe
before retry; no cross-run reuse.

#### Tests to add first
Wrong image/build/Git/mount/UID/GID/mode/nonce/expiry, stale proxy, cancel, no
socket/home/credentials/network, old container unknown.

#### Acceptance commands

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
 tests.control_plane.test_runtime_spec \
 tests.control_plane.test_runtime_attestation
bash tests/pi-docker-control-plane-e2e.sh
```

Python tests are mandatory regardless of Docker. Docker runner returns 77 when
daemon/image is unavailable and cannot make C6c staging evidence green.

#### Stop and escalate
Stop on label-based readiness, broad chmod, unexpected mount, or uncertain writer
handoff.

## 9. C7 — Complete workflow wiring

### C7a — Personal/secretary selection, status, workstream, and submission wiring

#### Goal
Drive shared controller APIs from actual personal and secretary extensions.

#### Prerequisites
C1c protocol, C4c exact launch resolution, C5c workstream/presentation, and C6c
runtime integration accepted.

#### Required reading
Product §§4–6, 11; Completion C7; current personal/secretary/control extension.

#### Allowed files

```text
pi/extensions/control-plane/index.ts
pi/extensions/secretary/index.ts
bin/pi
bin/pi-personal
bin/pi-secretary
scripts/pi_control/client.py
tests/control_plane/test_personal_client.py
tests/control_plane/test_secretary_client.py
tests/control_plane/secretary-extension.test.mjs
tests/system/scenarios/personal.py
tests/system/scenarios/secretary.py
```

#### Must remain unchanged
Child/review/integration/publication behavior, old lifecycle store in controller
mode except through C5b facade, and global boundary.

#### Required behavior
1. Load exact extension and negotiate protocol v2.
2. Personal explicitly selects primary or separate worktree/conversation.
3. Secretary status/focus/create uses semantic controller operations.
4. Both submit through same change queue.
5. System scenarios use launchers/tools, not direct row seeding after bootstrap.

#### Failure and retry behavior
Protocol/controller/project/version failure is projected with no legacy
fallback. Exact create/submit replay is idempotent; changed scope conflicts.

#### Tests to add first
Primary/separate, dirty heuristic absent, secretary no writer, status unavailable,
create/focus approval, submit visibility, cross-project and fallback refusal.

#### Acceptance commands
Run personal/secretary Python tests, extension behavioral test, and targeted T2
scenarios when C9a exists.

#### Stop and escalate
Stop if a required semantic API is missing; return to C1c/C5 rather than direct
DB access.

### C7b — Child/snapshot/artifact process wiring

#### Goal
Launch real children from exact snapshots with mechanical authority.

#### Prerequisites
C6c runtime integration and C7a personal/secretary semantic wiring accepted.

#### Required reading
Execution §§8–9; Acceptance §9; current child/snapshot/artifact/package code.

#### Allowed files

```text
scripts/pi_control/snapshot.py
scripts/pi_control/child_runs.py
scripts/pi_control/artifacts.py
pi/packages/pi-subagents-control/index.ts
pi/extensions/secretary-subagents/index.ts
pi/extensions/workstream-channel/index.ts
tests/control_plane/test_snapshot.py
tests/control_plane/test_child_runs.py
tests/control_plane/test_artifacts.py
tests/pi-child-control-plane-e2e.mjs
tests/system/scenarios/children.py
```

#### Must remain unchanged
Parent working copy, target refs, integration/publication, child depth/tool policy,
and global boundary.

#### Required behavior
1. Fresh quiescent clean/dirty snapshot through temp index/CAS ref.
2. Bind exact parent/run/project/tree/authority.
3. Mechanically deny read-only mutation; writer uses separate working copy.
4. Record terminal/result/artifact provenance and verify writer result.
5. Replace source-existence E2E with process behavior.

#### Failure and retry behavior
Changing source retries bounded capture or attention; wrong lineage/attestation
fails before child tools; ambiguous writer result preserved, never imported/reset.

#### Tests to add first
All snapshot path states/limits, parent advance, mutation denial, separate writer,
wrong route/parent, child crash/lost/dirty result, artifact tamper.

#### Acceptance commands
Run snapshot/child/artifact Python modules and Node process E2E. Missing Docker is
77 only for real runtime tier, not component tests.

#### Stop and escalate
Stop if read-only relies on role name or same-copy writer is proposed.

### C7c — Exact reviewer and authenticated receipt wiring

#### Goal
Run review through an exact read-only checkout/run and immutable receipt.

#### Prerequisites
C7a–b and existing change submission accepted.

#### Required reading
Change §§11–14; State review/authorization fields; Product §§4.5, 7.

#### Allowed files

```text
scripts/pi_control/reviews.py
scripts/pi_control/client.py
scripts/pi_control/cli.py
pi/extensions/control-plane/index.ts
pi/extensions/review-receipt/index.ts
bin/pi-review-agent
tests/control_plane/test_integration_analysis.py
tests/control_plane/test_review_process.py
tests/system/scenarios/reviews_integration.py
```

#### Must remain unchanged
Integration target mutation, change refs, publication/cleanup, and global
boundary.

#### Required behavior
1. Request exact change/revision/tip/tree/base/target evidence.
2. Create/verify detached mechanical read-only reviewer resources.
3. Bind reviewer conversation/run/actor/capability and source provenance.
4. Submit immutable verdict/evidence and preserve history/staleness.
5. Expose current/historical review read-only.

#### Failure and retry behavior
Wrong/stale/moved/unauthenticated review fails before receipt. Exact replay returns
same receipt; changed payload conflicts; receipt never grants integration.

#### Tests to add first
Reviewer mutation denial, wrong source/run/actor/capability, duplicate/replay,
receipt tamper, new revision/target staleness, cross-project.

#### Acceptance commands
Run focused integration-analysis plus new review-process and targeted T2 review
scenario.

#### Stop and escalate
Stop if project review policy is unspecified; use existing policy and ask rather
than defaulting acceptance.

### C7d — Integration authorization, CAS, worktree result, and recovery

#### Goal
Integrate exact current evidence under one authorization and recover every Git
boundary.

#### Prerequisites
C7c accepted; C5 workstream saga available for integration worktrees.

#### Required reading
Change §§12, 14–18; State §§8–9; Acceptance §12; Completion C7.

#### Allowed files

```text
scripts/pi_control/integration.py
scripts/pi_control/client.py
scripts/pi_control/cli.py
pi/extensions/control-plane/index.ts
tests/control_plane/test_integration_analysis.py
tests/control_plane/test_integration_cas.py
tests/control_plane/test_integration_recovery.py
tests/system/scenarios/reviews_integration.py
```

#### Must remain unchanged
Original revision/source, remote refs, cleanup, publication, and global boundary.

#### Required behavior
1. Side-effect-free deterministic analysis bound to exact OIDs.
2. Current accepted policy evidence and single-use exact authorization.
3. Global lock order and rollback ref.
4. Already-contained/fast-forward CAS with independent post-proof.
5. Non-FF/moved/conflict creates integration workstream/result revision.
6. Recover old/desired/ambiguous ref/index/worktree states.

#### Failure and retry behavior
Stale target/review/auth/version fails before target. Post-CAS cancellation never
claims success; resolution becomes durable. Exact terminal replay immutable.

#### Tests to add first
Auth scope/expiry/replay/cancel, target races, lock inversion, crash each Git/DB
boundary, dirty/in-use target, ambiguous external update, result provenance.

#### Acceptance commands
Run three focused integration modules and targeted T2 integration journey.

#### Stop and escalate
Stop on force/reset/stash/implicit rebase, missing current review policy, or
arbitrary merge strategy.

### C7e — Separate publication and cleanup adapters

#### Goal
Keep remote publication and deletion separate from local integration.

#### Prerequisites
C7d accepted; resources have exact controller ownership/provenance.

#### Required reading
Change §§19–20; Product §11; State security/authorization; current secretary Git
write/cleanup contracts.

#### Allowed files

```text
scripts/pi_control/publication.py
scripts/pi_control/cleanup.py
scripts/pi_control/client.py
scripts/pi_control/cli.py
scripts/pi-secretary-control.py
pi/extensions/secretary/index.ts
tests/control_plane/test_publication.py
tests/control_plane/test_cleanup.py
tests/system/scenarios/cleanup_publication.py
```

#### Must remain unchanged
Integration outcome, source content except exact publication/cleanup fixtures,
real remotes/live resources, and global boundary.

#### Required behavior
1. Read-only plan returns canonical exact actions/hash.
2. Apply repeats exact plan/hash under dedicated current authorization.
3. Publication permits existing origin/current branch/no-force only.
4. Cleanup permits exact controller-owned clean non-live retained resources.
5. Revalidate locks/OIDs/hashes/liveness immediately before effect.

#### Failure and retry behavior
Generic/review/integration authorization rejected cross-kind. Changed plan/state
requires new plan/authorization. Unknown live use blocks cleanup.

#### Tests to add first
Local bare remote success/refusal, no network/force, stale plan, wrong auth,
active/dirty/moved/unowned cleanup, startup race, prefix/wildcard rejection.

#### Acceptance commands
Run publication/cleanup modules and targeted T2 scenario using local bare remote.

#### Stop and escalate
Stop if remote policy or retention decision is absent; do not invent it.

## 10. C8 — Continuity, diagnostics, and configured packages

### C8a — Continuity, auto-continue, and task packet behavior

#### Goal
Prove one truthful persisted continuity and continuation per compaction.

#### Prerequisites
Frozen event/session/task-packet interfaces; C7 process driver path available or
component harness capable of events.

#### Required reading
Observability §§14–15; Product §10; Acceptance §13; current continuity,
auto-continue, workflow-state sources.

#### Allowed files

```text
pi/extensions/continuity/index.ts
pi/extensions/auto-continue/index.ts
pi/extensions/workflow-state/core.mjs
pi/extensions/workflow-state/index.ts
tests/control_plane/continuity-extension.test.mjs
tests/workflow-state.test.mjs
tests/workflow-state-extension.test.mjs
tests/system/scenarios/continuity_observability.mjs
```

#### Must remain unchanged
Session core, controller authority, raw prompt retention defaults, and global
boundary.

#### Required behavior
1. Derive card from actual compaction result + bounded active packet.
2. Persist one card keyed by session/compaction entry.
3. Queue at most one continuation; overflow retry deduplicates.
4. Resume/branch scans persisted entries; packet latest/tombstone wins.
5. Enforce privacy/size/digest consistency.

#### Failure and retry behavior
Crash after append reconciles from persisted entry; unavailable card says so;
malformed/newer entry never crashes Pi or fabricates state.

#### Tests to add first
Manual/threshold/overflow, crash-after-append, duplicate physical entry,
resume/branch, packet replace/clear, malformed/newer, privacy bounds.

#### Acceptance commands
Run three Node suites plus targeted scripted-Pi continuity scenario when C9a
exists. No model/network provider required.

#### Stop and escalate
Stop if card needs raw hidden reasoning/system/tool output.

### C8b — Inspector, error projection, footer, and diagnostics

#### Goal
Provide read-only truthful diagnosis with quiet healthy UI.

#### Prerequisites
C7 process state and controller detail APIs accepted.

#### Required reading
Observability §§7–13, 17; Product §§3, 9; current observability/control extension.

#### Allowed files

```text
pi/extensions/observability/index.ts
pi/extensions/observability/core.mjs
pi/extensions/control-plane/index.ts
scripts/pi_control/error_messages.py
pi/pi-statusline.json
tests/observability-extension.test.mjs
tests/control_plane/observability-extension.test.mjs
tests/control_plane/test_error_messages.py
tests/system/scenarios/continuity_observability.mjs
```

#### Must remain unchanged
Lifecycle state, mutation tools, healthy third-party footer semantics except
necessary aggregate hook, and global boundary.

#### Required behavior
1. Read-only task/fleet/control snapshots with bounded refresh.
2. Missing/malformed/newer data renders unavailable/unsupported.
3. Six-part errors avoid false unchanged/preserved claims.
4. Healthy control plane emits no footer status; one bounded attention aggregate.
5. Technical detail stays exact-project/redacted.

#### Failure and retry behavior
Diagnostic failure does not mutate or crash Pi. Required safety evidence remains
blocking in controller operation; UI telemetry does not.

#### Tests to add first
Rendered widths, live child success/failure/attention, cross-project detail,
malformed/newer, healthy zero status, redaction.

#### Acceptance commands
Run two Node observability suites and Python error module, then targeted T2 UI
scenario.

#### Stop and escalate
Stop if Inspector mutation or hidden reasoning/raw secret display is proposed.

### C8c — Host command, feedback, image, goal, BTW, fast mode, packages

#### Goal
Prove all remaining configured package/extension boundaries and representative
user actions.

#### Prerequisites
C6a exact package loading and C9a scripted driver for T2/T3 evidence.

#### Required reading
System §§7.2, 7.5; current settings/package manifests; host-command/feedback/
image/goal/BTW/fast extension contracts.

#### Allowed files

```text
pi/extensions/host-command/index.ts
pi/extensions/harness-feedback/index.ts
pi/extensions/fast-mode/index.ts
pi/settings.json                         # resource filters only; no version changes
pi/pi-image-tools.json
pi/pi-goal.json
pi/pi-plan-mode.json
tests/host-command.test.mjs
tests/feedback.test.mjs
tests/harness-feedback-extension.test.mjs
tests/fast-mode-extension.test.mjs
tests/test_pi_harness_feedback.py
tests/test_pi_image_attachment.py
tests/test_pi_compaction.py
tests/test_pi_acceptance.py
tests/system/configured-packages.v1.json
tests/system/scenarios/packages.mjs
tests/system/scenarios/host_command_feedback.mjs
```

#### Must remain unchanged
Pinned versions unless a separately approved dependency slice, host authority,
remote publication, and global boundary.

#### Required behavior
1. Host command exact approve/reject/expire/reconcile semantics.
2. Central feedback and project attribution without authority grant.
3. Native image persistence/resume/fork.
4. Goal continuation and BTW separation.
5. Fast mode has no trust/lifecycle effect.
6. Every settings package resolves exact source/version/path, enumerates every
   loaded tool/command/resource, and runs one System §7.5 representative action;
   remote-capable reads use mock/local transport.
7. Any third-party remote-mutating resource is filtered/disabled unless routed
   through the exact authorized C7e publication adapter.

#### Failure and retry behavior
Stale host/goal/feedback request fails; package mismatch fails staging; missing
required staged package is 77; no remote mutation.

#### Tests to add first
One positive/refusal per feature/package, stale restart, provenance, no raw
content, no publication.

#### Acceptance commands
Run all existing focused tests named by configured package manifest, then
`node tests/system/scenarios/packages.mjs` and host-command/feedback scenario.
Before C9a those target commands are incomplete, not evidence.

#### Stop and escalate
Stop on package version change, network mutation, or unbounded sensitive output.

## 11. C9 — Deterministic system harness

### C9a — Fixture, scripted provider, discovery, and evidence

#### Goal
Create isolated real-Pi process infrastructure and fail-closed runner shells.

#### Prerequisites
C0b accepted; pinned Pi is resolvable or preflight can return 77.

#### Required reading
System §§2–6, 9–12; Acceptance §§1–3, 20–24.

#### Allowed files

```text
tests/system/**            # fixtures/assertions/schemas/manifest/runner shells only
```

#### Must remain unchanged
Product/runtime source, real HOME/repository, provider/network, and global
boundary.

#### Required behavior
1. Disposable HOME/XDG/Git/state/stage/evidence fixture and host-state guard.
2. Real pinned Pi scripted no-network provider/driver.
3. Strict fake Pi/Docker/tmux/Herdr/process executables.
4. Capability/action/launcher/extension/package discovery validation.
5. Evidence schema and runner 0/1/2/77 propagation.
6. Empty/incomplete runner shells return 77.

#### Failure and retry behavior
Isolation uncertainty stops before launch. Fake unknown argument fails. Evidence
schema mismatch fails; unavailable prerequisite is 77, never pass.

#### Tests to add first
Fixture escape, fake strictness, no network, manifest discovery, evidence
validation, each exit propagation, incomplete shell 77.

#### Acceptance commands

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/system -p 'test_*.py'
bash tests/system/run-contract.sh
```

#### Stop and escalate
Stop if fixture cannot prove real HOME/repo unchanged or needs production test
hooks.

### C9b1 — Launch/session/presentation action scenarios

#### Goal
Implement HA-001–HA-013 deterministic success/refusal scenarios.

#### Prerequisites
C9a plus C4c, C5c, and C6c accepted.

#### Required reading
System §7.1, JOURNEY-04, assertion bundles.

#### Allowed files
`tests/system/scenarios/{launchers,sessions,personal,secretary,presentation}.py`,
action manifest, process runner.

#### Must remain unchanged
Product code and global boundary; defects return to owning C4–C6 slice.

#### Required behavior
Cover each HA-001…013 scenario IDs, all modes, resume/fork/restart, backend/
layout, help, and known-red regression repairs. Snapshot SQLite/Git/filesystem/
process/presentation/session before/after.

#### Failure and retry behavior
Scenario failure returns 1 with evidence; required backend unavailable 77;
unknown state must show no mutation.

#### Tests to add first
One manifest-driven test per action plus unrelated tmux and duplicate-history
assertions.

#### Acceptance commands
`bash tests/system/run-process-fixture.sh --group launch-session-presentation` and
real T5 subgroup where capability declares required.

#### Stop and escalate
Stop if product behavior fails; do not patch product in C9b1.

### C9b2 — Parent/secretary/workstream action scenarios

#### Goal
Implement HA-020–HA-050 deterministic success/refusal scenarios.

#### Prerequisites
C9a, C5, C7a–c, C8c accepted.

#### Required reading
System §§7.2–7.3 and assertion/evidence rules.

#### Allowed files
`tests/system/scenarios/{tools,children,secretary,workstreams,host_command_feedback,cleanup_publication}.*`, manifest, process runner.

#### Must remain unchanged
Product code, real remote/host, and global boundary.

#### Required behavior
Cover parent tools/modes/goal/delegation/BTW/feedback/host command; secretary
status/attention/create/focus/relaunch/progress/review/integration-workstream/
cleanup/publication. Assert authority, artifacts, process, Git, UI, and no
unexpected mutation.

#### Failure and retry behavior
Expected refusals are asserted semantic results; unexpected prerequisite 77;
mutation crash/replay is deferred to C9c2 but basic idempotency is required.

#### Tests to add first
Manifest parameterization for every HA ID, local bare remote publication, no
network, worker/secretary process identities.

#### Acceptance commands
`bash tests/system/run-process-fixture.sh --group parent-secretary-workstream`.

#### Stop and escalate
Stop on missing product API or real host/remote need.

### C9b3 — Controller/change/UI/package action scenarios

#### Goal
Implement HA-060–HA-091 deterministic process scenarios.

#### Prerequisites
C9a, C1–C8 accepted.

#### Required reading
System §§7.4–7.5 and current/planned status rules.

#### Allowed files
`tests/system/scenarios/{cli_actions,projects,runs,changes,reviews_integration,continuity_observability,packages}.*`, manifest, process/staged runners.

#### Must remain unchanged
Product code, current/planned classification except when owning slice already
landed, and global boundary.

#### Required behavior
Invoke every supported installed CLI/semantic tool; planned entries remain
planned/77 until implementation; cover snapshot/artifact/continuity/UI/image/
fast/package/diagnostic actions. Assert canonical output and all bundles.

#### Failure and retry behavior
Unsupported planned action cannot be marked pass. Current action absence is
fail. Staged package prerequisite unavailable is 77.

#### Tests to add first
Argparse/tool/package manifest parameterization and no direct row seeding after
registration bootstrap.

#### Acceptance commands
`bash tests/system/run-process-fixture.sh --group controller-change-ui` and
`run-staged-installed.sh --group packages` after C10a.

#### Stop and escalate
Stop if manifest/source disagree; return to owning implementation slice.

### C9b4 — Migration/install/admin action scenarios

#### Goal
Implement HA-100–HA-128 plan/refusal scenarios without live cutover.

#### Prerequisites
C9a, C2–C4, C6a, C7e accepted.

#### Required reading
System §§7.6–7.7; Migration; Completion C2–C4/C10.

#### Allowed files
`tests/system/scenarios/{migration,installation,cleanup_publication,recovery_security}.*`, manifest, non-live runners.

#### Must remain unchanged
Live activation/cutover, real user state, and global boundary.

#### Required behavior
Cover typed inventory/resolution/shadow/final plan, build/staged plan, GC/root
migration, activation plan, rollback plan, and every rejected bypass. HA-110–112
remain host-only/planned and cannot pass T0–T5 apply.

#### Failure and retry behavior
Required unavailable adapter/build/backend is 77 or needs_attention per action;
refusal paths produce no side effect.

#### Tests to add first
Each HA ID with fixture legacy records, all disposition states, and authority
bypass corpus.

#### Acceptance commands
`bash tests/system/run-process-fixture.sh --group migration-admin`.

#### Stop and escalate
Stop if a scenario would execute live cutover/cleanup/publication.

### C9c1 — Cross-action journeys

#### Goal
Implement JOURNEY-01–07 without direct resource-row seeding after bootstrap.

#### Prerequisites
C9b1–b4 and all owning product slices accepted.

#### Required reading
System §8 and §10 assertion bundles.

#### Allowed files
`tests/system/scenarios/journeys/**`, action manifest journey links, process/
staged/presentation runners.

#### Must remain unchanged
Product code and global boundary.

#### Required behavior
For each journey capture before/after SQLite, Git, filesystem, process/runtime,
presentation, session/UI/privacy; invoke exact launchers/tools; verify restart and
preservation. Journey-05 final import/cutover remains disposable.

#### Failure and retry behavior
First failed action stops journey, retains evidence, and runner exits 1; missing
required tier exits 77. No cleanup hides state.

#### Tests to add first
One scenario module per JOURNEY-01…07 and an evidence completeness assertion.

#### Acceptance commands
`bash tests/system/run-process-fixture.sh --group journeys`; staged/presentation
subgroups run in their owning tier.

#### Stop and escalate
Stop if any journey needs live or network state.

### C9c2 — Fault/race/security orchestration and aggregate gates

#### Goal
Apply System §§9–11 to every high-risk mutating action and aggregate truthful
gates.

#### Prerequisites
C9c1 accepted.

#### Required reading
System §§4, 9–14; Acceptance §§7, 16, 18, 20–24.

#### Allowed files
`tests/system/scenarios/recovery_security.py`, fault fixtures, assertion bundles,
manifest fault links, and aggregate runners.

#### Must remain unchanged
Product code, real state, and global boundary.

#### Required behavior
1. Crash before/after intent/locks/external effects/observation/event.
2. Concurrent/stale version/epoch/auth/project/path swap corpus.
3. Forbidden-content and authority-bypass scans.
4. Aggregate T0–T5 with structured evidence and preserve 77.
5. Fail if supported action lacks applicable fault/authorization evidence.

#### Failure and retry behavior
Expected ambiguous state is attention, not test failure if contract expects it;
wrong classification is fail. Runner 0/1/2/77 semantics are exact and nested 77
never becomes 0.

#### Tests to add first
Aggregator unit tests for mixed PASS/FAIL/STOP, fault manifest completeness, and
one known recovery action per effect class.

#### Acceptance commands

```bash
bash tests/system/run-source-gate.sh
bash tests/system/run-staging-gate.sh
```

Staging gate is expected 77 until C10/T5 prerequisites pass.

#### Stop and escalate
Stop on false-green skip, missing evidence bundle, or a fault requiring unsafe
host kill/mutation.

## 12. C10 — Staged, Docker, and rollback

### C10a — Exact staged install and loaded-byte proof

#### Goal
Prove source→stage→installed bytes and real Pi loaded resources.

#### Prerequisites
C6a and C9a/b3 accepted.

#### Required reading
Migration §§8–9; Completion C10; System T3; current installer/staged build.

#### Allowed files

```text
install.sh
scripts/pi_control/staged_build.py
tests/control_plane/test_build_manifest.py
tests/pi-installer-transaction.sh
tests/system/run-staged-installed.sh
tests/system/scenarios/installation.py
tests/system/scenarios/packages.mjs
```

#### Must remain unchanged
Live install, Docker runtime behavior, package versions/source beyond C6a, and
global boundary.

#### Required behavior
1. Install exact reviewed tree into disposable HOME.
2. Compare canonical manifest/tree/modes/symlinks.
3. Launch real pinned Pi scripted driver.
4. Prove loaded controller/extensions/package roots/build.
5. Run deterministic installed journeys twice.

#### Failure and retry behavior
Missing npm/Pi is 77; any byte/path/co-load mismatch is fail and exact disposable
prior generation restored.

#### Tests to add first
Unexpected/missing/tampered file, symlink/root swap, incomplete npm, wrong loaded
path, interrupted stage.

#### Acceptance commands
Run build-manifest test, installer transaction, and staged-installed runner.

#### Stop and escalate
Stop if live paths or repository source are used as installed proof.

### C10b — Real Docker runtime and image proof

#### Goal
Prove kernel/container execution guarantees on disposable labeled resources.

#### Prerequisites
C6c, C10a, C9a accepted.

#### Required reading
Execution §§4–7; Acceptance §8; System T4; Docker test scripts.

#### Allowed files

```text
pi/sandbox/Dockerfile
scripts/pi_control/runtime_adapter.py       # only if a failing proof identifies defect
tests/pi-docker-control-plane-e2e.sh
tests/system/run-docker.sh
tests/system/fixtures/docker_fixture.sh
tests/system/scenarios/docker.py
```

#### Must remain unchanged
Live/unlabeled Docker resources, host ports/network, source interfaces unless
returned to C6c, and global boundary.

#### Required behavior
Image/build/mount/user/security exactness; trusted/isolated/read-only runs;
attest/tool/stop; wrong-state matrix; unique labels; no host publication/socket.

#### Failure and retry behavior
Daemon/image unavailable → 77 before mutation; behavioral mismatch → fail with
resources retained/cleaned by exact fixture ownership only.

#### Tests to add first
Every runtime mismatch, old writable container, permission repair, disappearance,
network/socket/mount leak, graceful stop.

#### Acceptance commands
`bash tests/system/run-docker.sh` and existing Docker control-plane E2E. No test
may convert 77 to pass.

#### Stop and escalate
Stop if isolation cannot be proven or cleanup would touch unlabeled resources.

### C10c — Interrupted installer and exact rollback matrix

#### Goal
Prove every partial staged activation restores exact previous generation while
preserving new recovery data.

#### Prerequisites
C10a–b accepted.

#### Required reading
Migration §§12, 16–19; System JOURNEY-05; installer transaction contract.

#### Allowed files

```text
install.sh
scripts/pi_control/staged_build.py
tests/pi-installer-transaction.sh
tests/pi-staged-artifact-rollback.sh
tests/system/scenarios/installation.py
tests/system/scenarios/rollback.py
tests/system/run-staged-installed.sh
tests/system/run-staging-gate.sh
```

#### Must remain unchanged
Live install/activation, new DB/refs/worktrees/evidence during rollback, and
global boundary.

#### Required behavior
Failpoint every prepare/swap/verify step; restore exact files/symlinks/modes/
settings/packages/image selection; preserve controller DB/new refs/work/evidence;
compare before/after canonical manifests.

#### Failure and retry behavior
Uncertain restore is fail/attention, never success. Missing Docker prerequisite
is 77. Retry observes current generation before any swap.

#### Tests to add first
All swap boundaries, signal interruption, post-verify mismatch, rollback itself
interrupted, preserved recovery resources.

#### Acceptance commands
Run installer transaction, staged rollback, staged-installed, Docker, and final
staging gate. A 77 result remains STOP.

#### Stop and escalate
Stop if rollback requires deleting work/recovery data or touching live paths.

## 13. C11 — Phase 11D runbook document only

### C11 — Write and statically validate the canary runbook

#### Goal
Produce a complete non-executed canary/rollback document.

#### Prerequisites
C0–C10 accepted. If no user canary is selected, use
`BLOCKED_AWAITING_CANARY_SELECTION` and do not invent one.

#### Required reading
Migration §§10–21; Acceptance GO/STOP; Completion C11; System T7.

#### Allowed files

```text
pi/control-plane/PHASE_11D_CANARY_RUNBOOK.md
tests/system/test_canary_runbook.py
```

#### Must remain unchanged
All executable/source/live state and global boundary.

#### Required behavior
Include status/authorization placeholder, exact canary IDs, source/build/schema/
image/latch/migration/inventory/resolution IDs, capabilities, quiescence,
backup/restore, displayed host commands, journey/fault, evidence, stops,
rollback, post-diff, and general-rollout prohibition.

#### Failure and retry behavior
Missing selection remains a structurally valid blocked runbook. Missing safety
field fails static validation. No command executes.

#### Tests to add first
Validator checks all headings/placeholders, blocked status, command fences,
absence of secrets, and no executable invocation.

#### Acceptance commands

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.system.test_canary_runbook
```

#### Stop and escalate
Do not call `pi-host`, installer, activation, Docker/tmux/Herdr mutation, or ask a
worker to choose the canary. Execution is a new explicit user-authorized task.
