"""Recoverable controller-owned workstream creation saga."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Mapping, Protocol

from .conversations import conversation_session_binding
from .events import append_event_in_transaction
from .pi_store import default_state_root
from .models import bounded_text, canonical_json, json_digest, new_id, utc_now, validate_id
from .operations import update_operation_in_transaction


class WorkstreamCreationError(RuntimeError):
    pass


class Failpoint(Protocol):
    def hit(self, name: str, context: Mapping[str, str]) -> None: ...


def _hit(failpoint: Failpoint | None, name: str, context: Mapping[str, str]) -> None:
    if failpoint is not None:
        failpoint.hit(name, context)


def _git_env() -> dict[str, str]:
    return {
        "PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "GIT_EDITOR": "true",
        "GIT_ASKPASS": "true", "TMPDIR": tempfile.gettempdir(),
    }


def _git(repository: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git", path=os.defpath)
    if git is None:
        raise WorkstreamCreationError("Git is unavailable")
    result = subprocess.run(
        [git, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.sshCommand=", "-c", "credential.helper=", "-c", "protocol.allow=never", *args],
        cwd=str(repository), env=_git_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=120,
        check=False, shell=False,
    )
    if check and result.returncode != 0:
        raise WorkstreamCreationError(result.stderr.strip()[:1024] or "Git workstream operation failed")
    return result


def _work_root(store: Any, project_id: str) -> Path:
    configured = os.environ.get("PI_SYSTEM_WORK_ROOT")
    if configured:
        base = Path(configured).expanduser()
    elif Path(store.state_root).absolute() != default_state_root().absolute():
        base = Path(store.state_root).parent / "pi-system-work"
    else:
        base = Path.home() / ".local" / "share" / "pi-system-work"
    return base.absolute() / project_id


def _intent(store: Any, *, project: Mapping[str, Any], primary: Mapping[str, Any], title: str,
            brief: Mapping[str, Any], display_name: str | None, idempotency_key: str) -> tuple[Any, dict[str, Any]]:
    existing = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    title = bounded_text(title, name="title", limit=512)
    display = bounded_text(display_name or title, name="display_name", limit=512)
    if existing is not None:
        request = json.loads(existing["request_json"])
        expected = {"projectId": project["project_id"], "title": title, "brief": dict(brief), "displayName": display}
        comparable = {key: request[key] for key in expected} if all(key in request for key in expected) else {}
        if existing["kind"] != "workstream.create" or comparable != expected:
            raise WorkstreamCreationError("workstream idempotency key is bound to another request")
        return existing, request

    workstream_id, working_copy_id, conversation_id = new_id("ws"), new_id("wc"), new_id("conv")
    branch_name = f"pi-system/{workstream_id}"
    destination = _work_root(store, project["project_id"]) / workstream_id
    request = {
        "projectId": project["project_id"], "title": title, "brief": dict(brief), "displayName": display,
        "workstreamId": workstream_id, "workingCopyId": working_copy_id, "conversationId": conversation_id,
        "primaryWorkingCopyId": primary["working_copy_id"], "sourcePath": str(Path(primary["path"]).resolve(strict=True)),
        "gitCommonDir": str(Path(project["git_common_dir"]).resolve(strict=True)),
        "targetRef": primary["branch_ref"], "baseOid": primary["expected_head_oid"], "baseTreeOid": primary["expected_tree_oid"],
        "branchRef": f"refs/heads/{branch_name}", "branchName": branch_name, "worktreePath": str(destination),
        "packageEnvironmentRoot": str(Path(store.state_root).absolute() / "environments" / working_copy_id),
    }
    operation = store.create_operation(
        idempotency_key=idempotency_key, kind="workstream.create", resource_type="workstream",
        resource_id=workstream_id, actor_type="controller", request=request,
    )
    return store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone(), request


def _observe_effect(source: Path, request: Mapping[str, Any]) -> tuple[str, str] | None:
    destination = Path(str(request["worktreePath"]))
    listing = _git(source, ["worktree", "list", "--porcelain"]).stdout
    indexed = f"worktree {destination}\n" in listing or f"worktree {destination}\r\n" in listing
    if not destination.exists() and not destination.is_symlink() and not indexed:
        branch = _git(source, ["show-ref", "--verify", "--hash", str(request["branchRef"])], check=False)
        if branch.returncode == 0:
            raise WorkstreamCreationError("workstream branch exists without its indexed worktree; needs attention")
        return None
    if not destination.is_dir() or not indexed:
        raise WorkstreamCreationError("workstream path and Git index disagree; needs attention")
    head = _git(destination, ["rev-parse", "--verify", "HEAD"]).stdout.strip()
    tree = _git(destination, ["rev-parse", "--verify", "HEAD^{tree}"]).stdout.strip()
    branch = _git(destination, ["symbolic-ref", "--quiet", "HEAD"]).stdout.strip()
    if head != request["baseOid"] or tree != request["baseTreeOid"] or branch != request["branchRef"]:
        raise WorkstreamCreationError("workstream Git identity differs from durable intent; needs attention")
    return head, tree


def _mark_attention(store: Any, operation: Mapping[str, Any], request: Mapping[str, Any], error: BaseException) -> None:
    if operation["state"] in {"succeeded", "needs_attention"}:
        return
    now = utc_now()
    with store.transaction():
        update_operation_in_transaction(store.conn, operation["operation_id"], state="needs_attention", step="git-identity-ambiguous", error_code="WORKTREE_IDENTITY_AMBIGUOUS", error_detail=str(error)[:1024])
        store.conn.execute(
            "INSERT INTO attention(attention_id,project_id,conversation_id,run_id,change_id,kind,summary,detail_json,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (new_id("attention"), request["projectId"], None, None, None, "workstream-creation", "Workstream creation needs attention", canonical_json({"operationId": operation["operation_id"], "worktreePath": request["worktreePath"], "error": str(error)[:1024]}), "open", now, now),
        )


def create_workstream(
    store: Any, *, project_id: str, title: str, brief: Mapping[str, Any] | None = None,
    display_name: str | None = None, idempotency_key: str, failpoint: Failpoint | None = None,
) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    project = store.conn.execute("SELECT * FROM projects WHERE project_id=? AND desired_state='active'", (project_id,)).fetchone()
    primary = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary' AND desired_state='present'", (project_id,)).fetchone()
    if project is None or primary is None:
        raise WorkstreamCreationError("project primary working copy is missing")
    if not primary["expected_head_oid"] or not primary["expected_tree_oid"] or not primary["branch_ref"]:
        raise WorkstreamCreationError("workstream requires a committed branch-bound primary HEAD")
    operation, request = _intent(store, project=project, primary=primary, title=title, brief=dict(brief or {}), display_name=display_name, idempotency_key=idempotency_key)
    context = {"operationId": operation["operation_id"], "workstreamId": request["workstreamId"], "worktreePath": request["worktreePath"]}
    _hit(failpoint, "operation.intent.after", context)
    if operation["state"] == "succeeded":
        row = store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (request["workstreamId"],)).fetchone()
        if row is None:
            raise WorkstreamCreationError("completed workstream operation has no resource")
        return dict(row)
    if operation["state"] == "needs_attention":
        raise WorkstreamCreationError(operation["error_detail"] or "workstream creation needs attention")
    if operation["state"] == "planned":
        with store.transaction():
            update_operation_in_transaction(store.conn, operation["operation_id"], state="applying", step="git-worktree-pending")

    source = Path(str(request["sourcePath"]))
    destination = Path(str(request["worktreePath"]))
    try:
        effect = _observe_effect(source, request)
    except WorkstreamCreationError as error:
        _mark_attention(store, operation, request, error)
        raise
    if effect is None:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _hit(failpoint, "worktree.create.before", context)
        try:
            _git(source, ["worktree", "add", "-b", str(request["branchName"]), str(destination), str(request["baseOid"])])
        except BaseException:
            try:
                _observe_effect(source, request)
            except WorkstreamCreationError as ambiguous:
                _mark_attention(store, operation, request, ambiguous)
            raise
        _hit(failpoint, "worktree.create.after", context)
        effect = _observe_effect(source, request)
    assert effect is not None

    conversation_id = str(request["conversationId"])
    session_id, session_file = conversation_session_binding(store, project_id, conversation_id)
    package_root = Path(str(request["packageEnvironmentRoot"]))
    package_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(package_root, 0o700)
    git_marker = destination / ".git"
    marker = git_marker.read_text(encoding="utf-8").strip()
    if not marker.startswith("gitdir: "):
        error = WorkstreamCreationError("created worktree has no file-form Git identity")
        _mark_attention(store, operation, request, error)
        raise error
    git_dir = str(Path(marker.removeprefix("gitdir: ")).resolve(strict=True))
    mode = "trusted-live" if project["trust_mode"] == "trusted" else "isolated"
    now = utc_now()
    _hit(failpoint, "event.commit.before", context)
    with store.transaction():
        if store.conn.execute("SELECT 1 FROM workstreams WHERE workstream_id=?", (request["workstreamId"],)).fetchone() is None:
            store.conn.execute(
                "INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,git_dir,branch_ref,expected_head_oid,expected_tree_oid,effective_mode,desired_state,observed_state,writer_epoch,active_writer_run_id,resource_version,controller_owned,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request["workingCopyId"], project_id, request["displayName"], "worktree", "workstream", str(destination), git_dir, request["branchRef"], effect[0], effect[1], mode, "present", "ready", 0, None, 1, 1, now, now, now, None, None),
            )
            store.conn.execute(
                "INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,authority_profile,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (conversation_id, project_id, request["workingCopyId"], "workstream", "writer-container", request["displayName"], session_id, session_file, "active", "ready", 1, now, now, now, None, None),
            )
            store.conn.execute(
                "INSERT INTO workstreams(workstream_id,project_id,working_copy_id,conversation_id,title,brief_json,target_ref,starting_oid,primary_working_copy_id,branch_ref,worktree_path,package_environment_root,creation_operation_id,desired_state,observed_state,controller_owned,resource_version,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request["workstreamId"], project_id, request["workingCopyId"], conversation_id, request["title"], canonical_json(request["brief"]), request["targetRef"], request["baseOid"], request["primaryWorkingCopyId"], request["branchRef"], str(destination), str(package_root), operation["operation_id"], "active", "creating", 1, 1, now, now, now, None, None),
            )
            store.conn.execute(
                "INSERT INTO presentation_assignments(presentation_assignment_id,conversation_id,backend,desired_state,observed_state,locator_json,resource_version,observed_at,updated_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("pa"), conversation_id, "tmux", "present", "unknown", canonical_json({"conversationId": conversation_id, "workstreamId": request["workstreamId"]}), 1, None, now, None, None),
            )
            store.conn.execute("UPDATE workstreams SET observed_state='ready',resource_version=2,last_reconciled_at=?,updated_at=? WHERE workstream_id=?", (now, now, request["workstreamId"]))
            append_event_in_transaction(store.conn, event_kind="workstream.created", resource_type="workstream", resource_id=request["workstreamId"], resource_version=2, operation_id=operation["operation_id"], payload={"projectId": project_id, "workingCopyId": request["workingCopyId"], "conversationId": conversation_id, "targetRef": request["targetRef"], "branchRef": request["branchRef"], "startingOid": request["baseOid"], "worktreePath": str(destination)})
        update_operation_in_transaction(store.conn, operation["operation_id"], state="succeeded", step="workstream-ready", result={"workstreamId": request["workstreamId"], "workingCopyId": request["workingCopyId"], "conversationId": conversation_id})
    _hit(failpoint, "event.commit.after", context)
    return dict(store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (request["workstreamId"],)).fetchone())


__all__ = ["WorkstreamCreationError", "create_workstream"]
