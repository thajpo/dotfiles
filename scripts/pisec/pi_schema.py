"""The one supported Pisec v1 database schema."""

from __future__ import annotations

import hashlib
import sqlite3


SCHEMA_NAME = "pisec-core-v1"
SCHEMA_VERSION = 1

SCHEMA_SQL = r'''
CREATE TABLE control_meta (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    schema_name TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    schema_sha256 TEXT NOT NULL CHECK(length(schema_sha256) = 64 AND schema_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL
);

CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    repository_path TEXT NOT NULL,
    git_common_dir TEXT NOT NULL UNIQUE,
    default_ref TEXT NOT NULL,
    remote_url TEXT CHECK(remote_url IS NULL OR length(remote_url) BETWEEN 1 AND 2048),
    data_dirs TEXT NOT NULL DEFAULT '[]',
    external_domains TEXT NOT NULL DEFAULT '[]',
    secretary_workstream_id TEXT,
    coordination_mode TEXT NOT NULL DEFAULT 'project' CHECK(coordination_mode IN ('fleet','project')),
    active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)),
    lifecycle_attention_reason TEXT CHECK(lifecycle_attention_reason IS NULL OR length(lifecycle_attention_reason) BETWEEN 1 AND 2048),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deactivated_at TEXT,
    FOREIGN KEY(secretary_workstream_id) REFERENCES workstreams(workstream_id)
);

CREATE TABLE project_workspaces (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    workspace_adapter_id TEXT NOT NULL CHECK(length(workspace_adapter_id) BETWEEN 1 AND 64),
    workspace_session_name TEXT NOT NULL CHECK(length(workspace_session_name) BETWEEN 1 AND 128),
    workspace_id TEXT NOT NULL CHECK(length(workspace_id) BETWEEN 1 AND 256),
    repository_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE workstreams (
    workstream_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    kind TEXT NOT NULL CHECK(kind IN ('secretary','worker','first_mate')),
    title TEXT NOT NULL CHECK(length(title) <= 512),
    purpose TEXT NOT NULL CHECK(length(purpose) <= 4096),
    brief TEXT NOT NULL CHECK(length(brief) <= 4096),
    harness_id TEXT NOT NULL CHECK(length(harness_id) BETWEEN 1 AND 64),
    workspace_adapter_id TEXT NOT NULL CHECK(length(workspace_adapter_id) BETWEEN 1 AND 64),
    execution_profile TEXT NOT NULL CHECK(length(execution_profile) BETWEEN 1 AND 128),
    target_ref TEXT NOT NULL,
    base_commit_oid TEXT NOT NULL CHECK(length(base_commit_oid) IN (40,64) AND base_commit_oid NOT GLOB '*[^0-9a-f]*'),
    branch_name TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    desired_state TEXT NOT NULL CHECK(desired_state IN ('active','completed','retired')),
    provisioning_state TEXT NOT NULL CHECK(provisioning_state IN ('proposed','creating','bound','needs_attention')),
    attention_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    retired_at TEXT,
    UNIQUE(project_id, branch_name),
    UNIQUE(project_id, worktree_path)
);
CREATE UNIQUE INDEX one_active_first_mate
ON workstreams(kind) WHERE kind='first_mate' AND desired_state <> 'retired';
CREATE UNIQUE INDEX one_active_secretary_per_project
ON workstreams(project_id) WHERE kind='secretary' AND desired_state <> 'retired';

CREATE TABLE runtime_bindings (
    workstream_id TEXT PRIMARY KEY REFERENCES workstreams(workstream_id),
    workspace_adapter_id TEXT NOT NULL CHECK(length(workspace_adapter_id) BETWEEN 1 AND 64),
    workspace_session_name TEXT NOT NULL CHECK(length(workspace_session_name) BETWEEN 1 AND 128),
    workspace_id TEXT,
    workspace_view_id TEXT,
    workspace_surface_id TEXT,
    agent_name TEXT NOT NULL UNIQUE,
    harness_id TEXT NOT NULL CHECK(length(harness_id) BETWEEN 1 AND 64),
    harness_home TEXT NOT NULL,
    adapter_artifacts_json TEXT NOT NULL CHECK(length(CAST(adapter_artifacts_json AS BLOB)) <= 65536),
    native_session_kind TEXT CHECK(native_session_kind IS NULL OR native_session_kind IN ('path','id')),
    native_session_value TEXT,
    launch_secret_path TEXT NOT NULL,
    policy_path TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL CHECK(length(policy_sha256) = 64 AND policy_sha256 NOT GLOB '*[^0-9a-f]*'),
    runtime_token_sha256 TEXT NOT NULL CHECK(length(runtime_token_sha256) = 64 AND runtime_token_sha256 NOT GLOB '*[^0-9a-f]*'),
    desired_generation_sha256 TEXT CHECK(desired_generation_sha256 IS NULL OR (length(desired_generation_sha256) = 64 AND desired_generation_sha256 NOT GLOB '*[^0-9a-f]*')),
    applied_generation_sha256 TEXT CHECK(applied_generation_sha256 IS NULL OR (length(applied_generation_sha256) = 64 AND applied_generation_sha256 NOT GLOB '*[^0-9a-f]*')),
    launch_generation_sha256 TEXT CHECK(launch_generation_sha256 IS NULL OR (length(launch_generation_sha256) = 64 AND launch_generation_sha256 NOT GLOB '*[^0-9a-f]*')),
    refresh_pending INTEGER NOT NULL DEFAULT 0 CHECK(refresh_pending IN (0,1)),
    refresh_operation_id TEXT REFERENCES operations(operation_id),
    refresh_started_at TEXT,
    session_start_event_sequence INTEGER REFERENCES events(sequence),
    session_start_report_seq INTEGER CHECK(session_start_report_seq IS NULL OR session_start_report_seq > 0),
    session_started_at TEXT,
    runtime_instance_id TEXT,
    observed_state TEXT NOT NULL CHECK(observed_state IN ('unknown','starting','working','blocked','idle','done','stopped','missing','error')),
    report_seq INTEGER NOT NULL DEFAULT 0 CHECK(report_seq >= 0),
    workspace_report_seq INTEGER NOT NULL DEFAULT 0 CHECK(workspace_report_seq >= 0),
    last_observed_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK((refresh_pending = 0 AND refresh_operation_id IS NULL AND refresh_started_at IS NULL) OR (refresh_pending = 1 AND refresh_operation_id IS NOT NULL AND refresh_started_at IS NOT NULL)),
    CHECK((session_start_event_sequence IS NULL AND session_start_report_seq IS NULL AND session_started_at IS NULL) OR (session_start_event_sequence IS NOT NULL AND session_start_report_seq IS NOT NULL AND session_started_at IS NOT NULL)),
    CHECK(refresh_pending = 0 OR launch_generation_sha256 IS NOT NULL)
);

CREATE TABLE runtime_sessions (
    workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    session_key TEXT NOT NULL CHECK(length(session_key) BETWEEN 1 AND 256),
    task_packet_presented_at TEXT,
    last_turn_started_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(workstream_id, session_key)
);

CREATE TABLE retained_session_roots (
    workstream_id TEXT PRIMARY KEY REFERENCES workstreams(workstream_id),
    harness_id TEXT NOT NULL CHECK(length(harness_id) BETWEEN 1 AND 64),
    harness_home TEXT NOT NULL,
    native_session_kind TEXT CHECK(native_session_kind IS NULL OR native_session_kind IN ('path','id')),
    native_session_value TEXT,
    retained_at TEXT NOT NULL
);

CREATE TABLE task_packets (
    task_packet_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    workstream_id TEXT NOT NULL UNIQUE REFERENCES workstreams(workstream_id),
    scope_sha256 TEXT NOT NULL CHECK(length(scope_sha256) = 64 AND scope_sha256 NOT GLOB '*[^0-9a-f]*'),
    packet_json TEXT NOT NULL CHECK(length(CAST(packet_json AS BLOB)) <= 32768),
    packet_sha256 TEXT NOT NULL CHECK(length(packet_sha256) = 64 AND packet_sha256 NOT GLOB '*[^0-9a-f]*'),
    issued_at TEXT NOT NULL
);
CREATE TRIGGER task_packets_no_update BEFORE UPDATE ON task_packets
BEGIN SELECT RAISE(ABORT, 'Pisec task packets are immutable'); END;
CREATE TRIGGER task_packets_no_delete BEFORE DELETE ON task_packets
BEGIN SELECT RAISE(ABORT, 'Pisec task packets are immutable'); END;

CREATE TABLE workstream_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    runtime_instance_id TEXT NOT NULL CHECK(length(runtime_instance_id) BETWEEN 1 AND 256),
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 256),
    phase TEXT NOT NULL CHECK(phase IN ('investigating','implementing','verifying','ready_review')),
    summary TEXT NOT NULL CHECK(length(summary) BETWEEN 1 AND 1024),
    next_action TEXT NOT NULL CHECK(length(next_action) BETWEEN 1 AND 1024),
    remediation_issue_id TEXT REFERENCES issues(issue_id),
    evidence_json TEXT NOT NULL CHECK(length(CAST(evidence_json AS BLOB)) <= 32768),
    created_at TEXT NOT NULL,
    UNIQUE(workstream_id, idempotency_key),
    UNIQUE(workstream_id, sequence)
);
CREATE TRIGGER workstream_checkpoints_no_update BEFORE UPDATE ON workstream_checkpoints
BEGIN SELECT RAISE(ABORT, 'Pisec checkpoints are immutable'); END;
CREATE TRIGGER workstream_checkpoints_no_delete BEFORE DELETE ON workstream_checkpoints
BEGIN SELECT RAISE(ABORT, 'Pisec checkpoints are immutable'); END;

CREATE TABLE completion_packets (
    completion_packet_id TEXT PRIMARY KEY,
    workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    source_commit_oid TEXT NOT NULL CHECK(length(source_commit_oid) IN (40,64) AND source_commit_oid NOT GLOB '*[^0-9a-f]*'),
    task_packet_sha256 TEXT NOT NULL CHECK(length(task_packet_sha256) = 64 AND task_packet_sha256 NOT GLOB '*[^0-9a-f]*'),
    packet_sha256 TEXT NOT NULL UNIQUE CHECK(length(packet_sha256) = 64 AND packet_sha256 NOT GLOB '*[^0-9a-f]*'),
    packet_json TEXT NOT NULL CHECK(length(CAST(packet_json AS BLOB)) <= 65536),
    submitted_at TEXT NOT NULL,
    accepted_at TEXT,
    UNIQUE(workstream_id, sequence)
);
CREATE TRIGGER completion_packets_no_update BEFORE UPDATE ON completion_packets
WHEN NEW.completion_packet_id IS NOT OLD.completion_packet_id
  OR NEW.workstream_id IS NOT OLD.workstream_id
  OR NEW.sequence IS NOT OLD.sequence
  OR NEW.source_commit_oid IS NOT OLD.source_commit_oid
  OR NEW.task_packet_sha256 IS NOT OLD.task_packet_sha256
  OR NEW.packet_sha256 IS NOT OLD.packet_sha256
  OR NEW.packet_json IS NOT OLD.packet_json
  OR NEW.submitted_at IS NOT OLD.submitted_at
  OR (OLD.accepted_at IS NOT NULL AND NEW.accepted_at IS NOT OLD.accepted_at)
BEGIN SELECT RAISE(ABORT, 'Pisec completion packets are immutable'); END;
CREATE TRIGGER completion_packets_no_delete BEFORE DELETE ON completion_packets
BEGIN SELECT RAISE(ABORT, 'Pisec completion packets are immutable'); END;

CREATE TABLE workstream_acceptances (
    acceptance_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    completion_packet_id TEXT NOT NULL REFERENCES completion_packets(completion_packet_id),
    source_commit_oid TEXT NOT NULL CHECK(length(source_commit_oid) IN (40,64) AND source_commit_oid NOT GLOB '*[^0-9a-f]*'),
    target_branch TEXT NOT NULL CHECK(length(target_branch) BETWEEN 1 AND 512),
    candidate_patch_sha256 TEXT NOT NULL CHECK(length(candidate_patch_sha256) = 64 AND candidate_patch_sha256 NOT GLOB '*[^0-9a-f]*'),
    changed_paths_json TEXT NOT NULL CHECK(length(CAST(changed_paths_json AS BLOB)) <= 32768),
    scope_json TEXT NOT NULL CHECK(length(CAST(scope_json AS BLOB)) <= 65536),
    scope_sha256 TEXT NOT NULL CHECK(length(scope_sha256) = 64 AND scope_sha256 NOT GLOB '*[^0-9a-f]*'),
    accepted_at TEXT NOT NULL,
    UNIQUE(workstream_id)
);
CREATE TRIGGER workstream_acceptances_no_update BEFORE UPDATE ON workstream_acceptances
BEGIN SELECT RAISE(ABORT, 'Pisec workstream acceptances are immutable'); END;
CREATE TRIGGER workstream_acceptances_no_delete BEFORE DELETE ON workstream_acceptances
BEGIN SELECT RAISE(ABORT, 'Pisec workstream acceptances are immutable'); END;

CREATE TABLE integration_jobs (
    integration_id TEXT PRIMARY KEY,
    acceptance_id TEXT NOT NULL UNIQUE REFERENCES workstream_acceptances(acceptance_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    state TEXT NOT NULL CHECK(state IN ('queued','refreshing','awaiting_worker','verifying','applying','integrated','needs_attention')),
    target_branch TEXT NOT NULL CHECK(length(target_branch) BETWEEN 1 AND 512),
    candidate_completion_packet_id TEXT NOT NULL REFERENCES completion_packets(completion_packet_id),
    candidate_source_oid TEXT NOT NULL CHECK(length(candidate_source_oid) IN (40,64) AND candidate_source_oid NOT GLOB '*[^0-9a-f]*'),
    integration_source_oid TEXT CHECK(integration_source_oid IS NULL OR (length(integration_source_oid) IN (40,64) AND integration_source_oid NOT GLOB '*[^0-9a-f]*')),
    target_oid TEXT CHECK(target_oid IS NULL OR (length(target_oid) IN (40,64) AND target_oid NOT GLOB '*[^0-9a-f]*')),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
    last_error TEXT,
    next_action TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    integrated_at TEXT
);

CREATE TABLE integration_reports (
    integration_report_id TEXT PRIMARY KEY,
    integration_id TEXT NOT NULL REFERENCES integration_jobs(integration_id),
    workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    source_commit_oid TEXT NOT NULL CHECK(length(source_commit_oid) IN (40,64) AND source_commit_oid NOT GLOB '*[^0-9a-f]*'),
    verification_json TEXT NOT NULL CHECK(length(CAST(verification_json AS BLOB)) <= 65536),
    changed_surfaces_json TEXT NOT NULL CHECK(length(CAST(changed_surfaces_json AS BLOB)) <= 32768),
    residual_risk TEXT NOT NULL CHECK(length(residual_risk) <= 4096),
    report_sha256 TEXT NOT NULL CHECK(length(report_sha256) = 64 AND report_sha256 NOT GLOB '*[^0-9a-f]*'),
    submitted_at TEXT NOT NULL,
    UNIQUE(integration_id, source_commit_oid)
);
CREATE TRIGGER integration_reports_no_update BEFORE UPDATE ON integration_reports
BEGIN SELECT RAISE(ABORT, 'Pisec integration reports are immutable'); END;
CREATE TRIGGER integration_reports_no_delete BEFORE DELETE ON integration_reports
BEGIN SELECT RAISE(ABORT, 'Pisec integration reports are immutable'); END;

CREATE TABLE coordination_requests (
    request_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    task_packet_id TEXT NOT NULL REFERENCES task_packets(task_packet_id),
    kind TEXT NOT NULL CHECK(kind IN ('clarification','blocker','review_request')),
    summary TEXT NOT NULL CHECK(length(summary) BETWEEN 1 AND 1024),
    question TEXT NOT NULL CHECK(length(question) BETWEEN 1 AND 4096),
    blocking INTEGER NOT NULL CHECK(blocking IN (0,1)),
    state TEXT NOT NULL CHECK(state IN ('open','answered','acknowledged')),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 256),
    request_json TEXT NOT NULL CHECK(length(CAST(request_json AS BLOB)) <= 32768),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    answered_at TEXT,
    acknowledged_at TEXT,
    UNIQUE(workstream_id, idempotency_key)
);

CREATE TABLE coordination_packets (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL REFERENCES coordination_requests(request_id),
    actor TEXT NOT NULL CHECK(actor IN ('worker','secretary')),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 256),
    response TEXT NOT NULL CHECK(length(response) BETWEEN 1 AND 4096),
    decision_id TEXT REFERENCES decisions(decision_id),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    UNIQUE(request_id, actor, idempotency_key)
);
CREATE TRIGGER coordination_packets_no_update BEFORE UPDATE ON coordination_packets
BEGIN SELECT RAISE(ABORT, 'Pisec coordination packets are immutable'); END;
CREATE TRIGGER coordination_packets_no_delete BEFORE DELETE ON coordination_packets
BEGIN SELECT RAISE(ABORT, 'Pisec coordination packets are immutable'); END;

CREATE TABLE research_requests (
    request_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    task_packet_id TEXT NOT NULL REFERENCES task_packets(task_packet_id),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 256),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    state TEXT NOT NULL CHECK(state IN ('pending','researching','needs_context','answered','declined','acknowledged')),
    claimed_by_secretary_workstream_id TEXT REFERENCES workstreams(workstream_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    answered_at TEXT,
    acknowledged_at TEXT,
    UNIQUE(workstream_id, idempotency_key)
);

CREATE TABLE research_packets (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL REFERENCES research_requests(request_id),
    actor TEXT NOT NULL CHECK(actor IN ('worker','secretary')),
    kind TEXT NOT NULL CHECK(kind IN ('request','context','needs_context','result','declined')),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 256),
    payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB)) <= 32768),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    UNIQUE(request_id, actor, idempotency_key)
);
CREATE TRIGGER research_packets_no_update BEFORE UPDATE ON research_packets
BEGIN SELECT RAISE(ABORT, 'Pisec research packets are immutable'); END;
CREATE TRIGGER research_packets_no_delete BEFORE DELETE ON research_packets
BEGIN SELECT RAISE(ABORT, 'Pisec research packets are immutable'); END;

CREATE TABLE decisions (
    decision_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    workstream_id TEXT REFERENCES workstreams(workstream_id),
    summary TEXT NOT NULL CHECK(length(summary) <= 512),
    context_json TEXT NOT NULL CHECK(length(CAST(context_json AS BLOB)) <= 65536),
    state TEXT NOT NULL CHECK(state IN ('open','resolved')),
    resolution TEXT CHECK(resolution IS NULL OR length(resolution) <= 4096),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE issues (
    issue_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    reporter_workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    reporter_kind TEXT NOT NULL CHECK(reporter_kind IN ('worker','secretary')),
    category TEXT NOT NULL CHECK(category IN ('permission','access','lifecycle','tooling','other')),
    severity TEXT NOT NULL CHECK(severity IN ('blocking','degraded','improvement')),
    summary TEXT NOT NULL CHECK(length(summary) BETWEEN 1 AND 1024),
    details TEXT NOT NULL CHECK(length(details) BETWEEN 1 AND 4096),
    requested_action TEXT NOT NULL CHECK(length(requested_action) BETWEEN 1 AND 4096),
    evidence_json TEXT NOT NULL CHECK(length(CAST(evidence_json AS BLOB)) <= 32768),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 256),
    report_sha256 TEXT NOT NULL CHECK(length(report_sha256) = 64 AND report_sha256 NOT GLOB '*[^0-9a-f]*'),
    state TEXT NOT NULL CHECK(state IN ('open','acknowledged','remediating','verifying','resolved')),
    disposition TEXT CHECK(disposition IS NULL OR disposition IN ('fixed','declined','duplicate','not_reproducible')),
    resolution TEXT CHECK(resolution IS NULL OR length(resolution) <= 4096),
    escalated_from_issue_id TEXT REFERENCES issues(issue_id),
    resolved_decision_id TEXT REFERENCES decisions(decision_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    acknowledged_at TEXT,
    resolved_at TEXT,
    UNIQUE(reporter_workstream_id, idempotency_key),
    CHECK((state <> 'resolved' AND disposition IS NULL AND resolution IS NULL AND resolved_decision_id IS NULL AND resolved_at IS NULL) OR (state = 'resolved' AND disposition IS NOT NULL AND resolution IS NOT NULL AND resolved_at IS NOT NULL AND ((disposition = 'fixed' AND resolved_decision_id IS NULL) OR (disposition IN ('declined','duplicate','not_reproducible') AND resolved_decision_id IS NOT NULL))))
);
CREATE UNIQUE INDEX one_issue_escalation_source ON issues(escalated_from_issue_id) WHERE escalated_from_issue_id IS NOT NULL;

CREATE TABLE issue_updates (
    update_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    actor_kind TEXT NOT NULL CHECK(actor_kind IN ('worker','secretary','first_mate')),
    actor_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    update_kind TEXT NOT NULL CHECK(update_kind IN ('context','acknowledged','remediation_requested','remediation_linked','remediation_started','remediation_completed','remediation_failed','verification_requested','verification_passed','verification_failed','resolved')),
    payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB)) <= 32768),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 256),
    created_at TEXT NOT NULL,
    UNIQUE(issue_id, actor_id, idempotency_key)
);
CREATE TRIGGER issue_updates_no_update BEFORE UPDATE ON issue_updates
BEGIN SELECT RAISE(ABORT, 'Pisec issue updates are immutable'); END;
CREATE TRIGGER issue_updates_no_delete BEFORE DELETE ON issue_updates
BEGIN SELECT RAISE(ABORT, 'Pisec issue updates are immutable'); END;

CREATE TABLE issue_remediations (
    remediation_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    linked_by_workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    created_at TEXT NOT NULL,
    UNIQUE(issue_id, workstream_id)
);

CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(length(kind) BETWEEN 1 AND 128),
    project_id TEXT REFERENCES projects(project_id),
    workstream_id TEXT REFERENCES workstreams(workstream_id),
    idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key) BETWEEN 1 AND 256),
    request_json TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    state TEXT NOT NULL CHECK(state IN ('planned','applying','succeeded','failed','needs_attention','cancelled')),
    step TEXT NOT NULL,
    result_json TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX one_authoritative_workstream_create
ON operations(workstream_id)
WHERE kind='workstream.create' AND state IN ('planned','applying','needs_attention','succeeded');

CREATE TABLE authorizations (
    authorization_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
    scope_sha256 TEXT NOT NULL CHECK(length(scope_sha256) = 64 AND scope_sha256 NOT GLOB '*[^0-9a-f]*'),
    kind TEXT NOT NULL CHECK(length(kind) BETWEEN 1 AND 128),
    scope_json TEXT NOT NULL,
    actor TEXT NOT NULL CHECK(length(actor) BETWEEN 1 AND 64),
    consumed_at TEXT NOT NULL
);

CREATE TABLE events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    project_id TEXT REFERENCES projects(project_id),
    workstream_id TEXT REFERENCES workstreams(workstream_id),
    operation_id TEXT REFERENCES operations(operation_id),
    payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB)) <= 65536),
    created_at TEXT NOT NULL
);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'Pisec events are immutable'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'Pisec events are immutable'); END;

CREATE TABLE merge_receipts (
    workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    source_commit_oid TEXT NOT NULL CHECK(length(source_commit_oid) IN (40,64) AND source_commit_oid NOT GLOB '*[^0-9a-f]*'),
    target_branch TEXT NOT NULL,
    previous_target_oid TEXT NOT NULL CHECK(length(previous_target_oid) IN (40,64) AND previous_target_oid NOT GLOB '*[^0-9a-f]*'),
    acceptance_id TEXT REFERENCES workstream_acceptances(acceptance_id),
    integration_id TEXT REFERENCES integration_jobs(integration_id),
    accepted_source_commit_oid TEXT CHECK(accepted_source_commit_oid IS NULL OR (length(accepted_source_commit_oid) IN (40,64) AND accepted_source_commit_oid NOT GLOB '*[^0-9a-f]*')),
    verification_json TEXT,
    strategy TEXT NOT NULL DEFAULT 'ff-only',
    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(workstream_id, source_commit_oid)
);

CREATE TABLE attention_items (
    attention_id TEXT PRIMARY KEY,
    recipient_workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('coordination','research','issue','completion','integration')),
    source_id TEXT NOT NULL,
    source_event_sequence INTEGER NOT NULL REFERENCES events(sequence),
    priority INTEGER NOT NULL CHECK(priority IN (0,1,2)),
    created_at TEXT NOT NULL,
    revision_at TEXT NOT NULL,
    last_presented_revision INTEGER NOT NULL DEFAULT 0 CHECK(last_presented_revision >= 0 AND last_presented_revision <= source_event_sequence),
    first_presented_at TEXT,
    last_presented_at TEXT,
    presentation_count INTEGER NOT NULL DEFAULT 0 CHECK(presentation_count >= 0),
    updated_at TEXT NOT NULL,
    UNIQUE(recipient_workstream_id, source_kind, source_id)
);
CREATE INDEX attention_recipient_revision
ON attention_items(recipient_workstream_id, source_event_sequence, last_presented_revision);
CREATE INDEX attention_due_order
ON attention_items(priority, revision_at, recipient_workstream_id);
'''


def schema_digest() -> str:
    """Return the unprefixed digest of the canonical v1 schema text."""
    return hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()


def apply_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
