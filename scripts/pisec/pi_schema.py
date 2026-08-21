"""Fresh Pisec core epoch-eight schema."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess

SCHEMA_NAME = "pisec-core"
SCHEMA_VERSION = 8
MIGRATION_NAME = "pisec-core-epoch-8"
PREVIOUS_SCHEMA_VERSION = 7
PREVIOUS_SCHEMA_NAME = "pisec-core-epoch-7"
PREVIOUS_MIGRATION_NAME = "pisec-core-epoch-7"
PREVIOUS_SCHEMA_DIGEST = "sha256:35e63da90e5a851e2f57d7cddf21db58ace28471aa5b3af5cb73363165729c95"
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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(secretary_workstream_id) REFERENCES workstreams(workstream_id)
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

CREATE TABLE operations (
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
);

CREATE TABLE authorizations (
    authorization_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
    scope_sha256 TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind = 'workstream.create'),
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
    epoch_seven = (SCHEMA_NAME, PREVIOUS_SCHEMA_VERSION, PREVIOUS_SCHEMA_DIGEST, PREVIOUS_MIGRATION_NAME)
    epoch_six = (SCHEMA_NAME, EPOCH_SIX_SCHEMA_VERSION, EPOCH_SIX_SCHEMA_DIGEST, EPOCH_SIX_MIGRATION_NAME)
    if actual not in {epoch_six, epoch_seven}:
        raise sqlite3.DatabaseError("unsupported Pisec schema migration")
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if actual == epoch_six:
            connection.execute("ALTER TABLE projects ADD COLUMN data_dirs TEXT")
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
