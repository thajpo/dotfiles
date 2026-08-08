"""SQLite capability checks and the immutable Phase 2 schema definition."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Iterable

from .errors import SQLiteUnsupportedError

SCHEMA_VERSION = 7
MIGRATION_VERSION = 7
MIGRATION_NAME = "completion-resources"

# Applied after the v2 child-source schema.  Payload bytes remain on the
# filesystem; SQLite stores only the immutable index and lifecycle record.
ARTIFACT_SCHEMA_SQL = r'''
CREATE TABLE artifact_manifests (
  artifact_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  manifest_path TEXT NOT NULL UNIQUE,
  manifest_digest TEXT NOT NULL,
  checksum TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
  sensitive INTEGER NOT NULL CHECK (sensitive IN (0,1)),
  retention_class TEXT NOT NULL CHECK (retention_class IN
    ('run','change','recovery','debug')),
  created_at TEXT NOT NULL,
  expires_at TEXT,
  resource_version INTEGER NOT NULL CHECK (resource_version >= 1)
) STRICT;

CREATE TABLE child_terminal_records (
  child_run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
  parent_run_id TEXT NOT NULL REFERENCES runs(run_id),
  terminal_class TEXT NOT NULL CHECK (terminal_class IN
    ('success','failed','lost','attention')),
  changed_state TEXT NOT NULL CHECK (changed_state IN ('clean','dirty','unknown')),
  submission_class TEXT NOT NULL CHECK (submission_class IN ('none','submitted-change')),
  submitted_change_id TEXT,
  submitted_revision INTEGER,
  artifact_id TEXT REFERENCES artifact_manifests(artifact_id),
  result_json TEXT NOT NULL CHECK (json_valid(result_json)),
  provenance_json TEXT NOT NULL CHECK (json_valid(provenance_json)),
  terminal_digest TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (submitted_change_id, submitted_revision)
    REFERENCES change_revisions(change_id, revision),
  CHECK ((submission_class = 'none' AND submitted_change_id IS NULL AND submitted_revision IS NULL)
      OR (submission_class = 'submitted-change' AND submitted_change_id IS NOT NULL AND submitted_revision IS NOT NULL))
) STRICT;

CREATE TRIGGER artifact_manifests_immutable_update
BEFORE UPDATE ON artifact_manifests
BEGIN
  SELECT RAISE(ABORT, 'artifact manifest is immutable');
END;

CREATE TRIGGER artifact_manifests_immutable_delete
BEFORE DELETE ON artifact_manifests
BEGIN
  SELECT RAISE(ABORT, 'artifact manifest is immutable');
END;

CREATE TRIGGER child_terminal_record_shape_valid
BEFORE INSERT ON child_terminal_records
WHEN NOT EXISTS (
  SELECT 1 FROM runs
  WHERE run_id = NEW.child_run_id
    AND parent_run_id = NEW.parent_run_id
    AND child_source_json IS NOT NULL
)
BEGIN
  SELECT RAISE(ABORT, 'child terminal record lineage is invalid');
END;

CREATE TRIGGER child_terminal_records_immutable_update
BEFORE UPDATE ON child_terminal_records
BEGIN
  SELECT RAISE(ABORT, 'child terminal record is immutable');
END;

CREATE TRIGGER child_terminal_records_immutable_delete
BEFORE DELETE ON child_terminal_records
BEGIN
  SELECT RAISE(ABORT, 'child terminal record is immutable');
END;
'''

REVISION_SCHEMA_SQL = r'''
CREATE TRIGGER change_revisions_immutable_update
BEFORE UPDATE ON change_revisions
BEGIN
  SELECT RAISE(ABORT, 'change revision is immutable');
END;

CREATE TRIGGER change_revisions_immutable_delete
BEFORE DELETE ON change_revisions
BEGIN
  SELECT RAISE(ABORT, 'change revision is immutable');
END;

CREATE TRIGGER change_revision_inputs_immutable_update
BEFORE UPDATE ON change_revision_inputs
BEGIN
  SELECT RAISE(ABORT, 'change revision input is immutable');
END;

CREATE TRIGGER change_revision_inputs_immutable_delete
BEFORE DELETE ON change_revision_inputs
BEGIN
  SELECT RAISE(ABORT, 'change revision input is immutable');
END;
'''

RECEIPT_OPERATION_IMMUTABILITY_SQL = r'''
CREATE TRIGGER submitted_review_receipt_immutable
BEFORE UPDATE ON reviews
WHEN OLD.state = 'submitted'
  AND (NEW.review_id IS NOT OLD.review_id
       OR NEW.change_id IS NOT OLD.change_id
       OR NEW.revision IS NOT OLD.revision
       OR NEW.verdict IS NOT OLD.verdict
       OR NEW.summary IS NOT OLD.summary
       OR NEW.findings IS NOT OLD.findings
       OR NEW.evidence_json IS NOT OLD.evidence_json
       OR NEW.state IS NOT OLD.state
       OR NEW.created_at IS NOT OLD.created_at
       OR NEW.submitted_at IS NOT OLD.submitted_at)
BEGIN
  SELECT RAISE(ABORT, 'submitted review receipt is immutable');
END;

CREATE TRIGGER submitted_review_receipt_no_delete
BEFORE DELETE ON reviews
WHEN OLD.state = 'submitted'
BEGIN
  SELECT RAISE(ABORT, 'submitted review receipt is immutable');
END;

CREATE TRIGGER terminal_operation_immutable
BEFORE UPDATE ON operations
WHEN OLD.state IN ('succeeded','failed','needs_attention','cancelled')
  AND (NEW.operation_id IS NOT OLD.operation_id
       OR NEW.idempotency_key IS NOT OLD.idempotency_key
       OR NEW.kind IS NOT OLD.kind
       OR NEW.resource_type IS NOT OLD.resource_type
       OR NEW.resource_id IS NOT OLD.resource_id
       OR NEW.actor_type IS NOT OLD.actor_type
       OR NEW.actor_id IS NOT OLD.actor_id
       OR NEW.authorization_id IS NOT OLD.authorization_id
       OR NEW.request_digest IS NOT OLD.request_digest
       OR NEW.expected_resource_version IS NOT OLD.expected_resource_version
       OR NEW.writer_epoch IS NOT OLD.writer_epoch
       OR NEW.state IS NOT OLD.state
       OR NEW.step IS NOT OLD.step
       OR NEW.request_json IS NOT OLD.request_json
       OR NEW.result_json IS NOT OLD.result_json
       OR NEW.created_at IS NOT OLD.created_at
       OR NEW.updated_at IS NOT OLD.updated_at
       OR NEW.completed_at IS NOT OLD.completed_at
       OR NEW.error_code IS NOT OLD.error_code
       OR NEW.error_detail IS NOT OLD.error_detail)
BEGIN
  SELECT RAISE(ABORT, 'terminal operation is immutable');
END;

CREATE TRIGGER terminal_operation_no_delete
BEFORE DELETE ON operations
WHEN OLD.state IN ('succeeded','failed','needs_attention','cancelled')
BEGIN
  SELECT RAISE(ABORT, 'terminal operation is immutable');
END;

CREATE TRIGGER authorization_scope_immutable
BEFORE UPDATE OF kind, actor_type, actor_id, project_id, resource_type,
                 resource_id, request_context_id, scope_json, scope_digest,
                 issued_at, expires_at ON authorizations
WHEN NEW.kind IS NOT OLD.kind
  OR NEW.actor_type IS NOT OLD.actor_type
  OR NEW.actor_id IS NOT OLD.actor_id
  OR NEW.project_id IS NOT OLD.project_id
  OR NEW.resource_type IS NOT OLD.resource_type
  OR NEW.resource_id IS NOT OLD.resource_id
  OR NEW.request_context_id IS NOT OLD.request_context_id
  OR NEW.scope_json IS NOT OLD.scope_json
  OR NEW.scope_digest IS NOT OLD.scope_digest
  OR NEW.issued_at IS NOT OLD.issued_at
  OR NEW.expires_at IS NOT OLD.expires_at
BEGIN
  SELECT RAISE(ABORT, 'authorization scope is immutable');
END;

CREATE TRIGGER authorization_terminal_immutable
BEFORE UPDATE OF state, consumed_at ON authorizations
WHEN OLD.state IN ('consumed','cancelled','expired')
  AND (NEW.state IS NOT OLD.state OR NEW.consumed_at IS NOT OLD.consumed_at)
BEGIN
  SELECT RAISE(ABORT, 'terminal authorization is immutable');
END;

CREATE TRIGGER authorization_no_delete
BEFORE DELETE ON authorizations
BEGIN
  SELECT RAISE(ABORT, 'authorization history is immutable');
END;
'''

# Schema-v7 replaces the authorization vocabulary transactionally during the
# v6 upgrade.  Fresh databases use the same final table definition directly.
AUTHORIZATION_V7_TABLE_SQL = r'''
CREATE TABLE authorizations (
  authorization_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN
    ('create-workstream','relaunch-workstream','retire-workstream',
     'adopt-working-copy','archive-conversation','submit-change','close-change',
     'integrate-change','publish','cleanup','host-command','migration-resolve',
     'migration-cutover','activation-change')),
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
'''

# These triggers are re-created after the v6 authorization table is rebuilt.
# The other v6 immutability triggers remain in place during the migration.
AUTHORIZATION_IMMUTABILITY_SQL = r'''
CREATE TRIGGER authorization_scope_immutable
BEFORE UPDATE OF kind, actor_type, actor_id, project_id, resource_type,
                 resource_id, request_context_id, scope_json, scope_digest,
                 issued_at, expires_at ON authorizations
WHEN NEW.kind IS NOT OLD.kind
  OR NEW.actor_type IS NOT OLD.actor_type
  OR NEW.actor_id IS NOT OLD.actor_id
  OR NEW.project_id IS NOT OLD.project_id
  OR NEW.resource_type IS NOT OLD.resource_type
  OR NEW.resource_id IS NOT OLD.resource_id
  OR NEW.request_context_id IS NOT OLD.request_context_id
  OR NEW.scope_json IS NOT OLD.scope_json
  OR NEW.scope_digest IS NOT OLD.scope_digest
  OR NEW.issued_at IS NOT OLD.issued_at
  OR NEW.expires_at IS NOT OLD.expires_at
BEGIN
  SELECT RAISE(ABORT, 'authorization scope is immutable');
END;

CREATE TRIGGER authorization_terminal_immutable
BEFORE UPDATE OF state, consumed_at ON authorizations
WHEN OLD.state IN ('consumed','cancelled','expired')
  AND (NEW.state IS NOT OLD.state OR NEW.consumed_at IS NOT OLD.consumed_at)
BEGIN
  SELECT RAISE(ABORT, 'terminal authorization is immutable');
END;

CREATE TRIGGER authorization_no_delete
BEFORE DELETE ON authorizations
BEGIN
  SELECT RAISE(ABORT, 'authorization history is immutable');
END;
'''

# Completion resources are deliberately additive.  External-state predicates
# (active build, successful migration, quiesced writers) stay in operations.
COMPLETION_SCHEMA_SQL = r'''
CREATE TABLE workstreams (
  workstream_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  working_copy_id TEXT NOT NULL REFERENCES working_copies(working_copy_id),
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
  title TEXT NOT NULL,
  brief_json TEXT NOT NULL CHECK (json_valid(brief_json) AND length(CAST(brief_json AS BLOB)) <= 65536),
  target_ref TEXT NOT NULL,
  starting_oid TEXT NOT NULL,
  desired_state TEXT NOT NULL CHECK (desired_state IN ('active','retired')),
  observed_state TEXT NOT NULL CHECK (observed_state IN
    ('planned','creating','ready','stopped','drifted','missing','error')),
  controller_owned INTEGER NOT NULL CHECK (controller_owned IN (0,1)),
  resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_reconciled_at TEXT,
  error_code TEXT,
  error_detail TEXT,
  UNIQUE(project_id, workstream_id)
) STRICT;

CREATE UNIQUE INDEX workstreams_active_working_copy_uq
  ON workstreams(working_copy_id) WHERE desired_state = 'active';
CREATE UNIQUE INDEX workstreams_active_conversation_uq
  ON workstreams(conversation_id) WHERE desired_state = 'active';

CREATE TABLE presentation_assignments (
  presentation_assignment_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(conversation_id),
  backend TEXT NOT NULL CHECK (backend IN ('tmux','herdr')),
  desired_state TEXT NOT NULL CHECK (desired_state IN ('present','absent')),
  observed_state TEXT NOT NULL CHECK (observed_state IN
    ('unknown','present','missing','drifted','error')),
  locator_json TEXT NOT NULL CHECK (json_valid(locator_json) AND length(CAST(locator_json AS BLOB)) <= 16384),
  resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
  observed_at TEXT,
  updated_at TEXT NOT NULL,
  error_code TEXT,
  error_detail TEXT
) STRICT;

CREATE TABLE project_activations (
  project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
  mode TEXT NOT NULL CHECK (mode IN ('legacy','shadow','controller')),
  controller_build_id TEXT REFERENCES installed_builds(build_id),
  migration_id TEXT REFERENCES migration_runs(migration_id),
  expected_project_version INTEGER NOT NULL CHECK (expected_project_version >= 1),
  resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  activated_at TEXT,
  CHECK ((mode = 'legacy' AND controller_build_id IS NULL AND migration_id IS NULL)
      OR (mode IN ('shadow','controller') AND controller_build_id IS NOT NULL AND migration_id IS NOT NULL))
) STRICT;

CREATE TABLE migration_resource_mappings (
  migration_id TEXT NOT NULL REFERENCES migration_runs(migration_id),
  record_id TEXT NOT NULL,
  adapter_kind TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_digest TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id TEXT,
  disposition TEXT NOT NULL CHECK (disposition IN
    ('import','observe','unmigrated','exclude','requires-decision','contradiction')),
  reason_code TEXT NOT NULL,
  detail_json TEXT NOT NULL CHECK (json_valid(detail_json) AND length(CAST(detail_json AS BLOB)) <= 16384),
  created_at TEXT NOT NULL,
  PRIMARY KEY (migration_id, record_id)
) STRICT;

CREATE TRIGGER workstream_link_valid_insert
BEFORE INSERT ON workstreams
WHEN NOT EXISTS (
  SELECT 1
  FROM working_copies wc JOIN conversations c ON c.conversation_id = NEW.conversation_id
  WHERE wc.working_copy_id = NEW.working_copy_id
    AND wc.project_id = NEW.project_id
    AND c.project_id = NEW.project_id
    AND c.working_copy_id = NEW.working_copy_id
    AND c.role = 'workstream'
    AND wc.kind IN ('worktree','isolated')
    AND wc.purpose IN ('workstream','integration')
    AND wc.controller_owned = 1
)
BEGIN
  SELECT RAISE(ABORT, 'workstream project/resource binding is invalid');
END;

CREATE TRIGGER workstream_link_valid_update
BEFORE UPDATE OF project_id, working_copy_id, conversation_id ON workstreams
WHEN NOT EXISTS (
  SELECT 1
  FROM working_copies wc JOIN conversations c ON c.conversation_id = NEW.conversation_id
  WHERE wc.working_copy_id = NEW.working_copy_id
    AND wc.project_id = NEW.project_id
    AND c.project_id = NEW.project_id
    AND c.working_copy_id = NEW.working_copy_id
    AND c.role = 'workstream'
    AND wc.kind IN ('worktree','isolated')
    AND wc.purpose IN ('workstream','integration')
    AND wc.controller_owned = 1
)
BEGIN
  SELECT RAISE(ABORT, 'workstream project/resource binding is invalid');
END;

CREATE TRIGGER workstream_identity_immutable
BEFORE UPDATE OF workstream_id, project_id, working_copy_id, conversation_id ON workstreams
WHEN NEW.workstream_id IS NOT OLD.workstream_id
  OR NEW.project_id IS NOT OLD.project_id
  OR NEW.working_copy_id IS NOT OLD.working_copy_id
  OR NEW.conversation_id IS NOT OLD.conversation_id
BEGIN
  SELECT RAISE(ABORT, 'workstream identity is immutable');
END;

CREATE TRIGGER presentation_identity_immutable
BEFORE UPDATE OF presentation_assignment_id, conversation_id ON presentation_assignments
WHEN NEW.presentation_assignment_id IS NOT OLD.presentation_assignment_id
  OR NEW.conversation_id IS NOT OLD.conversation_id
BEGIN
  SELECT RAISE(ABORT, 'presentation assignment identity is immutable');
END;

CREATE TRIGGER migration_resource_mapping_immutable_update
BEFORE UPDATE ON migration_resource_mappings
BEGIN
  SELECT RAISE(ABORT, 'migration resource mapping is immutable');
END;

CREATE TRIGGER migration_resource_mapping_immutable_delete
BEFORE DELETE ON migration_resource_mappings
BEGIN
  SELECT RAISE(ABORT, 'migration resource mapping is immutable');
END;
'''

# This is the controller's lifecycle schema.  Git objects and session content
# deliberately do not have tables here.
SCHEMA_SQL = r'''
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

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
  project_id TEXT REFERENCES projects(project_id),
  working_copy_id TEXT REFERENCES working_copies(working_copy_id),
  parent_run_id TEXT REFERENCES runs(run_id),
  child_source_json TEXT CHECK (child_source_json IS NULL OR json_valid(child_source_json)),
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

CREATE TABLE reviews (
  review_id TEXT PRIMARY KEY,
  change_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  reviewer_conversation_id TEXT REFERENCES conversations(conversation_id),
  reviewer_run_id TEXT REFERENCES runs(run_id),
  reviewer_actor_id TEXT,
  reviewer_capability_hash TEXT,
  reviewer_source_json TEXT CHECK (reviewer_source_json IS NULL OR json_valid(reviewer_source_json)),
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

CREATE TABLE authorizations (
  authorization_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN
    ('create-workstream','relaunch-workstream','retire-workstream',
     'adopt-working-copy','archive-conversation','submit-change','close-change',
     'integrate-change','publish','cleanup','host-command','migration-resolve',
     'migration-cutover','activation-change')),
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
  last_sequence INTEGER NOT NULL DEFAULT 0 CHECK (last_sequence >= 0),
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
WHEN EXISTS (SELECT 1 FROM working_copies WHERE active_writer_run_id = OLD.run_id)
BEGIN
  SELECT RAISE(ABORT, 'active writer run is still claimed');
END;

CREATE TRIGGER writer_run_terminal_clears_claim
AFTER UPDATE OF observed_state ON runs
WHEN NEW.authority = 'writer'
  AND NEW.observed_state IN ('stopped','failed','lost')
  AND EXISTS (SELECT 1 FROM working_copies WHERE active_writer_run_id = NEW.run_id)
BEGIN
  UPDATE working_copies
     SET active_writer_run_id = NULL,
         resource_version = resource_version + 1,
         updated_at = NEW.updated_at
   WHERE active_writer_run_id = NEW.run_id;
END;

CREATE TRIGGER review_submission_authority_valid
BEFORE UPDATE OF state ON reviews
WHEN NEW.state = 'submitted'
  AND (NEW.reviewer_conversation_id IS NULL
       OR NEW.reviewer_run_id IS NULL
       OR NEW.reviewer_actor_id IS NULL
       OR NEW.reviewer_capability_hash IS NULL
       OR NEW.reviewer_source_json IS NULL)
BEGIN
  SELECT RAISE(ABORT, 'submitted review is missing reviewer authority binding');
END;

CREATE TRIGGER submitted_review_binding_immutable
BEFORE UPDATE OF reviewer_conversation_id, reviewer_run_id, reviewer_actor_id, reviewer_capability_hash, reviewer_source_json ON reviews
WHEN OLD.state = 'submitted'
  AND (NEW.reviewer_conversation_id IS NOT OLD.reviewer_conversation_id
       OR NEW.reviewer_run_id IS NOT OLD.reviewer_run_id
       OR NEW.reviewer_actor_id IS NOT OLD.reviewer_actor_id
       OR NEW.reviewer_capability_hash IS NOT OLD.reviewer_capability_hash
       OR NEW.reviewer_source_json IS NOT OLD.reviewer_source_json)
BEGIN
  SELECT RAISE(ABORT, 'submitted review authority binding is immutable');
END;

CREATE TRIGGER working_copy_review_binding_valid
BEFORE UPDATE OF kind, effective_mode, branch_ref ON working_copies
WHEN EXISTS (
  SELECT 1 FROM conversations
  WHERE working_copy_id = NEW.working_copy_id
    AND role = 'review'
)
AND NOT (
  NEW.kind = 'review'
  AND NEW.effective_mode = 'read-only'
  AND NEW.branch_ref IS NULL
)
BEGIN
  SELECT RAISE(ABORT, 'review working copy must remain read-only and detached');
END;

CREATE TRIGGER conversation_role_project_required_insert
BEFORE INSERT ON conversations
WHEN (NEW.role IN ('secretary','personal','workstream','review','integration') AND NEW.project_id IS NULL)
  OR (NEW.role IN ('workstream','review','integration') AND NEW.working_copy_id IS NULL)
  OR (NEW.role IN ('secretary','host') AND NEW.working_copy_id IS NOT NULL)
  OR (NEW.role = 'review' AND NOT EXISTS (
       SELECT 1 FROM working_copies
       WHERE working_copy_id = NEW.working_copy_id
         AND kind = 'review'
         AND effective_mode = 'read-only'
         AND branch_ref IS NULL
     ))
BEGIN
  SELECT RAISE(ABORT, 'conversation role binding is invalid');
END;

CREATE TRIGGER conversation_role_project_required_update
BEFORE UPDATE OF role, project_id, working_copy_id ON conversations
WHEN (NEW.role IN ('secretary','personal','workstream','review','integration') AND NEW.project_id IS NULL)
  OR (NEW.role IN ('workstream','review','integration') AND NEW.working_copy_id IS NULL)
  OR (NEW.role IN ('secretary','host') AND NEW.working_copy_id IS NOT NULL)
  OR (NEW.role = 'review' AND NOT EXISTS (
       SELECT 1 FROM working_copies
       WHERE working_copy_id = NEW.working_copy_id
         AND kind = 'review'
         AND effective_mode = 'read-only'
         AND branch_ref IS NULL
     ))
BEGIN
  SELECT RAISE(ABORT, 'conversation role binding is invalid');
END;

CREATE TRIGGER writer_run_shape_insert
BEFORE INSERT ON runs
WHEN (NEW.authority = 'writer' AND
      (NEW.working_copy_id IS NULL OR NEW.expected_working_copy_version IS NULL OR NEW.writer_epoch IS NULL))
  OR (NEW.authority = 'secretary' AND NEW.working_copy_id IS NOT NULL)
BEGIN
  SELECT RAISE(ABORT, 'run authority binding is invalid');
END;

CREATE TRIGGER writer_run_shape_update
BEFORE UPDATE OF authority, working_copy_id, expected_working_copy_version, writer_epoch ON runs
WHEN (NEW.authority = 'writer' AND
      (NEW.working_copy_id IS NULL OR NEW.expected_working_copy_version IS NULL OR NEW.writer_epoch IS NULL))
  OR (NEW.authority = 'secretary' AND NEW.working_copy_id IS NOT NULL)
BEGIN
  SELECT RAISE(ABORT, 'run authority binding is invalid');
END;

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

CREATE TRIGGER migration_runs_request_immutable
BEFORE UPDATE OF migration_id, operation_id, idempotency_key, mode,
                 controller_build_id, request_digest, source_manifest_digest,
                 created_at ON migration_runs
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
''' + ARTIFACT_SCHEMA_SQL + REVISION_SCHEMA_SQL + RECEIPT_OPERATION_IMMUTABILITY_SQL + COMPLETION_SCHEMA_SQL


def iter_statements(sql: str) -> Iterable[str]:
    """Yield complete SQL statements without breaking trigger bodies."""

    buffer: list[str] = []
    for line in sql.splitlines(True):
        buffer.append(line)
        candidate = "".join(buffer)
        if sqlite3.complete_statement(candidate):
            statement = candidate.strip()
            if statement:
                yield statement
            buffer.clear()
    if "".join(buffer).strip():
        raise ValueError("incomplete schema SQL")


def schema_digest() -> str:
    return hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()


def apply_schema(connection: sqlite3.Connection) -> None:
    for statement in iter_statements(SCHEMA_SQL):
        connection.execute(statement)


def probe_capabilities(connection: sqlite3.Connection | None = None) -> dict[str, str]:
    """Prove every SQLite feature used before any application state is made."""

    own = connection is None
    conn = connection or sqlite3.connect(":memory:")
    try:
        version = tuple(int(part) for part in sqlite3.sqlite_version.split(".")[:3])
        if version < (3, 40, 0):
            raise SQLiteUnsupportedError(
                "SQLite 3.40.0 or newer is required",
                detail={"sqlite_version": sqlite3.sqlite_version},
            )
        conn.execute("PRAGMA foreign_keys = ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise SQLiteUnsupportedError("SQLite foreign keys are unavailable")
        checks = {
            "strict": "CREATE TABLE _cp_probe_strict (x TEXT) STRICT",
            "json_valid": "SELECT json_valid(?)",
            "partial_index": "CREATE TABLE _cp_probe_index (x INTEGER); CREATE INDEX _cp_probe_partial ON _cp_probe_index(x) WHERE x IS NOT NULL",
            "trigger": "CREATE TABLE _cp_probe_trigger (x INTEGER); CREATE TRIGGER _cp_probe_trigger_t AFTER INSERT ON _cp_probe_trigger BEGIN SELECT NEW.x; END",
        }
        conn.execute(checks["strict"])
        if conn.execute("SELECT json_valid(?)", ('{"ok":true}',)).fetchone()[0] != 1:
            raise SQLiteUnsupportedError("SQLite JSON validation is unavailable")
        conn.execute("CREATE TABLE _cp_probe_index (x INTEGER)")
        conn.execute("CREATE INDEX _cp_probe_partial ON _cp_probe_index(x) WHERE x IS NOT NULL")
        conn.execute("CREATE TABLE _cp_probe_trigger (x INTEGER)")
        conn.execute("CREATE TRIGGER _cp_probe_trigger_t AFTER INSERT ON _cp_probe_trigger BEGIN SELECT NEW.x; END")
        return {"sqlite_version": sqlite3.sqlite_version, "strict": "ok", "json_valid": "ok", "partial_index": "ok", "trigger": "ok", "foreign_keys": "on"}
    except SQLiteUnsupportedError:
        raise
    except (sqlite3.Error, ValueError) as error:
        raise SQLiteUnsupportedError(
            "required SQLite capabilities are unavailable",
            detail={"sqlite_version": sqlite3.sqlite_version, "failure": type(error).__name__},
        ) from error
    finally:
        if own:
            conn.close()


__all__ = [
    "ARTIFACT_SCHEMA_SQL",
    "MIGRATION_NAME",
    "RECEIPT_OPERATION_IMMUTABILITY_SQL",
    "REVISION_SCHEMA_SQL",
    "MIGRATION_VERSION",
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "apply_schema",
    "iter_statements",
    "probe_capabilities",
    "schema_digest",
]
