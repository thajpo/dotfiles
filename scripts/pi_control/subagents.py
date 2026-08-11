"""Controller-brokered read-only child assignments on immutable Git snapshots."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from typing import Any, Mapping

from .conversations import conversation_session_binding
from .events import append_event_in_transaction
from .models import bounded_text, canonical_json, json_digest, new_id, utc_now, validate_child_source, validate_id
from .operations import update_operation_in_transaction


class SubagentError(RuntimeError):
    pass


def _git_env() -> dict[str, str]:
    return {
        "PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat",
        "GIT_EDITOR": "true", "GIT_ASKPASS": "true", "TMPDIR": tempfile.gettempdir(),
    }


def _git(repository: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git", path=os.defpath)
    if git is None:
        raise SubagentError("Git is unavailable")
    result = subprocess.run(
        [git, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.sshCommand=", "-c", "credential.helper=", "-c", "protocol.allow=never", *args],
        cwd=str(repository), env=_git_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", timeout=120, check=False, shell=False,
    )
    if check and result.returncode != 0:
        raise SubagentError(result.stderr.strip()[:1024] or "child snapshot Git operation failed")
    return result


def _request_ids(store: Any, parent: Mapping[str, Any], semantic_role: str, task: str, idempotency_key: str) -> tuple[Any, dict[str, Any]]:
    existing = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    if existing is not None:
        request = json.loads(existing["request_json"])
        if existing["kind"] != "subagent.create" or request.get("parentRunId") != parent["run_id"] or request.get("semanticRole") != semantic_role or request.get("task") != task:
            raise SubagentError("subagent idempotency key is bound to another request")
        return existing, request
    request_id, snapshot_id, wc_id, conversation_id = new_id("child"), new_id("snap"), new_id("wc"), new_id("conv")
    snapshot_ref = f"refs/pi/snapshots/{snapshot_id}"
    snapshot_path = Path(store.state_root).absolute() / "child-snapshots" / request_id
    runtime_role = "reviewer" if semantic_role == "reviewer" else "investigator"
    request = {
        "childRequestId": request_id, "parentRunId": parent["run_id"], "projectId": parent["project_id"],
        "parentWorkingCopyId": parent["working_copy_id"], "semanticRole": semantic_role, "runtimeRole": runtime_role,
        "task": task, "snapshotId": snapshot_id, "snapshotRef": snapshot_ref, "snapshotPath": str(snapshot_path),
        "childWorkingCopyId": wc_id, "childConversationId": conversation_id,
    }
    operation = store.create_operation(idempotency_key=idempotency_key, kind="subagent.create", resource_type="child-request", resource_id=request_id, actor_type="controller", actor_id=parent["run_id"], request=request)
    return store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone(), request


def create_child_assignment(store: Any, *, parent_run_id: str, semantic_role: str, task: str, idempotency_key: str) -> dict[str, Any]:
    validate_id(parent_run_id, prefix="run")
    if semantic_role not in {"investigator", "reviewer", "scout"}:
        raise SubagentError("subagent role must be investigator, reviewer, or scout")
    task = bounded_text(task, name="subagent task", limit=4096)
    parent = store.conn.execute("SELECT * FROM runs WHERE run_id=? AND desired_state='running' AND observed_state='running'", (parent_run_id,)).fetchone()
    if parent is None or parent["working_copy_id"] is None:
        raise SubagentError("subagent parent run is not active or scoped")
    conversation = store.conn.execute("SELECT role FROM conversations WHERE conversation_id=?", (parent["conversation_id"],)).fetchone()
    if conversation is None or conversation["role"] not in {"secretary", "personal", "workstream", "integration"}:
        raise SubagentError("authenticated parent role cannot create children")
    source = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=? AND project_id=? AND desired_state='present'", (parent["working_copy_id"], parent["project_id"])).fetchone()
    project = store.conn.execute("SELECT * FROM projects WHERE project_id=? AND desired_state='active'", (parent["project_id"],)).fetchone()
    if source is None or project is None:
        raise SubagentError("parent source scope is unavailable")
    operation, request = _request_ids(store, parent, semantic_role, task, idempotency_key)
    existing = store.conn.execute("SELECT * FROM child_requests WHERE child_request_id=?", (request["childRequestId"],)).fetchone()
    if existing is not None:
        return dict(existing)
    repository = Path(source["path"]).resolve(strict=True)
    commit_oid = _git(repository, ["rev-parse", "--verify", "HEAD"]).stdout.strip()
    tree_oid = _git(repository, ["rev-parse", "--verify", "HEAD^{tree}"]).stdout.strip()
    if commit_oid != parent["expected_head_oid"] or tree_oid != parent["expected_tree_oid"]:
        raise SubagentError("parent working-copy revision changed before snapshot creation")
    ref = str(request["snapshotRef"])
    current_ref = _git(repository, ["show-ref", "--verify", "--hash", ref], check=False)
    if current_ref.returncode == 0:
        if current_ref.stdout.strip() != commit_oid:
            raise SubagentError("controller snapshot ref differs from durable intent")
    else:
        _git(repository, ["update-ref", ref, commit_oid, ""])
    snapshot_path = Path(str(request["snapshotPath"]))
    if not snapshot_path.exists() and not snapshot_path.is_symlink():
        snapshot_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        _git(repository, ["worktree", "add", "--detach", str(snapshot_path), commit_oid])
    if not snapshot_path.is_dir():
        raise SubagentError("child snapshot path is unavailable")
    observed_head = _git(snapshot_path, ["rev-parse", "--verify", "HEAD"]).stdout.strip()
    observed_tree = _git(snapshot_path, ["rev-parse", "--verify", "HEAD^{tree}"]).stdout.strip()
    branch = _git(snapshot_path, ["symbolic-ref", "--quiet", "HEAD"], check=False)
    status = _git(snapshot_path, ["status", "--porcelain=v2", "--untracked-files=all"]).stdout
    if observed_head != commit_oid or observed_tree != tree_oid or branch.returncode == 0 or status.strip():
        raise SubagentError("child snapshot is not clean, detached, and exact")
    marker = (snapshot_path / ".git").read_text(encoding="utf-8").strip()
    if not marker.startswith("gitdir: "):
        raise SubagentError("child snapshot has no file-form Git identity")
    git_dir = str(Path(marker.removeprefix("gitdir: ")).resolve(strict=True))
    child_source = validate_child_source({
        "snapshotId": request["snapshotId"], "snapshotRef": ref, "snapshotCommitOid": commit_oid,
        "snapshotTreeOid": tree_oid, "sourceHeadOid": parent["expected_head_oid"],
        "sourceTreeOid": parent["expected_tree_oid"], "authority": "read-only",
    })
    now = utc_now()
    session_id, session_file = conversation_session_binding(store, parent["project_id"], request["childConversationId"])
    with store.transaction():
        store.conn.execute(
            "INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,git_dir,branch_ref,expected_head_oid,expected_tree_oid,effective_mode,desired_state,observed_state,writer_epoch,active_writer_run_id,resource_version,controller_owned,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (request["childWorkingCopyId"], parent["project_id"], f"{semantic_role} snapshot", "review", "review", str(snapshot_path), git_dir, None, commit_oid, tree_oid, "read-only", "present", "ready", 0, None, 1, 1, now, now, now, None, None),
        )
        store.conn.execute(
            "INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,authority_profile,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (request["childConversationId"], parent["project_id"], request["childWorkingCopyId"], request["runtimeRole"], "host-read-only", f"{semantic_role}: {task[:96]}", session_id, session_file, "active", "ready", 1, now, now, now, None, None),
        )
        if request["runtimeRole"] == "investigator":
            store.conn.execute("INSERT INTO investigations(investigation_id,project_id,conversation_id,run_id,purpose,state,result_json,created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (new_id("inv"), parent["project_id"], request["childConversationId"], None, task, "running", None, now, now, None))
        store.conn.execute(
            "INSERT INTO child_requests(child_request_id,operation_id,parent_run_id,project_id,parent_working_copy_id,semantic_role,runtime_role,task,snapshot_id,snapshot_ref,snapshot_commit_oid,snapshot_tree_oid,snapshot_path,child_working_copy_id,child_conversation_id,child_run_id,state,result_json,created_at,updated_at,completed_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (request["childRequestId"], operation["operation_id"], parent_run_id, parent["project_id"], parent["working_copy_id"], semantic_role, request["runtimeRole"], task, request["snapshotId"], ref, commit_oid, tree_oid, str(snapshot_path), request["childWorkingCopyId"], request["childConversationId"], None, "ready", None, now, now, None, None, None),
        )
        update_operation_in_transaction(store.conn, operation["operation_id"], state="succeeded", step="child-snapshot-ready", result={"childRequestId": request["childRequestId"], "conversationId": request["childConversationId"], "snapshotRef": ref})
        append_event_in_transaction(store.conn, event_kind="child.ready", resource_type="child-request", resource_id=request["childRequestId"], operation_id=operation["operation_id"], payload={"parentRunId": parent_run_id, "semanticRole": semantic_role, "snapshotRef": ref, "snapshotCommitOid": commit_oid, "snapshotTreeOid": tree_oid})
    result = dict(store.conn.execute("SELECT * FROM child_requests WHERE child_request_id=?", (request["childRequestId"],)).fetchone())
    result["childSource"] = child_source
    return result


def bind_child_run(store: Any, *, child_request_id: str, child_run_id: str) -> None:
    validate_id(child_request_id, prefix="child")
    validate_id(child_run_id, prefix="run")
    with store.transaction():
        cursor = store.conn.execute("UPDATE child_requests SET child_run_id=?,state='running',updated_at=? WHERE child_request_id=? AND state='ready' AND child_run_id IS NULL", (child_run_id, utc_now(), child_request_id))
        if cursor.rowcount != 1:
            raise SubagentError("child request could not bind its controller run")


def record_child_terminal(store: Any, *, child_request_id: str, terminal_class: str, result: Mapping[str, Any]) -> dict[str, Any]:
    validate_id(child_request_id, prefix="child")
    allowed = {"success", "failed", "lost", "needs-user", "interrupted", "needs_attention"}
    if terminal_class not in allowed:
        raise SubagentError("child terminal class is invalid")
    row = store.conn.execute("SELECT * FROM child_requests WHERE child_request_id=?", (child_request_id,)).fetchone()
    if row is None or row["child_run_id"] is None:
        raise SubagentError("child request has no controller-supervised run")
    existing = store.conn.execute("SELECT * FROM child_terminal_records WHERE child_run_id=?", (row["child_run_id"],)).fetchone()
    body = dict(result)
    provenance = {"childRequestId": child_request_id, "parentRunId": row["parent_run_id"], "snapshotRef": row["snapshot_ref"], "snapshotCommitOid": row["snapshot_commit_oid"], "snapshotTreeOid": row["snapshot_tree_oid"], "semanticRole": row["semantic_role"]}
    digest = "sha256:" + hashlib.sha256(canonical_json({"terminalClass": terminal_class, "result": body, "provenance": provenance}).encode()).hexdigest()
    if existing is not None:
        if existing["terminal_digest"] != digest:
            raise SubagentError("child already has a different immutable terminal record")
        return dict(existing)
    now = utc_now()
    with store.transaction():
        store.conn.execute("INSERT INTO child_terminal_records(child_run_id,parent_run_id,terminal_class,changed_state,submission_class,submitted_change_id,submitted_revision,artifact_id,result_json,provenance_json,terminal_digest,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (row["child_run_id"], row["parent_run_id"], terminal_class, "clean", "none", None, None, None, canonical_json(body), canonical_json(provenance), digest, now, now))
        store.conn.execute("UPDATE child_requests SET state=?,result_json=?,updated_at=?,completed_at=? WHERE child_request_id=?", (terminal_class, canonical_json(body), now, now, child_request_id))
        append_event_in_transaction(store.conn, event_kind="child.terminal", resource_type="child-request", resource_id=child_request_id, payload={"childRunId": row["child_run_id"], "parentRunId": row["parent_run_id"], "terminalClass": terminal_class, "terminalDigest": digest})
    return dict(store.conn.execute("SELECT * FROM child_terminal_records WHERE child_run_id=?", (row["child_run_id"],)).fetchone())


def run_controller_child(
    store: Any, *, parent_run_id: str, semantic_role: str, task: str, idempotency_key: str,
    build_id: str, model: str, acceptance_test_profile: str | None = None,
    test_provider: str | None = None, test_probe: str | None = None,
    cancellation: threading.Event | None = None,
) -> dict[str, Any]:
    assignment = create_child_assignment(store, parent_run_id=parent_run_id, semantic_role=semantic_role, task=task, idempotency_key=idempotency_key)
    terminal = store.conn.execute("SELECT * FROM child_terminal_records WHERE child_run_id=?", (assignment.get("child_run_id"),)).fetchone() if assignment.get("child_run_id") else None
    if terminal is not None:
        return {"childRequest": assignment, "terminal": dict(terminal)}
    child_source = validate_child_source({"snapshotId": assignment["snapshot_id"], "snapshotRef": assignment["snapshot_ref"], "snapshotCommitOid": assignment["snapshot_commit_oid"], "snapshotTreeOid": assignment["snapshot_tree_oid"], "sourceHeadOid": assignment["snapshot_commit_oid"], "sourceTreeOid": assignment["snapshot_tree_oid"], "authority": "read-only"})
    from .host_supervisor import launch_host_pi
    launch_error: BaseException | None = None
    try:
        return_code = launch_host_pi(
            store, conversation_id=assignment["child_conversation_id"], build_id=build_id,
            prompt=task, model=model, acceptance_test_profile=acceptance_test_profile,
            test_provider=test_provider, test_probe=test_probe, expected_role=assignment["runtime_role"],
            parent_run_id=parent_run_id, child_source=child_source,
            child_request_id=assignment["child_request_id"], cancellation=cancellation,
        )
        terminal_class = "success" if return_code == 0 else "failed"
        result = {"returnCode": return_code, "semanticRole": semantic_role}
    except BaseException as error:
        launch_error = error
        current = store.conn.execute("SELECT * FROM child_requests WHERE child_request_id=?", (assignment["child_request_id"],)).fetchone()
        if current is not None and current["child_run_id"] is not None:
            bound = store.conn.execute("SELECT observed_state FROM runs WHERE run_id=?", (current["child_run_id"],)).fetchone()
            if bound is not None and bound["observed_state"] not in {"stopped", "failed", "lost", "needs_attention"}:
                # A live run is already bound to this child request (replay or
                # concurrent launch). The failing launch created its own run
                # which launch_host_pi already failed. Do not terminalize the
                # still-executing bound child.
                raise
        terminal_class = "interrupted" if cancellation is not None and cancellation.is_set() else "failed"
        result = {"error": type(error).__name__, "message": str(error)[:1024], "semanticRole": semantic_role}
    current = store.conn.execute("SELECT * FROM child_requests WHERE child_request_id=?", (assignment["child_request_id"],)).fetchone()
    if current is None or current["child_run_id"] is None:
        raise SubagentError("child launch ended without a durable run identity") from launch_error
    terminal = record_child_terminal(store, child_request_id=assignment["child_request_id"], terminal_class=terminal_class, result=result)
    settled = store.conn.execute("SELECT * FROM child_requests WHERE child_request_id=?", (assignment["child_request_id"],)).fetchone()
    if settled is None:
        raise SubagentError("child request row vanished after its terminal record")
    return {"childRequest": dict(settled), "terminal": terminal}


__all__ = ["SubagentError", "bind_child_run", "create_child_assignment", "record_child_terminal", "run_controller_child"]
