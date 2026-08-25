"""Secretary-owned Git inspection, acceptance import, ff-only merge, and push."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Mapping

from .events import append_event_in_transaction
from .git_runner import run_git
from .models import ConflictError, InvalidRequestError, NeedsAttentionError, ScopeMismatchError, bounded_text, new_id, utc_now, validate_git_oid, validate_id, validate_sha256
from .operations import authoritative_workstream_creation
from .policies import enforce_merge_policy
from .projects import get_project
from .worker_repo import project_git_lock, validate_worker_repository

_MERGE_SCOPE_FIELDS = frozenset({
    "kind", "projectId", "workstreamId", "targetBranch", "targetCommitOid", "sourceBranch",
    "sourceCommitOid", "strategy", "completionPacketSha256", "completionSourceCommitOid",
    "effects", "nonEffects",
})


def _run_git(path: Path, *args: str, accepted: frozenset[int] = frozenset({0}), max_bytes: int = 128 * 1024, input_text: str | None = None, authenticated: bool = False, timeout: int = 30) -> tuple[int, str]:
    if not authenticated:
        try:
            result = run_git(path, args, accepted=accepted, input_text=input_text, timeout=timeout, max_bytes=max_bytes)
        except InvalidRequestError as error:
            raise ConflictError("Git operation refused", detail=error.detail) from error
        return result.returncode, result.stdout.rstrip("\n")
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise InvalidRequestError("git is unavailable")
    environment = {"HOME": str(Path.home()), "PATH": os.environ.get("PATH", os.defpath), "LANG": "C", "LC_ALL": "C", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    for name in ("DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    result = subprocess.run([executable, "-C", str(path), *args], env=environment, input=input_text, text=True, capture_output=True, timeout=timeout, check=False)
    if result.returncode not in accepted:
        raise ConflictError("authenticated Git operation refused", detail={"command": args[0], "stderr": result.stderr[:512]})
    if len(result.stdout.encode()) > max_bytes:
        raise InvalidRequestError("Git output is too large")
    return result.returncode, result.stdout.rstrip("\n")


def _oid(path: Path, revision: str) -> str:
    _code, value = _run_git(path, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}")
    oid = value.strip()
    validate_git_oid(oid, "Git commit object id")
    if oid != oid.lower():
        raise NeedsAttentionError("Git returned an invalid commit object id")
    return oid


def _repository(project: Mapping[str, Any]) -> Path:
    path = Path(str(project["repository_path"]))
    try:
        info = path.lstat()
        canonical = path.resolve(strict=True)
    except OSError as error:
        raise NeedsAttentionError("registered project checkout is unavailable") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022 or canonical != path:
        raise NeedsAttentionError("registered project checkout is unsafe")
    _code, common_value = _run_git(canonical, "rev-parse", "--git-common-dir")
    common = Path(common_value)
    if not common.is_absolute():
        common = canonical / common
    if common.resolve(strict=True) != Path(str(project["git_common_dir"])).resolve(strict=True):
        raise NeedsAttentionError("registered project Git identity drifted")
    return canonical


def _primary_state(store: Any, project_id: str) -> tuple[dict[str, Any], Path, str, str, str]:
    project = get_project(store, project_id)
    repository = _repository(project)
    _code, target_branch = _run_git(repository, "symbolic-ref", "--quiet", "--short", "HEAD")
    target_branch = bounded_text(target_branch.strip(), name="target_branch", limit=512)
    if target_branch.startswith("-") or any(ord(char) < 0x20 for char in target_branch):
        raise NeedsAttentionError("registered project checkout has an unsafe branch")
    _code, porcelain = _run_git(repository, "status", "--porcelain=v1", "--untracked-files=all", max_bytes=64 * 1024)
    return project, repository, target_branch, _oid(repository, "HEAD"), porcelain


def git_status(store: Any, project_id: str) -> dict[str, Any]:
    _project, _repository_path, branch, oid, porcelain = _primary_state(store, project_id)
    return {"projectId": project_id, "branch": branch, "commitOid": oid, "clean": not bool(porcelain), "changes": porcelain}


def _workstream(store: Any, project_id: str, workstream_id: str) -> dict[str, Any]:
    validate_id(workstream_id, prefix="ws")
    row = store.conn.execute("SELECT * FROM workstreams WHERE project_id=? AND workstream_id=?", (project_id, workstream_id)).fetchone()
    if row is None:
        raise InvalidRequestError("workstream was not found in the secretary project")
    value = dict(row)
    if value["kind"] != "worker":
        raise InvalidRequestError("Git operations require a worker workstream")
    if value["desired_state"] == "retired":
        raise ConflictError("retired workstream cannot be merged")
    return value


def _worker_repository(store: Any, workstream: Mapping[str, Any]) -> Path:
    operation = authoritative_workstream_creation(store, str(workstream["workstream_id"]))
    try:
        scope = json.loads(str(operation["result_json"]))
    except (TypeError, json.JSONDecodeError) as error:
        raise NeedsAttentionError("worker creation scope is invalid") from error
    if not isinstance(scope, dict) or scope.get("workstreamId") != workstream["workstream_id"]:
        raise NeedsAttentionError("worker creation scope does not match the workstream")
    path = Path(str(workstream["worktree_path"])).absolute()
    validate_worker_repository(path, branch_name=str(workstream["branch_name"]), base_oid=str(workstream["base_commit_oid"]), target_branch=str(workstream["target_ref"]).removeprefix("refs/heads/"))
    return path


def _push_oid(value: Any, *, field: str) -> str:
    return validate_git_oid(value, field)


def _push_branch_name(repository: Path, value: Any) -> str:
    branch = bounded_text(value, name="branch", limit=512)
    if branch.startswith("-") or any(ord(char) < 0x20 for char in branch):
        raise InvalidRequestError("push branch is unsafe")
    code, _output = _run_git(repository, "check-ref-format", "--branch", branch, accepted=frozenset({0, 1}))
    if code != 0:
        raise InvalidRequestError("push branch is invalid")
    return branch


def _remote_default_branch(staging: Path, remote_url: str) -> str:
    _code, output = _run_git(staging, "ls-remote", "--symref", "--", remote_url, "HEAD", authenticated=True, timeout=120)
    for line in output.splitlines():
        if line.startswith("ref: ") and line.endswith("\tHEAD"):
            ref = line[5:-5]
            if ref.startswith("refs/heads/"):
                return ref[len("refs/heads/"):]
    raise NeedsAttentionError("registered remote default branch could not be determined")


def push_branch(store: Any, project_id: str, *, branch: Any, expected_local_oid: Any, expected_remote_oid: Any) -> dict[str, Any]:
    project = get_project(store, project_id)
    repository = _repository(project)
    branch_name = _push_branch_name(repository, branch)
    local_oid = _push_oid(expected_local_oid, field="expected_local_oid")
    remote_oid = _push_oid(expected_remote_oid, field="expected_remote_oid")
    registered_remote = project.get("remote_url")
    if not isinstance(registered_remote, str) or not registered_remote:
        raise NeedsAttentionError("registered project has no safe pinned origin remote")
    _code, current_remote = _run_git(repository, "config", "--local", "--get", "remote.origin.url", accepted=frozenset({0, 1}))
    if current_remote.strip() != registered_remote:
        raise NeedsAttentionError("origin remote drifted")
    if _oid(repository, f"refs/heads/{branch_name}") != local_oid:
        raise ConflictError("registered push state moved")
    with tempfile.TemporaryDirectory(prefix="push-", dir=store.state_root) as temporary:
        staging = Path(temporary)
        _run_git(staging, "init", "--bare", "--quiet")
        if branch_name == _remote_default_branch(staging, registered_remote):
            raise InvalidRequestError("autonomous push refuses the remote default branch")
        _run_git(staging, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--", registered_remote, f"refs/heads/{branch_name}:refs/pisec/remote", authenticated=True, timeout=120)
        _run_git(staging, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--", str(repository), f"refs/heads/{branch_name}:refs/pisec/local")
        observed = _oid(staging, "refs/pisec/remote")
        if observed == local_oid:
            return {"projectId": project_id, "branch": branch_name, "previousRemoteOid": remote_oid, "commitOid": local_oid, "pushed": True, "reused": True}
        if observed != remote_oid:
            raise ConflictError("remote branch moved after the push request was prepared")
        code, _ = _run_git(staging, "merge-base", "--is-ancestor", observed, local_oid, accepted=frozenset({0, 1}))
        if code != 0:
            raise ConflictError("local branch is not a fast-forward of the remote branch")
        _run_git(staging, "update-ref", "refs/pisec/local", local_oid)
        _run_git(staging, "push", "--porcelain", f"--force-with-lease=refs/heads/{branch_name}:{remote_oid}", "--", registered_remote, f"refs/pisec/local:refs/heads/{branch_name}", authenticated=True, timeout=120)
        _code, verification = _run_git(staging, "ls-remote", "--heads", "--", registered_remote, f"refs/heads/{branch_name}", authenticated=True, timeout=120)
        if verification.strip() != f"{local_oid}\trefs/heads/{branch_name}":
            raise NeedsAttentionError("remote branch did not reach the expected commit")
    with store.transaction():
        append_event_in_transaction(store.conn, kind="project.git_pushed", project_id=project_id, payload={"remote": "origin", "branch": branch_name, "previousRemoteOid": remote_oid, "commitOid": local_oid, "strategy": "ff-only", "pushedAt": utc_now()})
    return {"projectId": project_id, "branch": branch_name, "previousRemoteOid": remote_oid, "commitOid": local_oid, "pushed": True, "reused": False}


def _comparison(store: Any, project_id: str, workstream_id: str) -> tuple[dict[str, Any], Path, str, str, str, str, bool]:
    workstream = _workstream(store, project_id, workstream_id)
    worker = _worker_repository(store, workstream)
    _project, primary, target_branch, target_oid, porcelain = _primary_state(store, project_id)
    if porcelain:
        raise ConflictError("registered project checkout is dirty")
    target_ref = str(workstream["target_ref"])
    _code, symbolic = _run_git(primary, "rev-parse", "--symbolic-full-name", "--verify", "--end-of-options", target_ref)
    if symbolic.strip() != f"refs/heads/{target_branch}" or _oid(primary, target_ref) != target_oid:
        raise ConflictError("registered checkout is not on the workstream target branch")
    source_branch = str(workstream["branch_name"])
    source_oid = _oid(worker, f"refs/heads/{source_branch}")
    code, _ = _run_git(worker, "merge-base", "--is-ancestor", target_oid, source_oid, accepted=frozenset({0, 1, 128}))
    return workstream, worker, target_branch, target_oid, source_branch, source_oid, code == 0


def inspect_workstream_changes(store: Any, project_id: str, workstream_id: str) -> dict[str, Any]:
    _workstream_value, worker, target_branch, target_oid, source_branch, source_oid, ff_only_ready = _comparison(store, project_id, workstream_id)
    _code, commits = _run_git(worker, "log", "--format=%H%x09%aI%x09%s", "--no-decorate", f"{target_oid}..{source_oid}", max_bytes=64 * 1024)
    _code, stat_text = _run_git(worker, "diff", "--stat", "--no-ext-diff", target_oid, source_oid, max_bytes=64 * 1024)
    _code, patch = _run_git(worker, "diff", "--no-ext-diff", "--no-color", target_oid, source_oid, max_bytes=128 * 1024)
    return {"projectId": project_id, "workstreamId": workstream_id, "targetBranch": target_branch, "targetCommitOid": target_oid, "sourceBranch": source_branch, "sourceCommitOid": source_oid, "fastForwardReady": ff_only_ready, "commits": commits, "diffStat": stat_text, "patch": patch}


def prepare_workstream_merge(store: Any, project_id: str, workstream_id: str) -> dict[str, Any]:
    workstream, worker, target_branch, target_oid, source_branch, source_oid, ff_only_ready = _comparison(store, project_id, workstream_id)
    if not ff_only_ready:
        raise ConflictError("workstream is not a fast-forward of the target branch")
    if workstream["desired_state"] != "completed":
        raise ConflictError("workstream must be completed before merge preparation")
    packet = store.conn.execute("SELECT * FROM completion_packets WHERE workstream_id=?", (workstream_id,)).fetchone()
    if packet is None or packet["source_commit_oid"] != source_oid:
        raise ConflictError("completion packet source commit is stale")
    completion_packet = json.loads(str(packet["packet_json"]))
    project = get_project(store, project_id)
    policy = enforce_merge_policy(project, target_branch=target_branch, completion_packet=completion_packet)
    if "maxChangedFiles" in policy:
        _code, names = _run_git(worker, "diff", "--name-only", "--no-ext-diff", target_oid, source_oid, max_bytes=128 * 1024)
        if len([line for line in names.splitlines() if line]) > policy["maxChangedFiles"]:
            raise ConflictError("merge exceeds the checked project changed-file limit")
    if "maxDiffBytes" in policy:
        _code, patch = _run_git(worker, "diff", "--no-ext-diff", "--no-color", target_oid, source_oid, max_bytes=policy["maxDiffBytes"] + 1)
        if len(patch.encode()) > policy["maxDiffBytes"]:
            raise ConflictError("merge exceeds the checked project diff-size limit")
    return {"kind": "git.merge.ff-only", "projectId": project_id, "workstreamId": workstream_id, "targetBranch": target_branch, "targetCommitOid": target_oid, "sourceBranch": source_branch, "sourceCommitOid": source_oid, "strategy": "ff-only", "completionPacketSha256": packet["packet_sha256"], "completionSourceCommitOid": packet["source_commit_oid"], "effects": [f"advance refs/heads/{target_branch} from {target_oid} to {source_oid}"], "nonEffects": ["no push", "no branch deletion", "no worktree cleanup", "no conflict resolution"]}


def _validate_scope(scope_value: Mapping[str, Any]) -> dict[str, Any]:
    scope = dict(scope_value)
    if set(scope) != _MERGE_SCOPE_FIELDS or scope.get("kind") != "git.merge.ff-only" or scope.get("strategy") != "ff-only":
        raise InvalidRequestError("merge approval scope is invalid")
    validate_id(scope.get("projectId"), prefix="prj")
    validate_id(scope.get("workstreamId"), prefix="ws")
    for field in ("targetCommitOid", "sourceCommitOid", "completionSourceCommitOid"):
        validate_git_oid(scope.get(field), f"merge approval scope {field}")
    validate_sha256(scope.get("completionPacketSha256"), "merge approval scope completion packet digest")
    if scope["completionSourceCommitOid"] != scope["sourceCommitOid"]:
        raise InvalidRequestError("completion source commit does not match merge source")
    return scope


def apply_workstream_merge(store: Any, project_id: str, scope_value: Mapping[str, Any]) -> dict[str, Any]:
    scope = _validate_scope(scope_value)
    if scope["projectId"] != project_id:
        raise ScopeMismatchError("merge approval scope belongs to another project")
    workstream = _workstream(store, project_id, str(scope["workstreamId"]))
    packet = store.conn.execute("SELECT * FROM completion_packets WHERE workstream_id=? AND packet_sha256=?", (scope["workstreamId"], scope["completionPacketSha256"])).fetchone()
    if packet is None or packet["source_commit_oid"] != scope["completionSourceCommitOid"] or workstream["desired_state"] != "completed":
        raise ScopeMismatchError("accepted completion packet no longer matches workstream state")
    project = get_project(store, project_id)
    with project_git_lock(store.state_root, project_id):
        _project, primary, current_branch, current_target, porcelain = _primary_state(store, project_id)
        existing_receipt = store.conn.execute("SELECT * FROM merge_receipts WHERE workstream_id=? AND source_commit_oid=? ORDER BY created_at DESC LIMIT 1", (scope["workstreamId"], scope["sourceCommitOid"])).fetchone()
        if existing_receipt is not None and not porcelain and current_branch == scope["targetBranch"] and current_target == scope["sourceCommitOid"]:
            return {"projectId": project_id, "workstreamId": scope["workstreamId"], "targetBranch": current_branch, "commitOid": current_target, "eventId": existing_receipt["event_id"], "merged": True, "reused": True}
        if porcelain:
            raise ConflictError("project target checkout is dirty")
        if current_branch != scope["targetBranch"] or current_target != scope["targetCommitOid"]:
            raise ConflictError("project target moved after acceptance")
        worker = _worker_repository(store, workstream)
        source_oid = _oid(worker, f"refs/heads/{scope['sourceBranch']}")
        if source_oid != scope["sourceCommitOid"]:
            raise ScopeMismatchError("accepted worker candidate moved")
        candidate_ref = f"refs/pisec/candidates/{new_id('int')}"
        _run_git(primary, "fetch", "--no-tags", "--no-write-fetch-head", "--", str(worker), f"refs/heads/{scope['sourceBranch']}:{candidate_ref}")
        if _oid(primary, candidate_ref) != scope["sourceCommitOid"]:
            raise NeedsAttentionError("candidate import did not reach the accepted commit")
        code, _ = _run_git(primary, "merge-base", "--is-ancestor", scope["targetCommitOid"], candidate_ref, accepted=frozenset({0, 1}))
        if code != 0:
            raise ConflictError("accepted candidate is not a fast-forward")
        _run_git(primary, "merge", "--ff-only", "--no-edit", "--end-of-options", candidate_ref)
        merged_oid = _oid(primary, "HEAD")
        if merged_oid != scope["sourceCommitOid"]:
            raise NeedsAttentionError("Git merge did not reach the approved source commit")
        with store.transaction():
            event = append_event_in_transaction(store.conn, kind="project.git_merged", project_id=project_id, workstream_id=scope["workstreamId"], payload={"targetBranch": scope["targetBranch"], "previousTargetOid": scope["targetCommitOid"], "sourceBranch": scope["sourceBranch"], "sourceCommitOid": scope["sourceCommitOid"], "strategy": "ff-only", "mergedAt": utc_now()})
            store.conn.execute("INSERT INTO merge_receipts(workstream_id,source_commit_oid,target_branch,previous_target_oid,event_id,created_at) VALUES(?,?,?,?,?,?)", (scope["workstreamId"], scope["sourceCommitOid"], scope["targetBranch"], scope["targetCommitOid"], event["event_id"], utc_now()))
        _run_git(primary, "update-ref", "-d", candidate_ref)
    return {"projectId": project_id, "workstreamId": scope["workstreamId"], "targetBranch": scope["targetBranch"], "commitOid": merged_oid, "eventId": event["event_id"], "merged": True, "reused": False}
