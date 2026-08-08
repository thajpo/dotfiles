# Execution, sandbox, and child contract

Status: **normative target; substantial manifest/runtime/child component source exists, but actual launcher/runtime cutover, full-system acceptance, and live activation remain incomplete**.

## 1. Purpose

This contract defines how a controller-selected project state becomes one Pi
run and how tools, containers, children, artifacts, cancellation, restart, and
failure behave. It eliminates route-derived identity and makes the sandbox an
execution adapter rather than a workspace controller.

## 2. Execution principles

1. A run is process-scoped and disposable.
2. A conversation and working copy are durable independently of a run.
3. Every run uses one immutable manifest created from one controller
   transaction.
4. Every writable run holds one working-copy writer lock and epoch.
5. Runtime preparation is desired/observed reconciliation.
6. Tool execution starts only after complete attestation.
7. Children receive exact parent-selected source state and explicit authority.
8. Container names, labels, route files, and PIDs are observations/projections.
9. A failed run does not select a fallback working copy or older commit.
10. Reuse is an optimization permitted only after exact identity/spec checks.

## 3. Run lifecycle

```text
created
  -> preparing
  -> ready
  -> running
  -> stopping
  -> stopped

created/preparing/ready/running
  -> failed | lost | needs_attention
```

Legal behavior:

- `created`: controller row and manifest intent exist; no runtime side effect is
  claimed.
- `preparing`: runtime adapter operation is applying.
- `ready`: independent attestation matches the manifest; no model tool has yet
  been allowed solely because a container exists.
- `running`: Pi process and tool proxy are active.
- `stopping`: graceful shutdown requested; new mutations blocked.
- `stopped`: process/runtime quiesced and final observation recorded.
- `failed`: a known terminal error with evidence.
- `lost`: expected owner/process disappeared without a complete terminal event.
- `needs_attention`: state is preserved but ambiguity prevents automatic repair.

A new run receives a new `run_id`. Restart never mutates or reuses the old run
row as though process identity continued.

## 4. Immutable run manifest

Manifest schema version 1:

```json
{
  "schemaVersion": 1,
  "runId": "run_...",
  "operationId": "op_...",
  "taskId": null,
  "conversationId": "conv_...",
  "piSessionId": "...",
  "parentRunId": null,
  "project": {
    "projectId": "prj_...",
    "resourceVersion": 7,
    "objectFormat": "sha1",
    "trustMode": "trusted",
    "policyHash": "sha256:..."
  },
  "workingCopy": {
    "workingCopyId": "wc_...",
    "resourceVersion": 12,
    "kind": "primary",
    "purpose": "personal",
    "effectiveMode": "trusted-live",
    "hostPath": "/absolute/controller-selected/path",
    "gitCommonDir": "/absolute/verified/path",
    "gitDir": "/absolute/verified/path",
    "branchRef": "refs/heads/feature/example",
    "headOid": "...",
    "treeOid": "...",
    "dirtyFingerprint": null,
    "writerEpoch": 4
  },
  "authority": "writer",
  "runtime": {
    "runtimeSpecVersion": 1,
    "runtimeSpecHash": "sha256:...",
    "executionTarget": "linux-container",
    "platform": "linux/amd64",
    "imageDigest": "sha256:...",
    "controllerBuildId": "build_...",
    "piVersion": "0.83.0"
  },
  "owner": {
    "uid": 1000,
    "gid": 1000,
    "pid": 12345,
    "processStartIdentity": "linux:..."
  },
  "capabilityHash": "sha256:...",
  "attestationNonce": "128-bit-random-base64url",
  "createdAt": "RFC3339",
  "expiresAt": null,
  "manifestDigest": "sha256:..."
}
```

Requirements:

- serialized canonical JSON with stable key ordering and UTF-8 encoding;
- `manifestDigest` computed with that field omitted, then inserted;
- user-owned non-symlink `0600` regular file outside repositories;
- capability secret passed separately; manifest stores only hash;
- manifest path does not define run ID;
- working-copy paths must exactly match controller-selected canonical paths;
- every OID length validated against object format;
- runtime image uses repository/digest syntax valid for Docker; a bare
  `FROM sha256:<id>` is forbidden;
- unknown fields rejected for the active schema version;
- `operationId` links preparation intent; optional `taskId` links a current
  workflow packet/task without becoming run/session identity;
- attestation nonce is unique per run/preparation attempt and accepted once; it
  prevents accidental/stale replay but is not a secret or defense against a
  malicious same-UID host process;
- terminal exit/result belongs in the run/terminal record, not an immutable
  start manifest;
- an adapter may add a separately versioned attestation, not mutate manifest.

## 5. Runtime specification

Runtime specification version 1 is canonical JSON containing:

```text
execution target and platform
image repository plus immutable digest
container user UID/GID
supplementary groups (normally none)
working-copy source and container mount target
Git common-dir and worktree Git-dir mounts
read/write or read-only flags
mount propagation and recursive read-only expectations
network mode and approved loopback publications
capability drops and security options
read-only root filesystem policy
private tmpfs paths and ownership
runtime cache volumes and scope keys
minimal Git identity file and read-only mode
allowed control-plane resource mounts
skill manifests and read-only paths
runtime helper path/build identity
environment allowlist and exact values/hashes
working directory in the container
```

The hash is over canonical specification content, not container name or creation
time.

### 5.1 Permission contract

Before ready:

- runtime-private writable directories exist inside container-owned tmpfs or
  volumes;
- UID/GID match the manifest;
- every required writable path is writable by the runtime user;
- every required read-only path rejects writes;
- no task resource is unexpectedly root-owned;
- reused volumes are validated after repair, not against stale pre-repair
  metadata;
- host source mount permissions are observed but never broadly `chmod`ed by the
  sandbox;
- a permission mismatch recreates a controller-owned private resource or fails
  with `CP_PERMISSION_INVALID`;
- unrelated host paths are never repaired.

The task-local dependency environment remains inside a runtime-private path
such as `/tmp/pi-home/task-env`; it is not a host `.venv`.

### 5.2 Runtime reuse

MVP default: do not reuse a container across different run IDs. Reuse within one
run for same-run children is allowed only when child authority and mount mode
remain valid.

A later cross-run reuse optimization requires all of:

- controller explicitly requests adoption;
- previous run is stopped and no process may write;
- exact project, working-copy ID, writer epoch, manifest digest, runtime hash,
  image digest, UID/GID, mounts, environment, platform, and policy agree;
- actual Git state agrees with the new manifest;
- reuse attestation reruns all checks;
- labels are observations, not sufficient proof;
- failure recreates rather than weakening checks.

Performance alone never authorizes reuse.

## 6. Attestation

The runtime adapter emits immutable attestation version 1:

```json
{
  "schemaVersion": 1,
  "runId": "run_...",
  "manifestDigest": "sha256:...",
  "attestationNonce": "128-bit-random-base64url",
  "observedAt": "RFC3339",
  "container": {
    "id": "...",
    "name": "...",
    "imageId": "sha256:...",
    "imageDigest": "sha256:...",
    "platform": "linux/amd64",
    "running": true
  },
  "identity": {"uid": 1000, "gid": 1000},
  "workingCopy": {
    "projectId": "prj_...",
    "workingCopyId": "wc_...",
    "branchRef": "refs/heads/...",
    "headOid": "...",
    "treeOid": "...",
    "gitCommonDir": "...",
    "gitDir": "...",
    "writable": true
  },
  "mounts": [],
  "checks": [],
  "attestationDigest": "sha256:..."
}
```

Controller compares every required field, requires the nonce/digest for the
currently applying preparation operation, and rejects a second/replayed
attestation after the operation has advanced. Shape-valid child output or
self-reported success is not attestation.

On disagreement:

- no tool execution;
- run state `failed` or `needs_attention` according to ambiguity;
- bounded technical evidence retained;
- no fallback commit/path/container;
- user receives consequence-oriented failure text.

## 7. Tool proxy contract

All built-in read/write/bash tools and user `!` commands for a managed run route
through one execution proxy.

Before each mutation-capable call, proxy verifies:

- run still desired/running;
- run capability;
- working-copy ID and writer epoch;
- active writer claim;
- runtime attestation still belongs to the run;
- requested cwd lies within allowed execution plane;
- no parent/child ownership handoff blocks this writer;
- cancellation has not fenced new mutation.

Read calls may use a less expensive cached check only while the immutable
manifest/run remains active; any runtime or working-copy event invalidates the
cache.

A stale in-memory extension cannot continue because it knows a route path. Its
run/epoch check fails.

### 7.1 Starting state versus current writer state

The manifest's HEAD/tree/dirty fingerprint is the **starting attestation**, not
a claim that a legitimate writer can never change files or commit during the
run. After readiness:

- assignment, project/Git identity, runtime spec, and writer epoch remain fixed;
- source content may advance only through the active writer in its assigned
  working copy;
- controller records fresh observations/checkpoints before child launch, change
  submission, review, integration, restart, or ownership handoff;
- an ordinary file edit does not require a SQLite transaction per write;
- a new child/change manifest is derived from a fresh quiescent observation or
  immutable snapshot, never copied from the run's old starting OID;
- external/unowned ref/worktree mutation is classified as drift when ownership
  evidence does not explain it;
- starting attestation remains immutable provenance for diagnosis.

This distinction prevents both errors: treating legitimate in-run commits as
runtime corruption and launching later children from a stale route
`startingOid`.

### 7.2 Direct trusted-live writes

Trusted-live mounts permit direct filesystem mutation inside the selected
working copy. Therefore:

- only one writable tool proxy/container may exist;
- controller cannot claim stale writers are fenced while their container still
  has a writable mount;
- before granting a new writer, old proxy/container termination must be proven;
- if termination cannot be proven, refuse and preserve state;
- long-running background commands require explicit tracking and cancellation;
- no new writer starts merely because the parent Pi PID disappeared.

## 8. Parent and child contract

### 8.1 Child identity

Every child receives:

```text
child run ID
parent run ID
project ID
source working-copy/snapshot ID
exact source revision/tree
role and authority
runtime spec/build ID
bounded task assignment
```

`context: fresh` versus `context: fork` changes conversation context only. It
never changes source-state selection.

### 8.2 Read-only child

A read-only child MUST be mechanically unable to mutate the parent's working
copy. Accepted MVP strategies, strongest first:

1. immutable Git snapshot in a separate read-only working copy/container;
2. read-only bind mounts plus no shell/write/edit/host mutation tools;
3. no filesystem mount, with bounded read APIs supplied by the parent.

A role name or acceptance policy is not mechanical read-only enforcement.
Current report-only roles with mutable sandboxed `bash` do not satisfy this
target contract.

The snapshot is selected from the exact parent state at launch. If parent state
is dirty, use the Git snapshot procedure below.

### 8.3 Writer child

Preferred MVP behavior: an independent writer receives a separate controller-
created working copy and writer epoch.

Optional exclusive delegation on the same working copy requires:

- parent mutation suspended;
- explicit sublease operation;
- child run owns the epoch/sublease;
- parent proxy blocks writes until child terminal observation;
- child result reconciles exact Git state;
- crash retains state and blocks parent until resolved.

Do not implement same-copy writer delegation before tests prove the simpler
separate-working-copy path.

### 8.4 Exact dirty-state snapshot

For read-only analysis or change submission, controller may create an immutable
Git snapshot without modifying the real worktree/index:

1. acquire working-copy observation lock; for a writable source coordinate with
   its writer so the capture is quiescent;
2. record HEAD, index checksum, status porcelain v2, object format, and selected
   path policy;
3. create a temporary index in a private `0700` operation directory;
4. seed it from the selected base tree;
5. add selected tracked contents and explicitly included untracked paths using
   sanitized Git environment;
6. reject unresolved conflicts, unsupported special files, symlink escapes,
   submodule ambiguity, path changes during capture, and size-policy violations;
7. write tree and internal commit with `git write-tree` / `git commit-tree`;
8. create controller-owned ref with `git update-ref <new> <expected-absent>`;
9. re-observe source status; if selected content changed during capture, discard
   only the newly owned ref and retry or ask;
10. record manifest and object IDs transactionally.

Ignored files are excluded by default. The caller must explicitly include an
ignored file, with size and secret warnings. The UI says which relevant files
would be omitted.

Snapshot-policy v1 defaults (host policy may lower them; raising them requires
an explicit controller/admin setting, never model input):

```text
maximum selected paths: 10,000
maximum selected content read for newly captured blobs: 256 MiB total
maximum single untracked or explicitly included ignored file: 32 MiB
special files: forbidden
submodules: record gitlink only when clean/exact; dirty submodule forbidden
symlinks: capture link text only; target is never followed
ignored files: forbidden unless exact path explicitly selected
```

Exceeding a bound creates attention with counts/bytes and no snapshot ref. Size
checks and final blob/tree observations are repeated so a file cannot grow past
policy between planning and capture.

### 8.5 Parent advancement during child run

The child retains its immutable source. Parent may continue read-only work.
Parent mutation policy is explicit:

- read-only child snapshot: parent may advance; child result is interpreted
  against its source revision;
- independent writer child: parent may advance independently; integration is a
  later change-queue concern;
- same-working-copy delegated writer: parent writes are blocked.

No child silently rebases to parent latest.

## 9. Child completion and artifacts

Child completion record includes:

```text
run and parent IDs
source revision/tree
terminal classification
result summary
changed-state declaration
artifact manifest
submitted change ID/revision if any
usage/evidence provenance
```

Artifacts live outside project repositories by default under:

```text
${XDG_STATE_HOME:-~/.local/state}/pi-control/artifacts/<artifact-id>/
```

The artifact root and per-artifact directory are owned non-symlink `0700`; files
are owned regular `0600`. An artifact manifest carries:

```text
artifact ID and checksum
producer run/child
project and source revision
content type and size
sensitive flag
retention class: run | change | recovery | debug
path in user-owned artifact storage
created/expiry timestamps where applicable
```

Default retention policy v1:

- `run`: eligible 30 days after terminal run if no change/attention references;
- `change`: retained while change/review/integration provenance references it;
- `recovery`: no automatic expiry; explicit reviewed cleanup only;
- `debug`: bounded by observability opt-in policy and never required for
  controller correctness.

Eligibility is not deletion authority. Cleanup still follows exact dry-run,
ownership, reference, hash, and live-use checks.

A writer result is not accepted merely because the container has a descendant
commit. The controller verifies:

- exact working copy and branch/ref;
- object integrity and ancestry or declared divergence;
- no hidden dirty changes when a clean candidate is claimed;
- submitted change revision content;
- writer epoch and run provenance.

Ambiguous, dirty, rebased, or unrelated state is retained and surfaced. It is
never reset automatically to fit the expected result.

## 10. Cancellation, retries, and transitions

### 10.1 Cancellation

Cancellation is desired state plus fencing:

1. controller marks run stopping and prevents new tool calls;
2. proxy sends graceful cancellation to active tools/processes;
3. runtime adapter observes quiescence;
4. container stops only after tracked processes terminate or timeout policy
   produces needs-attention evidence;
5. writer lock releases after writable access is gone;
6. final state and artifacts persist.

No SIGKILL-based assumption of cleanliness is allowed. A force-stop, if ever
implemented, is explicit host maintenance and records uncertainty.

### 10.2 Retry

Retry of controller operations uses the same idempotency key. Provider/model
retry is a separate run/turn concern and does not create new project/workspace
identity.

A runtime preparation retry:

- reuses operation intent;
- advances no Git ref unless previous state is proven;
- re-observes after uncertain side effects;
- never chooses a different base revision.

### 10.3 Parent transitions

Parent checkpoint, rebase, ref move, and teardown are typed controller
operations under ordered locks. Helpers receive operation context and do not
reacquire a non-reentrant transition. There is no independent
`parent-transition.json` live authority.

## 11. Session restart

Managed restart procedure:

1. resolve exact conversation from controller;
2. reconcile its project and assigned working copy;
3. verify exact session JSONL;
4. create a new run and writer epoch if writable;
5. create manifest from current controller/Git transaction;
6. prepare and attest runtime;
7. start Pi against exact session file;
8. update presentation projection.

A session header cwd cannot select an older worktree. A route `startingOid`
cannot survive as durable head authority. If controller expected and Git actual
differ, classify before running tools.

## 12. Host commands

`host_command` remains a one-shot, user-approved request. Its execution is not
covered by sandbox writer fencing and therefore:

- exact command, cwd, requester run, reason, and description are displayed;
- approval binds one request and expires;
- controller records request/result metadata without raw sensitive output by
  default;
- command cannot silently alter controller resources and then claim normal
  reconciliation success;
- after a host command touches project/control state, affected resources are
  marked observation-stale and re-reconciled.

## 13. Runtime error translation

Examples:

| Machine condition | User message concept |
|---|---|
| manifest head differs | Tools would open a different version; stopped before execution |
| writer lock held | Another Pi is changing this working copy; focus/wait/separate |
| image digest differs | Tool environment is not the tested version; recreate/details |
| UID/GID/mount invalid | Private tool area could not be prepared; project unchanged |
| owner unknown after crash | Previous run may still have access; no new writer granted |
| child source mismatch | Helper would inspect different work; child not started |

No healthy success message says "container synced."

## 14. Explicit non-goals

- The sandbox does not select projects, worktrees, branches, refs, or sessions.
- Container reuse is not part of MVP correctness.
- Read-only is not inferred from model role.
- Child completion does not directly move target branches.
- Runtime labels are not attestation by themselves.
- Fencing does not claim magical revocation of arbitrary host descriptors.
