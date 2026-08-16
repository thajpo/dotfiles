# Pi Harness Program

## Program Status

- Status owner: this section only.
- Recovery program: `PI_HARNESS_RECOVERY_PLAN.md` is the active program. The
  historical phase statuses below remain the recorded evidence of the original
  program; the recovery program re-runs acceptance against the repaired
  product and supersedes them on re-release. Slices 1-8 have landed
  (reconciled contracts, completed rename, one-build surface, secretary and
  personal grids with independent active sets, real personal role on the
  primary checkout, async headless subagents with detached launchers, workers,
  supervisor messages, channel submission/review/integration analysis, and
  observability projections), and Slice 9 legacy removal is complete (the
  unreachable legacy controller family, launchers, scripts, extensions, tests,
  installer, and historical records are gone; tmux-resurrect and the
  candidate runners reference only current surfaces). Installed-tier journeys
  for the original release actions all pass (HA-001..HA-018 with installed
  evidence on one deterministic build); the release verifier remains blocked
  only by the planned repair actions pending their own installed journeys.
  Live-state cleanup and re-activation remain pending final acceptance.
- Status as of: 2026-08-11, source `29688e6d8e9a075d80992257d7ff87ff3fbbcd23`, branch `master`.
- Active work: P1-P12 gates pass; the P11 release pipeline passes against deterministic build `build_2faecfee6b4280149422721827640bb4` — all 18 HA actions covered by installed evidence, OpenCode protected, harness fixes (provider credential provisioning, buffered channel framing, parallel tool requests, channel frame bounds, CLI field adaptation, idempotent build registration) landed and re-verified by the full release pipeline.
- Release: `release-passed`. The explicit controlling-TTY `bin/pi-activate` approval was recorded by the user on the real install on 2026-08-11; the live data root was switched to `build_2faecfee6b4280149422721827640bb4` with the prior generation preserved as a rollback generation.
- Activation: authorized. OpenCode unchanged throughout.

The only mechanical status values are:

| Value | Meaning |
|---|---|
| `not-started` | The phase has no accepted gate result. |
| `source-passed` | The phase source gate passed against one identified source tree. |
| `installed-passed` | The phase installed-product gate passed against one identified staged build. |
| `cumulative-passed` | The phase and every dependency passed their required gates against compatible evidence. |
| `release-passed` | P12 alone verified the complete release candidate and recorded final human approval. |

Current mechanical state:

| Phase | Mechanical status | Activity |
|---|---|---|
| P0 | `source-passed` | P0 source gate passed on 2026-08-09; no installed, cumulative, release, or activation claim. |
| P1 | `cumulative-passed` | Artifact gates and two installed journeys reran on 2026-08-09 against identical `build_26ae3dc4a2be58146d95cc13f635c638`; this remains artifact/installer evidence and makes no release or activation claim. |
| P2 | `cumulative-passed` | Schema/API/manifest gates and two installed journeys reran on 2026-08-09 against `build_26ae3dc4a2be58146d95cc13f635c638`; fresh-state and exact-manifest authority remain cumulative. |
| P3 | `cumulative-passed` | Exact staged host supervisor, controller-created sessions, inherited socketpair startup attestation, sanitized child environment, role resource/tool binding, negative channel/process tests, and two deterministic installed host-role journeys reran on 2026-08-09 against `build_26ae3dc4a2be58146d95cc13f635c638`. |
| P4 | `cumulative-passed` | Descriptor-relative scoped reads and fixed Git queries, per-call run/channel freshness, durable secretary resume, terminal investigator lifecycle, and detached exact-revision reviewer behavior after branch movement reran through the twice-installed gate on 2026-08-09. Installed evidence is `/tmp/tmp.ntLzRcPEjN/run-{1,2}.json`. |
| P5 | `cumulative-passed` | Broker-only writer tools, exact local image/runtime spec, race-safe writer claim, regular-file Git mask, per-call fencing, final staged host Pi writer journey, second-writer rejection, PID split, expected delta, isolation negatives, and exact cleanup reran against `build_43323fee4eba05e5ce759b317977234c` on 2026-08-09. Docker evidence is `/tmp/p678-evidence/v2/pi-p5-run_caf537936761853955b657ff1f9cf6b6.json`. No P10 exhaustive recovery claim. |
| P6 | `cumulative-passed` | Installed message/command/dependency extensions used the inherited authenticated channel; role grants and writer epochs fenced every mutation; `bin/pi-authorize` proved controlling-TTY approve/reject, one-use replay refusal, stale and restart invalidation; strict host and no-contact bridge operations proved success/failure/timeout/removal. Deterministic external test-harness caches materialized exact npm and hash-pinned Python environments with scripts disabled, immutable tree inputs, read-only cache mounts, private output identities/tree digests, exact installed versions, and no remote contact; unsupported manager, unlocked, range-only, tampered-cache, unmarked config-ID-only, and no-cache inputs failed closed. Installed evidence is `/tmp/p678-evidence/v2/pi-p6-conv_3138fdfbf86762015f2f5ce49f655a1d.json` against `build_43323fee4eba05e5ce759b317977234c`. No P8, release, or activation claim. |
| P7 | `cumulative-passed` | Installed personal and workstream journeys ran on 2026-08-09 against `build_43323fee4eba05e5ce759b317977234c`: recoverable idempotent workstream creation saga, controller-owned distinct worktree identities, three controller-created child requests (one personal investigator plus workstream investigator and reviewer) pinned to immutable snapshot commits with distinct refs and read-only review worktrees, success terminal records with distinct digests, writer messages bound to their workstream, closed managed containers, and no credential leak. Installed evidence is `/tmp/p7-evidence/v2/pi-p7-ha005-ws_41065cf96ff2c2f71288635a3ecd693f.json` and `/tmp/p7-evidence/v2/pi-p7-ha018-conv_1e0a311c2d0dba94f4d3f6cb009d91aa.json`. No P8, release, or activation claim. |
| P8 | `installed-passed` | `_reviewer_binding` accepts only `observed_state='running'` or cleanly-stopped; `_reviewer_run` test helper follows state-machine transitions; staleness test uses real `submit_change_revision`; `dependency_review_digest` column added to reviews schema (both Pi + legacy) and bound via `request_review`; writer-container runs rejected; submitted reviews create no automatic integration authority; 18 Pi receipt tests (negative: wrong role, wrong authority, cancelled state, actor/run mismatch, active submit, dependency digest, integration-authority negative); evidence includes real DB receipt fields with tipOid cross-reference against assignment. Staged build `build_add8b9db4929eacf60a64a983905ec09` completed two identical runs; evidence at `/tmp/pi-p8-evidence/`. Control-plane suite 309/309 (1 pre-existing `test_runtime_spec` failure). No release or activation claim. |
| P9 | `installed-passed` | Integration analyze→authorize→integrate pipeline exercised through staged `bin/pi-control` CLI: fast-forward strategy, target ref CAS-advanced, rollback ref created pointing to pre-integration OID, authorization consumed, change marked merged. Evidence at `/tmp/pi-p9-evidence/` (HA-009, `fast-forward-integrate` scenario, `staged-installed` tier). Staged build `build_194efd3e1859e21182c364d1e0519cb2`. No release or activation claim. |
| P10 | `installed-passed` | Installed robustness journey (`tests/system/fixtures/installed-p10.py`, `run-p10-installed.sh`) proves: SIGINT interruption terminalizes investigator (conversation archived, run failed); secretary resume preserves contiguous session + active conversation across controller restarts; integration killed mid-flight recovers deterministically on retry (target advanced, rollback ref present, no half-updated ref); unrelated tmux session survives; writer container killed mid-run with second writer refused and no managed containers remaining. Evidence at `/tmp/pi-p10-evidence/` (5 envelopes: HA-003 investigation-interrupt, HA-015 host-roles-installed, HA-016 secretary-scoped-read, HA-009 fast-forward-integrate, HA-005 workstream-create-and-run). No release or activation claim. |
| P11 | `installed-passed` | Full release pipeline (`tests/system/run-p11-release.sh`) ran every installed journey (staged-installed, P5 docker writer, P6, P7, P9, P10, user-scenario journeys, P12) against one deterministic build `build_a38720edd263766e9384b8f254a66e27`. User-scenario journeys added: coding-resume (HA-004), integration-agent-conflict (HA-009), multi-project register-project (HA-001), review-exact-revision loop (HA-008), investigation-complete (HA-003/HA-017), plus dedicated envelopes for tty-approve-execute-replay-refuse (HA-011), command-request-without-approval (HA-007), message-post-reply-acknowledge (HA-006), locked-package-environment (HA-014), second-writer-refused (HA-004), subagent-isolation (HA-018), secretary-resume (HA-002), p2-controller-contract (HA-015). 24 of 25 declared scenarios now have installed evidence; the conflict journey exposed and fixed a real gap where controller-created integration-result changes could not receive review assignments. Release aggregator (`tests/system/p11_release_verify.py`) confirms all 18 HA actions have PASS installed evidence on that build; OpenCode guard confirms `~/.config/opencode` and `~/.opencode` unchanged and launchable throughout. `bin/pi-activate` + `activation_cli.py` implemented (TTY gate, test-fixture gate with resolved-path check), HA-012 flipped to `implemented-source`. Evidence at `/tmp/pi-p11-release-evidence/`. No release or activation claim. |
| P12 | `release-passed` | Activation mechanics proven: `activation_approval.py` one-use approvals bound to exact build+cutover plan (schema v3, `activation_approvals` table); `pi_install.activate()` extended with launch lock (flock), bounded smoke (`bin/pi-control schema status`), protected-surface verification (OpenCode dirs + unrelated tmux sessions), and fresh-state init; P12 journey (`tests/system/fixtures/installed-p12.py`) staged A→activated A→journey→staged B→activated over A (A preserved as `.rollback.*`)→rollback restored A with state intact, OpenCode + tmux protected. HA-012 (`final-activation-approved`, tier `activation`) and HA-013 (`rollback-preserves-new-state`, tier `rollback`) evidence at `/tmp/pi-p11-release-evidence/`. The explicit controlling-TTY `bin/pi-activate` approval was recorded by the user on the real install on 2026-08-11: the live data root switched to `build_2faecfee6b4280149422721827640bb4`, the prior generation preserved as `~/.local/share/pi-system.rollback.*`. Release, activation, live-path, tmux, integration, and rollback actions all performed on production with the old generation preserved for rollback. |

The existing narrow staged host-read fixture is foundation evidence only. It
has exercised an installed host Pi process, a deterministic local provider, an
allowed scoped-read tool, and rejection of a write tool. It does not prove a
writer path, Docker isolation, cumulative acceptance, release readiness, or
activation safety.

## Document Authority

This document owns program scope, accepted decisions, stable invariant IDs,
phase dependencies, gates, and the sole status table. Contracts do not report
program status.

| Document | Sole normative surface |
|---|---|
| `pi/control-plane/PRODUCT_CONTRACT.md` | User-visible roles, capabilities, and release scope |
| `pi/control-plane/STATE_CONTRACT.md` | State identity, authority, freshness, and transitions |
| `pi/control-plane/EXECUTION_CONTRACT.md` | Process topology, tool execution, manifests, Docker, packages, and approvals |
| `pi/control-plane/CHANGE_INTEGRATION_CONTRACT.md` | Git ownership, submission, review, and local integration |
| `pi/control-plane/OBSERVABILITY_CONTINUITY_CONTRACT.md` | Evidence envelope, attention, and restart continuity |
| `pi/control-plane/SYSTEM_INTEGRATION_TEST_PLAN.md` | Scenario and tier coverage |
| `pi/control-plane/ACCEPTANCE_PLAN.md` | Gate evaluation and release-verifier behavior |
| `pi/control-plane/CUTOVER_AND_ROLLBACK.md` | Atomic cutover and rollback transaction |
| `pi/control-plane/PRE_ACTIVATION_ACCEPTANCE_RUNBOOK.md` | Human pre-activation procedure |

If documents conflict, this plan decides program sequencing and status; the
surface-owning contract decides behavior. A conflict is a source-gate failure,
not permission to choose either text.

## Target Topology

Release 1 is Linux-only.

```text
user TTY
  | starts/focuses and separately approves sensitive requests
  v
host conversational Pi / model / session / TUI process
  | inherited authenticated controller channel
  v
host controller
  |-- fresh Pi controller store and CLI family
  |-- scoped host read adapters
  |-- controller-owned Git objects, commits, and refs
  `-- scripts/pi_control/docker_runtime.py (sole Docker lifecycle owner)
        |
        v
      one controller-created, controller-owned writer container
      (read/write/edit/shell tool execution only; no Pi/model/session/TUI)
```

`pi-sandbox-control` is a broker client in the target design. It does not own
Docker lifecycle. Pi itself never runs inside the writer container.

Every run has one identity document exported as `PI_RUNTIME_MANIFEST`. No
`PI_TASK_*` compatibility behavior is permitted.

## Trust Boundary

| Component | Trust and authority |
|---|---|
| User at controlling TTY | Final authority for sensitive host/network requests and activation |
| Host controller | Trusted lifecycle, role mapping, store, Git, capability, and Docker authority |
| Host Pi/model process | Conversational principal; may invoke only controller-granted tools over its inherited channel |
| Host read adapter | Enforces controller-derived project, revision, and path scope on every call |
| Writer container | Untrusted execution area for one assignment; no host credentials, Docker socket, writable Git metadata, push, or unrelated paths |
| Tmux/TUI | Presentation only; no identity, role, approval, or liveness authority |
| Historical Pi state | Preserved external data with no Pi controller authority and no read/adoption path |

The model cannot select its project, role, authority, container, manifest, or
approval channel. Prompt text and environment claims are not authority.

## Invariant Index

| ID | Invariant | Owning contract |
|---|---|---|
| GF-PLAT-001 | Release 1 runs on Linux only. | Execution |
| GF-TOPO-001 | Every conversational Pi, model, session, and TUI process runs on the host. | Execution |
| GF-TOOL-001 | Writer read/write/edit/shell execution occurs only in one controller-created and controller-owned container. | Execution |
| GF-DOCKER-001 | `scripts/pi_control/docker_runtime.py` is the intended sole Docker lifecycle owner. | Execution |
| GF-MANIFEST-001 | One `PI_RUNTIME_MANIFEST` supplies run identity; task-prefixed runtime compatibility is absent. | Execution |
| GF-STATE-001 | Pi starts fresh and never reads or adopts historical Pi state or chats. | State |
| GF-REACH-001 | The Pi controller store/CLI family is canonical; retired runtime/controller families are unreachable from release actions. | State |
| GF-CHANNEL-001 | Host Pi uses an inherited authenticated controller channel; controller state derives role and authority. | Execution |
| GF-DEPS-001 | Release dependencies require npm `package-lock.json` and Python `uv.lock` plus hash-pinned requirements; unsupported managers fail closed. | Execution |
| GF-APPROVAL-001 | Sensitive host/network approval uses a separate TTY-bound host CLI absent from model-visible tools. | Execution |
| GF-GIT-001 | The controller alone creates commits and moves refs; writer containers have no writable Git metadata or push path. | Change Integration |
| GF-CUTOVER-001 | OpenCode remains unchanged until explicit final activation approval. | Cutover |

Removing or weakening an invariant requires a new accepted decision, an
updated invariant ID or explicit supersession record, and source-gate changes.

## Role And Authority Matrix

| Role | Conversation process | Tool execution | Authority source | Persistence |
|---|---|---|---|---|
| Secretary | Host Pi | Scoped host read/web/controller tools | Registered project and controller role record | Durable |
| Investigator | Host Pi | Scoped host read/web tools | Exact controller assignment | Temporary; result durable |
| Reviewer | Host Pi | Scoped exact-revision read tools | Exact controller review assignment | Temporary by default |
| Personal writer | Host Pi | Writer tools brokered to one container | Exact run and writer generation | Durable |
| Workstream writer | Host Pi | Writer tools brokered to one container | Exact workstream/run and writer generation | Durable |
| Integration writer | Host Pi | Writer tools brokered to one container | Exact immutable input set and run | Durable |
| Controller | Host non-model process | Store, Git, process, and Docker adapters | Local controller policy | Durable |
| Sensitive approval CLI | Host non-model TTY process | Approve/reject one exact request | Controlling TTY and request digest | One use |

## P0 Change Boundary

Changed surfaces are limited to this plan, the ten documents indexed by
`pi/control-plane/README.md`, and their document/action/evidence catalog tests
under `tests/system/`. Updating the narrow installed fixture is allowed only to
keep its evidence envelope truthful.

Unchanged surfaces in P0:

- Product implementation and runtime behavior.
- Docker, tmux, launcher, install, and live host state.
- Existing historical Pi state and chats.
- Existing Git commits, refs, worktrees, remotes, and production services.
- OpenCode configuration, installation, and activation.

Source files already present for later phases are observations only. P0 does
not require those future implementations to exist and does not grant them a
phase result.

## Dependency DAG And Gates

For P1-P12, every dependency must have `cumulative-passed` unless the row states
the P0 source-only exception. A runner returning STOP, SKIP, missing evidence,
or unobservable behavior does not satisfy an exit gate.

| Phase | Deliverable | Dependencies and exact entry gate | Exact exit gate |
|---|---|---|---|
| P0 | Canonical contracts and truthful catalogs | Accepted decisions in this document; no implementation prerequisite | Run the four P0 commands below with zero failures; then record `source-passed` |
| P1 | One immutable artifact and installer | P0 `source-passed` | One builder packages Pi core, controller, first-party resources, role profiles, and launchers; production staging and tests consume byte-identical artifacts; cumulative gate passes |
| P2 | Fresh schema, versioned controller API, and canonical manifest | P0 `source-passed` | Fresh-root-only identity, role-derived authority, legal transitions, controller-derived sessions, and unchanged old-state sentinels pass through source and installed CLI gates |
| P3 | Exact host Pi supervisor | P1 and P2 `cumulative-passed` | Final staged launchers start and attest the controller-selected host Pi executable, argv, session, role, resources, tools, environment, and inherited authenticated channel |
| P4 | Secretary, investigator, reviewer, and scoped host reads | P2 and P3 `cumulative-passed` | Installed host-role scenarios prove host residency, path/revision scope, forbidden mutation tools, child isolation, and continuity rules |
| P5 | Controller-owned writer tool plane | P2 and P3 `cumulative-passed` | Real installed Pi brokers read/write/edit/shell into one attested container; sole Docker ownership, fencing, no container Pi, no external shell network, and no writable Git metadata are observed |
| P6 | Messages, commands, network, approvals, and package adapters | P2 and P3 `cumulative-passed` | Installed extensions use the canonical API; stale writers and replays fail; separate TTY approval works; Linux npm/Python adapters reproduce locked inputs and reject unsupported managers |
| P7 | Personal/workstream lifecycle and controller-created subagents | P4, P5, and P6 `cumulative-passed` | Real personal and workstream journeys, isolated child runs, immutable read snapshots, private package environments, and recoverable creation sagas pass |
| P8 | Submission, trusted tests, and independent review | P7 `cumulative-passed` | Controller-owned commits and immutable change refs, exact test/package receipts, reviewer independence, and all staleness gates pass |
| P9 | Fast-forward and real integration-agent workflows | P5, P6, and P8 `cumulative-passed` | Safe exact target update, target-movement refusal, conflict integration writer, combined tests, independent review, and no remote mutation pass |
| P10 | Restart, cancellation, reconciliation, faults, and tmux presentation | P3 through P9 `cumulative-passed` | Failpoint matrix proves one writer, preserved ambiguity, exact process/container recovery, durable session continuity, temporary-run interruption, and unrelated tmux preservation |
| P11 | Complete installed journey, strict evidence, and OpenCode comparison | P1 through P10 `cumulative-passed` | Every release action has linked installed evidence; one artifact completes the journey twice; no planned/STOP/SKIP/unobservable action remains; OpenCode and human-use gates pass |
| P12 | Atomic cutover, exact-generation rollback, and activation decision | P11 `cumulative-passed` | Launch lock, exact generation switch, disposable smoke, fresh production state, tested rollback, preserved protected surfaces, and explicit final user approval record `release-passed` |

The P0 source gate is exactly:

```bash
python3 tests/system/validate_plan_docs.py
python3 -m unittest tests.system.test_pi_docs tests.system.test_action_manifest tests.system.test_evidence
bash tests/system/run-source-gate.sh
git diff --check
```

P0 must remain below `source-passed` until all four commands succeed against the
same worktree. P0 has no installed or release claim.

## Acceptance And Evidence Rules

- `tests/system/action-manifest.v1.json` is the release-action catalog and must
  validate against its explicitly linked schema.
- Every evidence envelope binds catalog action IDs, one catalog scenario, and
  one declared evidence tier. Unknown or mismatched values fail validation.
- Catalog status `planned` is legal during implementation. The release verifier
  must reject evidence for any planned or excluded action.
- Installed evidence states whether an installed product action was observed,
  whether a production mutation occurred, and whether a remote provider was
  contacted. A blanket no-live-action assertion is forbidden.
- Source evidence never implies installed evidence. Installed evidence never
  implies cumulative evidence. No phase result implies release approval.
- PASS requires observable assertions, before/after state where relevant,
  command results, exact source/build identity, and immutable evidence outside
  the repository. STOP and SKIP are not PASS.
- P0 validation tests may use synthetic document/catalog mutations. They must
  not require future product modules, Docker, launchers, tmux, installation,
  live files, network, or remote actions.

## Decision Ledger

| Decision | Accepted release-1 rule |
|---|---|
| D-001 | Linux only. |
| D-002 | All conversational Pi/model/session/TUI processes stay on the host. |
| D-003 | Writer tools execute only in one controller-created and controller-owned container; Pi is never container-resident. |
| D-004 | `scripts/pi_control/docker_runtime.py` is the intended sole Docker lifecycle owner; `pi-sandbox-control` becomes a broker client. |
| D-005 | One `PI_RUNTIME_MANIFEST`; no task-prefixed compatibility. |
| D-006 | Start fresh; preserve but never read or adopt old state and chats. |
| D-007 | Pi controller store/CLI is canonical; retired runtime/controller families are excluded from release reachability. |
| D-008 | Host Pi inherits an authenticated controller channel; the controller derives role and authority. |
| D-009 | npm package-lock and Python uv.lock/hash-pinned requirements are release scope; unsupported managers fail closed. |
| D-010 | Sensitive host/network approval is a separate TTY-bound host CLI inaccessible to model-visible tools. |
| D-011 | Controller owns commits and refs; writer containers have no writable Git metadata or push. |
| D-012 | OpenCode remains unchanged until explicit final activation approval. |

## Release Reachability And Legacy Exclusion

The release graph begins at Pi harness launchers, enters the controller CLI and
store family, and reaches only cataloged controller modules and packages. The
release verifier derives reachability from the staged file manifest, launcher
catalog, loaded-extension catalog, configured-package catalog, and action
catalog. Uncataloged dynamic loads fail closed.

The following are excluded as release authorities and entrypoints:

- Earlier controller store, schema, client, and CLI families.
- Earlier runtime/workspace launch families and route-file identity.
- Historical secretary/root registries and old chat/session discovery.
- State import, adoption, reconciliation, project modes, and dual-write paths.
- Remote publish/deploy and automatic cleanup.

Historical files remain untouched on disk. Exclusion from release reachability
does not authorize deletion.

## Cutover Authority

Passing source, installed, cumulative, or rollback evidence does not activate
Pi and does not modify OpenCode. Only the user at the controlling TTY may grant
the final P12 activation approval after reviewing the pre-activation runbook
and exact release evidence. The approval binds one build ID and one cutover
plan, is one use, and is invalidated by any byte, gate, or plan change.
