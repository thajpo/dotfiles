# Host-local control-plane state contract

Status: **normative target; substantial component source exists, but full harness integration, migration completion, staged acceptance, and live activation remain incomplete**.

## 1. Purpose

This contract defines the single authoritative lifecycle model for Pi projects,
working copies, conversations, runs, change submissions, and integration
operations. It replaces overlapping lifecycle authority; it does not replace
Git source content or Pi session JSONL.

The controller is a host-local distributed-systems control plane because its
clients and adapters are independent processes that can crash, restart, race,
and observe stale state. The design uses conventional local primitives and does
not require network consensus.

## 2. Authority boundaries

### 2.1 Controller database owns

- stable resource IDs and relationships;
- desired lifecycle state;
- observed lifecycle summaries;
- resource versions;
- writer epochs and run assignment;
- operation intent, step, outcome, and idempotency;
- local change and integration metadata;
- bounded attention/outbox events;
- schema and installed-build activation metadata.

### 2.2 Git owns

- commits, trees, blobs, tags, and refs;
- branch ancestry and merge bases;
- source diffs;
- submitted immutable revision content;
- final integrated target content.

The database records exact Git object IDs and expected old/new ref values. It
never stores source file contents as the canonical change representation.

### 2.3 Pi session JSONL owns

- conversation messages and branches;
- compaction entries;
- model/tool messages;
- extension entries local to the conversation.

The controller binds a conversation ID to a session file and project/working
copy. Session header `cwd` is an observation/projection, not binding authority.

### 2.4 Kernel and process substrate owns

- whether a file descriptor lock is currently held;
- process existence and start identity;
- filesystem access enforcement;
- container namespace enforcement.

The database records claims and observations but must not treat a PID or marker
file as proof that a live kernel lock exists.

### 2.5 Adapters own no lifecycle truth

Task-route files, Docker labels, tmux pane metadata, Herdr records, sandbox refs,
child lease files, generated host context, and extension in-memory state are
bounded projections. They may prove an observation but never override the
controller by being newer, more numerous, or located at an expected path.

## 3. MVP process architecture

The MVP is a Python library plus CLI using the standard `sqlite3` module.

```text
bin/pi-control
  -> scripts/pi_control/cli.py
      -> store.py / operations.py / reconcile.py
          -> git, process, runtime, and session adapters
```

There is no resident daemon in the MVP. Reconciliation runs:

- at every managed launcher start;
- before and after every consequential controller operation;
- when an adapter reports drift or failure;
- from `pi-control inspect` (no persistence) or
  `pi-control reconcile --observe-only` (persists controller observations/events
  but performs no external repair);
- from an explicit mutating reconcile command only after policy allows it.

A later daemon may invoke the same library and schema. It must not introduce a
second state store or change resource identity.

## 4. Storage contract

Default location:

```text
${XDG_STATE_HOME:-~/.local/state}/pi-control/control.db
```

Requirements:

- parent directory MUST be an owned, non-symlink `0700` directory;
- process umask MUST be `077` before SQLite creates database, WAL, or SHM files;
- database files MUST be regular, user-owned, and inaccessible to group/other;
- database MUST reside on a local filesystem with supported SQLite locking;
- symlinked database paths and network filesystems MUST fail closed;
- no API keys, raw auth headers, or model credentials are stored;
- capability tokens are stored only as cryptographic hashes;
- paths and Git metadata are sensitive local data and use the same permissions.

The MVP requires SQLite **3.40.0 or newer**. Before creating or migrating any
schema, initialization MUST:

- parse `sqlite3.sqlite_version` and reject older versions with
  `CP_SQLITE_UNSUPPORTED`;
- execute a temporary capability probe proving `STRICT` tables, partial indexes,
  triggers, foreign keys, and `json_valid()` are available;
- leave no application schema or state-root mutation when the preflight fails.

Connection initialization then MUST execute and verify:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;
```

Schema changes use `PRAGMA user_version` and explicit transactional migrations.
An implementation MUST NOT auto-downgrade an unknown newer schema.

## 5. Identifier contract

All logical IDs are controller-assigned random IDs with a type prefix, for
example:

```text
prj_<uuid-or-128-bit-random>
wc_<...>
conv_<...>
ws_<...>
pa_<...>
run_<...>
chg_<...>
review_<...>
op_<...>
int_<...>
mig_<...>
inv_<...>
evt_<...>
```

Requirements:

- at least 128 bits of cryptographically random entropy;
- lowercase ASCII with a strict bounded regex;
- globally unique within the database;
- immutable after creation;
- never derived solely from a path, branch, PID, timestamp, pane, or container;
- existing durable Pi conversation IDs may be preserved after strict validation
  and stored as `pi_session_id` even when the controller ID differs.

Human labels are mutable metadata and not IDs.

## 6. Resource schema

The following DDL is normative at the field/constraint level. Implementations
may factor timestamps or JSON validation into helper tables only if all
constraints and invariants remain mechanically enforced.

### 6.1 Metadata and builds

```sql
CREATE TABLE control_meta (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  schema_version INTEGER NOT NULL,
  controller_build_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  source_sha256 TEXT NOT NULL,
  applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE installed_builds (
  build_id TEXT PRIMARY KEY,
  source_commit TEXT,
  source_tree_hash TEXT NOT NULL,
  artifact_manifest_hash TEXT NOT NULL,
  pi_version TEXT NOT NULL,
  package_lock_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN
    ('staged','active','superseded','rolled_back','failed')),
  installed_at TEXT NOT NULL,
  activated_at TEXT,
  rollback_path TEXT,
  verification_json TEXT NOT NULL CHECK (json_valid(verification_json))
) STRICT;

CREATE UNIQUE INDEX installed_builds_one_active_uq
  ON installed_builds(status) WHERE status = 'active';
```

A run references one exact active build ID. Because a foreign key proves only
existence, run creation MUST transactionally query the referenced build and
require `status = 'active'`; staged, superseded, rolled-back, failed, or absent
builds are rejected before manifest creation. Repository source state alone
never proves activation. Each schema migration's source digest is checked
before an existing version is accepted; `PRAGMA user_version`, `control_meta`,
and the migration table must agree.

### 6.2 Projects

```sql
CREATE TABLE projects (
  project_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  git_common_dir TEXT NOT NULL,
  git_common_device INTEGER NOT NULL,
  git_common_inode INTEGER NOT NULL,
  primary_checkout TEXT NOT NULL,
  object_format TEXT NOT NULL CHECK (object_format IN ('sha1','sha256')),
  trust_mode TEXT NOT NULL CHECK (trust_mode IN ('trusted','isolated')),
  policy_hash TEXT NOT NULL,
  desired_state TEXT NOT NULL CHECK (desired_state IN ('active','archived')),
  observed_state TEXT NOT NULL CHECK (observed_state IN
    ('unknown','ready','drifted','missing','error')),
  resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_reconciled_at TEXT,
  error_code TEXT,
  error_detail TEXT
) STRICT;

CREATE UNIQUE INDEX projects_git_common_dir_uq ON projects(git_common_dir);
```

`git_common_dir` and `primary_checkout` are rebindable locators. Device/inode
observations detect replacement at the same path and same-filesystem renames;
they are not portable logical identity. Moving a repository requires an
explicit rebind operation that verifies prior/candidate common-directory
identity when available, object format, known anchor objects/refs, absence of a
conflicting registered project, and user intent when proof is incomplete. It
increments `resource_version`; it does not create a new project merely because
the path changed. A copied clone with the same objects is not silently treated
as the same project.

### 6.3 Working copies

```sql
CREATE TABLE working_copies (
  working_copy_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  display_name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN
    ('primary','worktree','isolated','review')),
  purpose TEXT NOT NULL CHECK (purpose IN
    ('personal','workstream','integration','review','recovery','other')),
  path TEXT NOT NULL,
  git_dir TEXT,
  branch_ref TEXT,
  expected_head_oid TEXT,
  expected_tree_oid TEXT,
  effective_mode TEXT NOT NULL CHECK (effective_mode IN
    ('trusted-live','isolated','read-only')),
  desired_state TEXT NOT NULL CHECK (desired_state IN ('present','absent')),
  observed_state TEXT NOT NULL CHECK (observed_state IN
    ('unknown','creating','ready','dirty','drifted','missing','removing','error')),
  writer_epoch INTEGER NOT NULL DEFAULT 0 CHECK (writer_epoch >= 0),
  active_writer_run_id TEXT,
  resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
  controller_owned INTEGER NOT NULL CHECK (controller_owned IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_reconciled_at TEXT,
  error_code TEXT,
  error_detail TEXT,
  UNIQUE(project_id, path),
  UNIQUE(project_id, git_dir)
) STRICT;
```

`active_writer_run_id` intentionally has no late-added foreign key: SQLite does
not support adding that constraint safely in place. The operations layer MUST
validate it transactionally after inserting the run, and MUST provide triggers
or equivalent acceptance-tested checks that the named run is a writer for this
working copy with the same current `writer_epoch`. Deletion/terminalization of
a writer run clears the claim in the same transaction.

Unmanaged Git worktrees may be returned by inventory as observations without a
`working_copies` row. They become controller-owned only through explicit
adoption.

### 6.4 Conversations

```sql
CREATE TABLE conversations (
  conversation_id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(project_id),
  working_copy_id TEXT REFERENCES working_copies(working_copy_id),
  role TEXT NOT NULL CHECK (role IN
    ('secretary','personal','workstream','review','integration','host')),
  display_name TEXT NOT NULL,
  pi_session_id TEXT NOT NULL,
  session_file TEXT NOT NULL,
  desired_state TEXT NOT NULL CHECK (desired_state IN ('active','archived')),
  observed_state TEXT NOT NULL CHECK (observed_state IN
    ('unknown','ready','missing','conflict','error')),
  resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_reconciled_at TEXT,
  error_code TEXT,
  error_detail TEXT,
  UNIQUE(pi_session_id),
  UNIQUE(session_file)
) STRICT;
```

Role constraints enforced by triggers or operation validation:

- secretary requires `project_id` and NULL `working_copy_id`;
- personal requires `project_id`; a writable run requires an explicit working
  copy assignment;
- workstream/integration/review require both project and working copy;
- review working copy must be read-only and detached;
- host may have NULL project/working copy and remains outside normal controller
  trust.

### 6.5 Runs

```sql
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
  project_id TEXT REFERENCES projects(project_id),
  working_copy_id TEXT REFERENCES working_copies(working_copy_id),
  parent_run_id TEXT REFERENCES runs(run_id),
  authority TEXT NOT NULL CHECK (authority IN
    ('read-only','writer','secretary','host-maintenance')),
  desired_state TEXT NOT NULL CHECK (desired_state IN ('running','stopped')),
  observed_state TEXT NOT NULL CHECK (observed_state IN
    ('created','preparing','ready','running','stopping','stopped',
     'failed','lost','needs_attention')),
  expected_working_copy_version INTEGER,
  expected_head_oid TEXT,
  expected_tree_oid TEXT,
  dirty_fingerprint TEXT,
  writer_epoch INTEGER,
  runtime_spec_hash TEXT NOT NULL,
  build_id TEXT NOT NULL REFERENCES installed_builds(build_id),
  owner_pid INTEGER,
  owner_start_identity TEXT,
  capability_hash TEXT NOT NULL,
  manifest_path TEXT,
  resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
  created_at TEXT NOT NULL,
  started_at TEXT,
  ended_at TEXT,
  updated_at TEXT NOT NULL,
  error_code TEXT,
  error_detail TEXT
) STRICT;

CREATE INDEX runs_conversation_idx ON runs(conversation_id, created_at);
CREATE INDEX runs_working_copy_idx ON runs(working_copy_id, observed_state);

CREATE TRIGGER working_copy_active_writer_valid
BEFORE UPDATE OF active_writer_run_id, writer_epoch ON working_copies
WHEN NEW.active_writer_run_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM runs
  WHERE run_id = NEW.active_writer_run_id
    AND working_copy_id = NEW.working_copy_id
    AND authority = 'writer'
    AND writer_epoch = NEW.writer_epoch
    AND observed_state NOT IN ('stopped','failed','lost')
)
BEGIN
  SELECT RAISE(ABORT, 'invalid active writer claim');
END;

CREATE TRIGGER active_writer_run_not_deleted
BEFORE DELETE ON runs
WHEN EXISTS (
  SELECT 1 FROM working_copies
  WHERE active_writer_run_id = OLD.run_id
)
BEGIN
  SELECT RAISE(ABORT, 'active writer run is still claimed');
END;
```

A writer run must carry a non-NULL working copy, expected version, and writer
epoch. A secretary run has no writable working copy. A read-only child may bind
an immutable snapshot working copy or a read-only view.

### 6.6 Changes and revisions

```sql
CREATE TABLE changes (
  change_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  source_working_copy_id TEXT REFERENCES working_copies(working_copy_id),
  created_by_conversation_id TEXT REFERENCES conversations(conversation_id),
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  target_ref TEXT NOT NULL,
  baseline_oid TEXT NOT NULL,
  baseline_tree_oid TEXT NOT NULL,
  baseline_state_json TEXT NOT NULL CHECK (json_valid(baseline_state_json)),
  state TEXT NOT NULL CHECK (state IN ('draft','open','merged','closed')),
  current_revision INTEGER NOT NULL CHECK (current_revision >= 0),
  resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  submitted_at TEXT,
  merged_at TEXT,
  closed_at TEXT,
  CHECK ((state = 'draft' AND current_revision = 0) OR
         (state IN ('open','merged') AND current_revision >= 1) OR
         (state = 'closed' AND current_revision >= 0)),
  CHECK (state <> 'open' OR submitted_at IS NOT NULL),
  CHECK (state <> 'merged' OR (submitted_at IS NOT NULL AND merged_at IS NOT NULL)),
  CHECK (state <> 'closed' OR closed_at IS NOT NULL),
  CHECK (state <> 'draft' OR
         (submitted_at IS NULL AND merged_at IS NULL AND closed_at IS NULL))
) STRICT;

CREATE UNIQUE INDEX changes_one_draft_per_working_copy_uq
  ON changes(source_working_copy_id)
  WHERE state = 'draft' AND source_working_copy_id IS NOT NULL;

CREATE TABLE change_revisions (
  change_id TEXT NOT NULL REFERENCES changes(change_id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  base_oid TEXT NOT NULL,
  tip_oid TEXT NOT NULL,
  tree_oid TEXT NOT NULL,
  source_head_oid TEXT,
  capture_mode TEXT NOT NULL CHECK (capture_mode IN
    ('branch-tip','temporary-index','integration-result')),
  source_status_hash TEXT,
  ref_name TEXT NOT NULL,
  changed_paths_json TEXT NOT NULL CHECK (json_valid(changed_paths_json)),
  diffstat_json TEXT NOT NULL CHECK (json_valid(diffstat_json)),
  verification_json TEXT NOT NULL CHECK (json_valid(verification_json)),
  provenance_json TEXT NOT NULL CHECK (json_valid(provenance_json)),
  created_at TEXT NOT NULL,
  PRIMARY KEY(change_id, revision),
  UNIQUE(ref_name),
  UNIQUE(change_id, tip_oid, tree_oid)
) STRICT;

CREATE TABLE change_revision_inputs (
  result_change_id TEXT NOT NULL,
  result_revision INTEGER NOT NULL,
  input_change_id TEXT NOT NULL,
  input_revision INTEGER NOT NULL,
  relation TEXT NOT NULL CHECK (relation IN
    ('supersedes','adapts','includes','resolves-conflict-with')),
  PRIMARY KEY(result_change_id, result_revision,
              input_change_id, input_revision, relation),
  FOREIGN KEY(result_change_id, result_revision)
    REFERENCES change_revisions(change_id, revision),
  FOREIGN KEY(input_change_id, input_revision)
    REFERENCES change_revisions(change_id, revision),
  CHECK (result_change_id <> input_change_id OR
         result_revision <> input_revision)
) STRICT;

CREATE TRIGGER changes_current_revision_exists_on_update
BEFORE UPDATE OF state, current_revision ON changes
WHEN NEW.current_revision > 0 AND NOT EXISTS (
  SELECT 1 FROM change_revisions
  WHERE change_id = NEW.change_id AND revision = NEW.current_revision
)
BEGIN
  SELECT RAISE(ABORT, 'current change revision does not exist');
END;

CREATE TRIGGER changes_current_revision_exists_on_insert
BEFORE INSERT ON changes
WHEN NEW.current_revision > 0 AND NOT EXISTS (
  SELECT 1 FROM change_revisions
  WHERE change_id = NEW.change_id AND revision = NEW.current_revision
)
BEGIN
  SELECT RAISE(ABORT, 'current change revision does not exist');
END;
```

Rows in `change_revisions` and their input links are immutable. Corrections
create a new revision. The controller verifies that each `ref_name` resolves to
`tip_oid` and that its tree is `tree_oid`. A draft change is the durable
conventional work record used to capture a personal/workstream baseline before
mutation; it becomes open only when revision 1 is submitted. It is not shown as
awaiting integration before submission. An integration result on the same
logical feature is normally a new revision linked with `adapts`/`supersedes`;
a result combining independent changes is a new change linked to every exact
input revision.

### 6.7 Reviews and integration attempts

```sql
CREATE TABLE reviews (
  review_id TEXT PRIMARY KEY,
  change_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  reviewer_conversation_id TEXT REFERENCES conversations(conversation_id),
  verdict TEXT CHECK (verdict IN ('accept','changes_requested','comment')),
  summary TEXT,
  findings TEXT,
  evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
  state TEXT NOT NULL CHECK (state IN ('requested','running','submitted','cancelled','failed')),
  created_at TEXT NOT NULL,
  submitted_at TEXT,
  FOREIGN KEY(change_id, revision) REFERENCES change_revisions(change_id, revision)
) STRICT;

CREATE TABLE integration_attempts (
  integration_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  change_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  requested_target_oid TEXT NOT NULL,
  strategy TEXT NOT NULL CHECK (strategy IN
    ('fast-forward','merge','integration-worktree')),
  state TEXT NOT NULL CHECK (state IN
    ('planned','applying','needs_resolution','succeeded','failed','cancelled')),
  result_oid TEXT,
  rollback_ref TEXT,
  operation_id TEXT,
  analysis_json TEXT NOT NULL CHECK (json_valid(analysis_json)),
  verification_json TEXT NOT NULL CHECK (json_valid(verification_json)),
  resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  error_code TEXT,
  error_detail TEXT,
  FOREIGN KEY(change_id, revision) REFERENCES change_revisions(change_id, revision)
) STRICT;
```

A review acceptance is evidence, not merge authorization. An integration row
cannot enter `applying` without a controller-recorded user authorization bound
to that change, revision, target, and current turn/request.

### 6.8 Authorizations, operations, and events

```sql
CREATE TABLE authorizations (
  authorization_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN
    ('create-workstream','submit-change','integrate-change','close-change',
     'cleanup','publish','host-command','migration-cutover')),
  actor_type TEXT NOT NULL CHECK (actor_type IN ('user','controller','host-admin')),
  actor_id TEXT,
  project_id TEXT REFERENCES projects(project_id),
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  request_context_id TEXT NOT NULL,
  scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
  scope_digest TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  expires_at TEXT,
  consumed_at TEXT,
  state TEXT NOT NULL CHECK (state IN ('active','consumed','cancelled','expired')),
  UNIQUE(request_context_id, kind, resource_type, resource_id, scope_digest)
) STRICT;

CREATE TABLE operations (
  operation_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  actor_type TEXT NOT NULL,
  actor_id TEXT,
  authorization_id TEXT REFERENCES authorizations(authorization_id),
  request_digest TEXT NOT NULL,
  expected_resource_version INTEGER,
  writer_epoch INTEGER,
  state TEXT NOT NULL CHECK (state IN
    ('planned','applying','succeeded','failed','needs_attention','cancelled')),
  step TEXT NOT NULL,
  request_json TEXT NOT NULL CHECK (json_valid(request_json)),
  result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  error_code TEXT,
  error_detail TEXT
) STRICT;

CREATE TABLE control_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  event_kind TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  resource_version INTEGER,
  operation_id TEXT REFERENCES operations(operation_id),
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE event_consumers (
  consumer_id TEXT PRIMARY KEY,
  last_sequence INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE attention (
  attention_id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(project_id),
  conversation_id TEXT REFERENCES conversations(conversation_id),
  run_id TEXT REFERENCES runs(run_id),
  change_id TEXT REFERENCES changes(change_id),
  kind TEXT NOT NULL,
  summary TEXT NOT NULL,
  detail_json TEXT NOT NULL CHECK (json_valid(detail_json)),
  state TEXT NOT NULL CHECK (state IN ('open','acknowledged','resolved')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE migration_runs (
  migration_id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
  idempotency_key TEXT NOT NULL UNIQUE,
  mode TEXT NOT NULL CHECK (mode IN
    ('inventory','shadow-import','final-import','canary-cutover','rollback')),
  controller_build_id TEXT NOT NULL REFERENCES installed_builds(build_id),
  request_digest TEXT NOT NULL,
  source_manifest_digest TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN
    ('planned','applying','succeeded','failed','needs_attention','rolled_back')),
  step TEXT NOT NULL,
  result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
  resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  error_code TEXT,
  error_detail TEXT
) STRICT;

CREATE TABLE migration_manifests (
  migration_id TEXT NOT NULL REFERENCES migration_runs(migration_id),
  kind TEXT NOT NULL CHECK (kind IN
    ('source-inventory','contradictions','backup','build','comparison','rollback-proof')),
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
  created_at TEXT NOT NULL,
  PRIMARY KEY(migration_id, kind)
) STRICT;

CREATE TRIGGER migration_runs_request_immutable
BEFORE UPDATE OF migration_id, operation_id, idempotency_key, mode,
                 controller_build_id, request_digest, source_manifest_digest,
                 created_at
ON migration_runs
BEGIN
  SELECT RAISE(ABORT, 'migration request identity is immutable');
END;

CREATE TRIGGER migration_runs_require_source_manifest
BEFORE UPDATE OF state, step ON migration_runs
WHEN NEW.state <> 'planned' AND NOT EXISTS (
  SELECT 1 FROM migration_manifests
  WHERE migration_id = NEW.migration_id
    AND kind = 'source-inventory'
    AND sha256 = NEW.source_manifest_digest
)
BEGIN
  SELECT RAISE(ABORT, 'migration source manifest is missing or mismatched');
END;

CREATE TRIGGER migration_runs_no_delete
BEFORE DELETE ON migration_runs
BEGIN
  SELECT RAISE(ABORT, 'migration run history cannot be deleted');
END;

CREATE TRIGGER migration_manifests_immutable_update
BEFORE UPDATE ON migration_manifests
BEGIN
  SELECT RAISE(ABORT, 'migration manifest is immutable');
END;

CREATE TRIGGER migration_manifests_immutable_delete
BEFORE DELETE ON migration_manifests
BEGIN
  SELECT RAISE(ABORT, 'migration manifest is immutable');
END;
```

State mutation and its control event MUST commit in the same SQLite transaction
(transactional outbox pattern). Consumers update cursors separately; delivery
failure cannot roll back the state transition or erase the event.

Migration lifecycle state is authoritative in `migration_runs`; its request
identity and append-only `migration_manifests` are immutable, while state/step/
result advance by resource-version CAS with events. Creation inserts the run
and its `source-inventory` manifest row in one transaction and requires that
row's SHA-256 to equal `source_manifest_digest`. Manifest files live under
`${XDG_STATE_HOME:-~/.local/state}/pi-control/migrations/<migration-id>/` in the
same secure-root model as the database. They are created with `O_EXCL` through
a same-filesystem temp-file/fsync/rename sequence, finalized regular `0400`,
and never overwritten by the controller. The table stores their exact hashes
and sizes. A crash resumes the linked `operations` saga by inspecting these records
and actual state; it never infers completion from a file's existence alone.
Pre-database read-only inventory may write a manifest only to a newly created
secure migration directory; the first shadow database transaction imports and
hash-verifies it before any migration lifecycle can advance.

## 7. Core invariants

The database and operations layer MUST enforce:

### I-01 Stable identity

Paths, branch names, refs, PIDs, container names, route paths, pane IDs, and
session filenames never serve as logical IDs.

### I-02 One project trust decision

Effective trust starts from the registered project. A working copy may narrow
trust to isolated/read-only; it may not broaden project trust. A verified linked
worktree inherits project trust regardless of storage path.

### I-03 Explicit working-copy assignment

Every writable run references exactly one working copy. Secretary runs reference
none. No launcher or sandbox may infer assignment from current cwd after the
controller has selected it.

### I-04 One writer

At most one writer run may hold the live kernel lock and current writer epoch
for a working copy. Read-only views must be mechanically read-only or immutable;
a role label alone is insufficient.

### I-05 Fenced mutation

Every controller-mediated mutation includes run ID, working-copy ID, expected
resource version, and writer epoch. A stale epoch or version fails before side
effects.

### I-06 Exact execution

A run becomes ready only after runtime attestation agrees with its immutable
manifest. "Latest" and path heuristics are forbidden.

### I-07 Immutable submissions

A submitted change revision never changes. New source work creates a new
revision. Reviews and integrations bind exact `(change_id, revision)`.

### I-08 Target compare-and-swap

Final integration names exact expected target OID and desired new OID. Target
movement returns `needs_resolution`; it never triggers implicit overwrite,
reset, or rebase.

### I-09 Durable intent before side effect

Every consequential multi-resource operation records intent and expected
versions before creating/removing worktrees, refs, sessions, containers, or
runtime records.

### I-10 Reconciliation over guessing

After a crash or disagreement, adapters observe and the controller classifies.
Only one provable state may be repaired automatically. Ambiguous state is
retained and surfaced for decision.

### I-11 No implicit destructive cleanup

Missing process, old timestamp, merged branch, clean worktree, or absent pane is
not sufficient deletion authority. Cleanup requires controller ownership,
exact expected state, no live use, retention eligibility, and explicit policy or
user authorization.

### I-12 Build identity

Every run binds one tested installed build. A newer repository source file or
successful installer log cannot retroactively change a live run.

## 8. Resource versions and compare-and-swap

Every mutable resource starts at `resource_version = 1`. Each committed state
change increments it exactly once.

Update pattern:

```sql
UPDATE working_copies
SET desired_state = ?, resource_version = resource_version + 1, updated_at = ?
WHERE working_copy_id = ? AND resource_version = ?;
```

Affected row count MUST be one. Zero means stale caller state; the operation
re-reads and either retries a pure read/plan or returns a conflict. It MUST NOT
blindly overwrite.

Git ref changes use the equivalent primitive:

```bash
git update-ref <ref> <new-oid> <expected-old-oid>
```

SQLite and Git cannot share one atomic transaction. The operation journal plus
idempotent reconciliation bridges them as specified below.

## 9. Operation protocol

Every mutating command accepts or generates an idempotency key. Retries with the
same key and identical request return the recorded result. The same key with a
different request fails.

Generic protocol:

1. Validate caller authority and bounded input.
2. Open `BEGIN IMMEDIATE`.
3. Read resources and check expected versions.
4. Insert `operations(state='planned', step='intent-recorded')` and desired
   resource state.
5. Commit.
6. Acquire the required ordered kernel locks.
7. Mark operation `applying` with CAS.
8. Perform one idempotent external step.
9. Observe actual state independently.
10. Transactionally update observed resource state, operation step/outcome,
    and control event.
11. Release locks.

If the process dies at steps 6-10, a later reconcile reads operation intent and
observations. It either continues the exact operation, records success already
achieved, safely compensates a controller-created resource, or marks
`needs_attention`. External side effects are **at-least-once attempts with
idempotent preconditions and post-observation**, never a claim of exactly-once
execution. The reconciler never reissues a destructive action without proving
its preconditions.

### 9.1 Lock order

To avoid deadlocks, acquire only needed locks in this order:

1. global schema/migration lock;
2. project Git-common-directory lock;
3. working-copy lifecycle/index lock (ordered by working-copy ID if more than
   one is required);
4. working-copy writer lock (same working-copy ID order);
5. target-ref/integration lock;
6. review/change lock.

Integration follows exactly this order: project, checked-out target working-copy
lifecycle/index, target writer when required, target-ref/integration, then
review/change. It never acquires the target-ref lock before a required target
working-copy lock. Immutable candidate refs do not require locking the source
working copy.

Code MUST NOT acquire an earlier lock while holding a later lock. Nested helper
calls receive an existing lock/operation context rather than reacquiring it.
This directly prevents the observed non-reentrant transition failure.

## 10. Writer leases and fencing

### 10.1 Kernel lock

A writer holds an exclusive `flock` file descriptor for the lifetime of the
writable run. Lock path is derived from controller working-copy ID under a
user-owned runtime directory, not from an untrusted model/path string.

Requirements:

- open with `O_CREAT|O_RDWR|O_NOFOLLOW` where supported;
- regular user-owned `0600` file;
- parent directory `0700`;
- lock file content is diagnostic only;
- lock release is automatic on process death;
- no JSON "active transition" marker is the live lock.

### 10.2 Writer epoch

While holding the lifecycle and writer locks, controller increments
`working_copies.writer_epoch` and assigns the value to the new run. Every host
bridge and tool proxy checks it before mutation.

Fencing protects controller/Git operations from stale processes. It does not by
itself revoke a process already writing through a trusted-live mount. Therefore:

- a second writer is not granted while an old writable container/process may
  still access the working copy;
- revocation stops the old tool proxy and container or fails closed;
- an unknown process state returns `needs_attention` rather than advancing the
  epoch and hoping;
- container/run labels include working-copy ID and epoch for observation only.

### 10.3 Delegated writer

A parent may delegate exclusive writing to one child under the same working
copy only through an explicit sublease operation that blocks parent mutation
until the child ends. The MVP MAY initially route every independent writer to a
separate working copy instead; it MUST NOT allow concurrent parent/child writes
because both claim the same task.

## 11. Trust contract

Project registration records the host-approved trust result and policy hash.

Effective mode:

```text
trusted project + default working copy     -> trusted-live
trusted project + explicit isolated copy   -> isolated
trusted project + review view              -> read-only
isolated project + any normal working copy -> isolated
```

Rules:

- path under a trusted root is input to registration, not perpetual identity;
- linked worktree trust is proven through project ID, Git common directory,
  object format, and controller record;
- an isolated project cannot be broadened by project files, model text,
  extension flags, or a managed path;
- trust changes affect new runs after resource-version update;
- tightening trust prevents new trusted-live starts and drains/stops old runs
  under explicit policy; it never silently broadens an active run;
- Pi `project_trust` hook and sandbox classification query the same controller
  decision.

## 12. Reconciliation contract

Reconciliation is read-observe-classify first. Mutating repair is separate.

For each resource:

1. Load desired controller record and pending operations.
2. Observe Git/filesystem/session/process/container/presentation state through
   bounded adapters.
3. Normalize observation without writing.
4. Compare expected and observed.
5. Classify:
   - `ready`: exact agreement;
   - `repairable`: one idempotent controller-owned action with exact proof;
   - `drifted`: external change, no automatic mutation;
   - `missing`: expected resource absent;
   - `ambiguous`: multiple plausible continuations;
   - `error`: adapter cannot safely observe.
6. Persist observation and event.
7. Apply repair only when operation policy allows and proof is unambiguous.

### 12.1 Examples

- Missing controller-created worktree, branch still exists at descendant of
  recorded base, no registration: repair may recreate after exact checks.
- Worktree HEAD differs from expected while a writer is active: observation,
  not repair; expected source state may legitimately advance through a
  controller-recorded update.
- Container missing for desired stopped run: ready.
- Container missing for desired running run: recreate from manifest if no
  writer ambiguity.
- Two refs contain divergent descendants after crash: ambiguous; preserve both.
- Session file header cwd differs from controller working copy: update display
  projection only after explicit compatible migration; never rebind controller
  from header.

## 13. Adapter interface

Every adapter exposes pure observation separately from mutation.

Conceptual Python protocol:

```python
class Adapter(Protocol):
    def observe(self, resource, *, signal=None) -> Observation: ...
    def plan(self, desired, observed) -> list[Action]: ...
    def apply(self, action, operation_context, *, signal=None) -> ApplyResult: ...
```

Requirements:

- observations are bounded, typed, and contain evidence/provenance;
- actions have stable idempotency keys;
- apply validates the operation context, resource version, locks, and fencing;
- adapters do not update controller rows directly;
- subprocesses run with sanitized Git environment and bounded output/timeouts;
- no adapter chooses a different resource because the requested one is absent;
- presentation adapters never create controller identities.

## 14. Security and authority

### 14.1 Threat model

The controller is designed to resist:

- model/project-controlled strings attempting path, ref, SQL, command, config,
  hook, environment, or capability injection;
- an untrusted repository attempting to broaden trust or mount host resources;
- stale Pi/child/runtime processes using old routes, capabilities, epochs, or
  approvals;
- concurrent legitimate processes racing state/ref/worktree operations;
- crashes and uncertain external side effects;
- PID reuse, symlink substitution, malformed records, duplicate events, and
  stale observations;
- accidental cross-project/session/artifact disclosure.

It does not claim to defend against:

- a malicious process already running as the same host user with unrestricted
  filesystem/process access;
- root, Docker-socket-equivalent authority, kernel compromise, or hostile
  administrator;
- direct external Git/filesystem edits by the user. Those are detected as drift
  and preserved, not prevented;
- remote repository compromise outside explicit publication operations.

`pi-host` remains an explicit high-authority mode. These limitations must be
stated in activation and user diagnostics; they do not justify weakening normal
sandbox/controller checks.

### 14.2 Enforcement rules

- Controller CLI has semantic subcommands, not arbitrary shell/Git arguments.
- Read-only commands cannot acquire mutation capability accidentally.
- User approvals are bound to exact operation type, project, resource, expected
  version/OID, and current interaction.
- Capability tokens are random, short-lived where possible, and hash-stored.
- Route/manifests are owned `0600` regular files outside repositories.
- Model-controlled text never becomes a filesystem path, ref, container name,
  SQL identifier, or command without strict structured validation.
- SQL uses parameters exclusively.
- Every filesystem traversal rejects symlink substitution where authority
  depends on inode/path identity.
- No remote push, deployment, or production mutation is implied by local
  integration.

## 15. Error codes

Machine errors use stable codes; UI translates them.

Minimum set:

```text
CP_INVALID_REQUEST
CP_SCHEMA_NEWER
CP_SQLITE_UNSUPPORTED
CP_DB_UNSAFE
CP_RESOURCE_STALE
CP_IDEMPOTENCY_CONFLICT
CP_LOCK_BUSY
CP_WRITER_STALE
CP_WRITER_UNKNOWN
CP_PROJECT_DRIFT
CP_WORKING_COPY_DRIFT
CP_WORKING_COPY_MISSING
CP_CONVERSATION_CONFLICT
CP_RUN_ATTESTATION_FAILED
CP_RUNTIME_UNAVAILABLE
CP_GIT_REF_MOVED
CP_CHANGE_AMBIGUOUS
CP_INTEGRATION_CONFLICT
CP_OPERATION_AMBIGUOUS
CP_ADAPTER_UNAVAILABLE
CP_MIGRATION_UNRESOLVED
CP_ACTIVATION_UNAVAILABLE
CP_ACTIVATION_MISMATCH
CP_WORKSTREAM_CONFLICT
CP_PRESENTATION_UNKNOWN
CP_BUILD_MISMATCH
CP_PERMISSION_INVALID
```

Error details include operation/resource IDs and technical evidence only in
bounded diagnostics. User messages follow `PRODUCT_CONTRACT.md`.

## 16. Completion resources and activation bootstrap

The original schema did not make durable workstreams, presentation selection,
per-project activation, or record-level migration disposition mechanically
explicit. The next additive schema migration after current schema v6 is **v7**
and MUST add the following resources before launcher cutover. It MUST NOT edit
or reuse an earlier migration.

### 16.1 Workstreams

A workstream is a stable controller resource with a `ws_` random ID. It binds:

```text
workstream_id
project_id
working_copy_id
conversation_id
title
bounded brief_json
target_ref
starting_oid
desired_state: active | retired
observed_state: planned | creating | ready | stopped | drifted | missing | error
controller_owned
resource_version
created_at / updated_at / last_reconciled_at
error_code / bounded error_detail
```

The linked project, working copy, and conversation MUST agree. The working copy
MUST be a separate controller-owned worktree/isolated copy with purpose
`workstream` or `integration`. One non-retired workstream may own a working copy
and conversation. Its separate presentation assignment is the sole desired
backend record. Creation records the workstream, desired working copy, and
desired conversation before any Git/session/presentation/runtime side effect.
Ready means every required resource and run has been independently observed;
row creation alone is not readiness.

### 16.2 Presentation assignments

A presentation assignment binds one controller conversation to a desired
backend and a bounded current adapter observation:

```text
presentation_assignment_id
conversation_id
backend: tmux | herdr
desired_state: present | absent
observed_state: unknown | present | missing | drifted | error
locator_json
resource_version
observed_at / updated_at
error_code / bounded error_detail
```

`locator_json` may contain exact external session/window/pane/Space/tab IDs, but
those values are mutable observations. They never identify or rebind the
project, working copy, conversation, workstream, or run. Backend changes require
a stopped/quiesced exact process or fail closed; presentation does not migrate a
live process.

### 16.3 Project activation

A `project_activations` row binds:

```text
project_id (one row per registered project)
mode: legacy | shadow | controller
controller_build_id
migration_id
expected_project_version
resource_version
created_at / updated_at / activated_at
```

Transitions use a typed operation, CAS, and transactional event. A `legacy`
row has NULL build/migration bindings. A `shadow` row has non-NULL build and
migration bindings; its build may be `staged` or `active` because it cannot own
external writers. A `controller` row has non-NULL bindings and requires the
named build to be `active`, the named migration to have succeeded for the exact
inventory/resolution, the project version to match, and no blocking mapping or
attention in scope. Direct `legacy -> controller` is forbidden; the project
passes through `shadow`. `controller -> legacy` is permitted only through an
authorized rollback after exact writers are quiesced. A model or caller
environment cannot select mode.

Bootstrapping uses the secure, host-owned
`${XDG_STATE_HOME:-~/.local/state}/pi-control/activation.v1.json` latch defined
by `COMPLETION_IMPLEMENTATION_PLAN.md` §4.3. The latch is a fail-closed
projection of activation rows, not a second lifecycle store. In `shadow` or
`controller` mode it and SQLite MUST agree exactly. Missing/malformed/mismatched
latch or DB never causes controller-mode fallback to legacy.

### 16.4 Migration resource mappings

Every inventory record receives an immutable mapping row containing:

```text
migration_id
record_id
adapter_kind
source_kind
source_digest
resource_type
resource_id (nullable)
disposition: import | observe | unmigrated | exclude | requires-decision | contradiction
reason_code
bounded detail_json
created_at
```

The primary key is `(migration_id, record_id)`. Source identity/digest,
disposition, and target resource link are immutable and rows cannot be deleted.
A correction uses a new inventory/migration. `import` references an exact
controller resource; `observe` never creates authority; `requires-decision` and
`contradiction` block a canary whose scope contains the record. Every configured
adapter also records `observed`, `unavailable`, `error`, or `unsupported`; an
unavailable adapter cannot be represented as an empty successful result.

### 16.5 Authorization vocabulary

Schema v7 MUST rebuild the `authorizations` CHECK constraint through its
transactional migration and allow exactly these consequential kinds:

```text
create-workstream
relaunch-workstream
retire-workstream
adopt-working-copy
archive-conversation
submit-change
close-change
integrate-change
publish
cleanup
host-command
migration-resolve
migration-cutover
activation-change
```

Read-only inspect/plan/focus operations do not manufacture authorization rows.
Internal run preparation/reconciliation uses controller operation authority,
not a forged user authorization. `migration-resolve`, `migration-cutover`, and
`activation-change` bind exact inventory/resolution/migration/build/project
versions; cutover/activation apply is host-only. A kind never authorizes another
kind.

### 16.6 Mechanical checks

Schema v7 and its operations MUST enforce and test:

- project consistency across workstream, working copy, conversation, and
  presentation assignment;
- resource-version CAS on every mutable row;
- state mutation and event insertion in one transaction;
- immutable migration mappings;
- legal activation transitions and exact build/migration/project bindings;
- no controller-mode legacy fallback or dual writer;
- no presentation locator as lifecycle identity;
- v6→v7 and fresh-v7 schema equality plus unknown-newer refusal.

The full field constraints, protocol operations, typed manifest formats, and
implementation order are frozen in `COMPLETION_IMPLEMENTATION_PLAN.md` §§5–8.

## 17. Explicit non-goals

- SQLite is not source-content storage.
- Control events are not a full event-sourced model; current resource tables
  remain authoritative.
- Reconciliation is not permission to reset externally changed work.
- Fencing is not claimed to revoke an arbitrary process that still has a direct
  writable filesystem descriptor.
- A newer timestamp is never conflict resolution.
- A daemon is not required for correctness.
