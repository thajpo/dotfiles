"""Role-derived run preparation and lifecycle for fresh Pi conversations."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
from typing import Any, Mapping

from .events import append_event_in_transaction
from .installed_builds import InstalledBuildError, verify_registered_build
from .models import canonical_json, json_digest, new_id, utc_now, validate_child_source, validate_id
from .operations import update_operation_in_transaction
from .process_adapter import process_start_identity
from .role_profiles import role_profile, validate_role_assignment
from .run_manifest import build_manifest, read_manifest, write_manifest
from .writer_lock import WriterLock, WriterLockError


class LaunchError(RuntimeError):
    pass


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass
class PreparedRun:
    run: dict[str, Any]
    manifest: dict[str, Any]
    manifest_path: Path
    environment: dict[str, str]
    writer_lock: WriterLock | None = None

    def close(self) -> None:
        if self.writer_lock is not None:
            self.writer_lock.close()
            self.writer_lock = None

    def __enter__(self) -> "PreparedRun":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _prepared(store: Any, run_id: str, lock: WriterLock | None = None) -> PreparedRun:
    run = dict(store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())
    path = Path(run["manifest_path"])
    manifest = dict(read_manifest(path).manifest)
    environment = {
        "PI_RUNTIME_MANIFEST": str(path), "PI_SYSTEM_RUN_ID": run_id,
        "PI_SYSTEM_PROJECT_ID": run["project_id"], "PI_SYSTEM_CONVERSATION_ID": run["conversation_id"],
        **({"PI_SYSTEM_WORKING_COPY_ID": run["working_copy_id"]} if run["working_copy_id"] else {}),
        **({"PI_SYSTEM_WRITER_GENERATION": str(run["writer_epoch"])} if run["writer_epoch"] is not None else {}),
    }
    return PreparedRun(run, manifest, path, environment, lock)


def prepare_run(
    store: Any,
    *,
    conversation_id: str,
    build_id: str,
    host_process: Mapping[str, Any],
    tool_runtime: Mapping[str, Any] | None = None,
    parent_run_id: str | None = None,
    owner_pid: int | None = None,
    idempotency_key: str | None = None,
    run_id: str | None = None,
    child_source: Mapping[str, Any] | None = None,
) -> PreparedRun:
    validate_id(conversation_id, prefix="conv")
    if parent_run_id is not None:
        validate_id(parent_run_id, prefix="run")
    conversation = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=? AND desired_state='active'", (conversation_id,)).fetchone()
    if conversation is None:
        raise LaunchError("active conversation was not found")
    project = store.conn.execute("SELECT * FROM projects WHERE project_id=? AND desired_state='active'", (conversation["project_id"],)).fetchone()
    if project is None:
        raise LaunchError("conversation project is not active")
    profile = role_profile(conversation["role"])
    if conversation["authority_profile"] != profile.authority_profile:
        raise LaunchError("stored conversation role profile is invalid")
    if conversation["role"] == "secretary":
        working = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary' AND desired_state='present'", (project["project_id"],)).fetchone()
    else:
        working = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=? AND desired_state='present'", (conversation["working_copy_id"],)).fetchone()
    try:
        validate_role_assignment(profile, None if conversation["role"] == "secretary" else working)
    except ValueError as error:
        raise LaunchError(str(error)) from error
    if working is None or working["project_id"] != project["project_id"]:
        raise LaunchError("controller-derived run scope is unavailable")
    try:
        build = verify_registered_build(store, build_id)
    except InstalledBuildError as error:
        raise LaunchError(str(error)) from error
    if profile.authority_profile == "host-read-only" and tool_runtime is not None:
        raise LaunchError("host-read-only roles cannot select a tool runtime")
    if profile.authority_profile == "writer-container" and tool_runtime is None:
        raise LaunchError("writer conversations require one complete toolRuntime")
    host_value = dict(host_process)
    tool_value = dict(tool_runtime) if tool_runtime is not None else None
    if run_id is not None:
        validate_id(run_id, prefix="run")
    child_source_value = validate_child_source(child_source) if child_source is not None else None
    if (parent_run_id is None) != (child_source_value is None):
        raise LaunchError("child runs require both parent identity and immutable child source")
    if child_source_value is not None and profile.authority_profile != "host-read-only":
        raise LaunchError("mutable child attempts require a separately created writer workstream")
    request = {"conversationId": conversation_id, "buildId": build_id, "hostProcess": host_value, "toolRuntime": tool_value, "parentRunId": parent_run_id, "childSource": child_source_value}
    key = idempotency_key or "run.prepare:" + new_id("op")
    existing = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (key,)).fetchone()
    if existing is not None:
        if existing["kind"] != "run.prepare" or existing["request_digest"] != json_digest(request):
            raise LaunchError("run preparation idempotency key is bound to another request")
        existing_run = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (existing["resource_id"],)).fetchone()
        if existing_run is not None and existing_run["manifest_path"]:
            replay_lock = None
            if existing_run["authority"] == "writer-container":
                replay_lock = WriterLock.acquire(store.state_root, existing_run["working_copy_id"], int(existing_run["writer_epoch"]))
                current = store.conn.execute("SELECT active_writer_run_id,writer_epoch FROM working_copies WHERE working_copy_id=?", (existing_run["working_copy_id"],)).fetchone()
                if current is None or current["active_writer_run_id"] != existing_run["run_id"] or int(current["writer_epoch"]) != int(existing_run["writer_epoch"]) or existing_run["observed_state"] not in {"preparing", "ready", "running", "stopping", "needs_attention"}:
                    replay_lock.close()
                    raise LaunchError("idempotent writer replay is not the current held writer")
            return _prepared(store, existing["resource_id"], replay_lock)
        run_id = existing["resource_id"]
        operation_id = existing["operation_id"]
    else:
        run_id = run_id or new_id("run")
        operation = store.create_operation(idempotency_key=key, kind="run.prepare", resource_type="run", resource_id=run_id, actor_type="controller", request=request)
        operation_id = operation.operation_id
    assert run_id is not None
    writer_epoch = None
    lock: WriterLock | None = None
    now = utc_now()
    pid = owner_pid or os.getpid()
    channel_binding_hash = "sha256:" + hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    runtime_spec_hash = str(tool_value["specHash"]) if profile.authority_profile == "writer-container" else _digest({"hostProcess": host_value, "toolRuntime": tool_value, "buildId": build["build_id"], "role": conversation["role"], "scopeWorkingCopyId": working["working_copy_id"]})
    if profile.authority_profile == "writer-container":
        writer_epoch = int(working["writer_epoch"]) + 1
        labels = tool_value.get("labels") if tool_value is not None else None
        if not isinstance(labels, Mapping) or labels.get("pi.control.run-id") != run_id or labels.get("pi.control.writer-epoch") != str(writer_epoch):
            raise LaunchError("tool runtime run identity or writer epoch is stale")
        try:
            lock = WriterLock.acquire(store.state_root, working["working_copy_id"], writer_epoch)
        except BaseException as error:
            try:
                store.fail_operation(operation_id, code="WRITER_LOCK_BUSY", detail="working copy already has a lifecycle owner", step="writer-lock-rejected")
            except Exception:
                pass
            if isinstance(error, WriterLockError):
                raise LaunchError(str(error)) from error
            raise
    try:
        with store.transaction():
            if profile.authority_profile == "writer-container":
                current = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (working["working_copy_id"],)).fetchone()
                if current is None or current["active_writer_run_id"] is not None or int(current["writer_epoch"]) + 1 != writer_epoch or int(current["resource_version"]) != int(working["resource_version"]):
                    raise LaunchError("working-copy writer state changed before acquisition")
                expected_working_version = int(current["resource_version"]) + 1
            else:
                expected_working_version = int(working["resource_version"])
            store.conn.execute(
                "INSERT INTO runs(run_id,operation_id,conversation_id,project_id,working_copy_id,parent_run_id,child_source_json,authority,desired_state,observed_state,expected_working_copy_version,expected_head_oid,expected_tree_oid,dirty_fingerprint,writer_epoch,runtime_spec_hash,build_id,owner_pid,owner_start_identity,child_pid,child_start_identity,channel_binding_hash,manifest_path,host_process_observation_json,container_id,container_observation_json,resource_version,created_at,started_at,ended_at,updated_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, operation_id, conversation_id, project["project_id"], working["working_copy_id"], parent_run_id, canonical_json(child_source_value) if child_source_value is not None else None, profile.authority_profile, "running", "preparing", expected_working_version, working["expected_head_oid"], working["expected_tree_oid"], None, writer_epoch, runtime_spec_hash, build["build_id"], pid, process_start_identity(pid), None, None, channel_binding_hash, None, None, None, None, 1, now, None, None, now, None, None),
            )
            if profile.authority_profile == "writer-container":
                cursor = store.conn.execute("UPDATE working_copies SET writer_epoch=?,active_writer_run_id=?,updated_at=?,resource_version=resource_version+1 WHERE working_copy_id=? AND resource_version=? AND writer_epoch=? AND active_writer_run_id IS NULL", (writer_epoch, run_id, now, working["working_copy_id"], int(working["resource_version"]), writer_epoch - 1))
                if cursor.rowcount != 1:
                    raise LaunchError("working-copy writer compare-and-swap failed")
            update_operation_in_transaction(store.conn, operation_id, state="applying", step="run-recorded")
    except Exception as error:
        if lock is not None:
            lock.close()
            lock = None
        try:
            store.fail_operation(operation_id, code="CP_CONSTRAINT", detail="run preparation violated a database invariant", step="run-rejected")
        except Exception:
            pass
        raise LaunchError("run preparation violated a database invariant") from error
    try:
        manifest = build_manifest(store, run_id, host_process=host_value, tool_runtime=tool_value)
        path = Path(store.state_root) / "runs" / run_id / "manifest.json"
        write_manifest(path, manifest)
        with store.transaction():
            store.conn.execute("UPDATE runs SET manifest_path=?,observed_state='ready',updated_at=? WHERE run_id=?", (str(path), utc_now(), run_id))
            update_operation_in_transaction(store.conn, operation_id, state="succeeded", step="manifest-written", result={"runId": run_id, "manifestDigest": manifest["manifestDigest"]})
            append_event_in_transaction(store.conn, event_kind="run.prepared", resource_type="run", resource_id=run_id, resource_version=1, operation_id=operation_id, payload={"projectId": project["project_id"], "conversationId": conversation_id, "workingCopyId": working["working_copy_id"], "authorityProfile": profile.authority_profile, "manifestDigest": manifest["manifestDigest"]})
        return _prepared(store, run_id, lock)
    except BaseException:
        with store.transaction():
            store.conn.execute("UPDATE runs SET desired_state='stopped',observed_state='failed',error_code=COALESCE(error_code,'MANIFEST_PREPARE_FAILED'),ended_at=COALESCE(ended_at,?),updated_at=? WHERE run_id=?", (utc_now(), utc_now(), run_id))
            store.conn.execute("UPDATE working_copies SET active_writer_run_id=NULL,updated_at=?,resource_version=resource_version+1 WHERE working_copy_id=? AND active_writer_run_id=? AND NOT EXISTS (SELECT 1 FROM runs WHERE run_id=? AND container_id IS NOT NULL)", (utc_now(), working["working_copy_id"], run_id, run_id))
        if lock is not None:
            lock.close()
        raise


def attest_run(store: Any, *, run_id: str, manifest_digest: str, observed: Mapping[str, Any] | None = None) -> dict[str, Any]:
    validate_id(run_id, prefix="run")
    row = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None or not row["manifest_path"]:
        raise LaunchError("run manifest is unavailable")
    manifest = read_manifest(row["manifest_path"]).manifest
    if manifest["manifestDigest"] != manifest_digest:
        raise LaunchError("run manifest digest mismatch")
    observed = dict(observed or {})
    expected = {"runId": run_id, "projectId": manifest["project"]["projectId"], "workingCopyId": manifest["scope"]["workingCopyId"]}
    for key, value in expected.items():
        if key in observed and observed[key] != value:
            raise LaunchError(f"runtime attestation mismatch: {key}")
    with store.transaction():
        store.conn.execute("UPDATE runs SET observed_state='running',host_process_observation_json=COALESCE(?,host_process_observation_json),started_at=COALESCE(started_at,?),updated_at=? WHERE run_id=?", (canonical_json(observed) if observed else None, utc_now(), utc_now(), run_id))
    return {"runId": run_id, "manifestDigest": manifest_digest, "state": "running", "observed": observed}


def start_run(store: Any, *, run_id: str, command: list[str]) -> dict[str, Any]:
    """The generic run API cannot bypass the host-supervisor writer saga."""
    row = store.conn.execute("SELECT authority,manifest_path FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None or row["authority"] != "writer-container" or not row["manifest_path"]:
        raise LaunchError("only prepared writer-container runs can reach the tool plane")
    manifest = read_manifest(row["manifest_path"]).manifest
    if manifest["toolRuntime"] is None:
        raise LaunchError("writer run has no complete toolRuntime")
    raise LaunchError("writer tool execution is available only through the host supervisor and inherited broker channel")


def _writer_container_absent(row: Mapping[str, Any]) -> bool:
    if row["container_id"] is None:
        return True
    try:
        observation = json.loads(row["container_observation_json"] or "null")
    except json.JSONDecodeError:
        return False
    return isinstance(observation, dict) and observation.get("state") in {"absent", "cleanup-proved"} and bool((observation.get("cleanup") or {}).get("absent"))


def stop_run(store: Any, *, run_id: str, reason: str = "stopped", container_absent: bool = False) -> dict[str, Any]:
    validate_id(run_id, prefix="run")
    with store.transaction():
        row = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise LaunchError("run not found")
        now = utc_now()
        observed = row["observed_state"] if row["observed_state"] in {"failed", "needs_attention"} else ("failed" if reason.startswith("process-failed") else "stopped")
        code = row["error_code"] if observed in {"failed", "needs_attention"} else None
        detail = row["error_detail"] if observed in {"failed", "needs_attention"} else reason[:1024]
        store.conn.execute("UPDATE runs SET desired_state='stopped',observed_state=?,error_code=?,error_detail=?,ended_at=COALESCE(ended_at,?),updated_at=? WHERE run_id=?", (observed, code, detail, now, now, run_id))
        if row["working_copy_id"] is not None and row["authority"] == "writer-container":
            if not container_absent or not _writer_container_absent(row):
                raise LaunchError("writer run cannot release its claim before exact container absence is proved")
            store.conn.execute("UPDATE working_copies SET active_writer_run_id=NULL,updated_at=?,resource_version=resource_version+1 WHERE working_copy_id=? AND active_writer_run_id=?", (now, row["working_copy_id"], run_id))
        return dict(store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())


def fail_run(store: Any, *, run_id: str, code: str, detail: str, needs_attention: bool = False, release_writer: bool = False) -> dict[str, Any]:
    validate_id(run_id, prefix="run")
    now = utc_now()
    with store.transaction():
        row = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise LaunchError("run not found")
        observed = row["observed_state"] if row["observed_state"] in {"failed", "needs_attention"} else ("needs_attention" if needs_attention else "failed")
        store.conn.execute("UPDATE runs SET desired_state='stopped',observed_state=?,error_code=COALESCE(error_code,?),error_detail=COALESCE(error_detail,?),ended_at=COALESCE(ended_at,?),updated_at=? WHERE run_id=?", (observed, code[:128], detail[:1024], now, now, run_id))
        if release_writer and not needs_attention and row["authority"] == "writer-container" and not _writer_container_absent(row):
            raise LaunchError("failed writer cannot release its claim before exact container absence is proved")
        if release_writer and not needs_attention and row["authority"] == "writer-container":
            store.conn.execute("UPDATE working_copies SET active_writer_run_id=NULL,updated_at=?,resource_version=resource_version+1 WHERE working_copy_id=? AND active_writer_run_id=?", (now, row["working_copy_id"], run_id))
        return dict(store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())


__all__ = ["LaunchError", "PreparedRun", "attest_run", "fail_run", "prepare_run", "start_run", "stop_run"]
