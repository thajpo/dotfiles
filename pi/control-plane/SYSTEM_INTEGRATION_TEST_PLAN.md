# Pi configured-harness full-system integration test plan

Status: **C9 deterministic fixture/process/action/fault harness implemented and
validated; staged/Docker/presentation/canary tiers remain capability-gated;
Phase 11D remains blocked**.

This document defines what “full system integration coverage” means for the Pi
configuration in this repository. It supplements the detailed component and
fault matrices in `ACCEPTANCE_PLAN.md`; it does not replace them.

**Pre-implementation status:** the target `tests/system/` runners and action
manifest described here do not yet exist and MUST NOT be cited as acceptance
evidence. C0b creates the manifest/discovery contract. C9a–C9c2 now provide
isolated per-action/per-fault runners and structured evidence for the source and
process tiers. Required staged, Docker, and presentation prerequisites still
return STOP/77; a missing tier is not passed and is never an invitation to
execute a nonexistent command.

## 1. Scope

The system under test is not only `scripts/pi_control`. It includes:

- installed launchers (`pi`, `pi-start`, `pi-personal`, `pisec`, secretary,
  workstream, review, restart, host, control, GC, help, stats, feedback);
- host-owned repository policy and activation latch;
- root/session/workspace/runtime helpers;
- controller SQLite, Git adapters, operation journal, events, migration, build,
  and rollback;
- normal parent, personal, secretary, worker, reviewer, and child boundaries;
- sandbox and subagent first-party packages;
- explicit global/secretary extensions and configured Pi packages;
- trusted-live, isolated, read-only, and host-maintenance execution modes;
- tmux and Herdr presentation adapters;
- Docker runtime/image/mount/permission behavior;
- session JSONL, task packets, compaction continuity, Inspector, feedback, and
  user-visible failures;
- local changes, review, integration, publication, cleanup, migration,
  activation, and rollback authorization boundaries.

### 1.1 What is not re-tested exhaustively

Pi core and third-party package internals are upstream dependencies. This suite
must prove the pinned version loads, the configured resource is reachable, the
harness-owned safety boundary is preserved, and one representative critical
path works. It does not duplicate every upstream editor, model, or UI test.

Natural language is unbounded. Deterministic acceptance invokes semantic tools
and exact approval payloads directly. A separate labeled model-evaluation tier
checks that representative language produces the expected proposal and does not
bypass approval. A model evaluation is evidence, never a replacement for a
mechanical authority test.

## 2. Completion rule

Every supported harness action must have:

1. a stable action ID in §7 and in `tests/system/action-manifest.v1.json`;
2. one or more scenario IDs;
3. a declared execution tier;
4. success and refusal cases;
5. explicit authoritative and external before/after assertions;
6. authorization and project-isolation assertions where applicable;
7. idempotency/restart/reconciliation assertions for mutation;
8. crash/race/rollback assertions for high-risk mutation;
9. a user-visible result/error assertion;
10. an evidence artifact outside the repository.

The manifest validator must fail when:

- a controller CLI subcommand, explicitly loaded extension tool/command,
  launcher mode, or configured package disappears from the catalog;
- a catalog action has no scenario;
- a scenario has no assertion set or required tier;
- a required tier is reported as skipped/not-tested;
- an evidence file does not bind the exact build and fixture.

## 3. Test topology

### 3.1 Disposable fixture

Every non-live scenario receives:

```text
private temporary HOME
private PI_CODING_AGENT_DIR
private XDG_CONFIG_HOME, XDG_STATE_HOME, XDG_RUNTIME_DIR, TMPDIR
private installed/staging roots
private Git repositories, common dirs, worktrees, refs, indexes, hooks/config
private session JSONL, root registry, secretary compatibility records
private controller DB, locks, manifests, artifacts, migration records
fake or namespace-isolated process/container/tmux/Herdr backends as declared
fixed clock and deterministic random-ID source where identity is asserted
scripted Pi provider/extension driver with no network provider call
local bare Git remote only for explicit publication tests
```

Before each scenario, capture the real repository and visible host state. After
it, assert the fixture is the only changed namespace. A fixture that cannot
prove isolation returns STOP before the action.

### 3.2 Scripted real-Pi driver

Source-text checks are insufficient. Add a test-only scripted provider/driver
that launches the pinned real Pi executable in RPC/process mode with the exact
staged settings and extensions. It must:

- emit deterministic assistant tool calls and final responses;
- invoke registered semantic tools through Pi, not import extension functions
  directly for system-tier evidence;
- drive session start, prompt, tool result, compaction, branch/resume, and
  shutdown events;
- never contact a remote model provider;
- record Pi argv, loaded resources, tool schemas/calls/results, session entries,
  and exit status;
- exist only under `tests/system/fixtures`; installed production code must not
  expose scripted-provider or failpoint selection.

Direct module tests remain appropriate in the component tier.

### 3.3 Fake executable boundary

Launchers also run against exact fake executables for hostile/failure cases:

- `pi` records argv/environment and can exit/crash at selected boundaries;
- Docker/tmux/Herdr/Git/process commands use fixed scripted observations;
- each fake rejects unrecognized arguments so tests prove command shape;
- fakes are selected only through the disposable fixture’s PATH/constructor
  wiring;
- production launchers do not accept caller mode/trust/authority overrides.

### 3.4 Real backend boundary

Fakes prove classification and orchestration; they do not prove kernel/runtime
behavior. Real disposable tiers separately exercise:

- Git worktrees/refs/index and filesystem modes/inodes;
- `flock`, concurrent processes, PID/start identity, cancellation;
- real pinned Pi and installed package loading;
- Docker image/container/mount/user/attestation and rollback;
- tmux sessions/windows/panes and unrelated-session preservation;
- Herdr named sessions/Spaces/tabs/panes and guarded restore where Herdr is a
  supported installed backend.

## 4. Required tiers and gates

| Tier | Runner | Required evidence | Unavailable behavior |
|---|---|---|---|
| T0 Contract/manifest | `tests/system/run-contract.sh` | Docs/schema/action traceability, source inventory | FAIL for mismatch; STOP only if parser runtime absent |
| T1 Component | existing Python/Node suites via `run-component.sh` | Pure state, schemas, adapters, extension modules | STOP if Python/SQLite/Git/Node prerequisite absent |
| T2 Process fixture | `run-process-fixture.sh` | Real CLI/launchers/Pi driver with fake backends and disposable state | STOP; never skip |
| T3 Staged installed | `run-staged-installed.sh` | Exact installed tree, package loading, real Pi, process workflows | STOP if npm/pinned Pi/staging unavailable |
| T4 Docker runtime | `run-docker.sh` | Image, mounts, UID/GID, attestation, tool proxy, stop, image rollback | STOP/77 if Docker/image unavailable |
| T5 Presentation | `run-presentation.sh` | Real tmux; real Herdr on target profiles that support it | STOP/77 for a required configured backend |
| T6 Model evaluation | `run-model-evals.sh` | Representative intent/wording and approval-card behavior | Separate non-deterministic report; cannot make deterministic gate green |
| T7 Live canary | reviewed Phase 11D runbook only | One real project, backup, activation, restart, fault, rollback | Not run without explicit user approval |

Gate composition:

```text
Source GO   = T0 + T1 + T2
Staging GO  = Source GO + T3 + T4 + required T5
Canary GO   = Staging GO + reviewed inventory/resolution/backup + explicit T7 approval
Rollout GO  = accepted canary + new per-project reviewed operation
```

T6 is reported alongside the gate but never weakens deterministic failures.

### 4.1 Capability declaration and conditional tiers

`tests/system/capabilities.v1.json` is produced by a read-only preflight and
validated against the machine profile and staged launcher surface. It records
platform/architecture, executable/version checks, Docker daemon/image
visibility, tmux, Herdr, graphical terminal/Kitty, and which backend/layouts the
host claims to support. Caller/model environment cannot promote a capability.

| Capability | Requirement for this harness | Gate behavior |
|---|---|---|
| Linux Docker execution | Mandatory for normal trusted/isolated task execution on every supported host, including macOS hosts whose tasks run in Linux Docker | T4 missing is STOP/77; no staging GO |
| tmux desktop and mobile layouts | Mandatory default presentation backend | Real T5 tmux missing is STOP/77 |
| Herdr desktop/mobile | Mandatory on a deployment host whose reviewed machine profile enables Herdr or whose canary/rollout scope exposes `-herdr`; otherwise its real-backend scenarios are platform-conditional while fake/refusal coverage remains mandatory | Claimed+missing/mismatched is STOP/77; unclaimed host must prove clean exit-127/refusal and cannot claim Herdr acceptance |
| Graphical terminal/Kitty second-window behavior | Optional presentation convenience; headless attach remains the normative path | Missing GUI is not STOP; exact headless behavior still required |
| Kitty key-transport diagnostic | Hardware/terminal compatibility evaluation, not controller authority | Report separately; never blocks source state correctness unless selected canary requires it |
| macOS host profile | Required only on a macOS deployment candidate; Linux source/staging cannot claim macOS host acceptance | Separate host evidence/STOP |

Mobile layout behavior is not GUI-dependent and remains mandatory for every
claimed tmux/Herdr backend. The evidence bundle records both declared and
observed capability; changing the declaration requires reviewed host policy,
not a test flag.

Exit codes for every runner:

```text
0   PASS: every required scenario in this runner passed
1   FAIL: behavior or assertion failed
2   runner usage/configuration error
77  STOP: required prerequisite/evidence unavailable; no acceptance claim
```

No runner converts 77 to 0. Aggregates name which earlier tiers passed before
returning 77.

## 5. Proposed test layout

```text
tests/system/
  README.md
  action-manifest.v1.json
  action-manifest.schema.json
  test_action_manifest.py
  evidence.schema.json
  run-contract.sh
  run-component.sh
  run-process-fixture.sh
  run-staged-installed.sh
  run-docker.sh
  run-presentation.sh
  run-model-evals.sh
  run-source-gate.sh
  run-staging-gate.sh
  fixtures/
    environment.py
    git.py
    state.py
    fake-pi
    scripted-provider.ts
    fake-docker
    fake-tmux
    fake-herdr
    fake-process-observer
    local-remote.py
  assertions/
    sqlite.py
    git.py
    filesystem.py
    process.py
    runtime.py
    presentation.py
    session.py
    ui.mjs
    privacy.py
  scenarios/
    installation.py
    launchers.py
    sessions.py
    personal.py
    secretary.py
    workstreams.py
    children.py
    changes.py
    reviews_integration.py
    continuity_observability.mjs
    packages.mjs
    migration.py
    cleanup_publication.py
    recovery_security.py
```

Existing tests remain; runners call them rather than copying assertions. Rename
or relocate only when a dedicated reviewed slice proves necessary.

## 6. Action manifest schema

Each action entry includes:

```json
{
  "actionId": "HA-001",
  "name": "launch normal root",
  "surface": "launcher|cli|extension|package|host-runbook",
  "entrypoints": ["bin/pi"],
  "authority": "user|parent|secretary|worker|reviewer|host-admin",
  "mutationClass": "read|controller|git|runtime|presentation|host|remote|cleanup",
  "authorizationClass": "none|exact-create|exact-host|exact-integrate|exact-publish|exact-cleanup|exact-cutover",
  "modes": ["legacy", "shadow", "controller"],
  "scenarios": ["SYS-LAUNCH-001"],
  "tiers": ["T2", "T3"],
  "assertions": ["sqlite", "git", "filesystem", "process", "ui"],
  "risk": "normal|high",
  "status": "supported|compatibility|planned|host-only|out-of-scope",
  "owningSlice": "C7c"
}
```

The validator derives comparison inventories from:

- argparse registrations in `scripts/pi_control/cli.py`;
- `registerTool`/`registerCommand` calls in installed first-party/global
  extensions;
- every literal explicit `-e` extension path and `--tools` entry assembled by
  `bin/pi-secretary`, review, workstream, personal, and host launchers;
- `tests/system/loaded-extensions.v1.json`, which names dynamic installed paths
  that cannot be derived statically and their owning launcher/profile;
- every public launcher action/flag in
  `tests/system/launcher-surface.v1.json`, checked against `bin/pi-help-custom`,
  launcher parsers, and process-level usage/refusal fixtures (including
  `pi-start project|host|all`, mobile/Herdr selection, restart no-attach, and
  internal-only forms);
- `packages` and exact source/version/load path in both current source settings
  and the planned staged controller settings;
- semantic host-only operations in the activation runbook API.

A `planned` action names its owning completion sub-slice and is not required to
exist in current source. It must have target scenarios but cannot be reported
passing until discovery finds the implementation and its status becomes
`supported`. Current discovered actions cannot be hidden as planned.
`out-of-scope` actions require refusal tests. Dynamic or intentionally internal
tools use an explicit allowlisted manifest entry; they are never silently
ignored.

## 7. Configured-harness action catalog

### 7.1 Launch, session, workspace, and presentation

| ID | User action / entrypoint | Minimum scenarios | Required proof |
|---|---|---|---|
| HA-001 | `pi` new managed root | SYS-LAUNCH-001/002 | Exact policy/project/session/worktree/run/build; trusted or isolated plane; no hidden fallback |
| HA-002 | `pi --continue`, `--resume`, exact `--session`, `--session-id` | SYS-SESSION-001..004 | Same conversation/worktree, new run, child sessions excluded, cwd cannot rebind |
| HA-003 | explicit `--fork` | SYS-SESSION-005 | New conversation/session lineage, explicit full-context fork, source assignment unchanged |
| HA-004 | `pi-start project [DIR]`, `pidev` desktop/mobile | SYS-LAUNCH-010/011 | Exact repo/session, pane layout, no unknown live process replacement |
| HA-005 | `pi-start personal`, `pi-personal open|launch|--ensure` | SYS-PERSONAL-001..004 | Four stable roles, exact dirs/sessions, safe dead-pane repair, backend ownership |
| HA-006 | `pisec register`, `list`, `launch-info` | SYS-SECRETARY-001..003 | Controller project identity/capability; compatibility registry is projection only in controller mode |
| HA-007 | `pisec`, `open`, `launch`, `pi-start secretary` | SYS-SECRETARY-004..007 | Exact active project set, process/session lock, no duplicate secretary, safe repair |
| HA-008 | `pisec activate`, `swap` | SYS-SECRETARY-008..011 | Ordered preference update; removed live secretary gracefully stops or action refuses; no lost turn |
| HA-009 | `pi-start all` four backend/layout cells | SYS-PRESENT-001..008 | Both surfaces; exact matrix; unknown conflicts fail; no worker migration |
| HA-010 | `pi-restart` four cells and `--no-attach` | SYS-PRESENT-009..014 | Only managed sessions stop; unrelated tmux survives; active workers preserved/refuse; recovery after partial stop |
| HA-011 | Herdr attach/restore and managed relaunch | SYS-HERDR-001..006 | Named sessions and exact guarded wrappers; native agent restore disabled; tmux-pinned worker untouched |
| HA-012 | `pi-host`, `pi-start host` | SYS-HOST-001..004 | Explicit unsandboxed warning/mode; nested fallback rejected; stable host session; ordinary Pi never escalates |
| HA-013 | `pi help`, `pi help all`, upstream `pi --help/version` | SYS-HELP-001..003 | Checked-in action vocabulary matches manifest and pinned Pi; no unsafe side effect |

**Known initial reds:** current `bin/pi-restart` calls `tmux kill-server`;
therefore SYS-PRESENT-009 and the unrelated-session assertion MUST fail until
C5c replaces that broad operation with exact managed-session handling. Current
legacy root migration also selects duplicate histories by file modified time;
SYS-ROOTMIG duplicate scenarios MUST fail until C2/C3/C4 replace that heuristic
with preserve-all plus explicit resolution. Neither behavior is accepted by the
target plan.

### 7.2 Parent, goal, delegation, child, and feedback

| ID | Action | Minimum scenarios | Required proof |
|---|---|---|---|
| HA-020 | Parent read/edit/write/bash/user `!` tools | SYS-TOOLS-001..006 | Correct execution plane/cwd; mutation fence; cancellation; no host path/socket leakage |
| HA-021 | FAST/RIP/BUILD/MAJOR and OFF/LIGHT/DEEP policy | EVAL-WORKFLOW-001..009 + deterministic context checks | Correct task packet/brief boundaries; one writer; evidence; no policy as authority escalation |
| HA-022 | `/goal` start/status/pause/resume/clear/complete/blocked | SYS-GOAL-001..008 | Same session; hidden continuation once; stale completion rejected; provider interruption retained |
| HA-023 | `subagent` single/parallel/chain/async | SYS-CHILD-001..008 | Exact parent/source, scoped context, status artifacts, concurrency, no implicit worktree for reports |
| HA-024 | status/fleet/interrupt/stop/resume/steer/append-step | SYS-CHILD-009..017 | Durable transitions, correct target run, stopped non-resumable, no lost result/lease |
| HA-025 | Worker nested read-only investigation | SYS-CHILD-018..022 | Depth/tool restriction, no shell/write/worktree/further spawn, worktree unchanged |
| HA-026 | Independent mutable candidate/worktree | SYS-CAND-001..006 | Separate branch/worktree/runtime/writer; commit before comparison; integrated candidate retested |
| HA-027 | BTW side question | SYS-BTW-001..004 | Separate context/execution; compact explicit injection; no implementation ownership/publication |
| HA-028 | `contact_supervisor` request/reply and feedback review | SYS-FEEDBACK-001..006 | Bounded central record, exact parent reply, no automatic capability, expiry/restart behavior |
| HA-029 | `harness_feedback` and `pi-harness-feedback` | SYS-FEEDBACK-007..012 | Cross-project central feed, provenance/digest, filter/review outcome, no project artifact/raw prompt |
| HA-030 | host-command request approve/reject/expire | SYS-HOSTCMD-001..008 | Exact one-shot command/cwd/reason, visible approval, stale replay denied, bounded sensitive output, reconcile stale resources |

### 7.3 Secretary and workstream actions

| ID | Action | Minimum scenarios | Required proof |
|---|---|---|---|
| HA-040 | Secretary project status and bounded Git reads | SYS-SECVIEW-001..006 | Observed/recorded/inferred distinction; fresh Git; unavailable visible; arbitrary Git args rejected |
| HA-041 | record idea; list/acknowledge attention | SYS-ATTN-001..006 | Bounded non-authoritative note, durable attention, exact ack, duplicate delivery idempotent |
| HA-042 | create workstream | SYS-WS-001..010 | Exact approval and full saga: rows, worktree, session, presentation, run, attestation; crash recovery |
| HA-043 | focus/open workstream | SYS-WS-011..014 | Read-only focus of same durable conversation; no retry/recreate/mutation |
| HA-044 | relaunch stopped tmux workstream in Herdr | SYS-WS-015..021 | Old process/window/worktree-use absence; same IDs; uncertainty leaves old state untouched |
| HA-045 | worker progress/needs-user/review-requested | SYS-WS-022..027 | Durable events, quiet progress, visible decision/failure, no arbitrary cross-session relay |
| HA-046 | create exact reviewer and submit receipt | SYS-REVIEW-001..010 | Detached mechanical read-only checkout/run/capability; immutable authenticated receipt; staleness |
| HA-047 | land reviewed / controller integrate | SYS-INT-001..014 | Separate current authorization, current receipt/policy, target CAS, rollback ref, post-proof |
| HA-048 | create integration workstream | SYS-INT-015..022 | Original revision preserved; exact inputs; result revision/change; final approval still required |
| HA-049 | retire/cleanup workstream/reviewer | SYS-CLEAN-001..010 | Dry-run hash, exact ownership/OID/path, clean/non-live/retention, race refusal, no force/prefix delete |
| HA-050 | secretary explicit commit/push/commit-and-push exception | SYS-PUBLISH-001..010 | Exact paths/current branch/existing origin, separate publish approval, local bare remote, no force/network |

### 7.4 Controller project, run, change, review, and recovery APIs

Every currently supported subcommand is invoked as an installed process, not
only through direct Python calls. Entries below distinguish the **current**
argparse surface from **planned** semantic operations owned by completion
slices; planned names are target contracts, not claims that the command exists.
The action manifest validates only current names against current argparse and
moves a planned entry to supported when its owning slice lands.

| ID | Current/planned CLI or semantic action | Minimum scenarios | Required proof |
|---|---|---|---|
| HA-060 | `schema status` | SYS-CLI-001..003 | Fresh/migrated/newer DB, canonical JSON, no mutation for read |
| HA-061 | Current: `project list/register/inspect`; planned C1c/C4: `project rebind` | SYS-PROJECT-001..010 | Stable random ID, Git anchors, policy/trust, duplicate/move/copy/symlink behavior |
| HA-062 | Current: `working-copy inventory`; planned C1c/C7d: `working-copy adopt` | SYS-WC-001..012 | Primary/linked/unmanaged/dirty/detached/conflict; adoption exact and authorized |
| HA-063 | `inspect`, `status`, `reconcile --observe-only` | SYS-OBS-001..010 | Read versus persisted observation, no external repair, unavailable/unknown, events/freshness |
| HA-064 | operation/event list and event cursor APIs | SYS-EVENT-001..010 | Ordering/pagination/emitted-only ack/CAS/dedup/poison behavior |
| HA-065 | focus exact resource | SYS-FOCUS-001..005 | Project ownership, exact ID/version, no fuzzy label, presentation-only |
| HA-066 | Planned C1c/C4b: conversation ensure/archive/focus | SYS-CONV-001..010 | Exact session/project/wc role, JSONL authority, no cwd rebind, archive preserves work |
| HA-067 | Planned C1c/C6b-c: run prepare/attest/start/stop/reconcile | SYS-RUN-001..020 | Manifest/build/epoch/lock/runtime exactness, no tools early, graceful quiescence, restart/lost |
| HA-068 | personal select primary/separate worktree | SYS-PERSONAL-010..016 | Explicit strategy, separate conversation for worktree, no dirtiness heuristic |
| HA-069 | Current: workstream create-row API; planned C5: plan/full-create/focus/relaunch/retire saga | SYS-WS-001..021 | Same full saga as user tools through controller API |
| HA-070 | Current: change submit (new revision through repeat submit); planned C1c/C7c: show/list/close | SYS-CHANGE-001..020 | Clean/dirty/task delta, ambiguity, immutable refs, idempotency, source unchanged, no target mutation |
| HA-071 | Current: review request/submit; planned C1c: review list | SYS-REVIEW-001..010 | Exact authenticated source/reviewer bindings and immutable receipt |
| HA-072 | integration analyze | SYS-INT-001..006 | Candidate/target/merge base/conflicts/overlap/current OIDs; target/index unchanged |
| HA-073 | integration authorize | SYS-INT-007..011 | Exact actor/request/review/scope/expiry; replay exact; changed/stale conflict |
| HA-074 | Current: integration `integrate` plus recovery views; planned C1c/C7c: named apply/reconcile protocol aliases | SYS-INT-012..022 | Lock order, rollback, ref/index/worktree post-proof, target race, cancellation, crash adoption |
| HA-075 | Current: recovery status/details | SYS-REC-001..012 | Project isolation, bounded redaction, pending/ambiguous/terminal evidence, no mutation |
| HA-076 | Planned C7d: cleanup plan/apply | SYS-CLEAN-001..010 | Exact dry-run hash and dedicated current authorization |
| HA-077 | Planned controller C7d; current compatibility secretary Git write: publish plan/apply | SYS-PUBLISH-001..010 | Separate remote authority and local bare-remote test; never implied by integration |

### 7.5 Snapshot, artifact, continuity, Inspector, and configured packages

| ID | Action | Minimum scenarios | Required proof |
|---|---|---|---|
| HA-080 | exact clean/dirty snapshot | SYS-SNAP-001..014 | Temp index, selected content, limits, concurrent mutation, immutable CAS ref, source/index unchanged |
| HA-081 | artifact create/read/retain/cleanup eligibility | SYS-ART-001..012 | External secure file, hash/provenance/sensitivity, corruption refusal, no deletion from eligibility alone |
| HA-082 | manual/threshold/overflow compaction | SYS-CONT-001..010 | One persisted card/continuation, actual result+packet, digest/privacy, crash/resume/branch dedup |
| HA-083 | `/continuity` | SYS-CONT-011..014 | Latest/history rendering, malformed/newer graceful behavior, no fabricated state |
| HA-084 | `/observe`/Ctrl-I and controller technical view | SYS-UI-001..010 | Read-only task/fleet/control data, live refresh, hidden reasoning absent, missing/newer safe |
| HA-085 | footer/activity/attention | SYS-UI-011..016 | Model/reasoning/activity/context, healthy control silence, one bounded attention status |
| HA-086 | image paste/resume/fork | SYS-IMAGE-001..006 | Native image block bytes, no shared `/tmp` dependency, session persistence, sandbox boundary |
| HA-087 | `/fast` toggle/status | SYS-FAST-001..004 | Host-owned provider setting semantics, no trust/authority effect, session visibility |
| HA-088 | configured editor/session packages (`pi-vim`, `pi-nvim`, `pisesh`) | SYS-PKG-001..006 | Exact pinned load, key/session critical path, no authority broadening |
| HA-089 | web/usage/status/GitHub/plan packages | SYS-PKG-007..016 | Exact pinned load and representative read-only path; remote mutation absent without publish auth |
| HA-090 | first-party sandbox/subagents packages | SYS-PKG-017..024 | Activated local provenance, no legacy co-load, tool/runtime/child critical paths |
| HA-091 | `/subagents-doctor`, `pi-secretary-stats [--json]`, fleet/usage diagnostics | SYS-DIAG-001..008 | Correct installed role/package discovery, bounded aggregate lifecycle/timing/usage, malformed/missing data visible, prompts/outputs omitted |

The package inventory is exact, not satisfied by the grouped HA-088–090 rows:

| Current configured source | Planned staged source where different | Representative required action/boundary |
|---|---|---|
| `npm:pi-btw@0.4.1` | same | side question remains separate and explicitly injected |
| `npm:pi-image-tools@1.4.0` | same | Ctrl-V native image block persists through resume/fork |
| `npm:@narumitw/pi-goal@0.43.0` | same | goal continuation/pause/resume/terminal stale guard |
| `npm:@narumitw/pi-plan-mode@0.31.0` | same | package loads exact version and representative plan-mode command does not alter controller authority |
| `npm:pi-vim@0.14.1` | same | configured key/mode boundary and no authority change |
| `npm:pisesh@0.1.12` | same | representative session/shell integration within selected execution plane |
| `npm:@narumitw/pi-statusline@0.31.0` | same | healthy footer and bounded activity/attention behavior |
| `npm:@narumitw/pi-usage@0.31.0` | same | bounded usage view without prompt/content leakage |
| `npm:@narumitw/pi-github-pr@0.31.0` | same | mocked/read-only PR path; no push/publication token or side effect |
| `npm:pi-nvim@0.2.4` | same | representative editor bridge inside assigned plane |
| `npm:pi-web-access@0.18.0` | same | mocked search/fetch/source-check path with bounded output; no publication |
| `npm:pi-subagents@0.35.1` | `./packages/pi-subagents-control` | exact first-party loaded path, orchestration/child boundary, no remote package co-load |
| `npm:@kjrjay/pi-sandbox@0.2.0` | `./packages/pi-sandbox-control` | exact first-party loaded path, runtime/tool boundary, no legacy co-load |

`tests/system/configured-packages.v1.json` records exact source, expected package
name/version, resolved path/root, every loaded tool/command/resource,
representative scenario, and remote capability classification for each row.
Remote-capable read actions use mocked/local transport. If a third-party package
exposes remote mutation/publication, staged settings MUST filter/disable that
resource unless it is wrapped by the semantic HA-050/HA-077 publication adapter
with dedicated authorization. Package provenance alone never allowlists an
undiscovered mutating tool.

### 7.6 Migration, build, install, GC, rollback, and activation

| ID | Action | Minimum scenarios | Required proof |
|---|---|---|---|
| HA-100 | typed migration inventory | SYS-MIG-001..016 | Every adapter state and record disposition; immutable manifest; no source/runtime mutation |
| HA-101 | migration resolution plan | SYS-MIG-017..024 | Exact inventory digest, deterministic decisions, human decisions retained, stale plan refusal |
| HA-102 | shadow import/reconcile | SYS-MIG-025..040 | Full typed rows/mappings, crash retry, field compare, shadow-only external behavior |
| HA-103 | final import/cutover plan | SYS-MIG-041..048 | Quiescence/lock/current inventory/backup/build/canary scope; plan only in non-live tier |
| HA-104 | build manifest and staged install | SYS-BUILD-001..016 | Canonical build ID/tree/package/image/config/helper/launcher hashes; unexpected file refusal |
| HA-105 | installer apply in disposable HOME | SYS-INSTALL-001..020 | Ordered atomic generation, failpoint rollback, real loaded bytes, old generation restorable |
| HA-106 | Docker runtime/image rollback | SYS-DOCKER-001..020 | Image/build/attestation and rollback; unavailable returns 77 |
| HA-107 | `pi-sandbox-gc` dry/apply | SYS-GC-001..012 | Exact labels/retention/recovery/liveness; unlabeled and ambiguous preserved; no global prune |
| HA-108 | root migration dry/apply/archive/cleanup | SYS-ROOTMIG-001..016 | Copy/preserve all histories, duplicate mapping is explicit (never mtime selection), idempotency, no broad worktree prune |
| HA-109 | activation inspect/plan | SYS-ACT-001..010 | Latch/DB/build/migration/project exact; no live change |
| HA-110 | activation/canary apply | T7 runbook scenarios only | Exact user+host authorization, one project, controller sole writer, full evidence |
| HA-111 | rollback | staged SYS-ROLL-001..016; T7 live rollback | Quiesce, preserve new DB/refs/work, restore exact generation/legacy route, verify |
| HA-112 | general rollout | later per-project runbook | Fresh evidence per project; never inherited from canary automatically |

### 7.7 Explicitly rejected/out-of-scope actions

| ID | Attempt | Required refusal proof |
|---|---|---|
| HA-120 | ordinary `pi` selects sandbox/trust/publish target, loads arbitrary extension, or uses approval bypass flags | Rejected before workspace/runtime mutation |
| HA-121 | model/secretary sends arbitrary shell/Git/path/ref/container arguments to controller | Schema/allowlist rejection; no subprocess |
| HA-122 | review/generic yes/workstream approval reused for integration, publish, cleanup, or cutover | Authorization mismatch/stale/consumed rejection |
| HA-123 | automatic merge/push/cleanup after worker success or review accept | No target/remote/delete side effect |
| HA-124 | force reset/stash/checkout/delete/prune to resolve drift | Operation refuses and preserves attention/evidence |
| HA-125 | same-working-copy parent/writer child or cross-run container reuse | Unsupported/fail-closed before writable access |
| HA-126 | live tmux↔Herdr process migration or Herdr native Pi restore | Refused; explicit stopped relaunch only |
| HA-127 | daemon/network control plane/remote deploy | No supported semantic action; explicit non-goal |
| HA-128 | Phase 11D/general rollout from ordinary sandbox or environment mode flag | Host boundary and exact approval refusal |

## 8. Cross-action system journeys

Atomic action tests are necessary but insufficient. Run these complete journeys
without direct resource-row seeding after fixture bootstrap.

### JOURNEY-01 — Personal primary, dirty change, child, review, integration

1. Install staged generation in disposable HOME.
2. Register trusted project and select `controller` fixture activation.
3. Launch personal root through `bin/pi` and attest run.
4. Record pre-existing dirty file, then make separate task changes.
5. Launch exact read-only child; parent advances afterward.
6. Prove child remains at old immutable source and cannot write.
7. Submit only task-owned dirty delta; source index/worktree remain unchanged.
8. Secretary lists change and fresh target state.
9. Launch exact reviewer, submit accepted receipt.
10. Authorize one fast-forward integration and CAS target.
11. Restart personal/secretary; re-query same conversation/work and merged
    provenance.
12. Cleanup dry-run only; verify pre-existing dirty file remains.

### JOURNEY-02 — Secretary workstream and target movement

1. Secretary status and exact create approval.
2. Full workstream saga creates worktree/session/run/runtime.
3. Worker edits/tests/commits/submits revision.
4. Target moves after review.
5. Old authorization fails; original revision preserved.
6. Create integration workstream, adapt result, submit linked revision.
7. Review/authorize/integrate current result.
8. Stop worker, verify no live use, cleanup exact resources after separate
   approval.

### JOURNEY-03 — Crash and cancellation

Repeat representative personal/workstream paths with death:

```text
after intent
while lock held
before/after worktree/ref/session/container creation
before/after attestation
while tool running
before/after change ref
before/after target CAS
before/after event/result
```

Fresh-process reconciliation must classify old/desired/ambiguous state, retain
work, prevent duplicate writers, and make retry behavior explicit.

### JOURNEY-04 — Presentation matrix

For desktop/mobile × tmux/Herdr:

- launch personal + secretary;
- navigate/focus existing conversation;
- create worker pinned to backend;
- attempt single-surface conflict and verify refusal;
- restart/switch grid while worker active and verify preservation/refusal;
- explicitly stop/relaunch eligible worker into other backend;
- simulate backend server restart;
- prove conversation/workstream IDs remain controller-owned;
- prove unrelated tmux sessions survive.

### JOURNEY-05 — Migration and staged rollback

1. Construct legacy fixture with projects, root/secretary sessions, workstreams,
   routes/leases, children/artifacts, Docker/tmux/Herdr/process/build evidence,
   malformed records, duplicates, divergence, and unavailable adapters.
2. Inventory twice; identical observations yield identical identity digest.
3. Resolve only deterministic/specified mappings.
4. Shadow import; legacy writers continue; controller external mutation remains
   impossible.
5. Modify one source; comparison reports exact mismatch.
6. Quiesce fixture, refresh inventory, final-import into disposable final DB.
7. Stage/install exact build and switch one fixture project latch.
8. Run JOURNEY-01 and a safe fault.
9. Roll back generation/latch; preserve controller DB and new refs/worktrees.
10. Verify legacy fixture behavior and exact before/after inventory.

### JOURNEY-06 — Continuity and diagnostics

During an active journey:

- maintain/replace task packet;
- run parallel child with hidden success and another failure/attention;
- open Inspector and fleet;
- manual then overflow compaction with crash-after-card injection;
- resume and branch session;
- inspect `/continuity` history;
- approve/reject host-command and feedback requests;
- paste/resume/fork image;
- scan DB/session/artifacts for prohibited content;
- verify healthy footer remains quiet.

### JOURNEY-07 — Isolated repository

Run launch, child, change submission, restart, and retained recovery with an
unknown external repository. Prove no host source/Git metadata/credentials are
mounted; trust cannot broaden; publication is separate; rollback retains
checkpoint state.

## 9. Fault and race matrix

Every high-risk mutating action declares applicable points:

```text
before intent commit
after intent commit
before lock
while waiting for lock
after lock
before external side effect
after side effect before observation
after observation before DB/event
between ref and checked-out index/worktree update
after result before notification
while a second client reads/plans/applies
after process death and PID reuse
while path parent/symlink is swapped
while authorization expires/cancels
while source/target/resource version moves
```

Generic proof:

- no intent → no controller-owned effect;
- durable intent/no effect → exact retry may apply;
- possible effect → observe before retry;
- desired exact effect → adopt once and record once;
- expected old state → retry only under current versions/authorization;
- neither old nor desired → needs-attention, no guessed repair;
- duplicate notification/event delivery → one action/attention;
- stale writer/version/auth → rejection before side effect;
- unknown runtime/process → no new writer or destructive cleanup.

## 10. Assertion bundles

### 10.1 SQLite

Rows, foreign/check/trigger constraints, resource versions, operation state,
immutable request/result/receipt/mapping, authorization use, event atomicity and
sequence, cursor, attention, build/migration/activation binding.

### 10.2 Git

Common-dir identity, refs/OIDs/trees/ancestry, index hash/stages, worktree list,
status/untracked manifest, rollback refs, reflog where useful, no remote/network
operation except local bare-remote publication fixture.

### 10.3 Filesystem

Allowed path set, owner/mode/type/inode/device, symlink/parent swap, file hashes,
manifest exact tree, session/artifact namespaces, no orchestration files in
project, fsync/atomic generation evidence.

### 10.4 Process/runtime

PID/start identity/ancestry, lock descriptor, writer epoch, tracked subprocess,
graceful cancellation, container image/mount/user/network/security, no old
writable access, no unrelated process mutation.

### 10.5 Presentation

Exact managed tmux/Herdr resources, conversation/workstream linkage, safe
focus/relaunch, no identity creation, no unrelated tmux loss, unknown state
visible.

### 10.6 Session/UI/privacy

Exact JSONL and branch, continuity/task entries, tool schemas/results, approval
card scope, plain error six-part structure, Inspector/footer rendering,
redaction, no hidden reasoning/prompts/capabilities/credentials/raw sensitive
output.

## 11. Security/adversarial corpus

Every applicable parser/semantic API receives:

- unknown/missing/duplicate/oversized fields and malformed UTF-8/JSON/JSONL;
- path traversal, NUL, relative/absolute confusion, symlink and parent swap;
- Git ref/option/config/hook/pager/diff/textconv/remote injection;
- SQL/format/command injection strings;
- wrong project, copied repo, moved repo, shared worktree, malicious nested repo;
- stale/replayed/cross-action/cross-project capability and authorization;
- secret-like fields nested in technical details/evidence;
- forged Docker labels, tmux titles, Herdr labels, route/session headers;
- PID reuse and inaccessible process/container/backend observation;
- unknown/newer schema/protocol/manifest/extension entry;
- dirty/conflicted/submodule/symlink/special/large file capture;
- interrupted install/migration/rollback and tampered manifest/artifact.

The expected result is a typed refusal or explicit unknown/needs-attention, with
no authority broadening or unproven “unchanged” claim.

## 12. Evidence artifacts

Each tier writes a canonical `system-acceptance-evidence.v1` bundle outside the
repository under a secure fixture/evidence root. It includes:

```text
tier and scenario/action IDs
source tree/dirty digest
staged and installed build IDs
Pi/package/image/controller/schema versions
host/backend capability declaration
exact commands/tool calls and exits
assertion outcomes and artifact paths/digests
before/after summaries
fault/race seed
PASS/FAIL/STOP and reason
remaining uncertainty
confirmation of no remote/live action
```

Secrets, raw prompts, hidden reasoning, credentials, and unbounded logs are not
included. Detailed sensitive logs use separate opt-in debug retention.

## 13. Target commands after their owning slices implement them

These commands are specifications and are now paired with C9 evidence where
implemented. Source/process runners return structured per-action evidence;
staged, Docker, and presentation runners return STOP/77 until their exact
prerequisites are available. A 77 result remains STOP and cannot become a GO.

```bash
# Contract and action coverage
bash tests/system/run-contract.sh

# Existing and new deterministic components
bash tests/system/run-component.sh

# Real launchers/Pi with disposable fake backends
bash tests/system/run-process-fixture.sh

# Exact staged generation and real loaded extensions/packages
bash tests/system/run-staged-installed.sh

# Real Docker runtime and image/artifact rollback
bash tests/system/run-docker.sh

# Real configured presentation backends
bash tests/system/run-presentation.sh

# Aggregate non-live gates
bash tests/system/run-source-gate.sh
bash tests/system/run-staging-gate.sh

# Existing compatibility gates remain until absorbed and compared
bash tests/run-candidate-tests.sh
bash tests/run-control-plane-candidate-tests.sh

git diff --check
```

The existing candidate runner may become a wrapper over the new staging gate
only after output/evidence and exit-code compatibility are tested. It must not
keep parallel untraced suites indefinitely.

## 14. Acceptance standard

A complete non-live acceptance report must say, separately:

- which action IDs and tiers passed;
- which configured platform/backend capabilities were present;
- whether Docker and presentation real-backend tiers ran;
- which model evaluations passed/failed/varied;
- source/staged/installed build identity;
- exact unresolved migration decisions;
- whether any live host/canary/remote action occurred;
- why Phase 11D remains blocked or the exact separate authorization that ran it.

A test count alone is never a full-system completion claim.
