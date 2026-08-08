# Control-plane acceptance and fault-injection plan

Status: **component and deterministic C9 source/process acceptance are implemented;
staged installed, Docker/presentation capability gates and live activation remain
separate and blocked as specified**.

## 1. Acceptance philosophy

A control-plane claim requires the strongest available evidence:

1. schema/type/constraint enforcement;
2. deterministic unit tests;
3. adapter integration tests in disposable resources;
4. process/crash/concurrency tests;
5. staged installed-artifact tests;
6. live canary observation after explicit activation;
7. human judgment only for product wording/semantic merge decisions.

Static patch-string checks and a worker's claim that tests passed are not
sufficient for restart, OID, permission, lease, artifact, migration, or
integration invariants.

Every test records:

```text
scenario ID
contract clauses/correction IDs
fixture and starting state
controller/source/installed build IDs
command/failpoint
expected state transitions
expected Git/filesystem/process/runtime observations
expected user error code/message shape
actual result
cleanup/retention result
```

## 2. Test isolation

Repository tests MUST use:

- temporary HOME;
- temporary XDG_STATE_HOME and XDG_RUNTIME_DIR;
- disposable Git repositories/worktrees;
- disposable SQLite DB;
- fake process/runtime/presentation adapters unless scenario explicitly requires
  Docker/tmux/Herdr;
- sanitized Git environment;
- unique Docker labels/network-disabled fixtures for live Docker tests;
- no remote URLs/operations;
- no host publication/deployment;
- explicit before/after current-worktree and host-state checks.

A fixture that cannot prove isolation fails before testing behavior.

## 3. Test suites and target commands

Expected eventual commands:

```bash
# Controller unit and adapter fixture tests
python3 -m unittest discover -s tests/control_plane -p 'test_*.py'

# Existing Python harness suite
python3 -m unittest discover -s tests -p 'test_*.py'

# TypeScript extension/unit suites
node --test tests/control-plane-*.test.mjs
NODE_PATH="$PWD/pi/npm/node_modules" node --test tests/control-plane-extension.test.mjs

# Existing candidate regression suite
./tests/run-candidate-tests.sh

# Disposable Docker/process integration
./tests/pi-control-plane-integration.sh

# Full staged candidate gate
./tests/run-control-plane-candidate-tests.sh

git diff --check
```

Each phase adds only commands whose implementation exists. Plans must not report
placeholder commands as passing. In the current candidate, the process/client
script exists and passes; the full gate emits STOP/77 when Docker/image
prerequisites are unavailable rather than treating that prerequisite as green.

## 4. Schema/storage matrix

| ID | Scenario | Expected proof |
|---|---|---|
| DB-001 | Fresh secure initialization | DB/WAL/SHM under owned `0700`; inaccessible to group/other; pragmas verified |
| DB-002 | Symlink DB or parent | Refuse with `CP_DB_UNSAFE`; no target opened |
| DB-003 | Foreign/check constraint | Invalid role/state/reference cannot commit |
| DB-004 | Unknown newer schema | Fail closed; no auto-downgrade |
| DB-005 | Interrupted migration | Entire migration rolled back or idempotently completed; checksum consistent |
| DB-006 | Concurrent writers | `BEGIN IMMEDIATE` serializes or returns typed busy; no partial rows |
| DB-007 | Resource CAS | One expected version succeeds; stale concurrent update fails |
| DB-008 | Idempotency same request | Exactly one operation/result/event; retry returns it |
| DB-009 | Idempotency changed request | Reject conflict; original intact |
| DB-010 | State + event atomicity | Cannot observe one committed without other |
| DB-011 | Duplicate event delivery | Consumer applies once by event ID; cursor monotonic |
| DB-012 | Forbidden content scan | No fixture secrets/raw prompts/source bodies/capability secrets in rows/files |
| DB-013 | DB corruption/unreadable | Stable failure; no recreation over corrupt DB |
| DB-014 | Local filesystem check | Unsupported/network lock environment rejected or explicitly unsupported |
| DB-015 | Run references staged/superseded build | Run creation rejects; no manifest/runtime side effect |
| DB-016 | Active-writer claim | Claim must reference matching nonterminal writer run/working copy/epoch; deletion blocked until cleared |
| DB-017 | SQLite version/capability | <3.40 or missing STRICT/JSON/trigger/index capability returns `CP_SQLITE_UNSUPPORTED` before application schema/state mutation |

## 5. Identity/trust matrix

| ID | Scenario | Expected proof |
|---|---|---|
| IDT-001 | Register primary repo | Stable random project ID; common dir/object format recorded |
| IDT-002 | Same repo via symlink | Same explicit project or refusal; no duplicate by path alias |
| IDT-003 | Repository moved | Explicit verified rebind; ID stable; version increments |
| IDT-004 | Unrelated clone same remote | Separate project unless explicit supported adoption; remote URL not identity |
| IDT-005 | Linked managed worktree | Inherits registered trusted project despite storage path |
| IDT-006 | Unrelated repo under managed root | Does not inherit another project's trust |
| IDT-007 | Explicit isolated narrowing | Trusted project working copy may be isolated |
| IDT-008 | Attempted trust broadening | Isolated project cannot become trusted from path/project config/model flag |
| IDT-009 | Session cwd tamper | Conversation remains controller-bound; technical drift reported |
| IDT-010 | Four conversations/same copy | Multiple readers possible; second writer blocked/explicit attach policy |
| IDT-011 | Ephemeral route IDs | New run ID does not change conversation/project/working copy |
| IDT-012 | Object format | Correct SHA-1 and supported SHA-256 validation |

## 6. Lock/fencing/concurrency matrix

| ID | Scenario | Expected proof |
|---|---|---|
| LCK-001 | First writer | Kernel lock held; epoch assigned; run active |
| LCK-002 | Concurrent second writer | Refused before writable runtime/tool access |
| LCK-003 | Graceful release | Runtime access gone, lock releases, state terminal |
| LCK-004 | PID dies | Kernel lock releases; reconcile classifies run lost; no automatic new writer until runtime access checked |
| LCK-005 | PID reuse | Start identity mismatch prevents false ownership |
| LCK-006 | Stale epoch call | Controller/Git/tool bridge rejects before side effect |
| LCK-007 | Old container still writable | New writer refused even if PID/lease appears dead |
| LCK-008 | Unknown process observation | Unknown, not stopped; needs attention |
| LCK-009 | Lock-order inversion fixture | Code detects/prevents; no deadlock |
| LCK-010 | Nested helper operation | Uses existing context; does not reacquire non-reentrant transition |
| LCK-011 | Crash after epoch before runtime | Reconcile releases/marks exact state; no duplicate writer |
| LCK-012 | Clock rollback/reboot | Correctness does not reclaim from wall-clock TTL alone |
| LCK-013 | Delegated writer disabled | Same-copy parent/child concurrent write cannot start in MVP |
| LCK-014 | Integration lock order | Project → target working-copy lifecycle/index → writer if needed → target-ref → review/change; inversion rejected/no deadlock |

## 7. Operation/reconciliation fault matrix

For every external operation step, inject process death:

```text
before intent commit
after intent commit
before lock
before side effect
after side effect before observation
after observation before DB result/event
after result before external notification
```

Generic expected outcomes:

- no intent -> no controller-owned side effect;
- durable intent + no side effect -> safe idempotent apply;
- side effect maybe applied -> observe before retry;
- exact desired observation -> record success once;
- exact old observation -> retry under current versions/epoch;
- neither old nor desired -> needs attention, preserve;
- notification may duplicate but consumer deduplicates.

Specific scenarios:

| ID | Operation | Fault/assertion |
|---|---|---|
| REC-001 | create worktree | crash each boundary; at most one owned worktree/branch; no pre-existing deletion |
| REC-002 | remove worktree | quarantine/registration/ref states classified; no force/unowned removal |
| REC-003 | create run manifest | no active run with absent/invalid manifest |
| REC-004 | create container | orphan observed/adopted or safely removed; no second writer |
| REC-005 | stop container | unknown stop outcome blocks writer handoff |
| REC-006 | snapshot ref | exact ref/DB pairing or orphan owned ref reconciled |
| REC-007 | submit revision | exactly one immutable revision/event |
| REC-008 | target integration | old/desired/ambiguous target classification and rollback ref |
| REC-009 | event consumer | duplicate delivery no duplicate attention/action |
| REC-010 | stale observation | resource version/freshness prevents old observation overwriting new |
| REC-011 | reconcile reentry | one operation context; no recursive transition marker |
| REC-012 | adapter unavailable | unknown/error; no mutation inferred |

## 8. Run manifest and sandbox matrix

| ID | Scenario | Expected proof |
|---|---|---|
| RUN-001 | Canonical manifest | Stable digest, strict fields, `0600`, capability separate |
| RUN-002 | Wrong capability/owner | Fail before runtime/tool |
| RUN-003 | Manifest path renamed | Run identity unchanged; path not authority |
| RUN-004 | Wrong project/worktree/common dir | Attestation rejected |
| RUN-005 | Wrong branch/HEAD/tree | Attestation rejected before first model tool |
| RUN-006 | MLRE stale base | Manifest expects newer state; `868db28` child/runtime rejected |
| RUN-007 | Wrong image digest/ID | Recreate/fail; label/name insufficient |
| RUN-008 | Invalid `FROM sha256` | Build fixture fails regression; valid repo@digest works |
| RUN-009 | Wrong UID/GID | Runtime not ready |
| RUN-010 | Wrong private-dir ownership | Recreate controller-owned private dir or fail; fresh validation |
| RUN-011 | Wrong mount mode/propagation | Attestation rejected |
| RUN-012 | Unexpected mount/secret/socket | Attestation rejected; no exposure |
| RUN-013 | Policy/build hash mismatch | Run blocked with typed error |
| RUN-014 | Reused old container | Cross-run reuse refused in MVP |
| RUN-015 | Restart | New run/container, same conversation/working copy/current Git state |
| RUN-016 | Stale in-memory tool proxy | Epoch/run check blocks mutation |
| RUN-017 | Container disappear | Desired running reconciles exact recreation or attention; no source switch |
| RUN-018 | Graceful cancel | No new tool calls; tracked process/runtime state terminal |

Live Docker tests inspect from both host and container and compare exact
manifest/attestation, not only command exit.

## 9. Child/snapshot/artifact matrix

| ID | Scenario | Expected proof |
|---|---|---|
| CHD-001 | Clean read-only child | Exact commit/tree, mechanically read-only |
| CHD-002 | Dirty tracked content | Snapshot contains selected working content |
| CHD-003 | Staged+unstaged same file | Explicit captured result and provenance |
| CHD-004 | Deletion/rename | Correct tree/diff manifest |
| CHD-005 | Untracked file | Included only by policy/task delta |
| CHD-006 | Ignored file | Excluded by default; explicit warning/include |
| CHD-007 | File changes during capture | Abort/retry; no false exact snapshot |
| CHD-008 | Conflict/index stages | Refuse/needs decision |
| CHD-009 | Symlink/special/submodule | Enforce explicit policy, no escape |
| CHD-010 | Parent advances | Child remains at immutable source; result names it |
| CHD-011 | Independent writer | Separate worktree/run/container/writer epoch |
| CHD-012 | Same-copy writer request | Disabled/refused until sublease implementation |
| CHD-013 | Child wrong parent/run | Attestation rejected |
| CHD-014 | Child returns dirty state | Not accepted as clean/committed candidate |
| CHD-015 | Child crash | Source/artifacts retained; parent gets attention; no guessed import |
| CHD-016 | Artifact manifest | Outside repo, `0700/0600`, checksum/provenance/sensitivity |
| CHD-017 | Artifact corruption | Hash failure; no use |
| CHD-018 | Root selector | Child session never appears as root |
| CHD-019 | Read-only delta audit | Parent/project worktree unchanged after child |

## 10. Change-submission matrix

| ID | Scenario | Expected proof |
|---|---|---|
| CHG-001 | Clean feature branch | Revision ref exact tip/tree; source unchanged |
| CHG-002 | Dirty personal task delta | Temp-index commit includes intended current files only |
| CHG-003 | Pre-existing dirty files | Excluded/preserved with explicit summary |
| CHG-004 | Ambiguous user/agent edits | Change stays draft; `change-selection-required` attention and `change.needs_selection` event; no revision/ref |
| CHG-005 | Partial staged/index state | Real index byte/hash unchanged |
| CHG-006 | New revision | Old ref/review retained; current projection advances |
| CHG-007 | Retry same request | One revision/event |
| CHG-008 | Crash boundaries | Ref/DB reconciled without duplicate/loss |
| CHG-009 | Malicious path/ref | Strict rejection; no external path/ref touched |
| CHG-010 | Target moved before submit | Recorded current target; no target mutation |
| CHG-011 | Source exits after submit | Secretary still discovers immutable change |
| CHG-012 | Every producer | Personal primary/worktree, secretary workstream, integration result use same queue |
| CHG-013 | Ignored secret fixture | Not captured/logged without explicit include |
| CHG-014 | Source branch deleted/advanced | Submitted revision remains intact |
| CHG-015 | Close/supersede | Explicit authorization/provenance; refs retained by policy |

For source-unchanged assertions record before/after:

```text
HEAD and branch refs
index file hash and staged diff
working-tree file hashes/status
untracked manifest
Git reflog where relevant
```

## 11. Secretary/personal workflow matrix

| ID | Scenario | Expected proof |
|---|---|---|
| WF-001 | Current project stats | Recent direction, managed/unmanaged working copies, changes, attention; unknown visible |
| WF-002 | Cheap status | No LLM/deep scan required; bounded latency |
| WF-003 | Dependency investigation | Focused fresh agents receive exact relevant revisions/evidence |
| WF-004 | Create workstream | Explicit approval; exact worktree/session/run; only report ready after attestation |
| WF-005 | Partial creation failure | Incomplete setup reported; journal/reconcile; no false agent start |
| WF-006 | Focus existing | Same durable conversation; no duplicate session/worktree |
| WF-007 | Personal direct branch | Explicit primary assignment, no clean/dirty heuristic |
| WF-008 | Personal separate worktree | Separate durable conversation; personal home stays bound |
| WF-009 | Submit from personal | Same queue; no secretary-creation ceremony |
| WF-010 | Submit from workstream | Same queue; no auto-review/merge |
| WF-011 | Unmanaged worktree | Listed read-only; no mutation/cleanup before adoption |
| WF-012 | Attention | Routine progress quiet; decision/failure durable and focusable |
| WF-013 | Generic yes | Does not authorize create/integrate/cleanup across action boundaries |
| WF-014 | Backend change | Tmux/Herdr labels equivalent; controller state unchanged |
| WF-015 | Restart | Exact conversation/working copy and current project state |

## 12. Review and integration matrix

| ID | Scenario | Expected proof |
|---|---|---|
| INT-001 | Deterministic analysis | Exact candidate/target/merge-base/paths; real worktree untouched |
| INT-002 | No text conflict, semantic overlap | Focused review evidence surfaced as inference |
| INT-003 | Current exact review | Receipt binds exact revision/tree/base/target policy |
| INT-004 | New revision | Old review historical, not current evidence |
| INT-005 | Target movement | Analysis/authorization stale; no mutation |
| INT-006 | Generic/replayed approval | Rejected |
| INT-007 | Already contained | Verified inclusion/provenance; no unnecessary mutation |
| INT-008 | Fast-forward | Rollback ref, target CAS, checked-out state consistent |
| INT-009 | Non-fast-forward | Integration worktree path; original candidate unchanged |
| INT-010 | Dirty/in-use target | Refuse; no reset/stash/force |
| INT-011 | External ref update race | CAS loses safely; change preserved |
| INT-012 | Crash before/after target/ref/index/worktree steps | Each observed independently; old/desired/mismatch classified; target never exposed ready while mismatched |
| INT-013 | Multiple changes | Recompute after each target update; stop on changed result |
| INT-014 | Failed integration workstream | Target and original revision unchanged |
| INT-015 | Merge record/event | Only after independent target proof |
| INT-016 | Remote | No push/network action |
| INT-017 | Cleanup | Separate dry-run/apply, exact owned clean/non-live resources only |

## 13. Continuity/UI/privacy matrix

| ID | Scenario | Expected proof |
|---|---|---|
| UX-001 | Manual compaction | Visible continuity card derives from actual result |
| UX-002 | Threshold compaction | Card persists and active task continues once |
| UX-003 | Overflow/extension retry and crash after card append | `(sessionId, compactionEntryId)` deduplicates card/continuation after persisted-entry reconciliation |
| UX-004 | Resume | Latest/historical card inspectable |
| UX-005 | Summary consistency | User/model retained decisions share digest/reference |
| UX-006 | Privacy | No hidden reasoning/system prompt/secrets/raw tool content |
| UX-007 | Healthy footer | Model/reasoning/activity/context; no container/route jargon |
| UX-008 | Error translation | Six required message elements; no false unchanged/preserved claim |
| UX-009 | Technical drill-down | Exact IDs/OIDs/paths/operation evidence available read-only |
| UX-010 | Newer/malformed diagnostic | UI degrades to unsupported/unavailable, Pi remains usable |
| UX-011 | Duplicate events | No duplicate attention/card/action |
| UX-012 | Cross-project isolation | Secretary/project views cannot leak other project details |

## 14. Migration/activation/rollback matrix

| ID | Scenario | Expected proof |
|---|---|---|
| MIG-001 | Legacy inventory | Hashes/provenance; no mutation |
| MIG-002 | Contradictory root/route/ref | Explicit contradiction; no timestamp choice |
| MIG-003 | Shadow import | Legacy writers unaffected; DB model compare only |
| MIG-004 | Re-import same digest | Idempotent |
| MIG-005 | Changed source during import | Abort/new attempt |
| MIG-006 | Dual writer attempt | One boundary rejects; never both |
| MIG-007 | Installed/source skew | Build mismatch visible; run blocked where required |
| MIG-008 | Staged artifact | Final hashes/image/build equal manifest |
| MIG-009 | Interrupted file swap/import | Activation journal recovers/rolls back exactly |
| MIG-010 | Canary | Full walking skeleton/restart/fault passes |
| MIG-011 | Rollback drill | Legacy launch restored; new refs/DB preserved; no cleanup |
| MIG-012 | Newer DB with old code | Fail closed; backup remains |
| MIG-013 | Active unknown process | Cutover blocked |
| MIG-014 | Backup restore | Disposable hash/mode/symlink proof before live |
| MIG-015 | MLRE divergent heads | Both preserved; migration requests human decision |
| MIG-016 | Migration record persistence | Request identity/manifest rows immutable; lifecycle CAS/event resumes same operation by idempotency key |
| MIG-017 | Manifest tamper/crash | Hash/size mismatch or crash between file/row/step yields attention/reconciliation, never inferred success |

## 15. Performance matrix

Measure before setting final budgets.

Fixtures:

- 1/10/100 projects;
- 1/10/100 working copies;
- 1/10/100 open changes;
- clean versus 1/100/10,000 changed paths;
- cold/warm SQLite and Git object cache;
- no-op versus drifted reconciliation.

Metrics:

```text
DB connection/init
project summary metadata query
bounded Git inventory
writer lock/epoch acquisition
manifest/spec generation
per-tool fence check
no-op reconcile per adapter
container preparation/attestation (separate from image pull)
clean branch submission
dirty snapshot by files/bytes
integration analysis
secretary project projection
```

Initial non-regression policy:

- record p50/p95 and environment;
- controller metadata should not dominate ordinary tool latency;
- no per-token/streaming SQLite writes;
- bounded per-tool fence check may be cached only within current run/epoch;
- no unsafe container/state reuse to meet a target;
- any proposed optimization includes mechanism and fault tests.

A final budget is accepted after baseline, not invented in this document.

## 16. Security/red-team matrix

- SQL injection/path/ref/command injection;
- malicious Git config/hooks/pager/diff/textconv;
- symlink/TOCTOU substitution;
- malformed JSON/schema/oversized fields;
- capability token replay/leak;
- stale authorization replay;
- wrong user/file owner/mode;
- project trust broadening;
- cross-project working-copy adoption;
- container unexpected mounts/network/socket;
- child self-asserted authority/attestation;
- outbox poison/duplicate event;
- debug/continuity content leak;
- untrusted ignored-file inclusion;
- remote push/deploy attempt;
- cleanup wildcard/prefix/unowned path;
- rollback artifact tamper.

Independent reviewer must classify findings before canary activation.

## 17. Walking-skeleton runbook

In one disposable trusted repository:

1. register project and primary checkout;
2. create personal conversation;
3. start writable run and attest container;
4. create initial dirty baseline;
5. make tracked and untracked implementation changes;
6. launch read-only child and prove exact snapshot/read-only;
7. parent advances after child launch;
8. submit selected change revision without altering source index/worktree;
9. stop/restart personal conversation; prove same working copy/current state;
10. launch project secretary; list working copies/change;
11. analyze candidate against target;
12. approve and fast-forward integrate under CAS;
13. prove target/candidate/change record/event;
14. restart all managed processes and re-query state;
15. rollback disposable integration using recorded recovery evidence only for test;
16. repeat with secretary-created worktree/headful conversation;
17. repeat target-moved path creating integration worktree;
18. force compaction and inspect continuity card;
19. scan repo/state for forbidden artifacts/content;
20. run cleanup dry-run only; do not require destructive cleanup for acceptance.

Run the skeleton twice from fresh fixtures and once against staged installed
artifacts before canary.

## 18. GO/STOP gates

### Source GO

- phase unit/integration tests pass;
- final diff scoped and reviewed;
- no known contract violation;
- correction ledger updated with source-only status;
- no remote/host activation occurred.

### Staging GO

- all source gates;
- artifact manifest/hash/build/image exact;
- staged process/fault suite passes twice;
- migration shadow compare has no unresolved blocking contradiction;
- disposable rollback succeeds.

### Canary GO

- explicit user intent and `pi-host` boundary;
- quiescence and backup verified;
- exact installed build;
- one project only;
- walking skeleton/live fault tests pass;
- no stale writer/OID/trust/permission/artifact/target anomaly;
- rollback remains available.

### STOP immediately

- wrong working copy, conversation, target, or OID;
- duplicate writer or stale epoch accepted;
- tools before full attestation;
- child sees wrong source;
- controller guesses among divergent refs/state;
- target mutates without exact approval/CAS;
- candidate/source/legacy recovery state lost;
- migration dual writes;
- installed build not provable;
- permission/trust boundary broadens;
- prohibited raw content/secret stored;
- rollback cannot be proven.

## 19. Evidence report template

```markdown
# Acceptance evidence — <phase/build>

## Identity
- source commit/tree/dirty digest
- controller build/schema
- installed artifact/image IDs

## Commands
- exact command, exit, duration, output artifact

## Deterministic tests
- count/pass/fail/skipped with reasons

## Process/fault tests
- scenario IDs and final state/refs/operations/events

## Changed system surfaces
- explicit list

## Migration/activation
- not run | staged | canary, with evidence

## Privacy/security
- scans and independent findings

## Remaining uncertainty
- bounded list

## Remote/production actions
- confirm none or exact authorized action
```

## 20. Component matrices versus full-system acceptance

Sections 4–18 define required invariant and fault coverage. They do not by
themselves prove the active configured harness traverses those components.
`SYSTEM_INTEGRATION_TEST_PLAN.md` is therefore normative for full-system
acceptance.

In particular:

- direct Python calls and direct SQLite resource seeding are component evidence;
- extension source regex/transformation proves syntax/reachability, not runtime
  invocation;
- CLI `--help` proves command registration, not semantic execution;
- fake runtime tests prove classification, not Docker/kernel enforcement;
- an installer transaction test proves swap/rollback mechanics, not that Pi
  loaded the expected package/extension bytes;
- a green test count does not prove every supported user action or execution
  tier is covered.

Full-system scenarios launch the actual staged wrappers and pinned Pi process
inside disposable HOME/XDG/Git/state roots, invoke semantic extension tools via
a scripted no-network provider/driver, and assert SQLite, Git, filesystem,
process/runtime, presentation, session/UI, and privacy evidence.

## 21. Required execution tiers

The final non-live report lists these independently:

```text
T0 contract/action-manifest traceability
T1 deterministic component suites
T2 real process/launcher/Pi fixture with fake external backends
T3 exact staged installed generation and loaded package/extension proof
T4 real Docker runtime/image/rollback
T5 real configured tmux/Herdr presentation behavior
T6 labeled model-language evaluations (never substitutes for T0–T5)
T7 explicitly authorized live canary
```

Source GO requires T0–T2. Staging GO requires T0–T5 for configured required
backends. T7 is Phase 11D and requires a separate user-approved runbook.

Every runner returns:

```text
0 PASS
1 FAIL
2 usage/configuration failure
77 STOP because required evidence/prerequisite is unavailable
```

A parent runner preserves STOP/77. `not tested`, missing Docker, missing staged
Pi/npm, or missing configured presentation backend cannot become a successful
staging report.

## 22. Complete action traceability

`tests/system/action-manifest.v1.json` must enumerate every supported semantic
action in `SYSTEM_INTEGRATION_TEST_PLAN.md`, including:

- launch/new/resume/fork/restart and trusted/isolated/host modes;
- personal, secretary, project selection, backend/layout, and workstream flows;
- parent/worker/reviewer/child delegation and control;
- host-command, feedback, goal, BTW, image, continuity, Inspector, and configured
  package boundaries;
- every `pi-control` CLI/API action;
- snapshot/artifact/change/review/integration/publication/cleanup;
- inventory/shadow/final migration, build/install/GC/rollback/activation;
- explicitly rejected authority bypasses and non-goals.

The validator compares the manifest with argparse subcommands, loaded extension
tools/commands, launcher help modes, activated settings packages, and host-only
semantic operations. New or removed actions without corresponding scenarios
fail T0.

Every supported action has deterministic success and refusal tests. Mutations
also have authorization, idempotency/restart, before/after side-effect, and
project-isolation evidence. High-risk mutations additionally run applicable
crash, race, stale-state, symlink/parent-swap, and rollback scenarios. Pairwise
coverage spans workflow role, trust mode, working-copy kind, clean/dirty state,
backend/layout, and new/resume/restart without pretending to test every natural-
language or Cartesian combination.

## 23. Required end-to-end journeys

Before staging GO, execute all journeys defined in
`SYSTEM_INTEGRATION_TEST_PLAN.md`:

1. personal primary + dirty baseline + exact child + submit + review + integrate
   + restart;
2. secretary workstream + target movement + integration workstream + cleanup;
3. crash/cancellation/reconciliation across lifecycle and Git boundaries;
4. desktop/mobile × tmux/Herdr presentation safety, including unrelated-session
   preservation;
5. full typed inventory + resolution + shadow/final disposable import + staged
   cutover + rollback;
6. continuity/Inspector/feedback/host-command/image/goal/BTW during active work;
7. isolated-repository execution and retained recovery.

System journeys may use fixture bootstrap APIs to create the initial installed
controller and registered project, but after bootstrap they MUST NOT insert
resource rows directly to simulate workstream/run/change/review/integration
success. They traverse the same semantic clients, launchers, extensions,
operation journal, and adapters used by the staged harness.

## 24. Additional full-system STOP conditions

Stop the applicable gate when:

- active launchers or secretary still write a lifecycle store separate from
  SQLite in controller mode;
- controller mode can fall back to legacy after an error;
- actual loaded sandbox/subagent package paths are not the reviewed first-party
  trees or a legacy package is co-loaded;
- a workstream is called ready without worktree, session, presentation, run, and
  runtime proof;
- `pi-restart` affects an unrelated tmux session or a project switch kills a
  live secretary/worker;
- a configured action has no manifest scenario;
- process/system evidence relies only on direct DB seeding, source text, or
  `--help` reachability;
- an inventory adapter/resource is silently omitted;
- a required backend reports unavailable and the runner continues green.
