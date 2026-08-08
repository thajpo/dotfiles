# Local change and integration contract

Status: **normative target; substantial change/review/integration component source exists, but complete personal/secretary launcher wiring and full-system acceptance remain incomplete**.

## 1. Purpose

This contract defines how implementation work from `pi-personal`, a secretary-
created workstream, an independent worker, or an integration workstream becomes
an immutable local change revision and how the secretary and user integrate it.

It uses standard Git commits, trees, refs, reviews, and compare-and-swap. It does
not create a proprietary parking format.

## 2. Conceptual model

A **change** is a durable local request to integrate source work into one target
ref. It is analogous to a pull request or Gerrit change.

A **revision** is one immutable submitted Git commit/tree for that change. It is
analogous to a patch set.

An **integration attempt** compares one exact revision with one exact target
commit under one explicit user authorization.

```text
working copy / branch
       │ submit
       v
change revision 1 ── review/evidence
       │ source evolves
       v
change revision 2 ── selected revision
       │
       ├── clean integration ──> target CAS update
       └── conflict ──────────> integration worktree ──> new revision
```

## 3. Change authority

- Git objects/refs are source-content authority.
- Controller metadata binds revision, target, provenance, evidence, and state.
- Source branch/worktree may continue changing after submission.
- Submitted revision never changes.
- Secretary coordinates integration but has no arbitrary Git shell.
- User explicitly authorizes final target mutation.
- Review evidence never authorizes integration by itself.

## 4. Ref namespace

Controller-owned local refs:

```text
refs/pi/changes/<change-id>/<revision>
refs/pi/integration/<integration-id>
refs/pi/rollback/<integration-id>
refs/pi/snapshots/<task-id>/<sequence>   # existing recoverable exploration use
```

Requirements:

- IDs validated before ref construction;
- refs created/updated/deleted only by controller Git adapter;
- every update uses expected old OID;
- change revision refs are immutable and therefore created only from absent;
- integration refs use expected old OID and operation journal;
- rollback refs are retained through acceptance/retention policy;
- no model-selectable arbitrary ref or target string bypasses structured
  validation;
- controller never deletes user refs or unrelated `pi/*` refs by prefix alone.

Regular human feature branches may remain the source. A controller change ref
preserves the submitted revision even if the feature branch advances or is
renamed.

## 5. Change lifecycle

```text
draft -> open -> merged
draft -> closed
open  -> closed
```

A **draft** is the durable conventional work record created before first
mutation. It has no submitted revision and is shown under working activity, not
in the integration queue. Submission of revision 1 changes it to open. This
avoids inventing a separate task-baseline resource while allowing work to span
multiple process runs.

A merged change records exact integration attempt and target result. A closed
change records reason and actor. Reopening creates either an explicit state
transition with resource-version CAS or a new change according to product
policy; it never silently clears integration history.

Conditions such as review requested, review accepted, target moved, textual
conflict, semantic overlap, checks failing, or needs-user are attributes/events,
not change lifecycle states.

## 6. Starting work and baseline

A controller-managed implementation cycle creates/updates one draft change and
records before first mutation:

```text
project and working-copy IDs
conversation/run and writer epoch
source branch/ref and HEAD
index tree/checksum
porcelain v2 status
tracked working-tree content fingerprint for dirty files
untracked path manifest and hashes within policy
ignored-file exclusion summary
target ref and target OID when known
bounded purpose/title
```

### 6.1 Secretary worktree default

A newly created secretary worktree starts clean at an exact target/base commit.
All non-ignored changes in that controller-owned worktree normally belong to the
workstream unless external mutation is observed.

### 6.2 Personal primary-checkout default

The primary checkout may contain pre-existing changes. Controller records them
as baseline rather than stashing, resetting, copying, or claiming ownership.
Submission defaults to files changed after baseline that the agent declares as
part of the change.

If manual/user edits or another tool make attribution ambiguous, the change
remains `draft`, no revision/ref is created, and the controller creates open
attention with kind `change-selection-required` plus event
`change.needs_selection`. `needs_selection` is a condition, not a change
lifecycle state.

## 7. Submit-change operation

Input schema:

```json
{
  "schemaVersion": 1,
  "projectId": "prj_...",
  "workingCopyId": "wc_...",
  "runId": "run_...",
  "writerEpoch": 4,
  "changeId": null,
  "title": "Fix child cancellation ownership",
  "summary": "...",
  "targetRef": "refs/heads/main",
  "pathPolicy": {
    "mode": "task-delta",
    "include": [],
    "exclude": [],
    "includeIgnored": []
  },
  "verification": [],
  "idempotencyKey": "..."
}
```

Preconditions:

- registered project and working copy agree with Git identity;
- caller owns current writer epoch or holds authorized read-only submission for
  an already immutable commit;
- no unresolved Git operation makes content ambiguous;
- target ref is an allowed local branch and exact target OID is observable;
- path policy resolves to bounded, repository-contained files;
- source content remains stable through capture;
- no secret/ignored file is included implicitly;
- change title/summary/evidence are bounded.

The operation records intent before Git side effects.

## 8. Capture modes

### 8.1 Branch-tip mode

Use when selected source content is already represented by a suitable immutable
commit and no omitted dirty changes belong to the submission.

Steps:

1. acquire project/read or working-copy lock as needed;
2. observe source branch tip and tree;
3. ensure target/base relationship is recorded, not assumed;
4. verify path/diff scope and object integrity;
5. create revision ref from absent with `git update-ref`;
6. verify ref and tree independently;
7. insert immutable revision and update current revision in one controller
   transaction with event;
8. leave source branch/worktree unchanged.

### 8.2 Temporary-index mode

Use for dirty or informal personal work.

Steps are the exact snapshot procedure in `EXECUTION_CONTRACT.md`, plus:

- base tree is the task baseline or explicitly selected base;
- include only task-owned/selected files;
- generated commit message identifies controller change/revision without
  pretending to be a user-authored product commit;
- author identity follows project policy; committer identifies local controller
  build in metadata/trailers if configured;
- create immutable revision ref;
- do not update source branch, HEAD, index, or worktree;
- re-observe selected source files and abort if changed during capture;
- store provenance and excluded-change summary.

### 8.3 Unsupported source states

Fail/ask rather than guess for:

- unresolved merge/rebase/cherry-pick;
- index stages 1/2/3;
- submodule state not covered by explicit policy;
- symlink target escape;
- special files;
- unbounded ignored/generated content;
- selected file changing during capture;
- ambiguous deletion/rename ownership;
- missing base object;
- object format disagreement.

## 9. Revision manifest

Stored in controller metadata and optionally exported as bounded canonical JSON:

```json
{
  "schemaVersion": 1,
  "changeId": "chg_...",
  "revision": 2,
  "projectId": "prj_...",
  "sourceWorkingCopyId": "wc_...",
  "sourceConversationId": "conv_...",
  "sourceRunId": "run_...",
  "targetRef": "refs/heads/main",
  "targetOidAtSubmission": "...",
  "baseOid": "...",
  "tipOid": "...",
  "treeOid": "...",
  "refName": "refs/pi/changes/chg_.../2",
  "captureMode": "temporary-index",
  "changedPaths": [],
  "diffstat": {},
  "verification": [],
  "excludedSourceState": {},
  "controllerBuildId": "...",
  "createdAt": "...",
  "manifestDigest": "sha256:..."
}
```

Raw prompts, hidden reasoning, credentials, and unbounded tool logs are not part
of the revision manifest.

## 10. Updating a change

A new submission for an existing open change:

- requires exact change resource version;
- creates revision `current_revision + 1`;
- creates a new immutable ref;
- records which revision it supersedes;
- does not delete old refs or reviews;
- invalidates "current revision" projections but not historical review truth;
- prompts if target changed intentionally.

A review of revision 1 remains a valid statement about revision 1 and is simply
not evidence for revision 2.

## 11. Secretary change queue

Minimum deterministic queue columns:

```text
change title
source (personal/workstream/integration)
target branch
current revision
submitted time
target unchanged/moved
textual mergeability result and freshness
verification summary
review evidence for current revision
known surface overlap
attention
```

The secretary MUST query current Git target before describing mergeability.
Cached analysis names exact source and target OIDs and age; stale analysis is
not displayed as current.

Unmanaged branches/worktrees may be inventoried, but they enter the queue only
through explicit submit/adopt operation.

## 12. Integration analysis

Analysis record binds:

```text
change ID and revision
tip/tree/base OIDs
target ref and currently observed target OID
merge base
textual conflict result
changed paths/symbols where available
other open changes with overlap
review/evidence summary
recommended strategy
analysis timestamp and tool/controller build IDs
```

### 12.1 Deterministic analysis

Use sanitized, side-effect-free Git operations in a temporary object/index
context. It MUST NOT touch target working tree or real index.

Determine:

- target contains candidate already;
- candidate contains target;
- fast-forward eligibility;
- common merge base;
- three-way merge-tree result;
- textual conflicting paths;
- rename/delete conflicts;
- submodule conflicts;
- changed-path overlap with other open changes;
- target movement since submission/review.

### 12.2 Semantic analysis

Run only when risk or user request warrants it. A fresh read-only agent receives:

- exact candidate revision;
- exact target revision;
- deterministic conflict/diff evidence;
- relevant project decisions and boundaries;
- focused questions;
- no unrelated secretary transcript.

It examines:

- API/schema/state-machine interactions;
- authority and transaction boundaries;
- numerical/concurrency behavior;
- tests that span both changes;
- merge ordering;
- behavior conflict without textual conflict.

The result is advisory evidence. The secretary synthesizes it and names
uncertainty.

## 13. Review contract

Review request binds exact `(change_id, revision, tip_oid, tree_oid, target_oid
or base_oid)`.

Review checkout:

- detached at exact revision;
- mechanically read-only for reviewer;
- in registered project and allowed working-copy root;
- clean and independently attested;
- own durable reviewer conversation when headful;
- no integration capability.

Verdicts:

- `accept`;
- `changes_requested`;
- `comment`.

An acceptance is evidence only. New revision or relevant target movement marks
its applicability stale in current queue projection. Historical receipt remains
immutable.

Project policy MAY require current acceptance for certain integration
strategies. This is an integration precondition, not automatic authorization.

## 14. Integration authorization

Authorization MUST bind:

```text
project ID
change ID and exact revision
target ref and expected current target OID
selected strategy
current user interaction/request ID
expiry
```

It is invalidated by:

- target movement;
- selected revision change;
- strategy change;
- expiry;
- project identity/policy change;
- cancellation;
- controller restart if token is process-scoped.

Generic affirmation or approval of review/workstream/cleanup is insufficient.

## 15. Integration strategies

### 15.1 Already contained

If target already contains submitted tip/tree with proven provenance, record
integration only after verifying the exact inclusion semantics and user intent.
Do not move refs unnecessarily.

### 15.2 Fast-forward

Allowed when:

- target current OID equals authorized expected OID;
- target is ancestor of candidate;
- project policy allows;
- required evidence is current;
- target working-copy/index handling is safe;
- project and target locks held;
- rollback ref created;
- `git update-ref target candidate expected` or equivalent safe checkout update
  succeeds;
- target and working-copy observation agree afterward.

### 15.3 Merge

MVP recommendation: non-fast-forward results use a separate integration
worktree rather than directly running a merge in the primary checkout.

A later direct merge strategy requires explicit project policy, deterministic
preflight, clean/locked target working copy, exact parent order, commit-message
contract, rollback ref, and post-merge tests.

### 15.4 Integration worktree

Create from current target through a journaled controller operation. The
integration agent receives exact candidate and analysis, resolves behavior,
runs tests, and submits an `integration-result` revision. The original revision
remains unchanged.

- If the result adapts one logical change to the moved target, it is normally a
  new revision of that change linked to the exact prior revision.
- If the result intentionally combines independent changes, it is a new change
  with immutable input links to every exact source revision.

The final result enters the same queue and requires final integration approval.

## 16. Target working-copy safety

If target branch is checked out:

- acquire target working-copy lifecycle/index locks;
- require no unowned dirty state;
- verify exact branch and OID;
- journal ref, index, and working-tree steps separately;
- independently re-observe all three before exposing the target as ready.

Git cannot atomically update a ref, index, and working tree as one transaction.
The implementation MUST NOT claim otherwise. Any post-crash mismatch is marked
recovery-required/needs-attention and retained; it is never hidden by resetting
or declaring success from the ref alone.

If primary target checkout is dirty or used by `pi-personal`, direct integration
is refused. Create/continue an integration worktree or wait for explicit target
availability. Never force checkout/reset/stash.

If target branch is not checked out, ref CAS may proceed under project/target
lock, followed by observation.

## 17. Integration operation saga

1. transactionally record planned attempt, expected target, selected revision,
   authorization digest, and idempotency key;
2. acquire locks in the single global order from `STATE_CONTRACT.md` §9.1:
   global migration lock if applicable, project Git-common lock, target
   working-copy lifecycle/index lock when the target is checked out, target
   working-copy writer lock/epoch when mutation requires it, target-ref/
   integration lock, then review/change lock if needed. An immutable candidate
   ref requires no source working-copy lock;
3. revalidate project, policy, change revision ref/tree, target OID, review
   requirements, and authorization;
4. create rollback ref from absent;
5. mark operation applying;
6. perform one exact Git action;
7. observe target ref and any checked-out working copy;
8. transactionally mark succeeded and change merged, emit event;
9. retain rollback ref through acceptance window;
10. release locks.

Crash recovery classification:

- target still expected old OID: operation not applied; safe to retry after all
  preconditions;
- target equals desired result and rollback ref matches expected old OID:
  record success after verification;
- target is neither old nor desired: ambiguous, preserve and require attention;
- partial working-copy/index state: quarantine from further mutation and open
  recovery procedure; never reset automatically.

## 18. Multiple changes and merge ordering

The secretary may propose an ordered integration plan. Each target mutation is
its own authorized CAS attempt. Success of one invalidates target-dependent
analysis for later changes; the controller recomputes before the next.

No "merge all" operation may reuse a single stale target OID or approval. A
batch UI may collect intent, but each step requires exact revalidation and stops
on conflict or changed semantics. A combined integration change records every
input revision; branch ancestry or a free-form summary alone is insufficient
provenance.

## 19. Remote publication

Local integration does not push. Push requires a separate explicit current-turn
authorization, existing approved remote policy, exact branch, no force, and the
current implemented host boundary. The controller records local and remote
operations separately.

## 20. Cleanup and retention

Change source worktree, conversation, reviews, refs, and artifacts are retained
until:

- change merged or explicitly closed;
- final accepted result remains reachable from intended target when applicable;
- no live process/session uses the resource;
- working copy clean and exact expected revision;
- review/integration recovery window elapsed;
- no unresolved operation/attention;
- dry-run plan names exact paths/refs/OIDs/hashes;
- controller ownership proven;
- policy or explicit user approval permits apply.

Change revision refs are removed only by exact recorded name and OID. Prefix
cleanup is forbidden. Failed or ambiguous integration extends retention.

## 21. User-facing examples

### Submit from personal

> **User:** Send this launcher work to the secretary.
>
> **Pi Personal:** I can submit the three launcher files and leave the two
> pre-existing documentation changes here. Seven checks passed. Submit those
> three files?
>
> **User:** Yes.
>
> **Pi Personal:** Submitted **Launcher restart fix**, revision 1. Nothing was
> merged and your working folder was not cleaned or reset.

### Secretary analysis

> **Secretary:** Launcher restart fix targets main. Git reports no textual
> conflict. It does overlap the active root-session change in startup ordering;
> I recommend integrating root-session first and rerunning the restart test.

### Target moved

> **Secretary:** Main changed after the review. I did not merge the change. The
> submitted revision is preserved. I can compare the new target or create an
> integration session.

## 22. Explicit non-goals

- No proprietary source snapshot storage.
- No automatic integration from agent completion.
- No review verdict as merge authority.
- No implicit rebase on target movement.
- No cleanup as part of successful integration.
- No remote push as part of local integration.
- No claim that conflict-free Git merge means semantic compatibility.
