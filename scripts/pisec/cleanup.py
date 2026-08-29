"""Explicit, fail-closed cleanup for retired worker workstreams."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
from typing import Any, Mapping

from .adapters import HarnessAdapter, WorkspaceAdapter
from .events import append_event_in_transaction
from .models import ConflictError, NeedsAttentionError, NotFoundError, canonical_json, json_digest, new_id, utc_now
from .projects import get_project, resolve_project
from .workstreams import inspect_workstream
from .worker_repo import validate_worker_repository
from .control_plane import control_plane_mutation


def _safe_owned_tree(path: Path) -> None:
    if path.is_symlink():
        raise NeedsAttentionError("cleanup path is a symlink")
    if not path.exists():
        return
    info = path.lstat()
    if not path.is_dir() or info.st_uid != os.geteuid():
        raise NeedsAttentionError("cleanup path is not an owner-controlled directory")
    device = info.st_dev
    for directory, names, files in os.walk(path, topdown=True, followlinks=False):
        for name in [*names, *files]:
            child = Path(directory) / name
            child_info = child.lstat()
            if child_info.st_uid != os.geteuid():
                raise NeedsAttentionError("cleanup tree contains an unsafe entry")
            if stat.S_ISLNK(child_info.st_mode):
                continue
            if child_info.st_dev != device:
                raise NeedsAttentionError("cleanup tree crosses a filesystem boundary")



def _validate_retained_session_root(binding: Mapping[str, Any], harness: HarnessAdapter) -> Path | None:
    harness.validate_native_session(binding, binding.get("native_session_kind"), binding.get("native_session_value"))
    if harness.manifest.adapter_id != "omp":
        return None
    harness_home = Path(str(binding["harness_home"])).absolute()
    session_root = harness_home / "sessions"
    for path, expected_mode in ((harness_home, 0o700), (session_root, 0o700)):
        try:
            info = path.lstat()
        except OSError as error:
            raise NeedsAttentionError("retained OMP session root is unavailable") from error
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != expected_mode:
            raise NeedsAttentionError("retained OMP session root is unsafe")
    for directory, names, files in os.walk(session_root, topdown=True, followlinks=False):
        for name in [*names, *files]:
            child = Path(directory) / name
            info = child.lstat()
            if info.st_uid != os.geteuid() or stat.S_ISLNK(info.st_mode) or info.st_mode & 0o022:
                raise NeedsAttentionError("retained OMP session tree is unsafe")
    return session_root

@control_plane_mutation
def cleanup_workstream(store: Any, payload: Mapping[str, Any], workspace: WorkspaceAdapter, harness: HarnessAdapter) -> dict[str, Any]:
    required = {"workstreamId", "confirm"}
    optional = {"project", "forceDirty"}
    if set(payload) < required or not set(payload) <= required | optional:
        raise ConflictError("cleanup requires the exact workstream id in --confirm")
    workstream_id = str(payload["workstreamId"])
    if payload.get("project") is None:
        row = store.conn.execute("SELECT project_id FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()
        if row is None:
            raise NotFoundError("workstream was not found")
        project = get_project(store, row["project_id"])
    else:
        project = resolve_project(store, str(payload["project"]))
    inspected = inspect_workstream(store, project["project_id"], workstream_id)
    row = inspected["workstream"]
    if row["kind"] != "worker" or row["desired_state"] != "retired":
        raise ConflictError("only retired worker workstreams can be cleaned")
    binding = inspected["binding"]
    if binding is not None and (
        binding["workspace_adapter_id"] != workspace.manifest.adapter_id
        or binding["harness_id"] != harness.manifest.adapter_id
    ):
        raise NeedsAttentionError("configured adapter does not match the durable binding")
    if binding is not None and binding["observed_state"] in {"starting", "working", "blocked"}:
        raise ConflictError("workstream runtime is still active")
    if not isinstance(payload.get("forceDirty", False), bool):
        raise ConflictError("cleanup force-dirty flag must be boolean")
    if str(payload["confirm"]) != workstream_id:
        raise ConflictError("cleanup confirmation does not match the workstream id")
    request = {"project": project["project_id"], "workstreamId": workstream_id, "confirm": workstream_id, "forceDirty": bool(payload.get("forceDirty", False))}
    idempotency_key = f"cleanup:{workstream_id}"
    existing = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    request_sha = json_digest(request)
    if existing is not None:
        if existing["request_sha256"] != request_sha:
            raise ConflictError("cleanup idempotency key is bound to another request")
        if existing["state"] == "succeeded":
            return {"operation": dict(existing), "workstream": dict(store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()), "reused": True}
        if existing["state"] == "needs_attention":
            with store.transaction():
                store.conn.execute(
                    "UPDATE operations SET state='applying',step='planned',error_code=NULL,error_message=NULL,updated_at=? WHERE operation_id=? AND state='needs_attention'",
                    (utc_now(), existing["operation_id"]),
                )
        operation_id = existing["operation_id"]
    else:
        operation_id = new_id("op")
        now = utc_now()
        with store.transaction():
            store.conn.execute(
                "INSERT INTO operations(operation_id,kind,project_id,workstream_id,idempotency_key,request_json,request_sha256,state,step,created_at,updated_at) VALUES(?,'workstream.cleanup',?,?,?,?,?,'applying','planned',?,?)",
                (operation_id, project["project_id"], workstream_id, idempotency_key, canonical_json(request), request_sha, now, now),
            )

    worktree = Path(row["worktree_path"]).absolute()
    try:
        if worktree.is_symlink():
            raise NeedsAttentionError("managed worktree is a symlink")
        if worktree.exists() and worktree.resolve() != worktree.absolute():
            raise NeedsAttentionError("managed worktree path is not canonical")
        if worktree.exists() and worktree.name != workstream_id:
            raise NeedsAttentionError("managed worktree basename does not match workstream")
        if binding is not None and binding["workspace_view_id"]:
            workspace.close_tab(binding["workspace_view_id"])
        receipt = store.conn.execute("SELECT * FROM merge_receipts WHERE workstream_id=? ORDER BY created_at DESC LIMIT 1", (workstream_id,)).fetchone()
        if receipt is None:
            raise ConflictError("unintegrated worker repository must be retained")
        if worktree.exists():
            final_oid = validate_worker_repository(
                worktree,
                branch_name=str(row["branch_name"]),
                base_oid=str(row["base_commit_oid"]),
                target_branch=str(row["target_ref"]).removeprefix("refs/heads/"),
                history_base_oid=str(receipt["previous_target_oid"]),
                review_base_oid=str(receipt["previous_target_oid"]),
            )
            if final_oid != str(receipt["source_commit_oid"]):
                raise ConflictError("worker repository does not match the final integration receipt")
            if payload.get("forceDirty", False):
                raise ConflictError("force-dirty cleanup is not permitted for an independent worker repository")
            _safe_owned_tree(worktree)
        retained_root: Path | None = None
        if binding is not None:
            retained_root = _validate_retained_session_root(binding, harness)
            if retained_root is not None:
                existing_root = store.conn.execute("SELECT * FROM retained_session_roots WHERE workstream_id=?", (workstream_id,)).fetchone()
                if existing_root is not None and (
                    existing_root["harness_id"] != binding["harness_id"]
                    or existing_root["harness_home"] != binding["harness_home"]
                    or existing_root["native_session_kind"] != binding["native_session_kind"]
                    or existing_root["native_session_value"] != binding["native_session_value"]
                ):
                    raise NeedsAttentionError("retained session root identity drifted")
                if existing_root is None:
                    with store.transaction():
                        store.conn.execute(
                            "INSERT INTO retained_session_roots(workstream_id,harness_id,harness_home,native_session_kind,native_session_value,retained_at) VALUES(?,?,?,?,?,?)",
                            (workstream_id, binding["harness_id"], binding["harness_home"], binding["native_session_kind"], binding["native_session_value"], utc_now()),
                        )
                        store.conn.execute("UPDATE operations SET step='retention_recorded',updated_at=? WHERE operation_id=?", (utc_now(), operation_id))
        if binding is not None:
            harness.cleanup_binding(binding)
        if worktree.exists():
            shutil.rmtree(worktree)
        now = utc_now()
        with store.transaction():
            store.conn.execute("DELETE FROM runtime_bindings WHERE workstream_id=?", (workstream_id,))
            result = {"workstreamId": workstream_id, "branchRetained": True, "retainedSessionRoot": None if retained_root is None else str(retained_root)}
            store.conn.execute("UPDATE operations SET state='succeeded',step='committed',result_json=?,error_code=NULL,error_message=NULL,updated_at=? WHERE operation_id=?", (canonical_json(result), now, operation_id))
            append_event_in_transaction(store.conn, kind="workstream.cleaned", project_id=project["project_id"], workstream_id=workstream_id, operation_id=operation_id, payload=result)
        return {"operation": dict(store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()), "workstream": dict(store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()), "reused": False}
    except ConflictError:
        raise
    except Exception as error:
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='needs_attention',error_code='cleanup_failed',error_message=?,updated_at=? WHERE operation_id=?", (str(error)[:512], utc_now(), operation_id))
            store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?", (str(error)[:512], utc_now(), workstream_id))
        raise NeedsAttentionError("workstream cleanup requires attention") from error
