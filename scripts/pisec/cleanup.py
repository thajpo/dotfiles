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
from .projects import _git, get_project, resolve_project
from .workstreams import inspect_workstream


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
            if child_info.st_uid != os.geteuid() or stat.S_ISLNK(child_info.st_mode):
                raise NeedsAttentionError("cleanup tree contains an unsafe entry")
            if child_info.st_dev != device:
                raise NeedsAttentionError("cleanup tree crosses a filesystem boundary")


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
            raise NeedsAttentionError(existing["error_message"] or "cleanup requires attention")
        operation_id = existing["operation_id"]
    else:
        operation_id = new_id("op")
        now = utc_now()
        with store.transaction():
            store.conn.execute(
                "INSERT INTO operations(operation_id,kind,project_id,workstream_id,idempotency_key,request_json,request_sha256,state,step,created_at,updated_at) VALUES(?,'workstream.cleanup',?,?,?,?,?,'applying','planned',?,?)",
                (operation_id, project["project_id"], workstream_id, idempotency_key, canonical_json(request), request_sha, now, now),
            )

    worktree = Path(row["worktree_path"])
    try:
        if worktree.is_symlink():
            raise NeedsAttentionError("managed worktree is a symlink")
        if worktree.exists() and worktree.resolve() != worktree.absolute():
            raise NeedsAttentionError("managed worktree path is not canonical")
        if worktree.exists() and worktree.name != workstream_id:
            raise NeedsAttentionError("managed worktree basename does not match workstream")
        if binding is not None and binding["workspace_id"]:
            workspace.close_workspace(binding["workspace_id"])
        branch_before = _git(Path(project["repository_path"]), "for-each-ref", "--format=%(refname:short)", f"refs/heads/{row['branch_name']}")
        if branch_before.strip() != row["branch_name"]:
            raise NeedsAttentionError("retired workstream branch is missing before cleanup")
        if worktree.exists():
            status = _git(worktree, "status", "--porcelain", "--untracked-files=all")
            if status and not bool(payload.get("forceDirty", False)):
                raise ConflictError("managed worktree is dirty; clean it before cleanup or pass --force-dirty")
            listed = _git(Path(project["repository_path"]), "worktree", "list", "--porcelain")
            if str(worktree) not in listed:
                raise NeedsAttentionError("approved worktree is not present in Git worktree inventory")
            _git(Path(project["repository_path"]), "worktree", "remove", "--force", str(worktree))
        branch_after = _git(Path(project["repository_path"]), "for-each-ref", "--format=%(refname:short)", f"refs/heads/{row['branch_name']}")
        if branch_after.strip() != row["branch_name"]:
            raise NeedsAttentionError("cleanup unexpectedly removed the retained branch")
        if binding is not None:
            harness.cleanup_binding(binding)
        now = utc_now()
        with store.transaction():
            store.conn.execute("UPDATE runtime_bindings SET observed_state='stopped',workspace_id=NULL,workspace_view_id=NULL,workspace_surface_id=NULL,updated_at=? WHERE workstream_id=?", (now, workstream_id))
            store.conn.execute("UPDATE operations SET state='succeeded',step='committed',result_json=?,error_code=NULL,error_message=NULL,updated_at=? WHERE operation_id=?", (canonical_json({"workstreamId": workstream_id, "branchRetained": True}), now, operation_id))
            append_event_in_transaction(store.conn, kind="workstream.cleaned", project_id=project["project_id"], workstream_id=workstream_id, operation_id=operation_id, payload={"workstreamId": workstream_id, "branchRetained": True})
        return {"operation": dict(store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()), "workstream": dict(store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()), "reused": False}
    except ConflictError:
        raise
    except Exception as error:
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='needs_attention',error_code='cleanup_failed',error_message=?,updated_at=? WHERE operation_id=?", (str(error)[:512], utc_now(), operation_id))
            store.conn.execute("UPDATE workstreams SET provisioning_state='needs_attention',attention_reason=?,updated_at=? WHERE workstream_id=?", (str(error)[:512], utc_now(), workstream_id))
        raise NeedsAttentionError("workstream cleanup requires attention") from error
