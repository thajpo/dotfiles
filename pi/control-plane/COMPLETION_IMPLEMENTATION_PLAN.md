# Pi control-plane completion implementation plan

Status: **C0–C10 implementation slices are complete as a non-live candidate and
C0/C1–C10 checkpoint reviews have accepted the current source evidence;
staged/Docker/presentation capability gates and Phase 11D remain blocked;
documentation only; no live authority**.

Audience: an implementation model that has no access to the architecture
conversation and must not invent product, authority, migration, or activation
semantics. Give that model only one lettered sub-slice, using the complete exact
handoff in `IMPLEMENTATION_SLICE_BRIEFS.md`.

This plan supplements the original phase history in
`MVP_IMPLEMENTATION_PLAN.md`. If an older phase-completion statement conflicts
with this document, this document controls the remaining-work classification.
The five contracts remain authoritative for behavior. Any required contract
change must be made and reviewed in Slice C0 before implementation code uses it.

## 1. What this harness is trying to achieve

The harness is meant to make local AI-assisted engineering feel like working
with projects, conversations, working copies, changes, and reviews—not like
managing containers, route files, session hashes, leases, pane IDs, or recovery
refs.

The complete system must provide all of the following:

1. **Durable work.** Restarting Pi reopens the exact conversation and working
   copy selected previously. A process restart creates a new run, not a new
   identity or a guessed workspace.
2. **Exact execution.** Every run and child starts from an explicitly selected
   project, working copy, Git state, installed build, runtime specification,
   and authority. Nothing chooses “latest” or falls back to an older path.
3. **One lifecycle authority.** SQLite owns lifecycle relationships and intent.
   Git owns source content. Pi JSONL owns conversation content. Host policy owns
   trust. Kernel locks and process state own live exclusion observations.
4. **One writer.** At most one run may mutate a working copy. A stale process,
   released marker, missing PID, or old timestamp never proves write access is
   gone.
5. **Safe delegation.** Read-only helpers receive immutable exact snapshots and
   are mechanically read-only. Independent writers receive separate working
   copies. Child results never move a target branch directly.
6. **A conventional local change workflow.** Personal agents and secretary
   workstreams submit immutable Git revisions to the same queue. Reviews bind
   exact revisions. Integration is a separate, explicitly authorized Git CAS
   operation.
7. **Backend-independent conversations.** tmux and Herdr present and focus
   conversations; they do not create project, workstream, run, or writer
   authority. Switching presentation never silently migrates or kills work.
8. **Understandable failure and recovery.** Errors state the attempted action,
   observed risk, known side effects, preserved state, safe choices, and a path
   to bounded technical evidence.
9. **Visible continuity.** Compaction retains one user-visible, privacy-bounded
   continuity card derived from the same result retained for the model.
10. **Reproducible activation.** Installed launchers, extensions, packages,
    Python controller, configuration, and image must be the exact reviewed
    build. Activation and rollback are explicit host operations.
11. **Quiet healthy operation.** Normal UI shows project/work activity, model,
    and attention—not route IDs, container names, leases, epochs, or hashes.
12. **User authority at consequential seams.** Creation, host commands,
    integration, publication, cleanup, migration cutover, and activation use
    separate exact approvals. A generic “yes” is never reusable authority.

## 2. What “complete” means

Do not use one unqualified `complete` label. Record these gates separately:

| Gate | Meaning | Current evidence |
|---|---|---|
| **Component source** | Library/schema/adapter behavior passes deterministic unit and component tests | Substantial candidate exists; 209 control-plane tests passed at the last review |
| **Harness integration** | Actual installed launchers, extensions, personal/secretary clients, workstreams, runtime, and child paths all use the controller contract | **Incomplete**; active launchers still use legacy root/secretary/workspace state and several current E2E checks do not launch the real path |
| **Migration-ready** | Every configured legacy source is typed and receives an explicit import/observe/defer/contradiction disposition; shadow replay is crash-safe | **Incomplete**; filesystem/Git project subset only |
| **Staging accepted** | Exact installed generation and real Pi extension/package loading pass the full non-live system suite, including Docker rollback | **Incomplete**; available checks pass, Docker prerequisite stops with exit 77 |
| **Canary accepted** | One explicitly selected real project runs controller-only and survives restart/fault/rollback evidence | **Not started; Phase 11D blocked** |
| **General rollout** | Additional projects migrate one at a time after canary acceptance | **Not started** |

The existing `tests/control_plane/test_walking_skeleton.py` is a valuable
component test, but it inserts resource rows directly and therefore is not
full-system evidence. Likewise, source-regex/transform tests establish
reachability and syntax, not real extension invocation or launcher wiring.

## 3. Fixed authority and safety boundaries

No worker may change these decisions.

| Concern | Authority | Adapter/projection | Forbidden shortcut |
|---|---|---|---|
| Source content | Git objects and refs | Working trees, containers, review checkouts | SQLite blobs, route `startingOid`, timestamps, or branch labels selecting content |
| Lifecycle relationships and intent | Controller SQLite | Routes, root registry, secretary facade, labels, UI | Keeping a second writable lifecycle registry in controller mode |
| Conversation content | Exact Pi session JSONL | Root selector, continuity view | Copying messages into SQLite or using header `cwd` as binding authority |
| Project trust | Host-owned policy bound to registered Git identity | Sandbox/project-trust hooks | Repository files, model input, environment, or storage path broadening trust |
| Live writer exclusion | Kernel lock + controller writer epoch + verified runtime/process observation | PID/start identity and container labels | PID absence, TTL, pane state, or a marker proving revocation |
| Runtime readiness | Immutable manifest + one-use independent attestation | Sandbox/container adapter | Container existence/name/label proving readiness |
| Child source | Parent-selected immutable Git snapshot/revision | Child route/runtime | Child selecting current/latest parent state |
| Change content | Immutable Git revision ref | SQLite provenance/review/integration metadata | Mutable submitted refs or review evidence authorizing integration |
| Target mutation | Exact user authorization + project policy + Git CAS | Secretary semantic tool | Generic approval, review verdict, force/reset/stash, implicit rebase |
| Presentation | No source/lifecycle authority | tmux/Herdr assignments and observations | Pane/session IDs creating or rebinding controller resources |
| Installed generation | Reviewed build manifest and active installed-build record | Symlinks/launchers/package loader | Repository source or successful installer output proving active bytes |
| Migration | Migration operation + immutable inventory/resolution/build manifests | Legacy readers and shadow DB | Timestamp precedence, dual writes, cleanup, or silent omission |
| Remote publication | Separate current authorization and host publication adapter | Secretary publication tool | Local integration automatically pushing/deploying |

Additional boundaries:

- No daemon is required for correctness.
- No live process/container migration. Existing processes are observations;
  cutover gracefully stops or explicitly excludes them, then starts new runs.
- No same-working-copy writer child in the MVP.
- No cross-run container reuse in the MVP.
- No automatic cleanup, force deletion, remote push, deployment, or general
  cutover.
- Preserve all ambiguous Git refs, worktrees, session histories, artifacts, and
  the unresolved MLRE `970ea8e` / `b296516` alternatives.
- An unavailable required adapter is `unknown`/`unavailable` and blocks the
  applicable gate; it is never an empty successful observation.

## 4. Ambiguities resolved by this plan

These were previously implied across documents. They are explicit now.

### 4.1 Legacy secretary state versus controller state

There will not be two long-term project/workstream authorities.

- In `legacy` mode, existing `scripts/pi-secretary-control.py` state remains the
  writer and controller access is absent or observation-only.
- In `shadow` mode, legacy remains the only writer. The controller may import
  and compare but may not create/stop/land/clean external resources.
- In `controller` mode, SQLite owns projects, working copies, conversations,
  workstreams, runs, attention, changes, reviews, and integrations.
  `scripts/pi-secretary-control.py` becomes a compatibility facade over semantic
  controller operations and MUST NOT update its old lifecycle registry.
- Existing secretary project/workstream IDs (including unprefixed 64-hex IDs)
  are legacy external identities only. Migration mappings link them to new
  cryptographically random `prj_...`/`ws_...` controller IDs. They are never
  reformatted, truncated, or reused as controller logical IDs. Compatibility
  responses may carry both names explicitly during transition.
- Presentation preferences (active secretary project order, desktop/mobile,
  tmux/Herdr) may remain in a bounded presentation store because they do not
  own project/workstream identity. Every referenced project/conversation must
  resolve through SQLite.
- Bounded human notes may remain in a separate note store. They cannot alter
  lifecycle, trust, writer, review, or integration state.

### 4.2 Workstream identity

A workstream is a first-class controller resource, not merely an informal
combination of a conversation and path. The next schema migration must add the
resource defined in `STATE_CONTRACT.md` §16. It binds one project, one separate
controller-owned working copy, one durable conversation, one bounded brief,
and one target/base. A separate presentation assignment is the sole desired
backend record; external pane/window IDs remain observations.

### 4.3 Activation selection and fail-closed bootstrap

The launcher cannot rely only on the controller DB to decide whether failure to
open that DB should fall back to legacy. A small host-owned activation latch is
therefore an allowed bootstrapping projection.

Canonical path:

```text
${XDG_STATE_HOME:-~/.local/state}/pi-control/activation.v1.json
```

Contract:

- owned by the host user, parent directory `0700`, regular file `0600`, no
  symlink components, canonical JSON, atomic replace, fsync, and manifest
  digest;
- contains only project Git identity anchors, controller project ID, mode
  (`legacy|shadow|controller`), activation resource version, expected DB path,
  schema, build ID, and record digest;
- generated only from committed `project_activations` rows by a host-authorized
  activation/rollback operation;
- cannot provide working-copy, conversation, run, trust, or source authority;
- in `shadow` or `controller` mode, the latch and SQLite row must agree exactly;
- in `controller` mode, missing/unreadable DB, row, build, migration proof, or
  matching generation is a hard stop—never legacy fallback;
- once a control-aware installed generation is active, a missing or malformed
  latch is a hard stop for managed launchers, not “all projects are legacy”;
- an unregistered repository may use legacy behavior only after a valid latch
  proves it has no matching activation record and host policy permits it;
- no model/caller environment variable can change production mode or DB path.
  Tests inject a disposable latch/root through constructor or fixture-only
  launcher wiring, never a production environment override.

### 4.4 Existing runtime and review records

- Existing routes, leases, PIDs, containers, tmux panes, and Herdr panes import
  as observations only. They never become active controller runs because their
  capabilities, epochs, manifests, and attestations cannot be reconstructed.
- Existing session JSONL may become controller conversations after exact
  identity and binding validation; JSONL remains content authority. Current
  legacy root migration behavior that selects a duplicate by file modified time
  is not accepted for controller migration: duplicates are all preserved and
  require an exact resolution decision.
- Existing candidate branches/refs do not automatically become changes. A
  typed adoption proposal names exact objects and requires an explicit mapping
  decision.
- Legacy review receipts may import as historical evidence only. They cannot
  satisfy controller review policy or integration authorization unless they
  already meet the exact authenticated receipt schema and provenance checks.
- Existing artifacts remain external files; only exact safe hashes/provenance
  are indexed.

### 4.5 Presentation restart and project switching

- `pi-restart` must stop only exact managed Pi tmux sessions. It MUST NOT call a
  broad `tmux kill-server` that terminates unrelated user sessions.
- Removing/switching an active secretary project must gracefully stop and
  verify the exact secretary process, or refuse. It must not kill a live turn
  by deleting its window.
- A live worker remains pinned to its recorded backend. Grid restart either
  leaves that worker intact or refuses; it never migrates or kills it.
- tmux/Herdr state that cannot be inspected is `unknown` and blocks mutation.

### 4.6 First-party package authority

The staged/activated configuration must load the reviewed first-party packages,
not merely install their trees:

```text
./packages/pi-sandbox-control
./packages/pi-subagents-control
```

The activated `settings.json` must stop selecting the legacy
`npm:@kjrjay/pi-sandbox@0.2.0` and remote `npm:pi-subagents@0.35.1` identities
for controller-mode runs. Pi package diagnostics and a real process test must
prove the loaded extension files resolve inside the activated first-party
package trees. Legacy packages may exist only for an explicit `legacy` project
and must never be co-loaded with their controller replacements in one run.
Every configured third-party package tool/command is inventoried; remote-mutating
resources are filtered/disabled unless routed through the separately authorized
publication adapter.

### 4.7 Meaning of complete action coverage

Natural-language utterances are unbounded. “Complete system coverage” means:

1. every harness-owned semantic action in
   `SYSTEM_INTEGRATION_TEST_PLAN.md` has a stable action ID;
2. every CLI subcommand, extension tool/command, launcher mode, configured
   package boundary, and user authorization class maps to at least one action;
3. every supported action has deterministic success and refusal coverage;
4. every mutating action has idempotency/restart or explicit non-retry coverage,
   authorization checks, and before/after state assertions;
5. high-risk actions additionally have crash, race, stale-state, cross-project,
   and rollback scenarios;
6. model wording/intent recognition is an evaluation tier, not substituted for
   deterministic semantic-tool tests;
7. the action manifest validator fails when a new action has no scenario.

This is complete semantic coverage, not every Cartesian combination of every
flag. Pairwise matrix coverage is required across trust mode, workflow role,
working-copy kind, clean/dirty state, backend/layout, and new/resume/restart.

## 5. Required schema and protocol freeze

Slice C0 must copy these additions into the normative state/API contracts before
code changes. The next migration is schema v7; do not reuse or edit v1–v6.

### 5.1 New controller resources

Schema v7 adds:

1. `workstreams` — stable `ws_...` identity; exact project, working copy,
   conversation, title/brief, target/base, desired/observed state, ownership,
   resource version, timestamps, and bounded error fields.
2. `presentation_assignments` — desired backend and current bounded observation
   for one conversation. External tmux/Herdr locators are mutable observations,
   never logical IDs.
3. `project_activations` — per-project `legacy|shadow|controller` mode, exact
   build/migration/project-version binding, resource version, and activation
   timestamps.
4. `migration_resource_mappings` — immutable record-level disposition and
   provenance from one inventory record to zero or one controller resource.

Mechanical requirements:

- workstream project, working-copy project, and conversation project must agree;
- workstream working copy must be separate, controller-owned, and have purpose
  `workstream` or `integration`;
- one active workstream per working copy and conversation;
- presentation assignment cannot change project/conversation identity;
- direct `legacy -> controller` transition is forbidden; use
  `legacy -> shadow -> controller`;
- `controller` requires the selected build to be active, the exact migration to
  have succeeded, no blocking mappings/attention in canary scope, and a
  current project resource version;
- rollback may transition `controller -> legacy` only through a recorded
  rollback operation after writers are quiesced;
- migration mappings and their source identity/digest/disposition cannot be
  updated or deleted; corrections create a new migration/inventory;
- every mutable state transition uses resource-version CAS and emits its event
  in the same transaction.

### 5.2 Inventory manifest v2

The generic file list is replaced by a typed canonical envelope:

```json
{
  "schemaVersion": 2,
  "inventoryId": "inv_...",
  "createdAt": "...",
  "host": {"platform": "...", "visibility": "bounded"},
  "sources": [],
  "records": [],
  "relationships": [],
  "contradictions": [],
  "adapterStates": [],
  "manifestDigest": "sha256:..."
}
```

Each record contains:

```text
recordId (digest-derived, deterministic within the manifest)
adapterKind and adapterSchemaVersion
sourceKind and bounded source locator/provenance
sourceDigest, owner/mode/size where applicable
observedAt and observation state
resourceKind
normalized identity and bounded normalized fields
relationships to other record IDs
proposed disposition
sensitivity/omission summary
```

Rules:

- no credentials, capabilities, raw prompts, arbitrary environment, or source
  file bodies;
- session JSONL records only header/identity, file digest/size, branch/content
  metadata needed for migration—not message bodies;
- process/container/presentation command/environment data is allowlisted and
  redacted;
- every configured adapter emits exactly one state:
  `observed|unavailable|error|unsupported` with reason and provenance;
- empty results differ from unavailable adapters;
- record IDs and manifest digest are stable for identical observations after
  excluding observation timestamp fields from identity hashing.

### 5.3 Resolution manifest v1

Contradictions and adoption choices are resolved in a separate immutable input:

```json
{
  "schemaVersion": 1,
  "inventoryDigest": "sha256:...",
  "scope": {"projectRecordIds": []},
  "decisions": [
    {
      "recordId": "...",
      "disposition": "import|observe|unmigrated|exclude|requires-decision",
      "resourceType": "...",
      "resourceId": null,
      "reason": "...",
      "expectedDigest": "sha256:..."
    }
  ],
  "createdBy": "user|migration-policy",
  "createdAt": "...",
  "manifestDigest": "sha256:..."
}
```

A policy may generate deterministic decisions only where the contracts define a
single answer. Human-owned decisions include duplicate conversation mapping,
incomplete repository rebind proof, change adoption, divergent refs, and canary
scope. A changed inventory invalidates the resolution manifest.

### 5.4 Semantic APIs required before launcher cutover

Add bounded protocol operations; no arbitrary shell/Git payloads:

```text
activation.inspect / activation.plan / activation.apply / activation.rollback
launch.resolve
conversation.ensure / conversation.archive / conversation.focus
run.prepare / run.attest / run.start / run.stop / run.reconcile
workstream.plan / workstream.create / workstream.focus / workstream.relaunch / workstream.retire
presentation.observe / presentation.assign
migration.inventory / migration.plan / migration.shadow-import / migration.compare
migration.final-import / migration.cutover / migration.rollback
change.submit / change.close
review.request / review.submit
integration.analyze / integration.authorize / integration.apply
cleanup.plan / cleanup.apply
```

`activation.apply`, final import, cutover, rollback, publication, and cleanup
apply are host-only semantic operations and are not registered as ordinary
model tools. Controller mode launchers call `launch.resolve`; they do not read
resource rows and assemble authority independently.

All protocol requests use exact-field canonical JSON bounded to 64 KiB unless a
narrower contract says otherwise. Responses use:

```json
{"protocolVersion": 2, "operation": "...", "value": {}}
```

Failures use the existing six-part projected error envelope and stable codes.
Completion slices add only these named codes to the State §15 vocabulary:

```text
CP_INVALID_REQUEST
CP_ADAPTER_UNAVAILABLE
CP_MIGRATION_UNRESOLVED
CP_ACTIVATION_UNAVAILABLE
CP_ACTIVATION_MISMATCH
CP_WORKSTREAM_CONFLICT
CP_PRESENTATION_UNKNOWN
```

Operation retry rules are fixed:

- same idempotency key + identical complete authority/request digest returns the
  existing operation/result;
- same key + any changed request, actor, resource/version, epoch,
  authorization, build, inventory, or resolution binding returns
  `CP_IDEMPOTENCY_CONFLICT`;
- `succeeded|failed|cancelled` outcomes never regress;
- `needs_attention` is durable and non-automatically-retryable. Repeating the
  same request returns the same attention result until an explicit reconcile or
  human resolution creates/authorizes a new operation step;
- adapter unavailable is not retried inside an open SQLite transaction and
  cannot be translated to an empty observation;
- after uncertain external effects, reconciliation observes before any retry.

## 6. Typed migration adapter contract

Create separate modules under
`scripts/pi_control/migration_adapters/`; do not grow another generic recursive
parser in `migration.py`.

| Adapter | Reads | Normalized result | Import rule |
|---|---|---|---|
| `git` | Explicit repositories, worktree porcelain, refs/OIDs/status/object format | Projects, working-copy candidates, refs, divergence, dirty state | Git is content authority; verified projects/copies may import; unmanaged refs/copies remain observations |
| `root_sessions` | Root registry and exact root/archive JSONL headers | Conversation/session/repository/worktree relationships | Import only exact unique bindings; duplicate/divergent bindings require decision |
| `secretary` | Project registry, active presentation set, workstream/runtime/attention/review records | Project aliases, workstream candidates, bounded intent and links | Lifecycle rows import only after Git/session validation; active set remains presentation preference |
| `routes_leases` | Task routes, transitions, child leases, async status/event metadata | Historical run/parent/source/process observations | Never import as active run/writer/authorization |
| `artifacts` | Exact controller/Pi-owned artifact manifests and files | Hash, size, producer/source references, sensitivity/retention | Index only safe exact files; unknown/unowned files remain observations |
| `processes` | `/proc` or bounded `ps` with PID/start identity/ancestry | Live/absent/unknown process observations | Observation only; unknown never stopped |
| `docker` | Bounded `docker ps/inspect/image inspect` for exact Pi labels | Container/image/mount/run-label observations | Observation only; unlabeled resources never adopted or cleaned |
| `tmux` | Exact managed server/session/window/pane formats and pane process IDs | Presentation observations linked to validated conversations | Observation only; never creates identity |
| `herdr` | Exact named session API snapshots and managed configs | Space/tab/pane/agent observations | Observation only; native restore remains disabled |
| `installed_build` | Active launchers/symlinks/settings/extensions/helpers/packages/manifest/image | Exact installed build candidate and mismatches | Register active build only when all required bytes and loaded package roots match |
| `policy` | Host repository policy and machine profile | Policy version/hash and trust inputs | Bind to project; repository data cannot override |
| `backups` | Exact prior installer/migration backup manifests | Restorable generations and verification status | Evidence only until disposable restore succeeds |

All adapters expose pure `observe()` separately from import/apply. Subprocess
commands use fixed executable/argument templates, sanitized environment,
timeouts, output limits, and structured parsers. No adapter receives a
model-supplied command or arbitrary root scan.

## 7. Record disposition contract

“Complete migration support” does not mean pretending every legacy record is
safe to import. It means every record has one explicit disposition:

| Disposition | Meaning | May block selected canary? |
|---|---|---|
| `import` | Exact identity/provenance permits a controller resource row | No, if compare matches |
| `observe` | Useful evidence but not lifecycle authority (for example PID/container/pane/route) | Yes when it may still write or conflicts |
| `unmigrated` | Preserved legacy resource outside current canary scope | No for other projects; yes inside selected scope |
| `exclude` | Explicitly outside scope with reason and preservation proof | No if exclusion cannot affect selected writers |
| `requires-decision` | More than one valid mapping or product choice | Yes |
| `contradiction` | Claims cannot simultaneously be true and safety depends on resolution | Yes |

Nothing disappears from the report because an adapter is unavailable or the
resource type is unsupported.

## 8. Dependency-ordered implementation slices

A lettered sub-slice below is the maximum ordinary worker assignment. The
parent owns integration and reruns the parent slice acceptance after all
sub-slices. Do not give a weaker worker a whole parent slice when this table
splits it.

| Parent | Sequential worker-sized sub-slices |
|---|---|
| C0 | **C0a** status/contract wording freeze; **C0b** action catalog/manifest schema and discovery validator |
| C1 | **C1a** v7 DDL/migration only; **C1b** models/store/CAS/triggers; **C1c** bounded client/CLI protocol using fake adapters |
| C2 | **C2a** root/session/secretary/route/artifact adapters; **C2b** Git/policy/installed-build/backup adapters; **C2c** process/Docker/tmux/Herdr adapters; **C2d** manifest graph, relationships, contradictions, canonical write |
| C3 | **C3a** resolution manifest/planner and mappings; **C3b** dependency-ordered importer; **C3c** field reconciliation, failpoints, retry/tamper recovery |
| C4 | **C4a** activation row/latch plan and secure I/O; **C4b** exact session/root projection; **C4c** launcher `launch.resolve` integration and no-fallback matrix |
| C5 | **C5a** workstream saga; **C5b** secretary compatibility facade; **C5c** tmux/Herdr assignment, restart/swap/relaunch safety |
| C6 | **C6a** staged settings and first-party package loading; **C6b** launcher run/manifest/lock lifecycle; **C6c** runtime preparation/attestation/tool fence/stop |
| C7 | **C7a** personal/secretary selection/status/workstream/submission wiring; **C7b** child/snapshot/artifact process wiring; **C7c** exact reviewer/receipt; **C7d** integration authorization/CAS/recovery; **C7e** separate publication/cleanup |
| C8 | **C8a** continuity/auto-continue/task-packet behavior; **C8b** Inspector/error/footer behavior; **C8c** host-command/feedback/image/goal/BTW/configured-package behavior |
| C9 | **C9a** fixture/scripted provider/discovery/evidence; **C9b1** launch/session/presentation actions; **C9b2** parent/secretary/workstream actions; **C9b3** controller/change/UI/package actions; **C9b4** migration/install/admin actions; **C9c1** cross-action journeys; **C9c2** crash/race/security and gate aggregation |
| C10 | **C10a** staged install/loaded-byte proof; **C10b** real Docker runtime/image proof; **C10c** interrupted install and exact rollback matrix |
| C11 | One documentation/runbook slice; no execution |

`IMPLEMENTATION_SLICE_BRIEFS.md` is normative for each sub-slice's exact
reading list, allowed files, algorithm/state transitions, failure/error behavior,
tests, commands, and stop conditions. If an interface shared with a later
sub-slice must change, C0/C1 owns and freezes it first; later workers stop rather
than editing the shared contract opportunistically.

### C0 — Contract, status, and traceability freeze

**Goal:** make the remaining implementation mechanically unambiguous before
code changes.

**Required reading:** all five contracts, this plan,
`SYSTEM_INTEGRATION_TEST_PLAN.md`, migration/acceptance plans, current source and
tests.

**Allowed files:** control-plane documentation and new test action manifest
schema only.

**Work:**

1. Copy schema/API/manifest additions from §§5–7 into the normative contracts.
2. Assign stable action IDs from the system integration plan.
3. Add a machine-readable action manifest schema and validator skeleton.
4. Update stale status headers to distinguish component source from integrated,
   staged, and live evidence.
5. Record every current CLI/tool/launcher action as supported, compatibility,
   host-only, or out of scope.

**Tests must show:** documentation links exist; no duplicate action IDs; every
controller CLI subcommand and explicitly loaded extension tool appears in the
action catalog; status claims do not call source tests live acceptance.

**Stop:** any unresolved authority conflict, especially activation bootstrap,
workstream identity, or legacy/controller writer ownership.

### C1 — Schema v7 and semantic protocol

**Goal:** add completion resources and typed protocol without external side
effects.

**Expected files:**

```text
scripts/pi_control/schema.py
scripts/pi_control/models.py
scripts/pi_control/store.py
scripts/pi_control/client.py
scripts/pi_control/cli.py
scripts/pi_control/migrations/v007_completion_resources.py
tests/control_plane/test_schema_migrations.py
tests/control_plane/test_activation.py
tests/control_plane/test_workstreams.py
tests/control_plane/test_migration_mappings.py
```

**Algorithm:** migrate v6→v7 transactionally; add model/validation/CAS helpers;
add read-only plan APIs and fake-adapter operations; emit state/event atomically.
No launcher or legacy file changes.

**Tests must show:** fresh schema and every upgrade path reach identical v7;
checks/triggers reject cross-project workstream links, direct legacy→controller,
mutable migration mapping, invalid presentation binding, stale CAS, and active
build/migration mismatch; old/newer schema failure is closed; no external state
changes.

**Stop:** editing prior migrations, storing raw session content, or using a
second writable state file.

### C2 — Typed inventory adapters

**Goal:** implement inventory manifest v2 for every configured source.

**Expected files:**

```text
scripts/pi_control/migration.py
scripts/pi_control/migration_adapters/{base,git,root_sessions,secretary,routes_leases,artifacts,processes,docker,tmux,herdr,installed_build,policy,backups}.py
tests/control_plane/migration_adapters/test_*.py
tests/control_plane/test_migration_inventory_v2.py
```

**Approach:** explicit roots and adapter calls only; typed normalization;
relationship graph; contradiction rules; canonical envelope; immutable secure
write. Runtime/presentation adapters are read-only.

**Tests must show:** each adapter success, legitimate empty result, unavailable,
malformed/oversized input, timeout, symlink/parent swap, duplicate records,
cross-project records, and redaction; shared references are not false
contradictions; divergent session/worktree/OID claims are; legacy sources and
Git/runtime state are byte-for-byte unchanged.

**Stop:** silent adapter omission, generic home-directory crawl, timestamp
selection, container/pane mutation, or unbounded command/output.

### C3 — Resolution planning and crash-safe shadow import

**Goal:** turn typed inventory plus exact decisions into complete shadow rows and
field-level comparisons without live mutation.

**Expected files:**

```text
scripts/pi_control/migration.py
scripts/pi_control/migration_planner.py
scripts/pi_control/migration_importer.py
scripts/pi_control/migration_reconcile.py
tests/control_plane/test_migration_plan.py
tests/control_plane/test_migration_import_v2.py
tests/control_plane/test_shadow_reconcile_v2.py
tests/control_plane/test_migration_failpoints.py
```

**Algorithm:**

1. Verify inventory and resolution manifests.
2. Create one migration operation, allocate cryptographically random logical
   resource IDs, and persist the complete record→resource mapping in the first
   transaction. Retries reuse those stored IDs; paths/digests never become
   logical IDs. Only inventory `recordId` values are digest-derived.
3. Persist intent and mappings before files/rows.
4. Import project, working-copy, conversation, workstream, presentation,
   artifact, and historical observation dispositions in dependency order.
5. Never create active runs, writer claims, accepted reviews, or
   authorizations from legacy observations.
6. Re-observe source and imported fields before success.
7. On retry, adopt only exact matching controller-owned rows/files.
8. On changed/ambiguous state, mark needs-attention and preserve both versions.

**Tests must show:** failpoints before/after manifest, operation, every resource
batch, mapping, event, and final comparison; exact retry produces one result;
changed request conflicts; source changes abort; tamper is attention; complete
field/relationship comparison catches wrong project, worktree, session,
workstream, policy, OID, build, and disposition.

**Stop:** live/default state root, unresolved canary-scope decision, active
legacy writer hidden as “observed,” or cleanup.

### C4 — Activation latch and exact root/project binding

**Goal:** make production launch selection host-owned and make root/session
registration consume exact controller bindings.

**Expected files:**

```text
scripts/pi_control/activation.py
scripts/pi_control/session_adapter.py
scripts/pi_control/client.py
scripts/pi_control/cli.py
scripts/pi-root-session.py
pi/extensions/root-session/index.ts
bin/pi
bin/pi-tmux-session
tests/control_plane/test_activation.py
tests/control_plane/test_launcher_resolution.py
tests/test_pi_root_sessions.py
tests/control_plane/root-session-extension.test.mjs
```

**Approach:** install/read secure latch; canonical Git lookup; call
`launch.resolve`; controller mode returns exact project/conversation/working
copy/session/build; root registry becomes a digest/version-bound projection.
Legacy and shadow retain their defined writers. No production environment mode
override.

**Tests must show:** all three modes; missing/corrupt/mismatched latch; missing
DB; stale activation/project/build/migration version; moved/copied repository;
header cwd mismatch; null→repository; wrong session file; child session
exclusion; project cross-binding; controller failure never falls back; legacy
mode remains unchanged; shadow performs no external mutations.

**Stop:** any path where cwd/header/route chooses a controller binding, or a
controller-mode error invokes legacy mutation.

### C5 — Controller-owned workstream and presentation lifecycle

**Goal:** replace secretary lifecycle registry writes with a controller saga and
make presentation safe.

**Expected files:**

```text
scripts/pi_control/workstreams.py
scripts/pi_control/presentation_adapter.py
scripts/pi-secretary-control.py
pi/extensions/secretary/index.ts
pi/extensions/workstream-brief/index.ts
pi/extensions/workstream-channel/index.ts
bin/pisec
bin/pi-secretary
bin/pi-personal
bin/pi-personal-herdr
bin/pi-secretary-herdr
bin/pi-herdr-workstream
bin/pi-restart
tests/control_plane/test_workstream_lifecycle.py
tests/control_plane/test_presentation_lifecycle.py
relevant existing secretary/personal/restart tests
```

**Create saga:** exact authorization → planned workstream/working-copy/
conversation rows → ordered project/worktree locks → Git worktree CAS create →
session file create → presentation assignment → run preparation/attestation →
ready event. Every boundary is observed and recoverable. “Ready” requires all
resources, not just rows.

**Focus is read-only. Relaunch** requires old process/runtime/pane absence proof
and keeps the same workstream/conversation/working copy. **Retire/cleanup** is a
separate dry-run/apply operation after clean, landed/closed, non-live, exact
ownership checks.

**Tests must show:** create/focus/relaunch/retire; duplicate approval/retry;
crash at each external step; wrong project/path/branch/session/backend;
concurrent create; worker starts during cleanup; unknown pane/process; active
secretary swap refusal/graceful stop; unrelated tmux sessions survive
`pi-restart`; live workers survive or block grid restart.

**Stop:** `tmux kill-server`, deleting a live secretary window, automatic tmux↔
Herdr worker migration, or legacy/controller dual workstream writes.

### C6 — Installed launcher, runtime, and first-party package wiring

**Goal:** make actual runs use controller manifests, writer fencing, and reviewed
first-party packages.

**Expected surfaces:**

```text
pi/settings.json
install.sh
bin/pi and managed launchers
scripts/pi-workspace.py
scripts/pi-runtime.py
scripts/pi_control/{run_manifest,runtime_adapter,leases,process_adapter}.py
pi/packages/pi-sandbox-control/**
pi/packages/pi-subagents-control/**
legacy patch installer only for explicit legacy mode
tests/system staged/runtime suites
```

**Approach:** staged settings load local first-party packages; launcher obtains
one launch plan; creates run/operation/capability/manifest; acquires writer
lock/epoch; prepares runtime; independently attests exact build/image/mount/Git/
UID/GID; starts Pi only after ready. Every mutation-capable tool uses the run
fence. Old writable runtime must be proven gone before handoff.

**Tests must show:** real Pi diagnostics resolve both package sources inside the
staged first-party trees; legacy package not co-loaded; exact extension set;
wrong build/image/mount/OID/UID/permission/capability/expiry rejected before
model tool; trusted/isolated/read-only modes; graceful stop/restart; PID reuse;
old container unknown; stale proxy/epoch; no Docker socket/credentials/home or
unexpected mount.

**Stop:** repository source used as installed proof, readiness from labels,
fallback package loading, broad chmod, or new writer with uncertain old access.

### C7 — Full personal, secretary, child, change, review, and integration wiring

**Goal:** make the user-visible workflows traverse real launchers/extensions and
the same controller APIs.

**Expected surfaces:** controller extension, secretary explicit extension/tool
list, root/personal/secretary launchers, child package adapter, change/review/
integration clients, and process-level fixtures.

**Required flows:**

1. personal primary → run → edit/commit or dirty delta → submit;
2. personal separate worktree/conversation → same queue;
3. secretary status → create workstream → worker → submit;
4. exact read-only child on dirty/clean parent → result/artifact;
5. exact reviewer → authenticated receipt;
6. analyze → exact authorization → fast-forward/already-contained CAS;
7. moved/non-fast-forward target → integration workstream → new revision;
8. close/defer/focus/attention/recovery;
9. explicit publication adapter separately from local integration;
10. exact cleanup plan/apply separately from integration.

**Tests must show:** no direct DB seeding after initial registration fixture;
real semantic extension calls; source/index/worktree invariants; exact parent/
child lineage; mechanical read-only; writer separation; current review and
authorization; target races; crash recovery; no automatic push/cleanup; local
bare remote only for publication tests.

**Stop:** secretary extension omitted from controller APIs, generic yes used as
authority, child completion moves target, or old secretary state changes in
controller mode.

### C8 — Continuity, Inspector, errors, and configured package behavior

**Goal:** prove actual extension behavior rather than source-text reachability.

**Expected surfaces:** continuity, observability, control-plane, workflow-state,
auto-continue, host-command, feedback, statusline, and configured package smoke
fixtures.

**Tests must show:** manual/threshold/overflow compaction; crash after continuity
append; dedup on resume/branch; packet replacement/tombstone; privacy/digest;
healthy footer silence; malformed/newer diagnostics; live child failure and
attention; `/observe`, `/continuity`, fleet; host-command approve/reject/expire;
feedback persistence; image paste/resume; goal continuation; BTW separation;
fast-mode toggle; configured package loading/provenance. Model-language
expectations run as labeled evaluations, not deterministic unit tests.

**Stop:** hidden reasoning/raw secrets, duplicate continuation, Inspector
mutation, healthy control-plane jargon, or package provenance mismatch.

### C9 — Full-system deterministic integration harness

Implement `SYSTEM_INTEGRATION_TEST_PLAN.md` exactly. The harness must launch the
actual staged wrappers and extensions with disposable HOME/XDG/Git/state and
scripted process/provider adapters. It must not call production user state.

**Acceptance:** every supported action has manifest coverage; fixture,
process, staged, Docker, and presentation tiers report structured evidence;
required unavailable tier returns STOP/77; all before/after assertions pass.

### C10 — Staged installation and Docker rollback proof

**Goal:** prove reviewed bytes are loaded and a failed activation can be undone
without touching live state.

**Approach:** exact staging root; real npm install from copied first-party
packages; real pinned Pi process; disposable controller DB/repositories/session
roots; immutable image; failure injection before/after every installer swap;
full restore comparison.

**Tests must show:** source→stage→installed manifest equality; package resolution;
controller schema/build; Docker create/attest/tool/stop; staged launcher full
walking skeleton twice; interrupted install at each swap; rollback restores
exact previous tree/config/symlinks/image selection and preserves new DB/refs/
worktrees/evidence.

**Gate:** Docker/npm/image unavailable is STOP/77. A source-only pass remains
valid source evidence but cannot become staging GO.

### C11 — Reviewed Phase 11D canary runbook (write only; do not execute)

**Goal:** produce `pi/control-plane/PHASE_11D_CANARY_RUNBOOK.md` and its static
validator `tests/system/test_canary_runbook.py`. This slice does not activate
anything. If the user has not selected a canary, the runbook remains structurally
complete with status `BLOCKED_AWAITING_CANARY_SELECTION`; the worker does not
invent one.

**Prerequisites:** C0–C10 accepted; no unresolved canary-scope mapping;
Docker/staged rollback passed; exact backup restore passed; user chooses canary.

**Runbook must name:** source/build IDs, controller DB/latch paths, canary project
and conversations, inventory/resolution/migration IDs, active processes and
quiescence commands, exact backup, install commands, latch transition, launch
and walking-skeleton actions, fault injection, stop criteria, rollback commands,
and post-state diff.

**Execution:** separate explicit user request in `pi-host`. General rollout is a
later reviewed operation.

## 9. Full-system evidence rules

Every scenario records:

```text
action/scenario ID and contract clauses
source, staged, and installed build IDs
fixture identity and prerequisites
before SQLite/Git/filesystem/process/presentation snapshot
exact command/tool request and authorization
fault/race injection if any
expected and actual state/resource versions/events
expected and actual Git refs/OIDs/index/worktree status
expected and actual files/modes/hashes
expected and actual process/container/pane state
user-visible result and bounded technical detail
retry/restart/rollback outcome
forbidden-content scan
cleanup/preservation result
```

Tests may assert no side effect only after independently observing the relevant
surface. Missing visibility is unknown, not unchanged.

## 10. Current checkpoint evidence

- C0a/C0b through C8: component and configured-action validation accepted.
- C9a–C9c2: 87-action manifest coverage, per-action source/process evidence,
  journeys, fault probes, isolation guards, and truthful STOP/77 aggregation;
  fresh review ACCEPT.
- C10a–C10c: disposable npm/source→stage→installed proof, loaded-resource
  attestation, twice-run staged journey path, executable rollback matrix, and
  Docker/installer gates; fresh review ACCEPT. Current environment reports
  missing pinned Pi/Docker as STOP/77, not staging GO.
- C11: `PHASE_11D_CANARY_RUNBOOK.md` is structurally complete with
  `BLOCKED_AWAITING_CANARY_SELECTION`; static validator passes; no command was
  executed.

Evidence gates remain separate: component, harness integration,
migration-ready, staging, canary, and rollout. Human decisions listed below
remain unresolved and no live action is authorized.

## 11. Human decisions that remain intentionally open

A weaker implementation model must stop and ask only for these decisions:

1. Which exact real project/conversation will be the Phase 11D canary.
2. How to map genuinely duplicate/divergent conversation histories when more
   than one survives exact validation.
3. Whether incomplete project-rebind proof refers to the same logical project.
4. Which files belong to a dirty personal change when task-delta attribution is
   ambiguous.
5. Whether to adopt a legacy candidate branch/ref as a controller change.
6. Which divergent MLRE continuation (`970ea8e`, `b296516`, or both as separate
   work) the user wants; migration always preserves both.
7. Whether and when to perform remote publication or destructive cleanup.
8. Final Phase 11D and later general-rollout authorization.

These are not implementation ambiguities:

- unavailable adapters return unavailable;
- old processes import as observations;
- non-fast-forward uses an integration worktree;
- review is evidence, not authority;
- no controller-mode legacy fallback;
- no broad tmux kill;
- no same-copy writer child;
- no automatic cleanup or push.

## 12. Weak-model slice brief

Every implementation assignment copied from this plan must contain:

```markdown
### Cn — name
#### Goal
One bounded result only.
#### Prerequisites
Accepted prior sub-slices and required capabilities.
#### Required reading
Exact contract sections and source paths.
#### Allowed files
Exact paths/globs.
#### Must remain unchanged
Exact boundaries plus the global non-live boundary.
#### Required behavior
Numbered state algorithm with durable intent and external observation.
#### Failure and retry behavior
Stable errors, known side effects, retained evidence, idempotency/reconciliation.
#### Tests to add first
Exact scenario/action IDs and before/after assertions.
#### Acceptance commands
Exact deterministic commands and STOP/77 prerequisite semantics.
#### Stop and escalate
Human decisions and unsafe/ambiguous/interface conditions.
```

Do not give a worker the whole parent transcript. Do not combine slices because
files overlap. Schema/interface owners integrate once, then all cross-boundary
acceptance reruns.

## 13. Completion checklist

The non-live implementation is complete only when all are true:

- [x] Normative contracts include schema v7, activation latch, typed migration,
      workstream, presentation, and action-coverage contracts.
- [ ] One controller owns project/workstream/run/change lifecycle in controller
      mode; legacy clients are facades and cannot dual-write.
- [ ] Every configured migration adapter emits a typed state and every record a
      disposition.
- [ ] Shadow import/reconciliation survives every declared failpoint and checks
      all fields/relationships.
- [ ] Actual launchers resolve exact controller bindings and never fall back in
      controller mode.
- [ ] Actual staged Pi loads first-party sandbox/subagent packages and the exact
      explicit secretary/controller extensions.
- [ ] Workstream create/focus/relaunch/retire is one controller saga.
- [ ] `pi-restart` preserves unrelated tmux sessions and never kills live work.
- [x] Personal, secretary, worker, child, review, integration, continuity,
      Inspector, host-command, feedback, image, goal, BTW, cleanup, and rollback
      actions have manifest-linked deterministic system scenarios.
- [x] Action manifest validation finds no uncovered or orphaned action.
- [x] Component and process source gates have exact evidence; staged, Docker,
      and presentation gates remain explicit STOP/77 where prerequisites are
      unavailable and are not silently skipped.
- [ ] Docker rollback proof passes in an authorized disposable environment.
- [ ] Independent final review accepts authority, security, recovery, scope,
      test adequacy, and documentation truthfulness.
- [x] Phase 11D runbook is structurally validated and not executed without a new
      explicit user authorization.

No item in this checklist authorizes live activation.
