"""Controller-brokered read-only child assignments on immutable Git snapshots."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Mapping

from .conversations import conversation_session_binding
from .events import append_event_in_transaction
from .models import bounded_text, canonical_json, json_digest, new_id, utc_now, validate_child_source, validate_id
from .operations import update_operation_in_transaction


class SubagentError(RuntimeError):
    pass


# Read-only semantic roles map to the investigator runtime role (snapshot,
# scoped reads, no mutation); reviewer stays the exact-revision reviewer.
# Worker is the mutable role: a controller-created working copy and writer
# container under the same one-writer rule as headful writers.
READ_ONLY_ROLES = frozenset({"scout", "investigator", "researcher", "planner", "oracle", "delegate", "reviewer"})
SEMANTIC_ROLES = frozenset({*READ_ONLY_ROLES, "worker"})
ROLE_PROMPTS = {
    "scout": "You are a fast read-only scout. Map the assigned surface, report concise findings, and stop when the brief is answered.",
    "investigator": "You are a bounded read-only investigator. Use scoped reads on the assigned snapshot; produce a durable result and stop when the brief is answered.",
    "researcher": "You are a read-only researcher. Investigate the assigned question against the scoped evidence and report findings with provenance.",
    "planner": "You are a read-only planner. Produce a concrete implementation plan from the accepted context; do not implement.",
    "oracle": "You are a read-only decision-consistency advisor. Check the proposed direction against the accepted context and report risks.",
    "delegate": "You are a lightweight read-only delegate. Extract or verify the exact requested evidence and return it without deciding.",
    "reviewer": "You are a read-only reviewer. Inspect the exact assigned revision and report findings; you have no mutation authority.",
    "worker": "You are a mutable implementation worker in your own controller-owned working copy. Edit, test, and submit through controller operations only; never touch another working copy.",
}


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
    if semantic_role not in SEMANTIC_ROLES:
        raise SubagentError("subagent role is not supported")
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


def _generation_launcher(store: Any, build_id: str, launcher_name: str) -> Path:
    """Resolve one launcher inside the registered generation for the build."""
    if not isinstance(build_id, str) or not build_id:
        raise SubagentError("child launch requires an exact build id")
    row = store.conn.execute("SELECT * FROM installed_builds WHERE build_id=? AND status IN ('staged','active')", (build_id,)).fetchone()
    if row is None or not isinstance(row["build_manifest_path"], str) or not row["build_manifest_path"]:
        raise SubagentError("child launch build is not a registered generation")
    root = Path(row["build_manifest_path"]).expanduser().absolute().parent
    if root.is_symlink() or not root.is_dir():
        raise SubagentError("child launch generation root is unsafe")
    launcher = root / "bin" / launcher_name
    if launcher.is_symlink() or not launcher.is_file():
        raise SubagentError(f"generation launcher is unavailable: {launcher}")
    return launcher


def _child_log_path(store: Any, child_request_id: str) -> Path:
    log_root = Path(store.state_root).expanduser().absolute() / "child-logs"
    log_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    return log_root / f"{child_request_id}.log"


def _spawn_detached(argv: list[str], log_path: Path) -> int:
    """Spawn one controller-owned child launcher detached from the parent.

    The child runs as its own process group so its supervision outlives the
    parent Pi process; its durable run and terminal records are written by
    the launcher itself against the controller state root.
    """
    try:
        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        try:
            log_fd = os.open(str(log_path), os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as error:
            raise SubagentError(f"cannot open child log: {error}") from error
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
            start_new_session=True, close_fds=True, shell=False,
        )
    except OSError as error:
        os.close(log_fd)
        raise SubagentError(f"cannot launch the detached child: {error}") from error
    os.close(log_fd)
    return int(process.pid)


def start_child_assignment(
    store: Any, *, parent_run_id: str, semantic_role: str, task: str, idempotency_key: str,
    build_id: str, model: str, acceptance_test_profile: str | None = None,
    test_provider: str | None = None, test_probe: str | None = None,
) -> dict[str, Any]:
    """Create a read-only child assignment and launch it detached (async)."""
    if semantic_role not in READ_ONLY_ROLES:
        raise SubagentError("async child role must be read-only")
    assignment = create_child_assignment(store, parent_run_id=parent_run_id, semantic_role=semantic_role, task=task, idempotency_key=idempotency_key)
    bound = store.conn.execute("SELECT * FROM child_requests WHERE child_request_id=?", (assignment["child_request_id"],)).fetchone()
    if bound is not None and bound["child_run_id"] is not None:
        return {"childRequest": dict(bound), "launched": False}
    launcher = _generation_launcher(store, build_id, "pi-system-run")
    prompt = f"{ROLE_PROMPTS[semantic_role]}\n\n{task}"
    argv = [
        str(launcher), "--state-root", str(store.state_root),
        "--conversation-id", assignment["child_conversation_id"],
        "--build-id", build_id, "--model", model, "--prompt", prompt,
        "--expected-role", assignment["runtime_role"],
        "--child-request-id", assignment["child_request_id"],
    ]
    if acceptance_test_profile is not None:
        argv += ["--acceptance-test-profile", acceptance_test_profile]
    if test_provider is not None:
        argv += ["--test-provider", test_provider]
    if test_probe is not None:
        argv += ["--test-probe", test_probe]
    log_path = _child_log_path(store, assignment["child_request_id"])
    pid = _spawn_detached(argv, log_path)
    with store.transaction():
        store.conn.execute("UPDATE child_requests SET error_detail=?,updated_at=? WHERE child_request_id=? AND child_run_id IS NULL", (f"launcher-pid={pid}", utc_now(), assignment["child_request_id"]))
    return {"childRequest": dict(store.conn.execute("SELECT * FROM child_requests WHERE child_request_id=?", (assignment["child_request_id"],)).fetchone()), "launched": True}


def start_worker_assignment(
    store: Any, *, parent_run_id: str, task: str, title: str, idempotency_key: str,
    build_id: str, model: str, tool_image: str,
    acceptance_test_profile: str | None = None,
    test_provider: str | None = None, test_probe: str | None = None,
) -> dict[str, Any]:
    """Create one mutable headless worker in its own controller-owned working copy."""
    validate_id(parent_run_id, prefix="run")
    parent = store.conn.execute("SELECT * FROM runs WHERE run_id=? AND desired_state='running' AND observed_state='running'", (parent_run_id,)).fetchone()
    if parent is None or parent["working_copy_id"] is None:
        raise SubagentError("worker parent run is not active or scoped")
    conversation = store.conn.execute("SELECT role FROM conversations WHERE conversation_id=?", (parent["conversation_id"],)).fetchone()
    if conversation is None or conversation["role"] not in {"personal", "workstream", "integration"}:
        raise SubagentError("only writer conversations can launch headless workers")
    from .pi_workstreams import create_workstream
    workstream = create_workstream(
        store, project_id=parent["project_id"], title=bounded_text(title or "worker", name="worker title", limit=200),
        brief={"kind": "headless-worker", "task": bounded_text(task, name="worker task", limit=4096)},
        idempotency_key=f"worker:{idempotency_key}",
        headful=False,
    )
    child_request_id = new_id("child")
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "INSERT INTO child_requests(child_request_id,operation_id,parent_run_id,project_id,parent_working_copy_id,semantic_role,runtime_role,task,snapshot_id,snapshot_ref,snapshot_commit_oid,snapshot_tree_oid,snapshot_path,child_working_copy_id,child_conversation_id,child_run_id,state,result_json,created_at,updated_at,completed_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (child_request_id, workstream["creation_operation_id"], parent_run_id, parent["project_id"], parent["working_copy_id"], "worker", "workstream", task, None, None, None, None, None, workstream["working_copy_id"], workstream["conversation_id"], None, "ready", None, now, now, None, None, None),
        )
    launcher = _generation_launcher(store, build_id, "pi-system-workstream-run")
    prompt = f"{ROLE_PROMPTS['worker']}\n\n{task}"
    argv = [
        str(launcher), "--state-root", str(store.state_root),
        "--conversation-id", workstream["conversation_id"],
        "--build-id", build_id, "--model", model, "--prompt", prompt,
        "--tool-image", tool_image,
        "--child-request-id", child_request_id,
    ]
    if acceptance_test_profile is not None:
        argv += ["--acceptance-test-profile", acceptance_test_profile]
    if test_provider is not None:
        argv += ["--test-provider", test_provider]
    if test_probe is not None:
        argv += ["--test-probe", test_probe]
    log_path = _child_log_path(store, child_request_id)
    pid = _spawn_detached(argv, log_path)
    with store.transaction():
        store.conn.execute("UPDATE child_requests SET error_detail=?,updated_at=? WHERE child_request_id=?", (f"launcher-pid={pid}", utc_now(), child_request_id))
    return {"childRequest": dict(store.conn.execute("SELECT * FROM child_requests WHERE child_request_id=?", (child_request_id,)).fetchone()), "workstream": dict(workstream), "launched": True}


def launch_reviewer_detached(store: Any, *, conversation_id: str, build_id: str, model: str, prompt: str, acceptance_test_profile: str | None = None, test_provider: str | None = None, test_probe: str | None = None) -> int:
    """Launch one exact-revision reviewer conversation detached.

    The reviewer conversation is created by the review assignment; this
    launches its read-only host Pi process and returns immediately. The run
    and conversation terminalize through the normal launcher lifecycle.
    """
    validate_id(conversation_id, prefix="conv")
    launcher = _generation_launcher(store, build_id, "pi-system-run")
    argv = [
        str(launcher), "--state-root", str(store.state_root),
        "--conversation-id", conversation_id,
        "--build-id", build_id, "--model", model, "--prompt", prompt,
        "--expected-role", "reviewer",
    ]
    if acceptance_test_profile is not None:
        argv += ["--acceptance-test-profile", acceptance_test_profile]
    if test_provider is not None:
        argv += ["--test-provider", test_provider]
    if test_probe is not None:
        argv += ["--test-probe", test_probe]
    log_path = _child_log_path(store, "reviewer-" + conversation_id.removeprefix("conv_"))
    return _spawn_detached(argv, log_path)


def child_status(store: Any, *, parent_run_id: str, child_request_id: str) -> dict[str, Any]:
    validate_id(child_request_id, prefix="child")
    row = store.conn.execute("SELECT * FROM child_requests WHERE child_request_id=? AND parent_run_id=?", (child_request_id, parent_run_id)).fetchone()
    if row is None:
        raise SubagentError("child request does not belong to the authenticated parent")
    terminal = None
    if row["child_run_id"] is not None:
        terminal_row = store.conn.execute("SELECT * FROM child_terminal_records WHERE child_run_id=?", (row["child_run_id"],)).fetchone()
        terminal = dict(terminal_row) if terminal_row is not None else None
    return {"childRequest": dict(row), "terminal": terminal}


def wait_child_terminal(store: Any, *, parent_run_id: str, child_request_id: str, timeout: float = 300.0) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, min(float(timeout), 3600.0))
    while True:
        status = child_status(store, parent_run_id=parent_run_id, child_request_id=child_request_id)
        if status["terminal"] is not None:
            return {**status, "waited": True}
        if time.monotonic() >= deadline:
            return {**status, "waited": False}
        time.sleep(1.0)


def list_child_requests(store: Any, *, parent_run_id: str, limit: int = 32) -> list[dict[str, Any]]:
    validate_id(parent_run_id, prefix="run")
    bound = max(1, min(int(limit), 256))
    rows = store.conn.execute(
        "SELECT child_request_id,semantic_role,runtime_role,state,child_conversation_id,child_run_id,created_at,updated_at,completed_at FROM child_requests WHERE parent_run_id=? ORDER BY created_at DESC LIMIT ?",
        (parent_run_id, bound),
    ).fetchall()
    return [dict(row) for row in rows]


def _launcher_pid(row: Mapping[str, Any]) -> int | None:
    detail = row["error_detail"]
    if not isinstance(detail, str) or not detail.startswith("launcher-pid="):
        return None
    try:
        pid = int(detail.removeprefix("launcher-pid="))
    except ValueError:
        return None
    return pid if pid > 0 else None


def _wait_for_bound_run(store: Any, *, child_request_id: str, parent_run_id: str, timeout: float = 15.0) -> dict[str, Any]:
    """Wait (bounded) for the detached child launcher to bind its controller run."""
    deadline = time.monotonic() + max(0.0, min(float(timeout), 60.0))
    while True:
        row = store.conn.execute("SELECT * FROM child_requests WHERE child_request_id=? AND parent_run_id=?", (child_request_id, parent_run_id)).fetchone()
        if row is None:
            raise SubagentError("child request does not belong to the authenticated parent")
        if row["child_run_id"] is not None:
            return dict(row)
        if row["state"] not in {"ready", "running"}:
            raise SubagentError("child is not running and cannot be interrupted")
        if time.monotonic() >= deadline:
            raise SubagentError("child did not bind a controller run in time")
        time.sleep(0.1)


def interrupt_child(store: Any, *, parent_run_id: str, child_request_id: str) -> dict[str, Any]:
    """Soft-interrupt one detached child: SIGINT to its launcher.

    The child's session and work are preserved; the run terminalizes as
    interrupted and may be resumed. Waits (bounded) for the child to bind its
    controller run before signaling, so an interrupt cannot race child startup.
    """
    row = _wait_for_bound_run(store, child_request_id=child_request_id, parent_run_id=parent_run_id)
    if row["state"] not in {"running", "ready"}:
        raise SubagentError("child is not running and cannot be interrupted")
    pid = _launcher_pid(row)
    if pid is None:
        raise SubagentError("child launcher identity is unavailable")
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        pass
    return {"childRequestId": child_request_id, "action": "interrupt", "signaled": True}


def stop_child(store: Any, *, parent_run_id: str, child_request_id: str) -> dict[str, Any]:
    """Stop one detached child terminally: SIGTERM to its launcher."""
    row = _wait_for_bound_run(store, child_request_id=child_request_id, parent_run_id=parent_run_id)
    if row["state"] not in {"running", "ready"}:
        raise SubagentError("child is not running and cannot be stopped")
    pid = _launcher_pid(row)
    if pid is None:
        raise SubagentError("child launcher identity is unavailable")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return {"childRequestId": child_request_id, "action": "stop", "signaled": True}


def resume_child(
    store: Any, *, parent_run_id: str, child_request_id: str,
    build_id: str, model: str, acceptance_test_profile: str | None = None,
    test_provider: str | None = None, test_probe: str | None = None,
) -> dict[str, Any]:
    """Resume one interrupted detached child with the same conversation and task."""
    row = store.conn.execute("SELECT * FROM child_requests WHERE child_request_id=? AND parent_run_id=?", (child_request_id, parent_run_id)).fetchone()
    if row is None:
        raise SubagentError("child request does not belong to the authenticated parent")
    terminal = store.conn.execute("SELECT terminal_class FROM child_terminal_records WHERE child_run_id=?", (row["child_run_id"],)).fetchone() if row["child_run_id"] else None
    if terminal is None or terminal["terminal_class"] != "interrupted":
        raise SubagentError("only an interrupted child can be resumed")
    semantic_role = row["semantic_role"]
    if semantic_role not in READ_ONLY_ROLES:
        raise SubagentError("only read-only children can be resumed")
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "UPDATE child_requests SET child_run_id=NULL,state='ready',result_json=NULL,completed_at=NULL,error_detail=NULL,updated_at=? WHERE child_request_id=?",
            (now, child_request_id),
        )
    launcher = _generation_launcher(store, build_id, "pi-system-run")
    prompt = f"{ROLE_PROMPTS[semantic_role]}\n\n{row['task']}"
    argv = [
        str(launcher), "--state-root", str(store.state_root),
        "--conversation-id", row["child_conversation_id"],
        "--build-id", build_id, "--model", model, "--prompt", prompt,
        "--expected-role", row["runtime_role"],
        "--child-request-id", child_request_id,
    ]
    if acceptance_test_profile is not None:
        argv += ["--acceptance-test-profile", acceptance_test_profile]
    if test_provider is not None:
        argv += ["--test-provider", test_provider]
    if test_probe is not None:
        argv += ["--test-probe", test_probe]
    log_path = _child_log_path(store, child_request_id)
    pid = _spawn_detached(argv, log_path)
    with store.transaction():
        store.conn.execute("UPDATE child_requests SET error_detail=?,updated_at=? WHERE child_request_id=?", (f"launcher-pid={pid}", utc_now(), child_request_id))
    return {"childRequestId": child_request_id, "action": "resume", "launched": True, "launcherPid": pid}


__all__ = ["SubagentError", "SEMANTIC_ROLES", "bind_child_run", "child_status", "create_child_assignment", "interrupt_child", "list_child_requests", "record_child_terminal", "resume_child", "run_controller_child", "start_child_assignment", "start_worker_assignment", "stop_child", "wait_child_terminal"]
