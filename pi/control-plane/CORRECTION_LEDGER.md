# Control-plane correction ledger

Status: **planning and traceability; not an implementation-completion claim**.

This ledger keeps the program anchored in the observed failures and requested
workflow. `Historical repair evidence` means source/tests and, where recorded,
activation evidence were produced during the prior repair effort. It does not
mean the target controller implements the replacement invariant or that every
future installed build contains the repair. Target delivery uses separate
statuses: `implemented-in-source`, `staged`, `activated`, and `accepted` with
exact evidence. `Open` means the target contract is not yet implemented.

## Post-implementation audit remediation status (non-live candidate)

This section is the current candidate disposition for `POST_IMPLEMENTATION_AUDIT_AND_REMEDIATION.md`.
It intentionally distinguishes source/test evidence from staged or activated
proof. Phase 11D is blocked; no status below claims live activation.

| Audit finding | Candidate status | Evidence / remaining boundary |
|---|---|---|
| AUD-001 | implemented-in-source; staged evidence recorded | First-party npm trees and installed Python controller are staged by `install.sh`; clean package provenance and installer tests pass. Live activation remains blocked. |
| AUD-002 | implemented-in-source; staged evidence recorded | Managed launcher and immutable installed controller tree are checked by installer tests. |
| AUD-003 | implemented-in-source; staged evidence recorded | Canonical manifest envelope, digest/build ID, exact tree verification, and rollback tests exist. Docker rollback is an explicit prerequisite, not silently skipped. |
| AUD-004 | partially implemented; C2 open | Inventory has bounded generic/file and Git identities, but explicit root/secretary/route/artifact/process/Docker/tmux/Herdr/installed-build/policy/backup adapters and record-level dispositions remain required by `COMPLETION_IMPLEMENTATION_PLAN.md`. |
| AUD-005 | partially implemented; C3 open | Shadow roots and manifest retry are hardened, but full resource mapping, immutable resolution decisions, dependency-ordered import, complete failpoint recovery, and field/relationship reconciliation remain open. |
| AUD-006 | implemented-in-source; independently accepted | Reviewer conversation/run/actor/capability and immutable revision provenance are enforced; schema v6 freezes submitted receipts and authorization scope. |
| AUD-007 | implemented-in-source; independently accepted | Writer operation replay and terminal replay fencing include expected working-copy version and epoch; operation bindings include authority metadata and terminal outcomes cannot regress. |
| AUD-008 | implemented-in-source; independently accepted | Public lifecycle mutations emit transactional control events; both cursor APIs reject unemitted sequences and advance with CAS. |
| AUD-009 | implemented-in-source; independently accepted | Deterministic result IDs recover ref/row/worktree boundaries; authorization cancellation after Git CAS produces durable resolution rather than false success. |
| AUD-010 | implemented-in-source; C8/C9 behavioral acceptance open | Versioned continuity cards derive from actual compaction entries and task packets, but real Pi compaction/crash/resume/branch/privacy behavior still needs system tests rather than source reachability alone. |
| AUD-011 | partially implemented; C4 authority boundary open | JSONL parsing and registry rebinding checks are hardened; launcher/root registration still requires exact SQLite project/conversation/working-copy resolution plus fail-closed activation mode. |
| AUD-012 | component API accepted; C4–C9 harness integration open | Semantic client/CLI/extension operations exist, but active root/secretary launchers still use legacy lifecycle state and current process checks do not exercise the complete installed launcher/extension/runtime path. |
| AUD-013 | implemented-in-source; test depth remains open | Error projections now include action, risk, changed/preserved uncertainty, next actions, and technical details without false reassurance. |
| AUD-014 | partially implemented; independently reviewed | Manifest, lease, observation-lock, migration-directory, and integration-worktree paths use retained no-follow directory FDs; parent-swap and symlink regressions pass. Analogous legacy adapters remain outside this slice. |
| AUD-015 | component candidate gate repaired; C9/C10 and Docker open | Gate passes 209 controller tests plus current process/client/installer/extension checks, then emits STOP/77 on unavailable Docker. A traceable full-action, tiered system gate remains required before staging GO. |

### Follow-up adversarial review evidence

Four independent read-only perspectives plus parent reproducers found and
repaired operation regression/idempotency gaps, unemitted cursor advancement,
cross-project detail disclosure, root binding drift, filesystem parent swaps,
mutable receipts/authorizations, manifest expiry gaps, and a post-CAS
authorization race. A fresh final reviewer **ACCEPTED** the surviving diff.
`python3 -m unittest discover -s tests/control_plane -p 'test_*.py'` passes
**209 tests**. Phase 11D remains blocked.

### Completion-plan integration findings

A subsequent plan/action-surface audit found gaps that component tests do not
close. They are now explicit slices in `COMPLETION_IMPLEMENTATION_PLAN.md` and
action scenarios in `SYSTEM_INTEGRATION_TEST_PLAN.md`:

| ID | Severity | Finding | Required correction |
|---|---:|---|---|
| CP-PLAN-001 | S0 | Active launchers/secretary use legacy root/workspace/secretary stores while `scripts/pi_control` is a separate candidate authority | C4–C7 activation latch, exact binding, compatibility facades, and controller-only writes in controller mode |
| CP-PLAN-002 | S1 | `pi-restart` uses broad `tmux kill-server`, which can terminate unrelated sessions | C5 exact managed-session stop plus unrelated-session system regression |
| CP-PLAN-003 | S1 | Secretary active-set/window rebuild may remove a live secretary without a graceful stop proof | C5 refuse or gracefully stop/verify exact process before presentation removal |
| CP-PLAN-004 | S1 | Current walking-skeleton/process/extension checks include direct DB seeding, help, transforms, or source checks rather than the complete installed path | C7–C10 scripted real-Pi process journeys and tiered action-manifest gate |
| CP-PLAN-005 | S1 | First-party sandbox/subagent trees are staged, but complete acceptance must prove Pi loaded those exact trees and did not co-load legacy packages | C6 staged settings/package provenance and real loaded-resource proof |
| CP-PLAN-006 | S1 | Workstream has no first-class controller resource and current create API assumes an already-created working copy/session | Schema v7 workstream/presentation resources and one journaled full creation saga |
| CP-PLAN-007 | S1 | Per-project `legacy|shadow|controller` selection was described but not mechanically specified | Schema v7 activation rows plus secure fail-closed host latch; no environment override/fallback |
| CP-PLAN-008 | S0 | Current legacy root migration selects duplicate histories by file modified time, conflicting with typed no-timestamp precedence | C2–C4 preserve every duplicate and require an exact resolution mapping before controller import/binding |

These findings do not invalidate accepted component invariants. They prevent
claiming full harness integration, staging acceptance, or live readiness.

## 1. Severity

- **S0 — data-loss/authority risk:** may overwrite, discard, or integrate the
  wrong source state.
- **S1 — execution unavailable or unsafe:** tools/containers/sessions cannot
  start reliably or authority may be broader than intended.
- **S2 — recovery/observability/workflow defect:** state is retained but hard to
  understand, resume, or integrate safely.
- **S3 — maintainability/performance:** increases future regression or upgrade
  cost without immediate incorrect execution.

## 2. Original transition/runtime repairs

| ID | Severity | Observation | Current disposition | Durable target invariant | Phase/tests |
|---|---:|---|---|---|---|
| TR-001 | S1 | Transition release parsed raw JSON text without `JSON.parse`, leaving stale ownership behavior | Historical repair evidence; target replacement open | No marker-file live authority; operation journal plus kernel lock | Preserve source regression; Phase 3 crash tests |
| TR-002 | S1 | Checkpoint/rebase nested non-reentrant transition acquisition | Historical repair evidence; target replacement open | One operation context; ordered locks; helpers never reacquire | Phase 1 operation-context tests; Phase 3 fault injection |
| TR-003 | S0 | Rebase omitted `oldSandboxTip` needed for safe comparison/recovery | Historical repair evidence; target replacement open | Every ref mutation stores expected old/new OIDs and uses CAS | Change/integration and run-ref tests |
| TR-004 | S1 | Shutdown could acquire transition without active container | Historical repair evidence; target replacement open | Desired/observed operation only when resource/intent warrants | Reconcile no-resource tests |
| RT-001 | S1 | `/opt/pi/task-env` owned by 1001 while tasks run as 1000 | Historical repair evidence; target attestation open | Runtime spec/attestation verifies UID/GID and writable private paths | Runtime permission matrix |
| RT-002 | S1 | Child-lease repair validated stale pre-`chmod` metadata | Historical repair evidence; target lease model open | Mutation followed by fresh independent observation | Permission repair regression |
| IMG-001 | S1 | Docker builds used invalid bare `FROM sha256:<id>` | Historical repair evidence; target build attestation open | Runtime image reference includes valid repository/digest and build attestation | Docker build fixture and live image digest test |

These fixes are not substitutes for the state model. They remain required
regressions while marker/ref/runtime logic is replaced incrementally.

## 3. Identity and authority defects

| ID | Severity | Observation | Status | Required correction | Contract |
|---|---:|---|---|---|---|
| ID-001 | S0 | MLRE child started from durable root `868db28` while newer work survived at descendant `b296516` under another sandbox ref | Open | Immutable controller run manifest from exact working-copy state; child attestation before tools | State I-03/I-06; Execution §§4-8 |
| ID-002 | S1 | Managed linked worktree under `~/.local/share/pi/worktrees` classified isolated despite trusted repository | Open | Trust bound to registered project/Git identity; worktree can narrow but not broaden | State §11 |
| ID-003 | S0 | `scripts/pi-workspace.py` creates fresh task ID and sets both task and session to it | Open | Distinct project, working copy, conversation, run, task/change IDs | State §§5-6; Execution §4 |
| ID-004 | S0 | Route filename/session hash determines sandbox ref/container identity | Open | Route is immutable run projection; path/name never identity | State I-01; Execution §4 |
| ID-005 | S0 | Root registry, route, sandbox, subagents, Docker, secretary, tmux/Herdr retain overlapping pointers | Open | One SQLite authority; all others adapters/projections | State §§2-3, 12-13 |
| ID-006 | S0 | Four active root-conversation records pointed to the same personal MLRE worktree | Open | Explicit binding plus one-writer uniqueness and conflict report | State I-03/I-04 |
| ID-007 | S1 | Session header cwd can feed root-session registration and overwrite current binding | Open | Controller binding wins; cwd historical/projection only | Product §5; State conversations; Execution §11 |
| ID-008 | S2 | Paths and branch names used as identity in UI/recovery logic | Open | Controller IDs internal; human labels mutable; locators rebind explicitly | State §5; Product §§2-3 |

## 4. Git/workspace continuity defects

| ID | Severity | Observation | Status | Required correction | Contract |
|---|---:|---|---|---|---|
| GIT-001 | S0 | 62 sandbox refs, 58 at old root OID; work scattered across ephemeral refs | Open | Controller-owned immutable change/run refs with recorded provenance/retention | Change §§4, 20 |
| GIT-002 | S0 | Current tests do not prove child base OID end to end | Open | Manifest/attestation process test including stale `868db28` rejection | Execution §§4-8; Acceptance E2E |
| GIT-003 | S0 | Dirty tracked/untracked state behavior not proven | Open | Exact baseline, bounded temp-index snapshot, explicit ignored/ambiguous policy | Execution §8.4; Change §§6-8 |
| GIT-004 | S0 | Artifact export/resume materialization not proven | Open | Artifact provenance plus immutable submitted revision and crash recovery | Execution §9; Acceptance |
| GIT-005 | S1 | `worktree:true` independent execution correctness not proven | Open | Separate controller working copy, run, writer epoch, common-dir verification | State/Execution child contracts |
| GIT-006 | S0 | Parent advancement while child active can disagree with child assumptions | Open | Immutable child source; explicit parent mutation policy; later integration | Execution §8.5 |
| GIT-007 | S0 | Ambiguous authoritative MLRE head (`970ea8e` versus `b296516`) remains unresolved | Separate recovery decision | Preserve both; no automatic target choice; explicit human reconciliation | State I-10; not solved by migration |
| GIT-008 | S2 | Personal workspace placement chosen by clean/dirty heuristic | Open | Explicit in-place versus separate-worktree operation | Product §5; MVP personal phase |

## 5. Lease, process, and reconciliation defects

| ID | Severity | Observation | Status | Required correction | Contract |
|---|---:|---|---|---|---|
| LIF-001 | S1 | Stale marker blocked repository tools while network/control-plane tools remained usable | Open architectural replacement | Kernel lock, operation journal, reconciliation, no JSON live lock | State §§9-12 |
| LIF-002 | S0 | PID existence alone insufficient because of reuse/stale in-memory code | Historical mitigation evidence; target fencing open | Process start identity plus writer epoch; unknown fails closed | State §10; Acceptance PID reuse |
| LIF-003 | S1 | Parent/child leases and transition locks duplicate lifecycle semantics | Open | One controller writer/lifecycle lock model; adapters do not invent leases | State §10 |
| LIF-004 | S0 | Direct writable mount can outlive stale logical owner | Open | Prove old proxy/container stopped before new writer; fencing per tool/bridge | Execution §7.2 |
| LIF-005 | S1 | Crash can occur between SQLite/Git/process/container side effects | Open | Durable intent, idempotency, post-side-effect observation, saga reconciliation | State §9 |
| LIF-006 | S2 | No single reconciliation view explains all drift | Open | Pure `inspect` plus `reconcile --observe-only` classifications/evidence; repairs separate | State §12 |
| LIF-007 | S1 | Adapter may be unavailable, currently conflated with missing/stale | Open | Unknown distinct from missing/stopped; no destructive inference | State §12; Observability |

## 6. Sandbox and child authority defects

| ID | Severity | Observation | Status | Required correction | Contract |
|---|---:|---|---|---|---|
| SBX-001 | S0 | Sandbox selects/refines workspace state and owns Git transitions | Open | Sandbox receives manifest, materializes/attests, never selects identity/ref | Execution §§4-6 |
| SBX-002 | S1 | Reuse can retain stale image/mount/workspace assumptions | Open | No cross-run reuse in MVP; exact adoption contract later | Execution §5.2 |
| SBX-003 | S2 | Healthy footer leaks container names and `sandbox: pending` | Open | Healthy control plane silent; one semantic attention projection | Product §3; Observability §9 |
| CH-001 | S0 | Same-task children inherit route but not proven exact current dirty state | Open | Immutable snapshot/manifest and mechanical attestation | Execution §8 |
| CH-002 | S0 | Report-only role with sandboxed bash is not mechanically read-only | Open | Read-only mount/snapshot or no mutation-capable tools | Execution §8.2 |
| CH-003 | S0 | Parent and writer child may share writable plane without explicit handoff | Open | Separate working copy by default; optional tested sublease later | Execution §8.3 |
| CH-004 | S2 | Child completion success may hide dirty/rebased/unrelated container state | Open | Verify Git/content/provenance; ambiguous state retained | Execution §9 |
| CH-005 | S1 | Child/session lifecycle tied to route/process records with duplicate owners | Open | Runs/parent lineage in controller; adapters project | State runs; Execution §8 |

## 7. Secretary, personal, and integration gaps

| ID | Severity | Observation/request | Status | Required correction | Contract |
|---|---:|---|---|---|---|
| WF-001 | S2 | Secretary workstream list does not provide complete project/worktree purpose and status | Open | Project projection combining controller/Git/attention/inference | Product §4; Observability §8 |
| WF-002 | S1 | Personal direct branch work is not a first-class secretary-visible change source | Open | Register explicit working cycle; submit to same local change queue | Product §5; Change |
| WF-003 | S2 | Secretary-created worktrees and personal work use different ease/safety paths | Open | Same controller/runtime/change contracts, different allocation UX | Product §§2, 4-5 |
| WF-004 | S2 | Worker completion tied to `review-requested` rather than generic change submission | Open | Submit immutable change; review optional/project policy evidence | Change §§5-13 |
| WF-005 | S0 | Integration target can move after review | Historical guarded-path evidence; unified target open | Exact revision/target authorization and CAS; stale analysis invalidated | Change §§12-17 |
| WF-006 | S2 | Textual mergeability alone cannot expose semantic conflicts | Open | Deterministic Git analysis plus focused read-only semantic analysis | Change §12 |
| WF-007 | S0 | Review acceptance could be mistaken for merge authority | Historical guard evidence to preserve; target authorization open | Explicit integration authorization bound to revision/target/turn | Change §§13-14 |
| WF-008 | S2 | Unmanaged worktrees invisible or unsafe to manage | Open | Inventory read-only; explicit adoption before mutation | Product §4; State working copies |
| WF-009 | S2 | Status proliferation (`ready`, `review`, `land`, etc.) obscures common flow | Open | Working/open changes/needs attention/integrated projections | Product §8; Change §5 |
| WF-010 | S1 | Primary target may be dirty/in use during integration | Partly guarded | Refuse direct apply; integration worktree; never reset/stash | Change §§15-16 |

## 8. UI, continuity, and observability gaps

| ID | Severity | Observation | Status | Required correction | Contract |
|---|---:|---|---|---|---|
| UX-001 | S2 | Compaction summary retained for model but unavailable to user | Open; feedback recorded | Visible continuity card derived from actual summary | Product §10; Observability §14 |
| UX-002 | S2 | Internal terms such as `synced`, generation, lease, OID, runtime contract are ambiguous in normal UI | Open | Consequence-oriented messages; details on demand | Product §§3, 9 |
| UX-003 | S2 | Secretary footer includes opaque project ID | Open | Tmux/Herdr show project; footer shows role/attention only | Product §3 |
| UX-004 | S2 | Model/tool status and project/location presentation were conflated in discussion | Contract corrected | Pi footer model/activity/context; tmux/Herdr project/location | Product §3 |
| OBS-001 | S2 | No normalized controller event/outbox joins lifecycle state and adapter observations | Open | Transactional control events and consumer cursors | State operations/events; Observability §§3-6 |
| OBS-002 | S1 | Process may die without durable terminal classification | Open | Start journal plus reconcile to `lost`/needs attention | Observability §15 |
| OBS-003 | S2 | Missing observation can look like healthy absence | Open | Unknown explicit; adapter failure visible | State §12; Observability |
| OBS-004 | S3 | Existing Inspector lacks controller desired/observed/change view | Later target | Read-only Control tab after controller foundation | Observability §11 |

## 9. Build, patch, migration, and test gaps

| ID | Severity | Observation | Status | Required correction | Contract/phase |
|---|---:|---|---|---|---|
| ACT-001 | S1 | Installed `/home/j/.local/bin/pi` and repository `bin/pi` can differ | Open | Installed build manifest/ID and run binding; activation gate | State builds; Migration |
| ACT-002 | S1 | Repository commit does not prove patched package/image activation | Historical documentation/activation evidence; unified target proof open | Stage/verify/activate record and live attestation | Migration; Acceptance |
| PATCH-001 | S3 | 39 verified patch stages/41 patches act as an implicit fork | Open; sandbox/subagents extraction is an MVP prerequisite, remaining packages later | First-party maintained local packages with upstream provenance; no new architecture patch chain | Phases 5/6, then post-MVP |
| PATCH-002 | S2 | Patch-order compatible hashes make effective source hard to reason about | Open | One-time source manifests for extracted packages plus one tested installed build ID | Phases 5/6 and migration |
| MIG-001 | S0 | JSON registries have ambiguous precedence during cutover | Open | Read-only inventory, precedence, shadow compare, single-writer cutover | Migration |
| MIG-002 | S0 | Destructive migration/cleanup could remove recoverable refs/worktrees | Historical safeguard evidence to preserve; target migration open | Copy/import only; no cleanup in cutover; exact dry run later | Migration |
| TEST-001 | S0 | Static patch-string tests do not prove process restart/OID/artifact behavior | Open | Disposable process/fault-injection suite | Acceptance |
| TEST-002 | S1 | Live installed package, Docker, and launcher E2E incomplete for target | Open | Staged build/live canary tests before activation | Acceptance |
| TEST-003 | S1 | No crash injection at every operation boundary | Open | Deterministic failpoints and reconcile assertions | Acceptance |
| PERF-001 | S3 | No controller startup/query/reconcile baseline | Open | Measure first; explicit non-regression budgets before optimization | Product §11; Acceptance |

## 10. Required MVP blockers

The workflow MUST NOT be used to launch parallel mutable implementation of the
remaining adapters until these blockers pass end to end:

1. ID-001/002/003/005 — canonical identity, trust, and one authority.
2. LIF-001/002/004/005 — locks, fencing, direct-write revocation, crash saga.
3. GIT-002/003 and CH-001/002/003 — exact child source and write boundaries.
4. ACT-001/002 — tested installed build identity.
5. TEST-001/003 — process/fault-injection proof.
6. Minimal change submission and secretary queue for integrating subsequent
   work without ad hoc branch selection.

Read-only investigation may continue in parallel before these gates. Mutable
foundation implementation should use one writer and independent review.

## 11. Deferred after walking skeleton

- cross-run container reuse optimization;
- full Herdr presentation parity;
- rich project dependency dashboard;
- automated semantic merge resolution;
- automatic resource retention/cleanup;
- resident controller daemon;
- broad observability/benchmark platform;
- remaining compatibility patch consolidation after the MVP-required sandbox
  and subagent package extractions;
- remote publication/deployment integration.

## 12. Ledger maintenance

Each implementation phase updates this ledger only after evidence exists:

- set status to implemented-in-source, staged, activated, or accepted; do not
  collapse those states;
- link exact tests/commands/artifacts;
- record superseding contract decisions;
- never mark a defect closed solely because a new code path exists;
- preserve the original symptom and decisive evidence so future regressions can
  be recognized.
