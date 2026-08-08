"""Phase 6B exact child lineage and mechanical workspace boundaries.

This slice owns planning and disposable workspace preparation only.  It does
not create a root conversation, mutate controller lifecycle rows, submit
changes, or expose artifact terminal state.  Every child is bound to an
immutable Phase 6A snapshot; there is no cwd/ref fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Any, Callable, Mapping, Sequence

from .errors import InvalidRequestError
from .models import canonical_json, validate_child_source, validate_id
from .snapshot import SnapshotRecord, SnapshotIntegrityError, load_snapshot

_CHILD_ID = re.compile(r"^child_[0-9a-f]{32}$")
_SNAPSHOT_ID = re.compile(r"^snap_[0-9a-f]{32}$")
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 100_000
_READ_ONLY_TOOLS = frozenset({"read", "grep", "find", "ls", "web_search", "fetch_content"})
_MUTATING_TOOLS = frozenset({"write", "edit", "bash", "host_command", "git_write", "git_cleanup", "runtime_create", "runtime_stop"})


class ChildError(RuntimeError):
    """Base class for child planning/workspace errors."""


class ChildLineageError(ChildError):
    """Child identity, parent binding, or source revision is invalid."""


class ChildPermissionError(ChildError, PermissionError):
    """A read-only child attempted a mutation-capable operation."""


class ChildWorkspaceError(ChildError):
    """A disposable child workspace could not be prepared safely."""


@dataclass(frozen=True)
class ChildRunPlan:
    child_id: str
    parent_run_id: str
    parent_conversation_id: str
    snapshot_id: str
    snapshot_ref: str
    snapshot_commit_oid: str
    snapshot_tree_oid: str
    repository_path: str
    authority: str
    parent_working_copy_id: str | None
    child_working_copy_id: str | None
    source_head_oid: str | None
    source_tree_oid: str | None
    plan_digest: str

    @property
    def read_only(self) -> bool:
        return self.authority == "read-only"

    def source_dict(self) -> dict[str, Any]:
        return {
            "snapshotId": self.snapshot_id,
            "snapshotRef": self.snapshot_ref,
            "snapshotCommitOid": self.snapshot_commit_oid,
            "snapshotTreeOid": self.snapshot_tree_oid,
            "sourceHeadOid": self.source_head_oid,
            "sourceTreeOid": self.source_tree_oid,
            "authority": self.authority,
        }

    def as_dict(self) -> dict[str, Any]:
        value = {
            "schemaVersion": 1,
            "childId": self.child_id,
            "parentRunId": self.parent_run_id,
            "parentConversationId": self.parent_conversation_id,
            "snapshotId": self.snapshot_id,
            "snapshotRef": self.snapshot_ref,
            "snapshotCommitOid": self.snapshot_commit_oid,
            "snapshotTreeOid": self.snapshot_tree_oid,
            "repositoryPath": self.repository_path,
            "authority": self.authority,
            "parentWorkingCopyId": self.parent_working_copy_id,
            "childWorkingCopyId": self.child_working_copy_id,
            "sourceHeadOid": self.source_head_oid,
            "sourceTreeOid": self.source_tree_oid,
        }
        value["planDigest"] = _plan_digest(value)
        return value


@dataclass(frozen=True)
class ChildWorkspace:
    plan: ChildRunPlan
    path: str
    writable: bool
    _repository_path: str
    _worktree: bool = False

    def assert_tool(self, tool_name: str, *, mutating: bool | None = None) -> None:
        """Check a tool call at the child boundary before executing it."""

        if not isinstance(tool_name, str) or not tool_name or len(tool_name) > 128:
            raise ChildPermissionError("child tool name is invalid")
        inferred_mutation = tool_name in _MUTATING_TOOLS
        is_mutating = inferred_mutation if mutating is None else bool(mutating)
        if self.plan.read_only and (is_mutating or tool_name not in _READ_ONLY_TOOLS):
            raise ChildPermissionError(f"read-only child cannot use tool: {tool_name}")
        if not self.plan.read_only and tool_name not in _READ_ONLY_TOOLS | _MUTATING_TOOLS:
            raise ChildPermissionError(f"child tool is not allowlisted: {tool_name}")


@dataclass(frozen=True)
class ChildCommandResult:
    state: str
    returncode: int | None
    stdout: str
    stderr: str


def _validate_child_id(value: str) -> str:
    if not isinstance(value, str) or _CHILD_ID.fullmatch(value) is None:
        raise ChildLineageError("child_id must be child_ followed by 32 lowercase hex characters")
    return value


def _validate_snapshot_id(value: str) -> str:
    if not isinstance(value, str) or _SNAPSHOT_ID.fullmatch(value) is None:
        raise ChildLineageError("child snapshot ID is invalid")
    return value


def _plan_digest(value: Mapping[str, Any]) -> str:
    body = dict(value)
    body.pop("planDigest", None)
    return "sha256:" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def _snapshot_value(snapshot: SnapshotRecord, name: str) -> Any:
    if not isinstance(snapshot, SnapshotRecord):
        raise TypeError("child planning requires a SnapshotRecord")
    return getattr(snapshot, name)


def _validate_parent_ids(parent_run_id: str, parent_conversation_id: str) -> None:
    try:
        validate_id(parent_run_id, prefix="run")
        validate_id(parent_conversation_id, prefix="conv")
    except ValueError as error:
        raise ChildLineageError("child requires exact parent run and conversation IDs") from error


def plan_read_only_child(
    snapshot: SnapshotRecord,
    *,
    child_id: str,
    parent_run_id: str,
    parent_conversation_id: str,
    parent_working_copy_id: str | None = None,
    expected_parent_head_oid: str | None = None,
    expected_parent_tree_oid: str | None = None,
) -> ChildRunPlan:
    """Bind a report/read-only child to one immutable snapshot revision."""

    _validate_child_id(child_id)
    _validate_parent_ids(parent_run_id, parent_conversation_id)
    _validate_snapshot_id(snapshot.snapshot_id)
    if parent_working_copy_id is not None:
        try:
            validate_id(parent_working_copy_id, prefix="wc")
        except ValueError as error:
            raise ChildLineageError("parent working-copy ID is invalid") from error
    if expected_parent_head_oid is not None and expected_parent_head_oid != snapshot.source_head_oid:
        raise ChildLineageError("child parent HEAD does not match the immutable snapshot")
    if expected_parent_tree_oid is not None and expected_parent_tree_oid != snapshot.source_tree_oid:
        raise ChildLineageError("child parent tree does not match the immutable snapshot")
    if snapshot.ref_name != f"refs/pi/snapshots/{snapshot.snapshot_id}":
        raise ChildLineageError("child snapshot ref is not bound to snapshot ID")
    value = {
        "schemaVersion": 1,
        "childId": child_id,
        "parentRunId": parent_run_id,
        "parentConversationId": parent_conversation_id,
        "snapshotId": snapshot.snapshot_id,
        "snapshotRef": snapshot.ref_name,
        "snapshotCommitOid": snapshot.snapshot_commit_oid,
        "snapshotTreeOid": snapshot.snapshot_tree_oid,
        "repositoryPath": snapshot.repository_path,
        "authority": "read-only",
        "parentWorkingCopyId": parent_working_copy_id,
        "childWorkingCopyId": None,
        "sourceHeadOid": snapshot.source_head_oid,
        "sourceTreeOid": snapshot.source_tree_oid,
    }
    plan = ChildRunPlan(**{
        "child_id": child_id,
        "parent_run_id": parent_run_id,
        "parent_conversation_id": parent_conversation_id,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_ref": snapshot.ref_name,
        "snapshot_commit_oid": snapshot.snapshot_commit_oid,
        "snapshot_tree_oid": snapshot.snapshot_tree_oid,
        "repository_path": snapshot.repository_path,
        "authority": "read-only",
        "parent_working_copy_id": parent_working_copy_id,
        "child_working_copy_id": None,
        "source_head_oid": snapshot.source_head_oid,
        "source_tree_oid": snapshot.source_tree_oid,
        "plan_digest": _plan_digest(value),
    })
    return validate_child_plan(plan)


def plan_writer_child(
    snapshot: SnapshotRecord,
    *,
    child_id: str,
    parent_run_id: str,
    parent_conversation_id: str,
    parent_working_copy_id: str,
    child_working_copy_id: str,
    expected_parent_head_oid: str | None = None,
) -> ChildRunPlan:
    """Bind an exclusive writer child to a distinct working-copy identity."""

    if parent_working_copy_id == child_working_copy_id:
        raise ChildLineageError("writer child must use a distinct working copy")
    try:
        validate_id(parent_working_copy_id, prefix="wc")
        validate_id(child_working_copy_id, prefix="wc")
    except ValueError as error:
        raise ChildLineageError("writer child working-copy IDs are invalid") from error
    read_only = plan_read_only_child(
        snapshot,
        child_id=child_id,
        parent_run_id=parent_run_id,
        parent_conversation_id=parent_conversation_id,
        parent_working_copy_id=parent_working_copy_id,
        expected_parent_head_oid=expected_parent_head_oid,
    )
    writer = replace(read_only, authority="writer", child_working_copy_id=child_working_copy_id, plan_digest="")
    return validate_child_plan(replace(writer, plan_digest=writer.as_dict()["planDigest"]))


def validate_child_plan(plan: ChildRunPlan) -> ChildRunPlan:
    """Fail closed on a serialized/transported child plan."""

    if not isinstance(plan, ChildRunPlan):
        raise TypeError("child plan has the wrong type")
    _validate_child_id(plan.child_id)
    _validate_parent_ids(plan.parent_run_id, plan.parent_conversation_id)
    _validate_snapshot_id(plan.snapshot_id)
    if plan.parent_working_copy_id is not None:
        try:
            validate_id(plan.parent_working_copy_id, prefix="wc")
        except ValueError as error:
            raise ChildLineageError("child parent working-copy ID is invalid") from error
    if plan.snapshot_ref != f"refs/pi/snapshots/{plan.snapshot_id}":
        raise ChildLineageError("child plan snapshot ref is not exact")
    if plan.authority not in {"read-only", "writer"}:
        raise ChildLineageError("child authority is invalid")
    try:
        validate_child_source(plan.source_dict())
    except InvalidRequestError as error:
        raise ChildLineageError("child source binding is invalid") from error
    if plan.authority == "read-only" and plan.child_working_copy_id is not None:
        raise ChildLineageError("read-only child cannot claim a writable working copy")
    if plan.authority == "writer":
        if plan.child_working_copy_id is None or plan.parent_working_copy_id == plan.child_working_copy_id:
            raise ChildLineageError("writer child working-copy binding is invalid")
        try:
            validate_id(plan.child_working_copy_id, prefix="wc")
        except ValueError as error:
            raise ChildLineageError("writer child working-copy ID is invalid") from error
    expected = dict(plan.as_dict())
    if expected["planDigest"] != plan.plan_digest:
        raise ChildLineageError("child plan digest is invalid")
    return plan


def _safe_environment() -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "GIT_EDITOR": "true",
        "GIT_ASKPASS": "true",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _git_process(repository: Path, args: Sequence[str], *, timeout: float = 30.0) -> tuple[str, str]:
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise ChildWorkspaceError("Git executable is unavailable")
    command = [executable, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.sshCommand=", "-c", "credential.helper=", *map(str, args)]
    process = subprocess.Popen(command, cwd=str(repository), env=_safe_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", shell=False)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired as stubborn:
            raise ChildWorkspaceError("child Git helper ignored graceful termination") from stubborn
        raise ChildWorkspaceError("child Git helper timed out") from error
    if process.returncode != 0:
        raise ChildWorkspaceError(f"child Git command failed: {stderr.strip()[:512]}")
    return stdout, stderr


def _safe_member_path(root: Path, name: str) -> Path:
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts or "" in pure.parts:
        raise ChildWorkspaceError("snapshot archive contains an unsafe path")
    target = root.joinpath(*pure.parts)
    if root not in target.resolve().parents and target.resolve() != root.resolve():
        raise ChildWorkspaceError("snapshot archive escapes workspace")
    return target


def _extract_archive(repository: Path, ref: str, workspace: Path) -> None:
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise ChildWorkspaceError("Git executable is unavailable")
    command = [executable, "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null", "-c", "core.excludesFile=/dev/null", "archive", "--format=tar", ref]
    process = subprocess.Popen(command, cwd=str(repository), env=_safe_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    archive_path = workspace.parent / ".snapshot.tar"
    total = 0
    try:
        with archive_path.open("wb") as stream:
            while True:
                block = process.stdout.read(1024 * 1024) if process.stdout is not None else b""
                if not block:
                    break
                total += len(block)
                if total > _MAX_ARCHIVE_BYTES:
                    process.terminate()
                    raise ChildWorkspaceError("child snapshot archive exceeds its bound")
                stream.write(block)
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr is not None else ""
        code = process.wait(timeout=30)
        if code != 0:
            raise ChildWorkspaceError(f"snapshot archive failed: {stderr[:512]}")
        with tarfile.open(archive_path, mode="r:") as archive:
            members = archive.getmembers()
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise ChildWorkspaceError("child snapshot member bound exceeded")
            for member in members:
                target = _safe_member_path(workspace, member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.issym():
                    link_target = PurePosixPath(member.linkname)
                    if link_target.is_absolute() or ".." in link_target.parts:
                        raise ChildWorkspaceError("snapshot archive contains an escaping symlink")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(member.linkname, target)
                elif member.isreg():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(descriptor, "wb") as stream:
                        source = archive.extractfile(member)
                        if source is None:
                            raise ChildWorkspaceError("snapshot archive member has no content")
                        shutil.copyfileobj(source, stream, length=1024 * 1024)
                else:
                    raise ChildWorkspaceError("snapshot archive contains a special member")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                raise ChildWorkspaceError("snapshot archive helper ignored graceful termination")
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        try:
            archive_path.unlink()
        except FileNotFoundError:
            pass


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            mode = stat.S_IMODE(info.st_mode)
            os.chmod(path, mode & ~0o222, follow_symlinks=False)
        except OSError as error:
            raise ChildWorkspaceError(f"could not make child workspace read-only: {path}") from error
    os.chmod(root, stat.S_IMODE(root.stat().st_mode) & ~0o222)


class ChildExecutor:
    """Prepare and release disposable exact-source child workspaces."""

    def __init__(self, state_root: os.PathLike[str] | str):
        self.state_root = Path(state_root).expanduser().resolve()
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_root, 0o700)

    def prepare(self, plan: ChildRunPlan) -> ChildWorkspace:
        validate_child_plan(plan)
        repository = Path(plan.repository_path).expanduser().resolve(strict=True)
        if not repository.is_dir():
            raise ChildWorkspaceError("child repository is unavailable")
        # Re-read the immutable manifest/ref at launch; a transported plan is
        # not authority by itself and cannot silently rebind to current HEAD.
        record = load_snapshot(self.state_root, plan.snapshot_id, repository)
        if (
            record.ref_name != plan.snapshot_ref
            or record.snapshot_commit_oid != plan.snapshot_commit_oid
            or record.snapshot_tree_oid != plan.snapshot_tree_oid
            or record.source_head_oid != plan.source_head_oid
            or record.source_tree_oid != plan.source_tree_oid
        ):
            raise ChildLineageError("child snapshot source changed after planning")
        child_root = self.state_root / "children" / plan.child_id
        if child_root.exists() or child_root.is_symlink():
            raise ChildWorkspaceError("child workspace already exists")
        child_root.mkdir(parents=True, mode=0o700)
        workspace = child_root / ("readonly" if plan.read_only else "worktree")
        try:
            if plan.read_only:
                workspace.mkdir(mode=0o700)
                _extract_archive(repository, plan.snapshot_ref, workspace)
                _make_read_only(workspace)
                return ChildWorkspace(plan, str(workspace), False, str(repository), False)
            workspace.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _git_process(repository, ["worktree", "add", "--detach", str(workspace), plan.snapshot_ref])
            observed, _ = _git_process(workspace, ["rev-parse", "HEAD"])
            if observed.strip() != plan.snapshot_commit_oid:
                raise ChildLineageError("writer child worktree is not at the planned snapshot")
            return ChildWorkspace(plan, str(workspace), True, str(repository), True)
        except Exception:
            shutil.rmtree(child_root, ignore_errors=True)
            raise

    def release(self, workspace: ChildWorkspace) -> None:
        path = Path(workspace.path)
        if not path.exists() and not path.is_symlink():
            return
        if workspace._worktree:
            _git_process(Path(workspace._repository_path), ["worktree", "remove", str(path)])
            shutil.rmtree(path.parent, ignore_errors=True)
            return
        for item in sorted(path.rglob("*"), key=lambda value: len(value.parts), reverse=True):
            try:
                os.chmod(item, stat.S_IMODE(item.lstat().st_mode) | 0o200, follow_symlinks=False)
            except OSError:
                pass
        os.chmod(path, stat.S_IMODE(path.stat().st_mode) | 0o200)
        shutil.rmtree(path.parent)

    def run_command(self, workspace: ChildWorkspace, command: Sequence[str], *, timeout: float = 30.0) -> ChildCommandResult:
        if not command or any(not isinstance(item, str) or not item or "\x00" in item for item in command):
            raise ChildError("child command must be a non-empty argument vector")
        process = subprocess.Popen(list(command), cwd=workspace.path, env={"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C"}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", shell=False)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired as stubborn:
                raise ChildError("child command ignored graceful termination") from stubborn
            raise ChildError("child command timed out") from error
        return ChildCommandResult("succeeded" if process.returncode == 0 else "failed", process.returncode, stdout[:512 * 1024], stderr[:512 * 1024])


__all__ = [
    "ChildCommandResult", "ChildError", "ChildExecutor", "ChildLineageError",
    "ChildPermissionError", "ChildRunPlan", "ChildWorkspace", "ChildWorkspaceError",
    "plan_read_only_child", "plan_writer_child", "validate_child_plan",
]
