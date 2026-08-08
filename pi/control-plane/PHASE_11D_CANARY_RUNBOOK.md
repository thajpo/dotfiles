# Phase 11D Canary Runbook (reviewed, not executed)

**Status:** `BLOCKED_AWAITING_CANARY_SELECTION`

**Execution authorization:** `NOT_GRANTED`

This is a static, display-only runbook. It is not an activation script. No
command in this document has been executed. Phase 11D, canary, rollout,
publication, cleanup, migration cutover, deployment, and activation require a
new explicit user request in `pi-host` after the gates below are independently
accepted. General rollout is prohibited by this document.

## 1. Human decision and exact canary scope

The operator must supply every value below. The runbook must not infer a
project, conversation, mapping, duplicate resolution, dirty-state disposition,
MLRE choice, or retention policy.

- `canaryDecision`: `CANARY_SELECTION_REQUIRED`
- `canaryProjectId`: `<CANARY_PROJECT_ID>`
- `canaryWorkingCopyId`: `<CANARY_WORKING_COPY_ID>`
- `canaryConversationIds`: `<CANARY_CONVERSATION_IDS>`
- `canaryRunIds`: `<CANARY_RUN_IDS>`
- `duplicateResolutionId`: `<DUPLICATE_RESOLUTION_ID>`
- `rebindResolutionId`: `<REBIND_RESOLUTION_ID>`
- `dirtyDispositionId`: `<DIRTY_DISPOSITION_ID>`
- `adoptionResolutionId`: `<ADOPTION_RESOLUTION_ID>`
- `mlreDecisionId`: `<MLRE_DECISION_ID>`
- `publicationDecisionId`: `<PUBLICATION_DECISION_ID>`
- `cleanupDecisionId`: `<CLEANUP_DECISION_ID>`

If any value is unfilled, the only valid outcome is STOP with
`BLOCKED_AWAITING_CANARY_SELECTION`.

## 2. Immutable source, build, schema, and image bindings

These values are captured from independently verified artifacts; none may be
chosen from a live process or from an unreviewed working tree.

- source commit: `<SOURCE_COMMIT_OID>`
- source tree: `<SOURCE_TREE_OID>`
- source manifest digest: `<SOURCE_MANIFEST_DIGEST>`
- controller build ID: `<CONTROLLER_BUILD_ID>`
- staged build manifest digest: `<STAGED_BUILD_MANIFEST_DIGEST>`
- installed build ID: `<INSTALLED_BUILD_ID>`
- schema version: `7`
- schema migration checksum: `<SCHEMA_V7_CHECKSUM>`
- Pi version: `0.83.0`
- Docker image reference: `<PINNED_IMAGE_REFERENCE>`
- Docker image ID/digest: `<PINNED_IMAGE_ID_OR_DIGEST>`
- runtime specification digest: `<RUNTIME_SPEC_DIGEST>`
- loaded-resource attestation digest: `<LOADED_RESOURCE_ATTESTATION_DIGEST>`
- package-lock digest: `<PACKAGE_LOCK_DIGEST>`

The source, staged, installed, and image identities are separate evidence
gates. A source-only pass cannot be promoted to a staging or canary GO.

## 3. Controller and migration identity

- controller DB path: `<CONTROLLER_DB_PATH>`
- activation latch path: `<ACTIVATION_LATCH_PATH>`
- host policy path and digest: `<HOST_POLICY_PATH>` / `<HOST_POLICY_DIGEST>`
- migration ID: `<MIGRATION_ID>`
- migration operation ID: `<MIGRATION_OPERATION_ID>`
- inventory manifest ID/digest: `<INVENTORY_MANIFEST_ID>` / `<INVENTORY_MANIFEST_DIGEST>`
- resolution plan ID/digest: `<RESOLUTION_PLAN_ID>` / `<RESOLUTION_PLAN_DIGEST>`
- shadow import ID: `<SHADOW_IMPORT_ID>`
- backup manifest ID/digest: `<BACKUP_MANIFEST_ID>` / `<BACKUP_MANIFEST_DIGEST>`
- rollback proof ID/digest: `<ROLLBACK_PROOF_ID>` / `<ROLLBACK_PROOF_DIGEST>`

SQLite remains lifecycle authority, Git remains source authority, Pi JSONL
remains conversation authority, and host policy remains trust authority. The
latch is only a fail-closed boot projection; it is never a lifecycle authority.

## 4. Required capabilities and gate matrix

Before any human-authorized execution request, attach immutable evidence for:

| Gate | Required evidence | Missing/failed result |
|---|---|---|
| Component | C0a/C0b and C1–C8 component suites | STOP; no canary |
| Harness integration | C9 per-action, journey, fault, and refusal evidence | STOP; no canary |
| Migration-ready | typed inventory, exact resolution, shadow reconciliation, backup | STOP; no cutover |
| Staging | source/stage/installed equality, loaded resources, two installed journeys | STOP/77 |
| Docker | image, UID/GID, mounts, labels, attestation, stop, rollback | STOP/77 |
| Presentation | required tmux and target-profile Herdr evidence | STOP/77 |
| Canary authorization | this selected project plus explicit user authorization | BLOCKED |
| Rollout authorization | separate later reviewed decision | PROHIBITED |

Required capability declarations:

- Docker daemon and exact labeled image: `<DOCKER_CAPABILITY_EVIDENCE>`
- pinned Pi executable and version: `<PI_CAPABILITY_EVIDENCE>`
- tmux backend and unrelated-session preservation: `<TMUX_CAPABILITY_EVIDENCE>`
- Herdr target-profile support, if configured: `<HERDR_CAPABILITY_EVIDENCE>`
- process/PID start-identity observation: `<PROCESS_CAPABILITY_EVIDENCE>`
- host policy and `pi-host` authorization: `<HOST_AUTHORIZATION_EVIDENCE>`

## 5. Quiescence and pre-state capture (display-only)

Quiescence is a human-operated precondition, not an automatic cleanup action.
No process is adopted, killed broadly, or silently removed.

The operator must display and review the following exact commands after filling
all placeholders; the commands below are not executable from this document:

```text
DISPLAY ONLY: inspect managed process/PID/start identities for <CANARY_PROJECT_ID>
DISPLAY ONLY: inspect labeled Docker resources for <CANARY_PROJECT_ID>
DISPLAY ONLY: inspect managed tmux sessions and unrelated-session preservation
DISPLAY ONLY: inspect Herdr named session <HERDR_SESSION_NAME>
DISPLAY ONLY: verify no active writer, lease, or run for <CANARY_WORKING_COPY_ID>
DISPLAY ONLY: verify Git worktree/index/ref status and dirty fingerprint
DISPLAY ONLY: verify Pi JSONL session headers for <CANARY_CONVERSATION_IDS>
DISPLAY ONLY: verify controller DB schema/resource/event versions
DISPLAY ONLY: verify host policy digest <HOST_POLICY_DIGEST>
```

Capture before-state evidence outside the repository:

- SQLite database, WAL/SHM, resource versions, events, leases, and operations;
- Git refs, OIDs, index, worktree status, object format, and rollback refs;
- filesystem paths, modes, symlinks, hashes, and installed package roots;
- process PID/start identity/ancestry and Docker labels/image IDs;
- tmux/Herdr presentation state and unrelated-resource preservation;
- Pi JSONL headers/entries, task packet digest, and privacy-bounded UI state.

## 6. Backup and restore proof

The backup is immutable and independently hashed before any activation request.

- exact backup root: `<BACKUP_ROOT>`
- backup manifest: `<BACKUP_MANIFEST_ID>`
- backup digest: `<BACKUP_MANIFEST_DIGEST>`
- controller DB backup and WAL handling: `<DB_BACKUP_PROCEDURE_ID>`
- Git ref/worktree backup: `<GIT_BACKUP_PROCEDURE_ID>`
- JSONL/session backup: `<SESSION_BACKUP_PROCEDURE_ID>`
- package/config/image backup: `<INSTALL_BACKUP_PROCEDURE_ID>`
- restore verifier: `<RESTORE_VERIFIER_ID>`

A restore that cannot prove exact bytes, modes, symlink targets, image identity,
new recovery DB/ref/worktree/evidence preservation, and post-restore authority
is FAIL/attention, never success. Rollback never deletes controller recovery
data or newly created refs/worktrees/evidence.

## 7. Staged installation and latch transition (not executed)

The following are displayed steps only. They require a separate explicit
execution authorization and a selected canary.

```text
DISPLAY ONLY: verify source/staged/installed build IDs and manifest digests
DISPLAY ONLY: verify exact first-party package trees and no legacy co-load
DISPLAY ONLY: verify loaded controller, extension, and package resource roots
DISPLAY ONLY: verify Docker/runtime/presentation prerequisites and STOP rules
DISPLAY ONLY: verify backup <BACKUP_MANIFEST_ID> against <BACKUP_MANIFEST_DIGEST>
DISPLAY ONLY: record current latch projection and SQLite lifecycle state
DISPLAY ONLY: request explicit host authorization for canary-only latch transition
DISPLAY ONLY: transition legacy -> shadow -> controller only with fail-closed validation
DISPLAY ONLY: reject controller-to-legacy fallback and dual writers
```

The activation latch must be written atomically with user-owned permissions,
validated on boot, and ignored if malformed, stale, unauthorized, or mismatched
to the selected source/build/schema/project. SQLite remains the lifecycle
writer and every controller operation uses CAS/lease/epoch checks.

## 8. Canary launch and walking-skeleton journey (not executed)

The selected canary must run the exact installed launcher and extension paths;
no direct database row seeding is allowed after bootstrap.

- launch mode: `<CANARY_MODE>`
- session selection: `<CANARY_SESSION_SELECTION>`
- worktree selection: `<CANARY_WORKTREE_SELECTION>`
- runtime/manifest/lease IDs: `<RUN_MANIFEST_ID>` / `<WRITER_LEASE_ID>`
- launch evidence destination: `<CANARY_EVIDENCE_ROOT>`

Displayed action sequence:

```text
DISPLAY ONLY: start selected canary root/session through the reviewed launcher
DISPLAY ONLY: verify launch.resolve project/worktree/session/build bindings
DISPLAY ONLY: verify writer lease/epoch before writable runtime access
DISPLAY ONLY: execute JOURNEY-01 through the exact installed semantic tools
DISPLAY ONLY: exercise parent/secretary/workstream status and attention paths
DISPLAY ONLY: exercise child/review/integration proposal paths without implicit apply
DISPLAY ONLY: verify user-visible approvals and controller event/resource versions
DISPLAY ONLY: stop and capture post-journey evidence before any next action
```

No publication, cleanup, general migration, or rollout is implied by a
successful walking skeleton.

## 9. Fault injection and stop criteria (not executed)

Fault IDs must be selected from the accepted C9 evidence and bound to the exact
operation/build/project. The canary request must not invent a fault outcome.

- fault corpus: `<FAULT_CORPUS_ID>`
- fault seed: `<FAULT_SEED>`
- recovery evidence root: `<RECOVERY_EVIDENCE_ROOT>`

Displayed fault sequence:

```text
DISPLAY ONLY: crash before intent, after intent, before lock, before external effect
DISPLAY ONLY: crash after external effect, after observation, and after event
DISPLAY ONLY: exercise stale resource version, epoch, authorization, project, and path
DISPLAY ONLY: verify ambiguous state becomes attention and is never auto-reclaimed
DISPLAY ONLY: verify unknown process/container/presentation state is observed, not killed
DISPLAY ONLY: verify forbidden content and authority-bypass scans remain clean
```

Immediate STOP conditions include: missing/changed source or build identity,
invalid latch, duplicate or dirty unresolved mapping, unknown writer/process,
lock or epoch mismatch, ambiguous external effect, missing backup, co-load,
network/remote mutation, unexpected Git/ref/index change, privacy violation,
failed evidence write, failed rollback proof, or any command requiring broad
kill/cleanup/adoption.

## 10. Evidence and post-state diff

Every action, journey, fault, and gate must produce immutable evidence outside
the repository containing:

- action/scenario/journey/fault IDs and contract clauses;
- source, staged, installed, controller, schema, image, and fixture IDs;
- exact command/tool request, authorization, expected/actual exit;
- before/after SQLite, Git, filesystem, process, runtime, presentation, and
  session/UI/privacy summaries with digests;
- resource versions/events/leases and ambiguity/attention classification;
- failure, retry, rollback, and retention result;
- explicit `noLiveAction`/remote-mutation declaration.

Post-state must be independently compared to the pre-state for every namespace.
Any unexpected difference is FAIL/attention. Display-only post-state checks:

```text
DISPLAY ONLY: compare controller DB/resource/event/lease versions
DISPLAY ONLY: compare Git refs/OIDs/index/worktree status and rollback refs
DISPLAY ONLY: compare files/modes/symlinks/package roots/image identity
DISPLAY ONLY: compare process/container/PID/start identity and presentation
DISPLAY ONLY: compare Pi JSONL/session/task-packet/privacy-bounded state
DISPLAY ONLY: verify new recovery DB/refs/worktrees/evidence were preserved
DISPLAY ONLY: archive immutable evidence outside the repository
```

## 11. Rollback and completion decision

Rollback is selected on any STOP criterion or explicit operator request. It is
not an automatic fallback and never uses a legacy dual writer.

```text
DISPLAY ONLY: quiesce and verify managed writers are stopped
DISPLAY ONLY: capture ambiguous state and retain all evidence
DISPLAY ONLY: restore the exact verified prior installed/config/image generation
DISPLAY ONLY: restore the prior latch projection only after writer quiescence
DISPLAY ONLY: verify controller DB, new refs, worktrees, and evidence remain
DISPLAY ONLY: run the independent post-restore diff and authority checks
DISPLAY ONLY: record rollback result and require human acceptance before retry
```

Canary completion requires all selected journeys, faults, post-state diffs, and
rollback evidence to pass. A canary pass does not authorize publication,
cleanup, general migration, or rollout.

## 12. General rollout prohibition and human decisions

`Phase 11D` is not authorized by this document. General rollout requires a
separate reviewed plan and explicit user authorization after a canary. The
following remain human decisions:

- exact canary project/conversations and scope;
- duplicate, rebind, dirty, adoption, and divergent mapping resolutions;
- MLRE `970ea8e` versus `b296516` handling;
- publication and cleanup authorization/timing;
- Herdr target-profile selection;
- Phase 11D execution authorization;
- any later general-rollout authorization.

Until those decisions and all gates are recorded, the only valid runbook
outcome is `BLOCKED_AWAITING_CANARY_SELECTION`.
