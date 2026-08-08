# Control-plane observability and conversation continuity contract

Status: **normative target; continuity, error, and Inspector component source exists, but behavioral full-system and live acceptance remain incomplete**.

## 1. Purpose

The controller must be understandable without exposing internal machinery as
normal UI. This contract defines:

- authoritative versus derived status;
- control-plane audit events and correlation;
- secretary/project projections;
- plain-language errors and technical drill-down;
- privacy and retention;
- user-visible compaction continuity;
- incomplete-run detection.

It complements, rather than replaces, `../HARNESS_OBSERVABILITY.md` and the
implemented read-only `/observe` Inspector.

## 2. Observability layers

### 2.1 Authoritative resources

Current controller tables and immutable Git objects/refs. These determine
lifecycle decisions.

### 2.2 Control events

Append-only, transactionally emitted records describing state changes and
operations. They are audit/replay evidence, not an event-sourced replacement for
current resource rows.

### 2.3 Adapter observations

Bounded facts from Git, filesystem, process, container, session, tmux, and Herdr
adapters. They include freshness and provenance. Unknown is never converted to
missing/stopped.

### 2.4 UI projections

Human summaries derived from current resources plus observations. Projections
may be regenerated and cached. They never become authority because they are
newer or user-visible.

### 2.5 Detailed diagnostics

Technical manifests, operation steps, hashes, paths, OIDs, labels, and error
output. Local, bounded, permission-restricted, and hidden by default.

## 3. Correlation model

Control events use explicit IDs:

```text
event_id
sequence
project_id
working_copy_id when applicable
conversation_id when applicable
run_id and parent_run_id when applicable
operation_id
change_id/revision when applicable
integration_id when applicable
controller build_id
```

Do not overload Pi session ID as task, run, working-copy, or project identity.

Existing harness trace/task/turn/tool IDs remain separate and may link to
`run_id`/`operation_id`. A controller operation is not necessarily one model
turn.

## 4. Control event envelope

```json
{
  "schemaVersion": 1,
  "eventId": "evt_...",
  "sequence": 123,
  "eventKind": "run.attestation_failed",
  "createdAt": "RFC3339",
  "controllerBuildId": "build_...",
  "resource": {
    "type": "run",
    "id": "run_...",
    "version": 4
  },
  "operationId": "op_...",
  "correlation": {
    "projectId": "prj_...",
    "workingCopyId": "wc_...",
    "conversationId": "conv_...",
    "parentRunId": null,
    "changeId": null,
    "revision": null
  },
  "status": "error",
  "payload": {
    "errorCode": "CP_RUN_ATTESTATION_FAILED",
    "observationDigest": "sha256:..."
  }
}
```

Events are bounded canonical JSON. Raw source, prompts, tool output, environment,
credentials, and hidden reasoning are excluded.

## 5. Event taxonomy

Minimum categories:

```text
project.registered / rebound / trust_changed / drifted
working_copy.desired / observed / writer_acquired / writer_released / drifted
conversation.bound / rebound / archived / missing
workstream.planned / creating / ready / stopped / retired / drifted
presentation.assigned / observed / missing / drifted
run.created / preparing / attested / started / stopping / stopped / failed / lost
operation.planned / applying / step / succeeded / failed / needs_attention
change.created / revision_submitted / revision_superseded / merged / closed
review.requested / submitted / failed / stale
integration.planned / applying / conflict / succeeded / failed
attention.opened / acknowledged / resolved
build.staged / activated / rejected / rolled_back
migration.started / mapped / imported / compared / cutover / rollback
activation.planned / shadowed / activated / rejected / rolled_back
continuity.compacted / displayed / inspected
```

Every consequential operation must produce terminal success/failure/needs-
attention or a startup journal marker that reconciliation classifies after
crash.

## 6. Outbox semantics

State mutation and event insertion commit in one SQLite transaction. Consumers
read monotonically increasing `sequence` and maintain independent cursors.

Requirements:

- at-least-once delivery;
- consumers deduplicate by `event_id`;
- delivery ordering is sequence order per database, not wall-clock order across
  unrelated systems;
- a consumer may be offline without losing events;
- acknowledgement updates consumer cursor, not event content;
- poison/malformed event is retained and surfaces attention rather than skipped
  silently;
- bounded retention cannot delete events newer than the minimum required
  consumer/recovery/checkpoint position;
- secretary attention is a durable state row, not merely a transient event.

No separate "committed but not emitted" window exists because events are in the
same transaction. External notifications may duplicate after a crash and must
be idempotent.

## 7. Desired/observed status projection

Technical status includes both sides:

```text
Desired: run R running against wc X version 12 at commit B
Observed: container C running against wc X at commit A
Classification: attestation failed; tools blocked
```

User projection states the consequence:

```text
I stopped before running tools because this session would have opened an older
version of the project. Nothing was changed.
```

A UI must not show a single `synced` boolean. If equality matters, name the
compared resources and freshness in technical details.

## 8. Secretary project projection

Secretary project status combines:

- deterministic controller resources;
- fresh bounded Git inventory;
- active/open attention;
- current submitted changes;
- recent control events;
- bounded project-history synthesis when requested.

Every row carries hidden provenance and freshness. Human output distinguishes:

- **Observed** — directly revalidated state;
- **Recorded** — user/agent purpose or decision in controller metadata;
- **Inferred** — analysis such as likely semantic overlap.

An unavailable adapter yields "could not inspect" rather than omission or a
healthy empty list.

## 9. Pi footer and working activity

`pi-statusline` remains owner of the footer. Its configured primary segments are
model, reasoning, observable tool/activity, and context.

A control-plane UI extension may publish at most one aggregated attention
status. Healthy state produces no control-plane status.

Activity labels are observable, not invented intent:

```text
read/grep/find/ls       -> reading the project
edit/write              -> editing files
recognized test command -> running tests
other shell command     -> running a command
web search              -> researching
subagent launches       -> asking helpers / N helpers working
controller approval     -> waiting for your decision
```

Internal tool name may remain available in expanded detail.

Sandbox extension MUST stop publishing normal statuses such as container name,
`sandbox: pending`, or ref/rebase implementation state into the everyday
footer. It may emit machine events consumed by the translator extension.

## 10. Error presentation

### 10.1 Required structure

Every user-actionable failure includes:

```text
Attempted action
Observed risk/disagreement
Whether anything changed
What was preserved
Safe next actions
Technical details action
```

### 10.2 No false reassurance

Do not say:

- project unchanged unless target/project side effects were independently
  observed absent;
- work preserved unless exact refs/objects/working paths were observed;
- retry is safe unless operation intent and side effects are reconciled;
- process stopped based only on missing PID;
- container/runtime matches based only on name/label;
- review is current without exact revision binding.

### 10.3 Machine-to-human mappings

| Error | Default user wording |
|---|---|
| `CP_RESOURCE_STALE` | State changed while preparing this action; refreshed and did not apply stale intent |
| `CP_LOCK_BUSY` | Another Pi operation is changing this resource; focus/wait/separate |
| `CP_WRITER_UNKNOWN` | Previous run may still have write access; no new writer granted |
| `CP_WORKING_COPY_DRIFT` | Working copy differs from recorded assignment; stopped before mutation |
| `CP_RUN_ATTESTATION_FAILED` | Tools would run against a different project/runtime state; blocked |
| `CP_GIT_REF_MOVED` | Target changed since approval; submitted change preserved; re-analyze |
| `CP_OPERATION_AMBIGUOUS` | Side effect may be partial; preserved evidence; technical recovery required |
| `CP_BUILD_MISMATCH` | Active installed control plane is not the tested build; no activation claim |
| `CP_PERMISSION_INVALID` | Private runtime area has invalid ownership/mode; project not broadly repaired |

## 11. Technical Inspector extension

The existing `/observe` view may be extended with a **Control** tab showing:

- controller build/schema;
- project and working-copy IDs/versions;
- desired and observed state;
- writer run/epoch and lock observation;
- current run manifest/attestation digest;
- pending/failed operations and steps;
- open changes/revisions/integration attempts;
- attention and event sequence freshness;
- adapter failures;
- exact paths/OIDs only in expanded mode.

It remains read-only initially. Mutation controls, if added, call semantic
controller operations and require the same user authorization as conversational
flows.

The Inspector must tolerate missing, malformed, concurrently changing, or newer
schema data by showing unavailable/unsupported rather than crashing Pi.

## 12. Privacy modes

Control-plane metadata defaults to local **metrics/state** mode:

Stored:

- logical IDs;
- local paths and Git refs/OIDs;
- timestamps, versions, states, error codes;
- bounded summaries deliberately supplied for project/change purpose;
- content hashes, sizes, and path manifests;
- controller/build/runtime hashes.

Not stored by default:

- raw prompts or full conversation text;
- hidden chain-of-thought;
- provider payloads/completions;
- raw tool output;
- credentials or authorization headers;
- capability secrets;
- arbitrary environment;
- source file contents;
- sensitive host-command output.

Debug/trace artifacts remain separate from controller DB and follow
`../HARNESS_OBSERVABILITY.md` opt-in/retention rules. Their absence cannot change
controller correctness.

## 13. Retention

Retain indefinitely or until explicit migration policy:

- project identity/history;
- change/revision/integration provenance needed to explain target content;
- build activation/rollback records;
- schema migrations.

Retain through recovery/acceptance window:

- operation steps;
- rollback refs;
- run manifests/attestations;
- detailed adapter observations;
- continuity cards.

Bounded expiry may remove derived metrics/debug previews after:

- no unresolved operation/attention;
- all required consumers/checkpoints passed sequence;
- references to retained provenance removed or summarized;
- dry-run reports what will be deleted.

## 14. Conversation compaction continuity

### 14.1 Problem

Compaction currently provides the model a retained summary that may not be
visible to the user. This creates asymmetric context and contributed to losing
the original control-plane diagnosis during the architecture discussion.

### 14.2 Required card

On `session_compact`, a user/global extension persists and renders one
`conversation-continuity.v1` entry:

```json
{
  "schemaVersion": 1,
  "sessionId": "...",
  "compactionEntryId": "...",
  "reason": "manual|threshold|overflow",
  "createdAt": "...",
  "retained": {
    "goal": "...",
    "currentSlice": "...",
    "decisions": [],
    "completed": [],
    "openQuestions": [],
    "risks": [],
    "firstKeptEntryId": "..."
  },
  "summaryDigest": "sha256:...",
  "detailsAvailable": true
}
```

The card derives from the actual compaction result plus bounded active task
packet. It does not ask a second independent model to invent another summary.
`compactionEntryId` is the idempotency key within one session. Before appending,
the extension scans existing custom continuity entries; after an uncertain
append/crash, resume performs the same scan. Duplicate physical entries, if a
Pi/session failure nevertheless creates them, are rendered and exported once
by `(sessionId, compactionEntryId)` and surface a diagnostic rather than two
continuations.

### 14.3 Rendering

Collapsed:

```text
Conversation compacted — current goal and 3 open decisions retained. Press or
run /continuity to inspect.
```

Expanded:

```text
Goal
Build a conventional Pi host-local control plane.

Decisions retained
• Git is source-content authority.
• Controller owns lifecycle relationships.
• Secretary integrates submitted changes.

Open
• Dirty personal-change attribution policy.
• Migration cutover order.
```

The card survives resume and branch navigation according to Pi session
semantics. A `/continuity` command lists the latest and historical cards.

### 14.4 Privacy and consistency

- no hidden reasoning;
- no raw system prompt;
- no secret/tool output;
- bounded fields and counts;
- user-visible card and model-visible retained summary share digest/reference;
- if a bounded card cannot be derived, state that continuity details are
  unavailable rather than fabricating them;
- at most one continuation is enqueued for each persisted compaction entry;
  overflow retry and extension retry share the same idempotency key, and
  uncertain delivery is reconciled from persisted session entries rather than
  claimed exactly-once.

## 15. Incomplete-run detection

Controller creates run/operation start markers before external launch. On
reconciliation:

- desired/running with no live owner and no terminal event -> `lost` plus
  attention;
- adapter unavailable -> unknown, not lost;
- stopped observation with terminal evidence -> stopped;
- process crash after side effect -> operation reconciled against observations;
- model/provider failure does not imply working-copy failure;
- runtime/tool failure does not discard source state.

The UI tells the user whether implementation work remains observable and which
part failed.

## 16. Performance and health telemetry

Controller records its own:

- DB lock wait and transaction duration;
- reconciliation duration by adapter;
- queue/event consumer lag;
- failed/dropped UI notification count;
- operation retries and ambiguous outcomes;
- manifest/attestation generation time;
- change capture time and bytes/files;
- startup contribution.

A diagnostic failure must not block normal state mutation unless the missing
evidence is required for safety. Required attestation is not optional
telemetry; metrics are.

## 17. Acceptance requirements

- every fault-injection scenario has a stable error code and plain message;
- no healthy run displays container/route jargon;
- unavailable observations are visible;
- duplicate outbox delivery does not duplicate attention/action;
- compaction card is visible, persisted, and consistent with retained summary;
- controller DB contains no prohibited raw content under fixture scans;
- exact technical evidence is accessible without entering the task container;
- source/install build mismatch is clearly visible;
- UI remains functional with malformed/newer diagnostic records.

## 18. Explicit non-goals

- No remote telemetry by default.
- No hidden-reasoning display.
- No status dashboard as lifecycle authority.
- No claim of complete filesystem observation for arbitrary shell processes.
- No user requirement to learn controller schema or internal IDs.
