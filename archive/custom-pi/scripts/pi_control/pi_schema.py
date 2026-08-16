"""Fresh Pi system schema.

This schema is intentionally separate from the historical ``pi-control``
database. A new installation starts at epoch two and never imports rows from
the old controller, secretary, migration, or activation stores. Epoch one was
disposable pre-production state and is deliberately not migrated.
"""

from __future__ import annotations

import hashlib
import sqlite3

PI_SCHEMA_VERSION = 3
PI_MIGRATION_NAME = "pi-harness-epoch-3"

SCHEMA_SQL = r'''
CREATE TABLE control_meta (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  schema_version INTEGER NOT NULL,
  controller_build_id TEXT NOT NULL,
  controller_restart_epoch TEXT NOT NULL,
  controller_started_at TEXT NOT NULL,
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
  build_manifest_path TEXT NOT NULL UNIQUE,
  build_manifest_digest TEXT NOT NULL UNIQUE,
  resource_manifest_path TEXT NOT NULL UNIQUE,
  resource_manifest_digest TEXT NOT NULL UNIQUE,
  pi_version TEXT NOT NULL,
  package_lock_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('staged','active','superseded','rolled_back','failed')),
  installed_at TEXT NOT NULL,
  activated_at TEXT,
  rollback_path TEXT,
  verification_json TEXT NOT NULL CHECK (json_valid(verification_json))
) STRICT;
CREATE UNIQUE INDEX installed_builds_one_active_uq ON installed_builds(status) WHERE status = 'active';

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
  observed_state TEXT NOT NULL CHECK (observed_state IN ('unknown','ready','drifted','missing','error')),
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
  kind TEXT NOT NULL CHECK (kind IN ('primary','worktree','isolated','review')),
  purpose TEXT NOT NULL CHECK (purpose IN ('personal','workstream','integration','review','recovery','other')),
  path TEXT NOT NULL,
  git_dir TEXT,
  branch_ref TEXT,
  expected_head_oid TEXT,
  expected_tree_oid TEXT,
  effective_mode TEXT NOT NULL CHECK (effective_mode IN ('trusted-live','isolated','read-only')),
  desired_state TEXT NOT NULL CHECK (desired_state IN ('present','absent')),
  observed_state TEXT NOT NULL CHECK (observed_state IN ('unknown','creating','ready','dirty','drifted','missing','removing','error')),
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
  role TEXT NOT NULL CHECK (role IN ('secretary','investigator','reviewer','personal','workstream','integration')),
  authority_profile TEXT NOT NULL CHECK (authority_profile IN ('host-read-only','writer-container')),
  display_name TEXT NOT NULL,
  pi_session_id TEXT NOT NULL,
  session_file TEXT NOT NULL,
  desired_state TEXT NOT NULL CHECK (desired_state IN ('active','archived')),
  observed_state TEXT NOT NULL CHECK (observed_state IN ('unknown','ready','missing','conflict','error')),
  resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_reconciled_at TEXT,
  error_code TEXT,
  error_detail TEXT,
  UNIQUE(pi_session_id),
  UNIQUE(session_file)
) STRICT;

CREATE TABLE workstreams (
  workstream_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  working_copy_id TEXT NOT NULL REFERENCES working_copies(working_copy_id),
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
  title TEXT NOT NULL,
  brief_json TEXT NOT NULL CHECK (json_valid(brief_json)),
  target_ref TEXT NOT NULL,
  starting_oid TEXT NOT NULL,
  primary_working_copy_id TEXT NOT NULL REFERENCES working_copies(working_copy_id),
  branch_ref TEXT NOT NULL,
  worktree_path TEXT NOT NULL,
  package_environment_root TEXT NOT NULL,
  creation_operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
  desired_state TEXT NOT NULL CHECK (desired_state IN ('active','retired')),
  observed_state TEXT NOT NULL CHECK (observed_state IN ('planned','creating','ready','stopped','drifted','missing','error')),
  controller_owned INTEGER NOT NULL CHECK (controller_owned IN (0,1)),
  resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_reconciled_at TEXT,
  error_code TEXT,
  error_detail TEXT,
  UNIQUE(project_id, working_copy_id),
  UNIQUE(project_id, conversation_id)
) STRICT;

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
  project_id TEXT REFERENCES projects(project_id),
  working_copy_id TEXT REFERENCES working_copies(working_copy_id),
  parent_run_id TEXT REFERENCES runs(run_id),
  child_source_json TEXT CHECK (child_source_json IS NULL OR json_valid(child_source_json)),
  authority TEXT NOT NULL CHECK (authority IN ('host-read-only','writer-container')),
  desired_state TEXT NOT NULL CHECK (desired_state IN ('running','stopped')),
  observed_state TEXT NOT NULL CHECK (observed_state IN ('created','preparing','ready','running','stopping','stopped','failed','lost','needs_attention')),
  expected_working_copy_version INTEGER,
  expected_head_oid TEXT,
  expected_tree_oid TEXT,
  dirty_fingerprint TEXT,
  writer_epoch INTEGER,
  runtime_spec_hash TEXT NOT NULL,
  build_id TEXT NOT NULL,
  owner_pid INTEGER,
  owner_start_identity TEXT,
  child_pid INTEGER,
  child_start_identity TEXT,
  channel_binding_hash TEXT NOT NULL,
  manifest_path TEXT,
  host_process_observation_json TEXT CHECK (host_process_observation_json IS NULL OR json_valid(host_process_observation_json)),
  container_id TEXT,
  container_observation_json TEXT CHECK (container_observation_json IS NULL OR json_valid(container_observation_json)),
  resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
  created_at TEXT NOT NULL,
  started_at TEXT,
  ended_at TEXT,
  updated_at TEXT NOT NULL,
  error_code TEXT,
  error_detail TEXT
) STRICT;
CREATE INDEX runs_conversation_idx ON runs(conversation_id, created_at);
CREATE UNIQUE INDEX runs_one_live_writer_uq ON runs(working_copy_id)
  WHERE authority='writer-container' AND observed_state IN ('created','preparing','ready','running','stopping','needs_attention');

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
  closed_at TEXT
) STRICT;
CREATE UNIQUE INDEX changes_one_draft_per_working_copy_uq ON changes(source_working_copy_id) WHERE state = 'draft' AND source_working_copy_id IS NOT NULL;

CREATE TABLE change_revisions (
  change_id TEXT NOT NULL REFERENCES changes(change_id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  base_oid TEXT NOT NULL,
  tip_oid TEXT NOT NULL,
  tree_oid TEXT NOT NULL,
  source_head_oid TEXT,
  capture_mode TEXT NOT NULL CHECK (capture_mode IN ('branch-tip','temporary-index','integration-result')),
  source_status_hash TEXT,
  ref_name TEXT NOT NULL UNIQUE,
  changed_paths_json TEXT NOT NULL CHECK (json_valid(changed_paths_json)),
  diffstat_json TEXT NOT NULL CHECK (json_valid(diffstat_json)),
  verification_json TEXT NOT NULL CHECK (json_valid(verification_json)),
  provenance_json TEXT NOT NULL CHECK (json_valid(provenance_json)),
  created_at TEXT NOT NULL,
  PRIMARY KEY(change_id, revision)
) STRICT;
CREATE TABLE change_revision_inputs (
  result_change_id TEXT NOT NULL,
  result_revision INTEGER NOT NULL,
  input_change_id TEXT NOT NULL,
  input_revision INTEGER NOT NULL,
  relation TEXT NOT NULL CHECK (relation IN ('supersedes','adapts','includes','resolves-conflict-with')),
  PRIMARY KEY(result_change_id,result_revision,input_change_id,input_revision,relation),
  FOREIGN KEY(result_change_id,result_revision) REFERENCES change_revisions(change_id,revision),
  FOREIGN KEY(input_change_id,input_revision) REFERENCES change_revisions(change_id,revision)
) STRICT;

CREATE TABLE reviews (
  review_id TEXT PRIMARY KEY,
  change_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  reviewer_conversation_id TEXT REFERENCES conversations(conversation_id),
  reviewer_run_id TEXT REFERENCES runs(run_id),
  reviewer_actor_id TEXT,
  reviewer_capability_hash TEXT,
  dependency_review_digest TEXT,
  reviewer_source_json TEXT CHECK (reviewer_source_json IS NULL OR json_valid(reviewer_source_json)),
  verdict TEXT CHECK (verdict IN ('accept','changes_requested','comment')),
  summary TEXT,
  findings TEXT,
  evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
  state TEXT NOT NULL CHECK (state IN ('requested','running','submitted','cancelled','failed')),
  created_at TEXT NOT NULL,
  submitted_at TEXT,
  FOREIGN KEY(change_id,revision) REFERENCES change_revisions(change_id,revision)
) STRICT;

CREATE TABLE integration_attempts (
  integration_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  change_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  requested_target_oid TEXT NOT NULL,
  strategy TEXT NOT NULL CHECK (strategy IN ('fast-forward','merge','integration-worktree')),
  state TEXT NOT NULL CHECK (state IN ('planned','applying','needs_resolution','succeeded','failed','cancelled')),
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
  FOREIGN KEY(change_id,revision) REFERENCES change_revisions(change_id,revision)
) STRICT;

CREATE TABLE authorizations (
  authorization_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('create-workstream','start-integration','integrate-change','host-command','container-network-command','package-network-operation','archive-conversation','retire-workstream','close-change')),
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
  UNIQUE(request_context_id,kind,resource_type,resource_id,scope_digest)
) STRICT;

CREATE TABLE activation_approvals (
  approval_id TEXT PRIMARY KEY,
  build_id TEXT NOT NULL,
  staged_root TEXT NOT NULL,
  data_root TEXT NOT NULL,
  rollback_plan_json TEXT NOT NULL CHECK (json_valid(rollback_plan_json)),
  scope_digest TEXT NOT NULL,
  actor_id TEXT,
  issued_at TEXT NOT NULL,
  consumed_at TEXT,
  state TEXT NOT NULL CHECK (state IN ('active','consumed','cancelled','expired')),
  UNIQUE(build_id,staged_root,data_root,scope_digest)
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
  state TEXT NOT NULL CHECK (state IN ('planned','applying','succeeded','failed','needs_attention','cancelled')),
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
CREATE TABLE investigations (
  investigation_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
  run_id TEXT REFERENCES runs(run_id),
  purpose TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('running','result','failed','needs-user','interrupted')),
  result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
) STRICT;
CREATE TABLE presentation_assignments (
  presentation_assignment_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(conversation_id),
  backend TEXT NOT NULL CHECK (backend = 'tmux'),
  desired_state TEXT NOT NULL CHECK (desired_state IN ('present','absent')),
  observed_state TEXT NOT NULL CHECK (observed_state IN ('unknown','present','missing','drifted','error')),
  locator_json TEXT NOT NULL CHECK (json_valid(locator_json)),
  resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
  observed_at TEXT,
  updated_at TEXT NOT NULL,
  error_code TEXT,
  error_detail TEXT
) STRICT;
CREATE TABLE artifact_manifests (
  artifact_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  manifest_path TEXT NOT NULL UNIQUE,
  manifest_digest TEXT NOT NULL,
  checksum TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
  sensitive INTEGER NOT NULL CHECK (sensitive IN (0,1)),
  retention_class TEXT NOT NULL CHECK (retention_class IN ('run','change','recovery','debug')),
  created_at TEXT NOT NULL,
  expires_at TEXT,
  resource_version INTEGER NOT NULL CHECK (resource_version >= 1)
) STRICT;
CREATE TABLE child_terminal_records (
  child_run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
  parent_run_id TEXT NOT NULL REFERENCES runs(run_id),
  terminal_class TEXT NOT NULL CHECK (terminal_class IN ('success','failed','lost','needs-user','interrupted','needs_attention')),
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
  FOREIGN KEY(submitted_change_id,submitted_revision) REFERENCES change_revisions(change_id,revision)
) STRICT;

CREATE TABLE child_requests (
  child_request_id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
  parent_run_id TEXT NOT NULL REFERENCES runs(run_id),
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  parent_working_copy_id TEXT REFERENCES working_copies(working_copy_id),
  semantic_role TEXT NOT NULL CHECK (semantic_role IN ('scout','investigator','researcher','planner','oracle','delegate','reviewer','worker')),
  runtime_role TEXT NOT NULL CHECK (runtime_role IN ('investigator','reviewer','workstream')),
  task TEXT NOT NULL,
  snapshot_id TEXT UNIQUE,
  snapshot_ref TEXT UNIQUE,
  snapshot_commit_oid TEXT,
  snapshot_tree_oid TEXT,
  snapshot_path TEXT UNIQUE,
  child_working_copy_id TEXT UNIQUE REFERENCES working_copies(working_copy_id),
  child_conversation_id TEXT UNIQUE REFERENCES conversations(conversation_id),
  child_run_id TEXT UNIQUE REFERENCES runs(run_id),
  state TEXT NOT NULL CHECK (state IN ('creating','ready','running','success','failed','lost','needs-user','interrupted','needs_attention')),
  result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  error_code TEXT,
  error_detail TEXT
) STRICT;
CREATE INDEX child_requests_parent_idx ON child_requests(parent_run_id,created_at);

CREATE TABLE project_messages (
  message_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  workstream_id TEXT REFERENCES workstreams(workstream_id),
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  writer_generation INTEGER,
  kind TEXT NOT NULL CHECK (kind IN ('progress','needs-user','decision-reply','review-requested','failure','interrupted','submitted-change','package-review-required','package-review-complete')),
  request_id TEXT,
  idempotency_key TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  state TEXT NOT NULL CHECK (state IN ('pending','delivered','acknowledged','resolved')),
  created_at TEXT NOT NULL,
  delivered_at TEXT,
  acknowledged_at TEXT,
  resolved_at TEXT,
  reply_to_message_id TEXT REFERENCES project_messages(message_id),
  UNIQUE(project_id,idempotency_key)
) STRICT;
CREATE INDEX project_messages_project_idx ON project_messages(project_id,created_at);

CREATE TABLE command_requests (
  command_request_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  workstream_id TEXT REFERENCES workstreams(workstream_id),
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  writer_generation INTEGER NOT NULL,
  execution_place TEXT NOT NULL CHECK (execution_place IN ('container-network','host')),
  operation_name TEXT NOT NULL,
  operation_json TEXT NOT NULL CHECK (json_valid(operation_json)),
  working_directory TEXT NOT NULL,
  required_resource TEXT NOT NULL,
  purpose TEXT NOT NULL,
  expected_effect TEXT NOT NULL,
  change_scope_json TEXT NOT NULL CHECK (json_valid(change_scope_json)),
  expected_output TEXT NOT NULL,
  sensitive_output INTEGER NOT NULL CHECK (sensitive_output IN (0,1)),
  expected_duration_ms INTEGER NOT NULL CHECK (expected_duration_ms BETWEEN 1 AND 3600000),
  request_digest TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK (state IN ('requested','approved','rejected','running','succeeded','failed','expired','cancelled')),
  authorization_id TEXT REFERENCES authorizations(authorization_id),
  result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  completed_at TEXT
) STRICT;

CREATE TABLE package_requests (
  package_request_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  working_copy_id TEXT NOT NULL REFERENCES working_copies(working_copy_id),
  writer_generation INTEGER NOT NULL,
  change_id TEXT NOT NULL REFERENCES changes(change_id),
  revision INTEGER NOT NULL,
  ecosystem TEXT NOT NULL CHECK (ecosystem IN ('npm','python')),
  action TEXT NOT NULL CHECK (action IN ('add','remove','sync')),
  package_name TEXT,
  exact_version TEXT,
  base_tree_oid TEXT NOT NULL,
  candidate_tree_oid TEXT NOT NULL,
  manifest_path TEXT NOT NULL,
  manifest_digest TEXT NOT NULL,
  lock_path TEXT NOT NULL,
  base_lock_digest TEXT,
  candidate_lock_digest TEXT NOT NULL,
  scripts_policy TEXT NOT NULL CHECK (scripts_policy = 'disabled'),
  cache_root TEXT,
  cache_inventory_digest TEXT,
  cache_test_only INTEGER NOT NULL CHECK (cache_test_only IN (0,1)),
  allow_local_config_id_only INTEGER NOT NULL CHECK (allow_local_config_id_only IN (0,1)),
  image_reference TEXT NOT NULL,
  image_config_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  request_digest TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK (state IN ('requested','approved','rejected','running','succeeded','failed','expired','cancelled')),
  authorization_id TEXT REFERENCES authorizations(authorization_id),
  environment_id TEXT,
  result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  completed_at TEXT,
  FOREIGN KEY(change_id,revision) REFERENCES change_revisions(change_id,revision)
) STRICT;

CREATE TABLE dependency_changes (
  dependency_change_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  change_id TEXT NOT NULL REFERENCES changes(change_id),
  revision INTEGER NOT NULL,
  ecosystem TEXT NOT NULL,
  change_kind TEXT NOT NULL CHECK (change_kind IN ('add','remove','change')),
  package_name TEXT NOT NULL,
  exact_version TEXT NOT NULL,
  manifest_path TEXT NOT NULL,
  manifest_digest TEXT NOT NULL,
  lock_path TEXT,
  lock_digest TEXT,
  worker_reason TEXT NOT NULL,
  disposition TEXT NOT NULL CHECK (disposition IN ('standard','review-required','rejected')),
  created_at TEXT NOT NULL,
  UNIQUE(change_id,revision,ecosystem,package_name,exact_version),
  FOREIGN KEY(change_id,revision) REFERENCES change_revisions(change_id,revision)
) STRICT;
CREATE TABLE package_security_reviews (
  package_security_review_id TEXT PRIMARY KEY,
  dependency_change_id TEXT NOT NULL REFERENCES dependency_changes(dependency_change_id),
  candidate_change_id TEXT NOT NULL,
  candidate_revision INTEGER NOT NULL,
  investigator_run_id TEXT REFERENCES runs(run_id),
  package_name TEXT NOT NULL,
  exact_version TEXT NOT NULL,
  lock_digest TEXT,
  evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
  risk_level TEXT NOT NULL CHECK (risk_level IN ('low','medium','high','unknown')),
  recommendation TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('requested','complete','stale','rejected')),
  created_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(dependency_change_id,candidate_change_id,candidate_revision)
) STRICT;
CREATE TABLE package_environments (
  environment_id TEXT PRIMARY KEY,
  working_copy_id TEXT NOT NULL REFERENCES working_copies(working_copy_id),
  manifest_digest TEXT NOT NULL,
  lock_digest TEXT,
  ecosystem TEXT NOT NULL,
  platform TEXT NOT NULL,
  image_config_id TEXT NOT NULL,
  image_reference TEXT NOT NULL,
  environment_path TEXT NOT NULL,
  cache_scope TEXT NOT NULL,
  cache_inventory_digest TEXT NOT NULL,
  scripts_policy TEXT NOT NULL CHECK (scripts_policy = 'disabled'),
  lock_delta_json TEXT NOT NULL CHECK (json_valid(lock_delta_json)),
  result_json TEXT NOT NULL CHECK (json_valid(result_json)),
  environment_tree_digest TEXT NOT NULL,
  installed_package_json TEXT NOT NULL CHECK (json_valid(installed_package_json)),
  cleanup_json TEXT NOT NULL CHECK (json_valid(cleanup_json)),
  private_identity TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(working_copy_id,manifest_digest,lock_digest,platform,image_config_id)
) STRICT;

CREATE TRIGGER change_revision_immutable_update BEFORE UPDATE ON change_revisions BEGIN SELECT RAISE(ABORT,'change revision is immutable'); END;
CREATE TRIGGER change_revision_immutable_delete BEFORE DELETE ON change_revisions BEGIN SELECT RAISE(ABORT,'change revision is immutable'); END;
CREATE TRIGGER submitted_review_immutable BEFORE UPDATE ON reviews WHEN OLD.state='submitted' AND (NEW.change_id IS NOT OLD.change_id OR NEW.revision IS NOT OLD.revision OR NEW.verdict IS NOT OLD.verdict OR NEW.evidence_json IS NOT OLD.evidence_json OR NEW.state IS NOT OLD.state) BEGIN SELECT RAISE(ABORT,'submitted review is immutable'); END;
CREATE TRIGGER authorization_immutable BEFORE UPDATE OF kind,actor_type,actor_id,project_id,resource_type,resource_id,request_context_id,scope_json,scope_digest,issued_at,expires_at ON authorizations WHEN NEW.scope_digest IS NOT OLD.scope_digest OR NEW.kind IS NOT OLD.kind OR NEW.resource_id IS NOT OLD.resource_id BEGIN SELECT RAISE(ABORT,'authorization scope is immutable'); END;
CREATE TRIGGER conversation_role_profile_insert BEFORE INSERT ON conversations
WHEN (NEW.role IN ('secretary','investigator','reviewer') AND NEW.authority_profile != 'host-read-only')
  OR (NEW.role IN ('personal','workstream','integration') AND NEW.authority_profile != 'writer-container')
BEGIN SELECT RAISE(ABORT,'conversation role profile mismatch'); END;
CREATE TRIGGER conversation_role_profile_update BEFORE UPDATE OF role,authority_profile ON conversations
WHEN NEW.role IS NOT OLD.role OR NEW.authority_profile IS NOT OLD.authority_profile
BEGIN SELECT RAISE(ABORT,'conversation role profile is immutable'); END;
CREATE TRIGGER conversation_project_binding_insert BEFORE INSERT ON conversations WHEN NEW.working_copy_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM working_copies w WHERE w.working_copy_id=NEW.working_copy_id AND w.project_id=NEW.project_id
) BEGIN SELECT RAISE(ABORT,'conversation working copy crosses project'); END;
CREATE TRIGGER conversation_project_binding_update BEFORE UPDATE OF project_id,working_copy_id ON conversations WHEN NEW.project_id IS NOT OLD.project_id OR NEW.working_copy_id IS NOT OLD.working_copy_id
BEGIN SELECT RAISE(ABORT,'conversation project binding is immutable'); END;
CREATE TRIGGER run_project_binding_insert BEFORE INSERT ON runs WHEN NOT EXISTS (
  SELECT 1 FROM conversations c WHERE c.conversation_id=NEW.conversation_id AND c.project_id=NEW.project_id
) OR (NEW.working_copy_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM working_copies w WHERE w.working_copy_id=NEW.working_copy_id AND w.project_id=NEW.project_id
)) BEGIN SELECT RAISE(ABORT,'run binding crosses project'); END;
CREATE TRIGGER run_project_binding_update BEFORE UPDATE OF conversation_id,project_id,working_copy_id,authority ON runs
WHEN NEW.conversation_id IS NOT OLD.conversation_id OR NEW.project_id IS NOT OLD.project_id OR NEW.working_copy_id IS NOT OLD.working_copy_id OR NEW.authority IS NOT OLD.authority
BEGIN SELECT RAISE(ABORT,'run project, scope, and authority binding is immutable'); END;
CREATE TRIGGER run_role_binding_insert BEFORE INSERT ON runs WHEN NOT EXISTS (
  SELECT 1 FROM conversations c WHERE c.conversation_id=NEW.conversation_id AND c.authority_profile=NEW.authority
    AND ((c.role='secretary' AND c.working_copy_id IS NULL)
      OR (c.role!='secretary' AND c.working_copy_id=NEW.working_copy_id))
) BEGIN SELECT RAISE(ABORT,'run authority or scope differs from conversation'); END;
CREATE TRIGGER workstream_project_binding_insert BEFORE INSERT ON workstreams WHEN NOT EXISTS (
  SELECT 1 FROM working_copies w JOIN conversations c ON c.conversation_id=NEW.conversation_id
  WHERE w.working_copy_id=NEW.working_copy_id AND w.project_id=NEW.project_id
    AND c.project_id=NEW.project_id AND c.working_copy_id=NEW.working_copy_id AND c.role='workstream'
) BEGIN SELECT RAISE(ABORT,'workstream binding crosses project'); END;
CREATE TRIGGER workstream_project_binding_update BEFORE UPDATE OF project_id,working_copy_id,conversation_id ON workstreams
WHEN NEW.project_id IS NOT OLD.project_id OR NEW.working_copy_id IS NOT OLD.working_copy_id OR NEW.conversation_id IS NOT OLD.conversation_id
BEGIN SELECT RAISE(ABORT,'workstream project binding is immutable'); END;
CREATE TRIGGER project_desired_transition BEFORE UPDATE OF desired_state ON projects
WHEN NEW.desired_state != OLD.desired_state AND NOT (OLD.desired_state='active' AND NEW.desired_state='archived')
BEGIN SELECT RAISE(ABORT,'illegal project desired-state transition'); END;
CREATE TRIGGER conversation_desired_transition BEFORE UPDATE OF desired_state ON conversations
WHEN NEW.desired_state != OLD.desired_state AND NOT (OLD.desired_state='active' AND NEW.desired_state='archived')
BEGIN SELECT RAISE(ABORT,'illegal conversation desired-state transition'); END;
CREATE TRIGGER working_copy_desired_transition BEFORE UPDATE OF desired_state ON working_copies
WHEN NEW.desired_state != OLD.desired_state AND NOT (OLD.desired_state='present' AND NEW.desired_state='absent')
BEGIN SELECT RAISE(ABORT,'illegal working-copy desired-state transition'); END;
CREATE TRIGGER run_desired_transition BEFORE UPDATE OF desired_state ON runs
WHEN NEW.desired_state != OLD.desired_state AND NOT (OLD.desired_state='running' AND NEW.desired_state='stopped')
BEGIN SELECT RAISE(ABORT,'illegal run desired-state transition'); END;
CREATE TRIGGER run_observed_transition BEFORE UPDATE OF observed_state ON runs
WHEN NEW.observed_state != OLD.observed_state AND NOT (
  (OLD.observed_state='created' AND NEW.observed_state IN ('preparing','failed')) OR
  (OLD.observed_state='preparing' AND NEW.observed_state IN ('ready','failed','needs_attention')) OR
  (OLD.observed_state='ready' AND NEW.observed_state IN ('running','stopping','stopped','failed','lost','needs_attention')) OR
  (OLD.observed_state='running' AND NEW.observed_state IN ('stopping','stopped','failed','lost','needs_attention')) OR
  (OLD.observed_state='stopping' AND NEW.observed_state IN ('stopped','failed','lost','needs_attention')) OR
  (OLD.observed_state='needs_attention' AND NEW.observed_state IN ('running','stopped','failed','lost'))
) BEGIN SELECT RAISE(ABORT,'illegal run observed-state transition'); END;
CREATE TRIGGER project_observed_transition BEFORE UPDATE OF observed_state ON projects
WHEN NEW.observed_state != OLD.observed_state AND NOT (
  (OLD.observed_state='unknown' AND NEW.observed_state IN ('ready','drifted','missing','error')) OR
  (OLD.observed_state='ready' AND NEW.observed_state IN ('drifted','missing','error')) OR
  (OLD.observed_state IN ('drifted','missing','error') AND NEW.observed_state IN ('ready','drifted','missing','error'))
) BEGIN SELECT RAISE(ABORT,'illegal project observed-state transition'); END;
CREATE TRIGGER conversation_observed_transition BEFORE UPDATE OF observed_state ON conversations
WHEN NEW.observed_state != OLD.observed_state AND NOT (
  (OLD.observed_state='unknown' AND NEW.observed_state IN ('ready','missing','conflict','error')) OR
  (OLD.observed_state IN ('ready','missing','conflict','error') AND NEW.observed_state IN ('ready','missing','conflict','error'))
) BEGIN SELECT RAISE(ABORT,'illegal conversation observed-state transition'); END;
CREATE TRIGGER working_copy_observed_transition BEFORE UPDATE OF observed_state ON working_copies
WHEN NEW.observed_state != OLD.observed_state AND NOT (
  (OLD.observed_state='unknown' AND NEW.observed_state IN ('creating','ready','dirty','drifted','missing','removing','error')) OR
  (OLD.observed_state='creating' AND NEW.observed_state IN ('ready','dirty','drifted','missing','error')) OR
  (OLD.observed_state IN ('ready','dirty','drifted','missing','error') AND NEW.observed_state IN ('ready','dirty','drifted','missing','removing','error')) OR
  (OLD.observed_state='removing' AND NEW.observed_state IN ('missing','error'))
) BEGIN SELECT RAISE(ABORT,'illegal working-copy observed-state transition'); END;
'''


def schema_digest() -> str:
    return "sha256:" + hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()


def apply_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)


__all__ = ["PI_MIGRATION_NAME", "PI_SCHEMA_VERSION", "SCHEMA_SQL", "apply_schema", "schema_digest"]
