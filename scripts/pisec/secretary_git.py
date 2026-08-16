"""Project-scoped Git inspection and fail-closed fast-forward merge operations."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
from typing import Any, Mapping

from .events import append_event_in_transaction
from .models import ConflictError, InvalidRequestError, NeedsAttentionError, ScopeMismatchError, bounded_text, canonical_json, utc_now, validate_id
from .projects import get_project

_OID_LENGTHS = frozenset({40, 64})
_MERGE_SCOPE_FIELDS = frozenset({
    "kind",
    "projectId",
    "workstreamId",
    "targetBranch",
    "targetCommitOid",
    "sourceBranch",
    "sourceCommitOid",
    "strategy",
    "effects",
    "nonEffects",
})


def _run_git(
    path: Path,
    *args: str,
    accepted: frozenset[int] = frozenset({0}),
    max_bytes: int = 128 * 1024,
    alternate_objects: tuple[Path, ...] = (),
    input_text: str | None = None,
) -> tuple[int, str]:
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise InvalidRequestError("git is unavailable")
    environment = {
        "HOME": "/nonexistent",
        "PATH": os.defpath,
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if alternate_objects:
        values = [str(item) for item in alternate_objects]
        if any(os.pathsep in value for value in values):
            raise NeedsAttentionError("Git object path cannot be represented safely")
        environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = os.pathsep.join(values)
    result = subprocess.run(
        [executable, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", str(path), *args],
        env=environment,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode not in accepted:
        raise ConflictError(
            "Git operation refused",
            detail={"command": args[0], "stderr": result.stderr.strip()[:512]},
        )
    encoded = result.stdout.encode("utf-8")
    if len(encoded) > max_bytes:
        raise InvalidRequestError("Git output is too large", detail={"command": args[0], "maxBytes": max_bytes})
    return result.returncode, result.stdout.rstrip("\n")


def _oid(path: Path, revision: str, *, alternate_objects: tuple[Path, ...] = ()) -> str:
    _code, value = _run_git(
        path,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{revision}^{{commit}}",
        alternate_objects=alternate_objects,
    )
    oid = value.strip().lower()
    if len(oid) not in _OID_LENGTHS or any(char not in "0123456789abcdef" for char in oid):
        raise NeedsAttentionError("Git returned an invalid commit object id")
    return oid


def _repository(project: Mapping[str, Any]) -> Path:
    path = Path(str(project["repository_path"]))
    try:
        info = path.lstat()
        canonical = path.resolve(strict=True)
    except OSError as error:
        raise NeedsAttentionError("registered project checkout is unavailable") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise NeedsAttentionError("registered project checkout is unsafe")
    if canonical != path:
        raise NeedsAttentionError("registered project checkout identity drifted")
    _code, common_value = _run_git(canonical, "rev-parse", "--git-common-dir")
    observed_common = Path(common_value)
    if not observed_common.is_absolute():
        observed_common = canonical / observed_common
    if observed_common.resolve(strict=True) != Path(str(project["git_common_dir"])).resolve(strict=True):
        raise NeedsAttentionError("registered project Git identity drifted")
    return canonical


def _primary_state(store: Any, project_id: str) -> tuple[dict[str, Any], Path, str, str, str]:
    project = get_project(store, project_id)
    repository = _repository(project)
    _code, target_branch = _run_git(repository, "symbolic-ref", "--quiet", "--short", "HEAD")
    target_branch = bounded_text(target_branch.strip(), name="target_branch", limit=512)
    if not target_branch or target_branch.startswith("-") or any(ord(char) < 0x20 for char in target_branch):
        raise NeedsAttentionError("registered project checkout has an unsafe branch")
    target_oid = _oid(repository, "HEAD")
    _code, porcelain = _run_git(repository, "status", "--porcelain=v1", "--untracked-files=all", max_bytes=64 * 1024)
    return project, repository, target_branch, target_oid, porcelain


def git_status(store: Any, project_id: str) -> dict[str, Any]:
    _project, _repository_path, branch, oid, porcelain = _primary_state(store, project_id)
    return {
        "projectId": project_id,
        "branch": branch,
        "commitOid": oid,
        "clean": not bool(porcelain),
        "changes": porcelain,
    }


def _workstream(store: Any, project_id: str, workstream_id: str) -> dict[str, Any]:
    validate_id(workstream_id, prefix="ws")
    row = store.conn.execute(
        "SELECT * FROM workstreams WHERE project_id=? AND workstream_id=?",
        (project_id, workstream_id),
    ).fetchone()
    if row is None:
        raise InvalidRequestError("workstream was not found in the secretary project")
    value = dict(row)
    if value["kind"] != "worker":
        raise InvalidRequestError("Git operations require a worker workstream")
    if value["desired_state"] == "retired":
        raise ConflictError("retired workstream cannot be merged")
    return value


def _private_objects(store: Any, workstream: Mapping[str, Any]) -> Path:
    binding = store.conn.execute(
        "SELECT private_git_object_dir FROM runtime_bindings WHERE workstream_id=?",
        (workstream["workstream_id"],),
    ).fetchone()
    raw_path = None if binding is None else binding["private_git_object_dir"]
    if raw_path is None:
        operation = store.conn.execute(
            "SELECT result_json FROM operations WHERE kind='workstream.create' AND workstream_id=? ORDER BY created_at LIMIT 1",
            (workstream["workstream_id"],),
        ).fetchone()
        if operation is None or operation["result_json"] is None:
            raise NeedsAttentionError("worker Git object binding is unavailable")
        try:
            scope = json.loads(operation["result_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise NeedsAttentionError("worker Git object binding is invalid") from error
        if not isinstance(scope, dict) or scope.get("workstreamId") != workstream["workstream_id"]:
            raise NeedsAttentionError("worker Git object binding does not match the workstream")
        raw_path = scope.get("privateGitObjectDir")
    if not isinstance(raw_path, str) or not raw_path:
        raise NeedsAttentionError("worker Git object binding is invalid")
    path = Path(raw_path).absolute()
    try:
        info = path.lstat()
        canonical = path.resolve(strict=True)
    except OSError as error:
        raise NeedsAttentionError("worker Git object store is unavailable") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
        or canonical != path
    ):
        raise NeedsAttentionError("worker Git object store is unsafe")
    if os.pathsep in str(canonical):
        raise NeedsAttentionError("worker Git object store path is unsupported")
    return canonical


def _promote_worker_objects(
    project: Mapping[str, Any],
    repository: Path,
    workstream_id: str,
    private_objects: Path,
    target_oid: str,
    source_oid: str,
) -> None:
    available, _output = _run_git(
        repository,
        "cat-file",
        "-e",
        f"{source_oid}^{{commit}}",
        accepted=frozenset({0, 1, 128}),
    )
    if available == 0:
        return
    common_objects = (Path(str(project["git_common_dir"])) / "objects").absolute()
    try:
        common_info = common_objects.lstat()
        canonical_common = common_objects.resolve(strict=True)
    except OSError as error:
        raise NeedsAttentionError("project Git object store is unavailable") from error
    if (
        stat.S_ISLNK(common_info.st_mode)
        or not stat.S_ISDIR(common_info.st_mode)
        or common_info.st_uid != os.geteuid()
        or common_info.st_mode & 0o022
        or canonical_common != common_objects
    ):
        raise NeedsAttentionError("project Git object store is unsafe")
    pack_dir = canonical_common / "pack"
    if pack_dir.is_symlink():
        raise NeedsAttentionError("project Git pack directory is unsafe")
    pack_dir.mkdir(mode=0o700, exist_ok=True)
    pack_info = pack_dir.lstat()
    if not stat.S_ISDIR(pack_info.st_mode) or pack_info.st_uid != os.geteuid() or pack_info.st_mode & 0o022:
        raise NeedsAttentionError("project Git pack directory is unsafe")
    _code, pack_hash = _run_git(
        repository,
        "pack-objects",
        "--revs",
        str(pack_dir / f"pisec-{workstream_id}"),
        alternate_objects=(private_objects,),
        input_text=f"{source_oid}\n^{target_oid}\n",
        max_bytes=4096,
    )
    if len(pack_hash) not in _OID_LENGTHS or any(char not in "0123456789abcdef" for char in pack_hash):
        raise NeedsAttentionError("Git returned an invalid promoted pack id")
    promoted, _output = _run_git(
        repository,
        "cat-file",
        "-e",
        f"{source_oid}^{{commit}}",
        accepted=frozenset({0, 1, 128}),
    )
    if promoted != 0:
        raise NeedsAttentionError("worker Git objects were not promoted into the project store")


def _comparison(
    store: Any,
    project_id: str,
    workstream_id: str,
) -> tuple[dict[str, Any], Path, str, str, str, str, bool, Path]:
    workstream = _workstream(store, project_id, workstream_id)
    private_objects = _private_objects(store, workstream)
    _project, repository, target_branch, target_oid, porcelain = _primary_state(store, project_id)
    if porcelain:
        raise ConflictError("registered project checkout is dirty")
    target_ref = str(workstream["target_ref"])
    _code, symbolic_target = _run_git(repository, "rev-parse", "--symbolic-full-name", "--verify", "--end-of-options", target_ref)
    if symbolic_target.strip() != f"refs/heads/{target_branch}" or _oid(repository, target_ref) != target_oid:
        raise ConflictError("registered checkout is not on the workstream target branch")
    source_branch = str(workstream["branch_name"])
    source_oid = _oid(
        repository,
        f"refs/heads/{source_branch}",
        alternate_objects=(private_objects,),
    )
    ancestor_code, _output = _run_git(
        repository,
        "merge-base",
        "--is-ancestor",
        target_oid,
        source_oid,
        accepted=frozenset({0, 1}),
        alternate_objects=(private_objects,),
    )
    return workstream, repository, target_branch, target_oid, source_branch, source_oid, ancestor_code == 0, private_objects


def inspect_workstream_changes(store: Any, project_id: str, workstream_id: str) -> dict[str, Any]:
    _workstream_value, repository, target_branch, target_oid, source_branch, source_oid, ff_only_ready, private_objects = _comparison(store, project_id, workstream_id)
    alternates = (private_objects,)
    _code, commits = _run_git(repository, "log", "--format=%H%x09%aI%x09%s", "--no-decorate", f"{target_oid}..{source_oid}", max_bytes=64 * 1024, alternate_objects=alternates)
    _code, stat_text = _run_git(repository, "diff", "--stat", "--no-ext-diff", target_oid, source_oid, max_bytes=64 * 1024, alternate_objects=alternates)
    _code, patch = _run_git(repository, "diff", "--no-ext-diff", "--no-color", target_oid, source_oid, max_bytes=128 * 1024, alternate_objects=alternates)
    return {
        "projectId": project_id,
        "workstreamId": workstream_id,
        "targetBranch": target_branch,
        "targetCommitOid": target_oid,
        "sourceBranch": source_branch,
        "sourceCommitOid": source_oid,
        "fastForwardReady": ff_only_ready,
        "commits": commits,
        "diffStat": stat_text,
        "patch": patch,
    }


def prepare_workstream_merge(store: Any, project_id: str, workstream_id: str) -> dict[str, Any]:
    _workstream_value, _repository_path, target_branch, target_oid, source_branch, source_oid, ff_only_ready, _private_objects_path = _comparison(store, project_id, workstream_id)
    if not ff_only_ready:
        raise ConflictError("workstream is not a fast-forward of the target branch")
    return {
        "kind": "git.merge.ff-only",
        "projectId": project_id,
        "workstreamId": workstream_id,
        "targetBranch": target_branch,
        "targetCommitOid": target_oid,
        "sourceBranch": source_branch,
        "sourceCommitOid": source_oid,
        "strategy": "ff-only",
        "effects": [f"advance refs/heads/{target_branch} from {target_oid} to {source_oid}"],
        "nonEffects": ["no push", "no branch deletion", "no worktree cleanup", "no conflict resolution"],
    }


def _validate_scope(scope_value: Mapping[str, Any]) -> dict[str, Any]:
    scope = dict(scope_value)
    if set(scope) != _MERGE_SCOPE_FIELDS:
        raise InvalidRequestError("merge approval scope fields do not match the contract")
    if scope.get("kind") != "git.merge.ff-only" or scope.get("strategy") != "ff-only":
        raise InvalidRequestError("merge approval scope strategy is invalid")
    validate_id(scope.get("projectId"), prefix="prj")
    validate_id(scope.get("workstreamId"), prefix="ws")
    for field in ("targetBranch", "sourceBranch"):
        bounded_text(scope.get(field), name=field, limit=512)
    for field in ("targetCommitOid", "sourceCommitOid"):
        value = scope.get(field)
        if not isinstance(value, str) or len(value) not in _OID_LENGTHS or any(char not in "0123456789abcdef" for char in value):
            raise InvalidRequestError("merge approval scope contains an invalid commit id")
    for field in ("effects", "nonEffects"):
        values = scope.get(field)
        if not isinstance(values, list) or not values or any(not isinstance(value, str) or not value or len(value) > 1024 for value in values):
            raise InvalidRequestError("merge approval scope effects are invalid")
    canonical_json(scope, max_bytes=16 * 1024, max_text=4096)
    return scope


def apply_workstream_merge(store: Any, project_id: str, scope_value: Mapping[str, Any]) -> dict[str, Any]:
    scope = _validate_scope(scope_value)
    if scope["projectId"] != project_id:
        raise ScopeMismatchError("merge approval scope belongs to another project")
    workstream_id = str(scope["workstreamId"])
    workstream = _workstream(store, project_id, workstream_id)
    private_objects = _private_objects(store, workstream)
    project = get_project(store, project_id)
    repository = _repository(project)
    _code, current_branch = _run_git(repository, "symbolic-ref", "--quiet", "--short", "HEAD")
    current_branch = current_branch.strip()
    current_target_oid = _oid(repository, "HEAD")
    current_source_oid = _oid(
        repository,
        f"refs/heads/{workstream['branch_name']}",
        alternate_objects=(private_objects,),
    )
    if (
        current_branch == scope["targetBranch"]
        and str(workstream["branch_name"]) == scope["sourceBranch"]
        and current_target_oid == scope["sourceCommitOid"]
        and current_source_oid == scope["sourceCommitOid"]
    ):
        return {
            "projectId": project_id,
            "workstreamId": workstream_id,
            "targetBranch": current_branch,
            "commitOid": current_target_oid,
            "merged": True,
            "reused": True,
        }
    expected = prepare_workstream_merge(store, project_id, workstream_id)
    if canonical_json(scope) != canonical_json(expected):
        raise ScopeMismatchError("merge approval scope no longer matches Git state")
    _promote_worker_objects(
        project,
        repository,
        workstream_id,
        private_objects,
        scope["targetCommitOid"],
        scope["sourceCommitOid"],
    )
    _run_git(repository, "merge", "--ff-only", "--no-edit", "--end-of-options", f"refs/heads/{scope['sourceBranch']}")
    merged_oid = _oid(repository, "HEAD")
    if merged_oid != scope["sourceCommitOid"]:
        raise NeedsAttentionError("Git merge did not reach the approved source commit")
    with store.transaction():
        append_event_in_transaction(
            store.conn,
            kind="project.git_merged",
            project_id=project_id,
            workstream_id=workstream_id,
            payload={
                "targetBranch": scope["targetBranch"],
                "previousTargetOid": scope["targetCommitOid"],
                "sourceBranch": scope["sourceBranch"],
                "sourceCommitOid": scope["sourceCommitOid"],
                "strategy": "ff-only",
                "mergedAt": utc_now(),
            },
        )
    return {
        "projectId": project_id,
        "workstreamId": workstream_id,
        "targetBranch": scope["targetBranch"],
        "commitOid": merged_oid,
        "merged": True,
        "reused": False,
    }
