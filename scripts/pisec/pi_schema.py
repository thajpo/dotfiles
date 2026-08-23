"""Fresh Pisec core epoch-fourteen schema."""

from __future__ import annotations
import json
import hashlib
import os
import shutil
import sqlite3
from .models import utc_now

SCHEMA_NAME = "pisec-core"
SCHEMA_VERSION = 14
MIGRATION_NAME = "pisec-core-epoch-14"
PREVIOUS_SCHEMA_VERSION = 13
PREVIOUS_SCHEMA_NAME = "pisec-core"
PREVIOUS_MIGRATION_NAME = "pisec-core-epoch-13"
PREVIOUS_SCHEMA_DIGEST = "sha256:db5e4cb13fe6f4aeaa929130a9ce136b08720cdf7eee211590c63f27054ef814"
PREVIOUS_EPOCH_TWELVE_SCHEMA_VERSION = 12
PREVIOUS_EPOCH_TWELVE_SCHEMA_NAME = "pisec-core"
PREVIOUS_EPOCH_TWELVE_MIGRATION_NAME = "pisec-core-epoch-12"
PREVIOUS_EPOCH_TWELVE_SCHEMA_DIGEST = "sha256:21f28f9aab4dace088a2bb84f55f5f934296003462043b6d0d74c314315573bd"
PREVIOUS_EPOCH_ELEVEN_SCHEMA_VERSION = 11
PREVIOUS_EPOCH_ELEVEN_SCHEMA_NAME = "pisec-core"
PREVIOUS_EPOCH_ELEVEN_MIGRATION_NAME = "pisec-core-epoch-11"
PREVIOUS_EPOCH_ELEVEN_SCHEMA_DIGEST = "sha256:2dbbf3e3e01957a231a7b75c87d7a1799836884fafa2b3f53dfc2fd4a5c3e466"
DEPLOYED_EPOCH_ELEVEN_SCHEMA_DIGEST = PREVIOUS_EPOCH_ELEVEN_SCHEMA_DIGEST
PREVIOUS_EPOCH_TEN_SCHEMA_VERSION = 10
PREVIOUS_EPOCH_TEN_SCHEMA_NAME = "pisec-core-epoch-10"
PREVIOUS_EPOCH_TEN_MIGRATION_NAME = "pisec-core-epoch-10"
PREVIOUS_EPOCH_TEN_SCHEMA_DIGEST = "sha256:8094c25204bb183f330101c41ac4a643bc288f402619370c279e3a3517c84f58"
DEPLOYED_EPOCH_TEN_SCHEMA_DIGEST = PREVIOUS_EPOCH_TEN_SCHEMA_DIGEST
PREVIOUS_EPOCH_NINE_SCHEMA_VERSION = 9
PREVIOUS_EPOCH_NINE_SCHEMA_NAME = "pisec-core-epoch-9"
PREVIOUS_EPOCH_NINE_MIGRATION_NAME = "pisec-core-epoch-9"
PREVIOUS_EPOCH_NINE_SCHEMA_DIGEST = "sha256:7f5bf97e11cfee4a7c15fe059fb52756f2b64df086d7a7a7d4f2a2aaac595d32"
DEPLOYED_EPOCH_NINE_SCHEMA_DIGEST = PREVIOUS_EPOCH_NINE_SCHEMA_DIGEST
PREVIOUS_EPOCH_EIGHT_SCHEMA_VERSION = 8
PREVIOUS_EPOCH_EIGHT_SCHEMA_NAME = "pisec-core-epoch-8"
PREVIOUS_EPOCH_EIGHT_MIGRATION_NAME = "pisec-core-epoch-8"
PREVIOUS_EPOCH_EIGHT_SCHEMA_DIGEST = "sha256:89af304d8407a4e8832113465b1e5d6c320b41cc84e9659fcf53e46e5ea8c504"
DEPLOYED_EPOCH_EIGHT_SCHEMA_DIGEST = "sha256:d57f44d6ad0f5648199d070f59f03bfc36c9b0c63dcfac5790b044050499aded"
EPOCH_SEVEN_SCHEMA_VERSION = 7
EPOCH_SEVEN_MIGRATION_NAME = "pisec-core-epoch-7"
EPOCH_SEVEN_SCHEMA_DIGEST = "sha256:35e63da90e5a851e2f57d7cddf21db58ace28471aa5b3af5cb73363165729c95"
EPOCH_SIX_SCHEMA_VERSION = 6
EPOCH_SIX_MIGRATION_NAME = "pisec-core-epoch-6"
EPOCH_SIX_SCHEMA_DIGEST = "sha256:c00cd142b2cd4dd775c3d7878820c4fd69f945e9e4254cfd18414bc82877ca59"

SCHEMA_SQL = r'''
CREATE TABLE control_meta (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    schema_name TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    schema_sha256 TEXT NOT NULL,
    migration_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    repository_path TEXT NOT NULL,
    git_common_dir TEXT NOT NULL UNIQUE,
    default_ref TEXT NOT NULL,
    remote_url TEXT CHECK(remote_url IS NULL OR length(remote_url) BETWEEN 1 AND 2048),
    data_dirs TEXT,
    secretary_workstream_id TEXT,
    coordination_mode TEXT NOT NULL DEFAULT 'direct' CHECK(coordination_mode IN ('fleet','project','direct')),
    worker_creation_policy TEXT NOT NULL DEFAULT 'review' CHECK(worker_creation_policy IN ('review','bounded_auto')),
    worker_creation_policy_json TEXT NOT NULL DEFAULT '{}',
    merge_policy TEXT NOT NULL DEFAULT 'review' CHECK(merge_policy IN ('review','checked_auto')),
    merge_policy_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
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
    base_commit_oid TEXT NOT NULL,
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

CREATE TABLE runtime_releases (
    release_id TEXT PRIMARY KEY,
    harness_id TEXT NOT NULL CHECK(length(harness_id) BETWEEN 1 AND 64),
    adapter_version TEXT NOT NULL CHECK(length(adapter_version) BETWEEN 1 AND 128),
    content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256) = 64),
    manifest_json TEXT NOT NULL CHECK(length(CAST(manifest_json AS BLOB)) <= 262144),
    root_path TEXT,
    created_at TEXT NOT NULL
);
CREATE TRIGGER runtime_releases_no_update BEFORE UPDATE ON runtime_releases
BEGIN SELECT RAISE(ABORT, 'Pisec runtime releases are immutable'); END;
CREATE TRIGGER runtime_releases_no_delete BEFORE DELETE ON runtime_releases
BEGIN SELECT RAISE(ABORT, 'Pisec runtime releases are immutable'); END;
CREATE TABLE runtime_release_channels (
    channel TEXT PRIMARY KEY CHECK(channel = 'current'),
    release_id TEXT NOT NULL REFERENCES runtime_releases(release_id),
    activated_at TEXT NOT NULL
);

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
    private_git_object_dir TEXT,
    policy_path TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL CHECK(length(policy_sha256) = 64),
    runtime_token_sha256 TEXT NOT NULL CHECK(length(runtime_token_sha256) = 64),
    desired_release_id TEXT REFERENCES runtime_releases(release_id),
    applied_release_id TEXT REFERENCES runtime_releases(release_id),
    launch_release_id TEXT REFERENCES runtime_releases(release_id),
    desired_generation_sha256 TEXT CHECK(desired_generation_sha256 IS NULL OR length(desired_generation_sha256) = 64),
    applied_generation_sha256 TEXT CHECK(applied_generation_sha256 IS NULL OR length(applied_generation_sha256) = 64),
    launch_generation_sha256 TEXT CHECK(launch_generation_sha256 IS NULL OR length(launch_generation_sha256) = 64),
    refresh_pending INTEGER NOT NULL DEFAULT 0 CHECK(refresh_pending IN (0,1)),
    runtime_instance_id TEXT,
    observed_state TEXT NOT NULL CHECK(observed_state IN ('unknown','starting','working','blocked','idle','done','stopped','missing','error')),
    report_seq INTEGER NOT NULL DEFAULT 0 CHECK(report_seq >= 0),
    workspace_report_seq INTEGER NOT NULL DEFAULT 0 CHECK(workspace_report_seq >= 0),
    last_observed_at TEXT,
    updated_at TEXT NOT NULL
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
    scope_sha256 TEXT NOT NULL CHECK(length(scope_sha256) = 64),
    packet_json TEXT NOT NULL CHECK(length(CAST(packet_json AS BLOB)) <= 32768),
    packet_sha256 TEXT NOT NULL CHECK(length(packet_sha256) = 64),
    issued_at TEXT NOT NULL
);
CREATE TRIGGER task_packets_no_update BEFORE UPDATE ON task_packets
BEGIN
    SELECT RAISE(ABORT, 'Pisec task packets are immutable');
END;
CREATE TRIGGER task_packets_no_delete BEFORE DELETE ON task_packets
BEGIN
    SELECT RAISE(ABORT, 'Pisec task packets are immutable');
END;
CREATE TABLE workstream_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    runtime_instance_id TEXT NOT NULL CHECK(length(runtime_instance_id) BETWEEN 1 AND 256),
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 256),
    phase TEXT NOT NULL CHECK(phase IN ('investigating','implementing','verifying','needs_input','ready_review')),
    summary TEXT NOT NULL CHECK(length(summary) BETWEEN 1 AND 1024),
    next_action TEXT NOT NULL CHECK(length(next_action) BETWEEN 1 AND 1024),
    blocker_code TEXT CHECK(blocker_code IS NULL OR length(blocker_code) BETWEEN 1 AND 128),
    blocker TEXT CHECK(blocker IS NULL OR length(blocker) BETWEEN 1 AND 2048),
    evidence_json TEXT NOT NULL CHECK(length(CAST(evidence_json AS BLOB)) <= 32768),
    created_at TEXT NOT NULL,
    UNIQUE(workstream_id, idempotency_key),
    UNIQUE(workstream_id, sequence),
    CHECK((phase = 'needs_input') = (blocker_code IS NOT NULL AND blocker IS NOT NULL)),
    CHECK(phase <> 'needs_input' OR blocker_code IS NOT NULL)
);
CREATE TRIGGER workstream_checkpoints_no_update BEFORE UPDATE ON workstream_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'Pisec checkpoints are immutable');
END;
CREATE TRIGGER workstream_checkpoints_no_delete BEFORE DELETE ON workstream_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'Pisec checkpoints are immutable');
END;

CREATE TABLE completion_packets (
    completion_packet_id TEXT PRIMARY KEY,
    workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    source_commit_oid TEXT NOT NULL CHECK(length(source_commit_oid) IN (40,64)),
    task_packet_sha256 TEXT NOT NULL CHECK(length(task_packet_sha256) = 64),
    packet_sha256 TEXT NOT NULL UNIQUE CHECK(length(packet_sha256) = 64),
    packet_json TEXT NOT NULL CHECK(length(CAST(packet_json AS BLOB)) <= 65536),
    submitted_at TEXT NOT NULL,
    accepted_at TEXT NOT NULL
);
CREATE TRIGGER completion_packets_no_update BEFORE UPDATE ON completion_packets
BEGIN
    SELECT RAISE(ABORT, 'Pisec completion packets are immutable');
END;
CREATE TRIGGER completion_packets_no_delete BEFORE DELETE ON completion_packets
BEGIN
    SELECT RAISE(ABORT, 'Pisec completion packets are immutable');
END;

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
    linked_request_id TEXT REFERENCES coordination_requests(request_id),
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
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE(request_id, actor, idempotency_key)
);
CREATE TRIGGER coordination_packets_no_update BEFORE UPDATE ON coordination_packets
BEGIN
    SELECT RAISE(ABORT, 'Pisec coordination packets are immutable');
END;
CREATE TRIGGER coordination_packets_no_delete BEFORE DELETE ON coordination_packets
BEGIN
    SELECT RAISE(ABORT, 'Pisec coordination packets are immutable');
END;

CREATE TABLE runtime_bootstrap_sessions (
    workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    session_file TEXT NOT NULL CHECK(length(session_file) BETWEEN 1 AND 4096),
    task_packet_delivered INTEGER NOT NULL DEFAULT 0 CHECK(task_packet_delivered IN (0,1)),
    bootstrap_generation INTEGER NOT NULL DEFAULT 0 CHECK(bootstrap_generation >= 0),
    acknowledged_generation INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged_generation >= 0 AND acknowledged_generation <= bootstrap_generation),
    last_event_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_event_sequence >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(workstream_id, session_file)
);

CREATE TABLE research_requests (
    request_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    task_packet_id TEXT NOT NULL REFERENCES task_packets(task_packet_id),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 256),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
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
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE(request_id, actor, idempotency_key)
);
CREATE TRIGGER research_packets_no_update BEFORE UPDATE ON research_packets
BEGIN
    SELECT RAISE(ABORT, 'Pisec research packets are immutable');
END;
CREATE TRIGGER research_packets_no_delete BEFORE DELETE ON research_packets
BEGIN
    SELECT RAISE(ABORT, 'Pisec research packets are immutable');
END;

CREATE TABLE research_inbox (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
    notified_generation INTEGER NOT NULL DEFAULT 0 CHECK(notified_generation >= 0 AND notified_generation <= generation),
    updated_at TEXT NOT NULL
);

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
    report_sha256 TEXT NOT NULL CHECK(length(report_sha256) = 64),
    state TEXT NOT NULL CHECK(state IN ('open','acknowledged','remediating','verifying','resolved')),
    disposition TEXT CHECK(disposition IS NULL OR disposition IN ('fixed','declined','duplicate','not_reproducible')),
    resolution TEXT CHECK(resolution IS NULL OR length(resolution) <= 4096),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    acknowledged_at TEXT,
    resolved_at TEXT,
    UNIQUE(reporter_workstream_id, idempotency_key)
);
CREATE TABLE secretary_issue_reports (
    issue_id TEXT PRIMARY KEY REFERENCES issues(issue_id),
    project_id TEXT NOT NULL,
    secretary_workstream_id TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    summary TEXT NOT NULL,
    details TEXT NOT NULL,
    requested_action TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    acknowledged_at TEXT,
    resolved_at TEXT,
    state TEXT NOT NULL
);
CREATE TRIGGER issues_secretary_report_insert AFTER INSERT ON issues
WHEN NEW.reporter_kind = 'secretary'
BEGIN
    INSERT INTO secretary_issue_reports SELECT NEW.issue_id,NEW.project_id,NEW.reporter_workstream_id,NEW.category,NEW.severity,NEW.summary,NEW.details,NEW.requested_action,NEW.evidence_json,NEW.created_at,NEW.updated_at,NEW.acknowledged_at,NEW.resolved_at,NEW.state;
END;
CREATE TRIGGER issues_secretary_report_update AFTER UPDATE OF state,updated_at,acknowledged_at,resolved_at ON issues
WHEN NEW.reporter_kind = 'secretary'
BEGIN
    UPDATE secretary_issue_reports SET updated_at=NEW.updated_at,acknowledged_at=NEW.acknowledged_at,resolved_at=NEW.resolved_at,state=NEW.state WHERE issue_id=NEW.issue_id;
END;
CREATE TABLE issue_updates (
    update_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    actor_kind TEXT NOT NULL CHECK(actor_kind IN ('worker','secretary','first_mate')),
    actor_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    update_kind TEXT NOT NULL CHECK(update_kind IN ('context','acknowledged','remediation_linked','verification_requested','verification_passed','verification_failed','resolved')),
    payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB)) <= 32768),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
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
    kind TEXT NOT NULL CHECK(kind IN ('access_grant','workstream','deployment')),
    access_grant_id TEXT REFERENCES access_grants(grant_id),
    workstream_id TEXT REFERENCES workstreams(workstream_id),
    deployment_id TEXT REFERENCES deployment_actions(deployment_id),
    created_at TEXT NOT NULL,
    CHECK((access_grant_id IS NOT NULL) + (workstream_id IS NOT NULL) + (deployment_id IS NOT NULL) = 1),
    UNIQUE(issue_id, access_grant_id),
    UNIQUE(issue_id, workstream_id),
    UNIQUE(issue_id, deployment_id)
);
CREATE TABLE issue_inbox (
    workstream_id TEXT PRIMARY KEY REFERENCES workstreams(workstream_id),
    generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
    notified_generation INTEGER NOT NULL DEFAULT 0 CHECK(notified_generation >= 0 AND notified_generation <= generation),
    updated_at TEXT NOT NULL
);
CREATE TABLE access_grants (
    grant_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    subject_kind TEXT NOT NULL CHECK(subject_kind IN ('workstream','project_workers')),
    workstream_id TEXT REFERENCES workstreams(workstream_id),
    path TEXT NOT NULL CHECK(length(path) BETWEEN 1 AND 4096),
    access_mode TEXT NOT NULL CHECK(access_mode = 'read'),
    state TEXT NOT NULL CHECK(state IN ('proposed','activating','active','revoking','revoked')),
    proposal_operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    issue_id TEXT REFERENCES issues(issue_id),
    created_at TEXT NOT NULL,
    approved_at TEXT,
    revoked_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK((subject_kind = 'workstream') = (workstream_id IS NOT NULL))
);
CREATE UNIQUE INDEX access_grants_active_subject_path ON access_grants(subject_kind, IFNULL(workstream_id, project_id), path) WHERE state <> 'revoked';
CREATE TABLE merge_receipts (
    workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    source_commit_oid TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    previous_target_oid TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(workstream_id, source_commit_oid)
);
CREATE TABLE deployment_actions (
    deployment_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
    source_commit_oid TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    platform TEXT NOT NULL CHECK(platform IN ('linux','macos')),
    installed_control_plane_root TEXT NOT NULL,
    recipe_json TEXT NOT NULL CHECK(length(CAST(recipe_json AS BLOB)) <= 32768),
    recipe_sha256 TEXT NOT NULL CHECK(length(recipe_sha256) = 64),
    request_json TEXT CHECK(request_json IS NULL OR length(CAST(request_json AS BLOB)) <= 32768),
    request_sha256 TEXT CHECK(request_sha256 IS NULL OR length(request_sha256) = 64),
    state TEXT NOT NULL CHECK(state IN ('planned','authorized','running','applied','failed','needs_attention')),
    current_step TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    authorized_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('project.register','project.deactivate','secretary.ensure','first_mate.ensure','workstream.create','workstream.complete','workstream.retire','workstream.cleanup','access.grant','access.revoke','deployment.apply','runtime.release.build','runtime.release.activate')),
    project_id TEXT REFERENCES projects(project_id),
    workstream_id TEXT REFERENCES workstreams(workstream_id),
    idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key) BETWEEN 1 AND 256),
    request_json TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('planned','applying','succeeded','failed','needs_attention','cancelled')),
    step TEXT NOT NULL,
    result_json TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE authorizations (
    authorization_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
    scope_sha256 TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('workstream.create','access.grant','access.revoke','deployment.apply')),
    scope_json TEXT NOT NULL,
    actor TEXT NOT NULL CHECK(actor IN ('secretary','first_mate')),
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
BEGIN
    SELECT RAISE(ABORT, 'Pisec events are immutable');
END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'Pisec events are immutable');
END;
'''


def schema_digest() -> str:
    return "sha256:" + hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()


def apply_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)

def _registered_remote_url(repository_path: str) -> str | None:
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        return None
    result = subprocess.run(
        [
            executable,
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            repository_path,
            "config",
            "--local",
            "--get",
            "remote.origin.url",
        ],
        env={
            "HOME": "/nonexistent",
            "PATH": os.defpath,
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value or len(value) > 2048 or value.startswith("-") or any(ord(char) < 0x20 for char in value):
        return None
    return value


def _rebuild_epoch_eight_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE authorizations_epoch8 (
            authorization_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
            kind TEXT NOT NULL CHECK(kind = 'workstream.create'),
            scope_json TEXT NOT NULL,
            scope_sha256 TEXT NOT NULL,
            actor TEXT NOT NULL CHECK(actor IN ('secretary','first_mate')),
            consumed_at TEXT NOT NULL
        )
        """
    )
    connection.execute("INSERT INTO authorizations_epoch8 SELECT authorization_id,operation_id,kind,scope_json,scope_sha256,actor,consumed_at FROM authorizations")
    connection.execute("DROP TABLE authorizations")
    connection.execute("ALTER TABLE authorizations_epoch8 RENAME TO authorizations")

    connection.execute(
        """
        CREATE TABLE operations_epoch8 (
            operation_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('project.register','secretary.ensure','first_mate.ensure','workstream.create','workstream.complete','workstream.retire','workstream.cleanup')),
            project_id TEXT REFERENCES projects(project_id),
            workstream_id TEXT REFERENCES workstreams(workstream_id),
            idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key) BETWEEN 1 AND 256),
            request_json TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('planned','applying','succeeded','failed','needs_attention','cancelled')),
            step TEXT NOT NULL,
            result_json TEXT,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute("INSERT INTO operations_epoch8 SELECT operation_id,kind,project_id,workstream_id,idempotency_key,request_json,request_sha256,state,step,result_json,error_code,error_message,created_at,updated_at FROM operations")
    connection.execute("DROP TABLE operations")
    connection.execute("ALTER TABLE operations_epoch8 RENAME TO operations")

    connection.execute(
        """
        CREATE TABLE workstreams_epoch8 (
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
            base_commit_oid TEXT NOT NULL,
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
        )
        """
    )
    connection.execute("INSERT INTO workstreams_epoch8 SELECT workstream_id,project_id,kind,title,purpose,brief,harness_id,workspace_adapter_id,execution_profile,target_ref,base_commit_oid,branch_name,worktree_path,desired_state,provisioning_state,attention_reason,created_at,updated_at,completed_at,retired_at FROM workstreams")
    connection.execute("DROP TABLE workstreams")
    connection.execute("ALTER TABLE workstreams_epoch8 RENAME TO workstreams")
    connection.execute("CREATE UNIQUE INDEX one_active_secretary_per_project ON workstreams(project_id) WHERE kind='secretary' AND desired_state <> 'retired'")
    connection.execute("CREATE UNIQUE INDEX one_active_first_mate ON workstreams(kind) WHERE kind='first_mate' AND desired_state <> 'retired'")

def _add_epoch_ten_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS workstream_checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
            runtime_instance_id TEXT NOT NULL CHECK(length(runtime_instance_id) BETWEEN 1 AND 256),
            sequence INTEGER NOT NULL CHECK(sequence > 0),
            idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 256),
            phase TEXT NOT NULL CHECK(phase IN ('investigating','implementing','verifying','needs_input','ready_review')),
            summary TEXT NOT NULL CHECK(length(summary) BETWEEN 1 AND 1024),
            next_action TEXT NOT NULL CHECK(length(next_action) BETWEEN 1 AND 1024),
            blocker_code TEXT CHECK(blocker_code IS NULL OR length(blocker_code) BETWEEN 1 AND 128),
            blocker TEXT CHECK(blocker IS NULL OR length(blocker) BETWEEN 1 AND 2048),
            evidence_json TEXT NOT NULL CHECK(length(CAST(evidence_json AS BLOB)) <= 32768),
            created_at TEXT NOT NULL,
            UNIQUE(workstream_id, idempotency_key),
            UNIQUE(workstream_id, sequence),
            CHECK((phase = 'needs_input') = (blocker_code IS NOT NULL AND blocker IS NOT NULL)),
            CHECK(phase <> 'needs_input' OR blocker_code IS NOT NULL)
        );
        CREATE TRIGGER IF NOT EXISTS workstream_checkpoints_no_update BEFORE UPDATE ON workstream_checkpoints
        BEGIN SELECT RAISE(ABORT, 'Pisec checkpoints are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS workstream_checkpoints_no_delete BEFORE DELETE ON workstream_checkpoints
        BEGIN SELECT RAISE(ABORT, 'Pisec checkpoints are immutable'); END;
        CREATE TABLE IF NOT EXISTS completion_packets (
            completion_packet_id TEXT PRIMARY KEY,
            workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
            source_commit_oid TEXT NOT NULL CHECK(length(source_commit_oid) IN (40,64)),
            task_packet_sha256 TEXT NOT NULL CHECK(length(task_packet_sha256) = 64),
            packet_sha256 TEXT NOT NULL UNIQUE CHECK(length(packet_sha256) = 64),
            packet_json TEXT NOT NULL CHECK(length(CAST(packet_json AS BLOB)) <= 65536),
            submitted_at TEXT NOT NULL,
            accepted_at TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS completion_packets_no_update BEFORE UPDATE ON completion_packets
        BEGIN SELECT RAISE(ABORT, 'Pisec completion packets are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS completion_packets_no_delete BEFORE DELETE ON completion_packets
        BEGIN SELECT RAISE(ABORT, 'Pisec completion packets are immutable'); END;
        CREATE TABLE IF NOT EXISTS coordination_requests (
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
            linked_request_id TEXT REFERENCES coordination_requests(request_id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            answered_at TEXT,
            acknowledged_at TEXT,
            UNIQUE(workstream_id, idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS coordination_packets (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            packet_id TEXT NOT NULL UNIQUE,
            request_id TEXT NOT NULL REFERENCES coordination_requests(request_id),
            actor TEXT NOT NULL CHECK(actor IN ('worker','secretary')),
            idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 256),
            response TEXT NOT NULL CHECK(length(response) BETWEEN 1 AND 4096),
            decision_id TEXT REFERENCES decisions(decision_id),
            payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
            created_at TEXT NOT NULL,
            UNIQUE(request_id, actor, idempotency_key)
        );
        CREATE TRIGGER IF NOT EXISTS coordination_packets_no_update BEFORE UPDATE ON coordination_packets
        BEGIN SELECT RAISE(ABORT, 'Pisec coordination packets are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS coordination_packets_no_delete BEFORE DELETE ON coordination_packets
        BEGIN SELECT RAISE(ABORT, 'Pisec coordination packets are immutable'); END;
        CREATE TABLE IF NOT EXISTS runtime_bootstrap_sessions (
            workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
            session_file TEXT NOT NULL CHECK(length(session_file) BETWEEN 1 AND 4096),
            task_packet_delivered INTEGER NOT NULL DEFAULT 0 CHECK(task_packet_delivered IN (0,1)),
            bootstrap_generation INTEGER NOT NULL DEFAULT 0 CHECK(bootstrap_generation >= 0),
            acknowledged_generation INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged_generation >= 0 AND acknowledged_generation <= bootstrap_generation),
            last_event_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_event_sequence >= 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(workstream_id, session_file)
        );
        """
    )


def _add_epoch_twelve_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS issues (
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
            report_sha256 TEXT NOT NULL CHECK(length(report_sha256) = 64),
            state TEXT NOT NULL CHECK(state IN ('open','acknowledged','remediating','verifying','resolved')),
            disposition TEXT CHECK(disposition IS NULL OR disposition IN ('fixed','declined','duplicate','not_reproducible')),
            resolution TEXT CHECK(resolution IS NULL OR length(resolution) <= 4096),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            acknowledged_at TEXT,
            resolved_at TEXT,
            UNIQUE(reporter_workstream_id, idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS issue_updates (
            update_id TEXT PRIMARY KEY,
            issue_id TEXT NOT NULL REFERENCES issues(issue_id),
            actor_kind TEXT NOT NULL CHECK(actor_kind IN ('worker','secretary','first_mate')),
            actor_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
            update_kind TEXT NOT NULL CHECK(update_kind IN ('context','acknowledged','remediation_linked','verification_requested','verification_passed','verification_failed','resolved')),
            payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB)) <= 32768),
            payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
            idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 256),
            created_at TEXT NOT NULL,
            UNIQUE(issue_id, actor_id, idempotency_key)
        );
        CREATE TRIGGER IF NOT EXISTS issue_updates_no_update BEFORE UPDATE ON issue_updates
        BEGIN SELECT RAISE(ABORT, 'Pisec issue updates are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS issue_updates_no_delete BEFORE DELETE ON issue_updates
        BEGIN SELECT RAISE(ABORT, 'Pisec issue updates are immutable'); END;
        CREATE TABLE IF NOT EXISTS issue_inbox (
            workstream_id TEXT PRIMARY KEY REFERENCES workstreams(workstream_id),
            generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
            notified_generation INTEGER NOT NULL DEFAULT 0 CHECK(notified_generation >= 0 AND notified_generation <= generation),
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS access_grants (
            grant_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(project_id),
            subject_kind TEXT NOT NULL CHECK(subject_kind IN ('workstream','project_workers')),
            workstream_id TEXT REFERENCES workstreams(workstream_id),
            path TEXT NOT NULL CHECK(length(path) BETWEEN 1 AND 4096),
            access_mode TEXT NOT NULL CHECK(access_mode = 'read'),
            state TEXT NOT NULL CHECK(state IN ('proposed','activating','active','revoking','revoked')),
            proposal_operation_id TEXT NOT NULL REFERENCES operations(operation_id),
            issue_id TEXT REFERENCES issues(issue_id),
            created_at TEXT NOT NULL,
            approved_at TEXT,
            revoked_at TEXT,
            updated_at TEXT NOT NULL,
            CHECK((subject_kind = 'workstream') = (workstream_id IS NOT NULL))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS access_grants_active_subject_path ON access_grants(subject_kind, IFNULL(workstream_id, project_id), path) WHERE state <> 'revoked';
        CREATE TABLE IF NOT EXISTS issue_remediations (
            remediation_id TEXT PRIMARY KEY,
            issue_id TEXT NOT NULL REFERENCES issues(issue_id),
            kind TEXT NOT NULL CHECK(kind IN ('access_grant','workstream','deployment')),
            access_grant_id TEXT REFERENCES access_grants(grant_id),
            workstream_id TEXT REFERENCES workstreams(workstream_id),
            deployment_id TEXT REFERENCES deployment_actions(deployment_id),
            created_at TEXT NOT NULL,
            CHECK((access_grant_id IS NOT NULL) + (workstream_id IS NOT NULL) + (deployment_id IS NOT NULL) = 1),
            UNIQUE(issue_id, access_grant_id),
            UNIQUE(issue_id, workstream_id),
            UNIQUE(issue_id, deployment_id)
        );
        CREATE TABLE IF NOT EXISTS secretary_issue_reports (
            issue_id TEXT PRIMARY KEY REFERENCES issues(issue_id),
            project_id TEXT NOT NULL,
            secretary_workstream_id TEXT NOT NULL,
            category TEXT NOT NULL,
            severity TEXT NOT NULL,
            summary TEXT NOT NULL,
            details TEXT NOT NULL,
            requested_action TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            acknowledged_at TEXT,
            resolved_at TEXT,
            state TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS issues_secretary_report_insert AFTER INSERT ON issues
        WHEN NEW.reporter_kind = 'secretary'
        BEGIN
            INSERT INTO secretary_issue_reports SELECT NEW.issue_id,NEW.project_id,NEW.reporter_workstream_id,NEW.category,NEW.severity,NEW.summary,NEW.details,NEW.requested_action,NEW.evidence_json,NEW.created_at,NEW.updated_at,NEW.acknowledged_at,NEW.resolved_at,NEW.state;
        END;
        CREATE TRIGGER IF NOT EXISTS issues_secretary_report_update AFTER UPDATE OF state,updated_at,acknowledged_at,resolved_at ON issues
        WHEN NEW.reporter_kind = 'secretary'
        BEGIN
            UPDATE secretary_issue_reports SET updated_at=NEW.updated_at,acknowledged_at=NEW.acknowledged_at,resolved_at=NEW.resolved_at,state=NEW.state WHERE issue_id=NEW.issue_id;
        END;
        CREATE TABLE IF NOT EXISTS merge_receipts (
            workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
            source_commit_oid TEXT NOT NULL,
            target_branch TEXT NOT NULL,
            previous_target_oid TEXT NOT NULL,
            event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
            created_at TEXT NOT NULL,
            PRIMARY KEY(workstream_id, source_commit_oid)
        );
        CREATE TABLE IF NOT EXISTS deployment_actions (
            deployment_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(project_id),
            workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id),
            source_commit_oid TEXT NOT NULL,
            target_branch TEXT NOT NULL,
            platform TEXT NOT NULL CHECK(platform IN ('linux','macos')),
            installed_control_plane_root TEXT NOT NULL,
            recipe_json TEXT NOT NULL CHECK(length(CAST(recipe_json AS BLOB)) <= 32768),
            recipe_sha256 TEXT NOT NULL CHECK(length(recipe_sha256) = 64),
            request_json TEXT CHECK(request_json IS NULL OR length(CAST(request_json AS BLOB)) <= 32768),
            request_sha256 TEXT CHECK(request_sha256 IS NULL OR length(request_sha256) = 64),
            state TEXT NOT NULL CHECK(state IN ('planned','authorized','running','applied','failed','needs_attention')),
            current_step TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL,
            authorized_at TEXT,
            updated_at TEXT NOT NULL
        );
        """
    )

def _rebuild_epoch_twelve_operations(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE operations_epoch12 (
            operation_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('project.register','project.deactivate','secretary.ensure','first_mate.ensure','workstream.create','workstream.complete','workstream.retire','workstream.cleanup','access.grant','access.revoke','deployment.apply')),
            project_id TEXT REFERENCES projects(project_id),
            workstream_id TEXT REFERENCES workstreams(workstream_id),
            idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key) BETWEEN 1 AND 256),
            request_json TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('planned','applying','succeeded','failed','needs_attention','cancelled')),
            step TEXT NOT NULL,
            result_json TEXT,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute("INSERT INTO operations_epoch12 SELECT operation_id,kind,project_id,workstream_id,idempotency_key,request_json,request_sha256,state,step,result_json,error_code,error_message,created_at,updated_at FROM operations")
    connection.execute("DROP TABLE operations")
    connection.execute("ALTER TABLE operations_epoch12 RENAME TO operations")
    connection.execute(
        """
        CREATE TABLE authorizations_epoch12 (
            authorization_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
            scope_sha256 TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('workstream.create','access.grant','access.revoke','deployment.apply')),
            scope_json TEXT NOT NULL,
            actor TEXT NOT NULL CHECK(actor IN ('secretary','first_mate')),
            consumed_at TEXT NOT NULL
        )
        """
    )
    connection.execute("INSERT INTO authorizations_epoch12 SELECT authorization_id,operation_id,scope_sha256,kind,scope_json,actor,consumed_at FROM authorizations")
    connection.execute("DROP TABLE authorizations")
    connection.execute("ALTER TABLE authorizations_epoch12 RENAME TO authorizations")


def _add_epoch_thirteen_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS runtime_releases (
            release_id TEXT PRIMARY KEY,
            harness_id TEXT NOT NULL CHECK(length(harness_id) BETWEEN 1 AND 64),
            adapter_version TEXT NOT NULL CHECK(length(adapter_version) BETWEEN 1 AND 128),
            content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256) = 64),
            manifest_json TEXT NOT NULL CHECK(length(CAST(manifest_json AS BLOB)) <= 262144),
            root_path TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS runtime_releases_no_update BEFORE UPDATE ON runtime_releases
        BEGIN SELECT RAISE(ABORT, 'Pisec runtime releases are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS runtime_releases_no_delete BEFORE DELETE ON runtime_releases
        BEGIN SELECT RAISE(ABORT, 'Pisec runtime releases are immutable'); END;
        CREATE TABLE IF NOT EXISTS runtime_release_channels (
            channel TEXT PRIMARY KEY CHECK(channel = 'current'),
            release_id TEXT NOT NULL REFERENCES runtime_releases(release_id),
            activated_at TEXT NOT NULL
        );
        """
    )
    binding_columns = {row[1] for row in connection.execute("PRAGMA table_info(runtime_bindings)")}
    if "desired_release_id" not in binding_columns:
        connection.execute("ALTER TABLE runtime_bindings ADD COLUMN desired_release_id TEXT REFERENCES runtime_releases(release_id)")
    if "applied_release_id" not in binding_columns:
        connection.execute("ALTER TABLE runtime_bindings ADD COLUMN applied_release_id TEXT REFERENCES runtime_releases(release_id)")
    if "launch_release_id" not in binding_columns:
        connection.execute("ALTER TABLE runtime_bindings ADD COLUMN launch_release_id TEXT REFERENCES runtime_releases(release_id)")


def _rebuild_epoch_thirteen_operations(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE operations_epoch13 (
            operation_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('project.register','project.deactivate','secretary.ensure','first_mate.ensure','workstream.create','workstream.complete','workstream.retire','workstream.cleanup','access.grant','access.revoke','deployment.apply','runtime.release.build','runtime.release.activate')),
            project_id TEXT REFERENCES projects(project_id),
            workstream_id TEXT REFERENCES workstreams(workstream_id),
            idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key) BETWEEN 1 AND 256),
            request_json TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('planned','applying','succeeded','failed','needs_attention','cancelled')),
            step TEXT NOT NULL,
            result_json TEXT,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute("INSERT INTO operations_epoch13 SELECT operation_id,kind,project_id,workstream_id,idempotency_key,request_json,request_sha256,state,step,result_json,error_code,error_message,created_at,updated_at FROM operations")
    connection.execute("DROP TABLE operations")
    connection.execute("ALTER TABLE operations_epoch13 RENAME TO operations")


def _add_epoch_fourteen_schema(connection: sqlite3.Connection) -> None:
    project_columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
    additions = (
        ("coordination_mode", "TEXT NOT NULL DEFAULT 'direct' CHECK(coordination_mode IN ('fleet','project','direct'))"),
        ("worker_creation_policy", "TEXT NOT NULL DEFAULT 'review' CHECK(worker_creation_policy IN ('review','bounded_auto'))"),
        ("worker_creation_policy_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("merge_policy", "TEXT NOT NULL DEFAULT 'review' CHECK(merge_policy IN ('review','checked_auto'))"),
        ("merge_policy_json", "TEXT NOT NULL DEFAULT '{}'"),
    )
    for name, definition in additions:
        if name not in project_columns:
            connection.execute(f"ALTER TABLE projects ADD COLUMN {name} {definition}")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS project_workspaces (
            project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
            workspace_adapter_id TEXT NOT NULL CHECK(length(workspace_adapter_id) BETWEEN 1 AND 64),
            workspace_session_name TEXT NOT NULL CHECK(length(workspace_session_name) BETWEEN 1 AND 128),
            workspace_id TEXT NOT NULL CHECK(length(workspace_id) BETWEEN 1 AND 256),
            repository_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        UPDATE projects
        SET coordination_mode='project'
        WHERE coordination_mode='direct'
          AND EXISTS (
              SELECT 1 FROM workstreams w
              WHERE w.project_id=projects.project_id
                AND w.kind='secretary'
                AND w.desired_state <> 'retired'
          )
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO project_workspaces(project_id,workspace_adapter_id,workspace_session_name,workspace_id,repository_path,created_at,updated_at)
        SELECT p.project_id,r.workspace_adapter_id,r.workspace_session_name,r.workspace_id,p.repository_path,p.created_at,p.updated_at
        FROM projects p
        JOIN workstreams w ON w.project_id=p.project_id AND w.kind='secretary' AND w.desired_state <> 'retired'
        JOIN runtime_bindings r ON r.workstream_id=w.workstream_id
        WHERE r.workspace_id IS NOT NULL
        """
    )


def _rebuild_epoch_ten_operations(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE operations_epoch10 (
            operation_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('project.register','project.deactivate','secretary.ensure','first_mate.ensure','workstream.create','workstream.complete','workstream.retire','workstream.cleanup')),
            project_id TEXT REFERENCES projects(project_id),
            workstream_id TEXT REFERENCES workstreams(workstream_id),
            idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key) BETWEEN 1 AND 256),
            request_json TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('planned','applying','succeeded','failed','needs_attention','cancelled')),
            step TEXT NOT NULL,
            result_json TEXT,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute("INSERT INTO operations_epoch10 SELECT operation_id,kind,project_id,workstream_id,idempotency_key,request_json,request_sha256,state,step,result_json,error_code,error_message,created_at,updated_at FROM operations")
    connection.execute("DROP TABLE operations")
    connection.execute("ALTER TABLE operations_epoch10 RENAME TO operations")


def migrate_schema(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT schema_name,schema_version,schema_sha256,migration_name FROM control_meta WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise sqlite3.DatabaseError("control metadata is missing")
    actual = tuple(row)
    expected = (SCHEMA_NAME, SCHEMA_VERSION, schema_digest(), MIGRATION_NAME)
    if actual == expected:
        return False
    epoch_thirteen_identities = {
        (PREVIOUS_SCHEMA_NAME, PREVIOUS_SCHEMA_VERSION, PREVIOUS_SCHEMA_DIGEST, PREVIOUS_MIGRATION_NAME),
    }
    epoch_twelve_identities = {
        (PREVIOUS_EPOCH_TWELVE_SCHEMA_NAME, PREVIOUS_EPOCH_TWELVE_SCHEMA_VERSION, PREVIOUS_EPOCH_TWELVE_SCHEMA_DIGEST, PREVIOUS_EPOCH_TWELVE_MIGRATION_NAME),
    }
    epoch_eleven_identities = {
        (PREVIOUS_EPOCH_ELEVEN_SCHEMA_NAME, PREVIOUS_EPOCH_ELEVEN_SCHEMA_VERSION, PREVIOUS_EPOCH_ELEVEN_SCHEMA_DIGEST, PREVIOUS_EPOCH_ELEVEN_MIGRATION_NAME),
        (PREVIOUS_EPOCH_ELEVEN_SCHEMA_NAME, PREVIOUS_EPOCH_ELEVEN_SCHEMA_VERSION, DEPLOYED_EPOCH_ELEVEN_SCHEMA_DIGEST, PREVIOUS_EPOCH_ELEVEN_MIGRATION_NAME),
    }
    epoch_ten_identities = {
        (SCHEMA_NAME, PREVIOUS_EPOCH_TEN_SCHEMA_VERSION, PREVIOUS_EPOCH_TEN_SCHEMA_DIGEST, PREVIOUS_EPOCH_TEN_MIGRATION_NAME),
        (SCHEMA_NAME, PREVIOUS_EPOCH_TEN_SCHEMA_VERSION, DEPLOYED_EPOCH_TEN_SCHEMA_DIGEST, PREVIOUS_EPOCH_TEN_MIGRATION_NAME),
    }
    epoch_nine_identities = {
        (PREVIOUS_EPOCH_NINE_SCHEMA_NAME, PREVIOUS_EPOCH_NINE_SCHEMA_VERSION, PREVIOUS_EPOCH_NINE_SCHEMA_DIGEST, PREVIOUS_EPOCH_NINE_MIGRATION_NAME),
        (PREVIOUS_EPOCH_NINE_SCHEMA_NAME, PREVIOUS_EPOCH_NINE_SCHEMA_VERSION, DEPLOYED_EPOCH_NINE_SCHEMA_DIGEST, PREVIOUS_EPOCH_NINE_MIGRATION_NAME),
    }
    epoch_eight_identities = {
        (PREVIOUS_EPOCH_EIGHT_SCHEMA_NAME, PREVIOUS_EPOCH_EIGHT_SCHEMA_VERSION, PREVIOUS_EPOCH_EIGHT_SCHEMA_DIGEST, PREVIOUS_EPOCH_EIGHT_MIGRATION_NAME),
        (PREVIOUS_EPOCH_EIGHT_SCHEMA_NAME, PREVIOUS_EPOCH_EIGHT_SCHEMA_VERSION, DEPLOYED_EPOCH_EIGHT_SCHEMA_DIGEST, PREVIOUS_EPOCH_EIGHT_MIGRATION_NAME),
    }
    epoch_seven = (SCHEMA_NAME, EPOCH_SEVEN_SCHEMA_VERSION, EPOCH_SEVEN_SCHEMA_DIGEST, EPOCH_SEVEN_MIGRATION_NAME)
    epoch_six = (SCHEMA_NAME, EPOCH_SIX_SCHEMA_VERSION, EPOCH_SIX_SCHEMA_DIGEST, EPOCH_SIX_MIGRATION_NAME)
    if actual not in epoch_thirteen_identities | epoch_twelve_identities | epoch_eleven_identities | epoch_ten_identities | epoch_nine_identities | epoch_eight_identities | {epoch_six, epoch_seven}:
        raise sqlite3.DatabaseError("unsupported Pisec schema migration")
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if actual not in epoch_twelve_identities | epoch_thirteen_identities:
            if actual == epoch_six and "data_dirs" not in {row[1] for row in connection.execute("PRAGMA table_info(projects)")}:
                connection.execute("ALTER TABLE projects ADD COLUMN data_dirs TEXT")
            if actual in {epoch_six, epoch_seven} | epoch_eight_identities:
                _rebuild_epoch_eight_tables(connection)
            project_columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
            if "active" not in project_columns:
                connection.execute("ALTER TABLE projects ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            if "deactivated_at" not in project_columns:
                connection.execute("ALTER TABLE projects ADD COLUMN deactivated_at TEXT")
            decision_columns = {row[1] for row in connection.execute("PRAGMA table_info(decisions)")}
            if "resolved_at" not in decision_columns:
                connection.execute("ALTER TABLE decisions ADD COLUMN resolved_at TEXT")
            if "updated_at" not in decision_columns:
                connection.execute("ALTER TABLE decisions ADD COLUMN updated_at TEXT")
            _rebuild_epoch_ten_operations(connection)
            _add_epoch_ten_schema(connection)
            old_issue_table = False
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='secretary_issue_reports'").fetchone() is not None:
                connection.execute("ALTER TABLE secretary_issue_reports RENAME TO secretary_issue_reports_epoch11")
                connection.execute("DROP TRIGGER IF EXISTS issues_secretary_report_insert")
                connection.execute("DROP TRIGGER IF EXISTS issues_secretary_report_update")
                old_issue_table = True
            _rebuild_epoch_twelve_operations(connection)
            _add_epoch_twelve_schema(connection)
            if old_issue_table:
                rows = connection.execute("SELECT * FROM secretary_issue_reports_epoch11").fetchall()
                for old in rows:
                    report = {
                        "category": old["category"],
                        "severity": old["severity"],
                        "summary": old["summary"],
                        "details": old["details"],
                        "requestedAction": old["requested_action"],
                        "evidence": json.loads(old["evidence_json"]),
                    }
                    report_sha = hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                    connection.execute(
                        "INSERT INTO issues(issue_id,project_id,reporter_workstream_id,reporter_kind,category,severity,summary,details,requested_action,evidence_json,idempotency_key,report_sha256,state,disposition,resolution,created_at,updated_at,acknowledged_at,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (old["issue_id"], old["project_id"], old["secretary_workstream_id"], "secretary", old["category"], old["severity"], old["summary"], old["details"], old["requested_action"], old["evidence_json"], old["idempotency_key"], report_sha, "resolved" if old["state"] == "resolved" else old["state"], "fixed" if old["state"] == "resolved" else None, old["resolution"], old["created_at"], old["updated_at"], old["acknowledged_at"], old["resolved_at"]),
                    )
                connection.execute("DROP TABLE secretary_issue_reports_epoch11")
            now = utc_now()
            connection.execute("INSERT OR IGNORE INTO issue_inbox(workstream_id,generation,notified_generation,updated_at) SELECT workstream_id,0,0,? FROM workstreams WHERE kind IN ('secretary','first_mate') AND desired_state <> 'retired'", (now,))
        if actual not in epoch_thirteen_identities:
            _rebuild_epoch_thirteen_operations(connection)
            _add_epoch_thirteen_schema(connection)
        _add_epoch_fourteen_schema(connection)
        connection.execute(
            "UPDATE control_meta SET schema_version=?,schema_sha256=?,migration_name=? WHERE singleton=1",
            (SCHEMA_VERSION, schema_digest(), MIGRATION_NAME),
        )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.DatabaseError("foreign key check failed after Pisec schema migration")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
    return True
