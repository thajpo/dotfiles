"""Controller-created read-only snapshots for exact-revision reviewer roles."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from typing import Any

from .conversations import create_conversation
from .models import new_id, utc_now, validate_id


class ReviewAssignmentError(RuntimeError):
    pass


def _environment() -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "XDG_CONFIG_HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ASKPASS": "false",
        "SSH_ASKPASS": "false",
        "TMPDIR": tempfile.gettempdir(),
    }


def _git(repository: Path, args: list[str]) -> str:
    git = shutil.which("git", path=os.defpath)
    if git is None:
        raise ReviewAssignmentError("Git is unavailable")
    command = [
        git,
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.fsmonitor=false",
        "-c", "core.sshCommand=false",
        "-c", "credential.helper=",
        "-c", "protocol.allow=never",
        *args,
    ]
    try:
        result = subprocess.run(command, cwd=repository, env=_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False, shell=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReviewAssignmentError("review snapshot Git operation was unavailable") from error
    if len(result.stdout.encode()) + len(result.stderr.encode()) > 512 * 1024:
        raise ReviewAssignmentError("review snapshot Git output exceeded its bound")
    if result.returncode != 0:
        raise ReviewAssignmentError(result.stderr.strip()[:1024] or "review snapshot Git operation failed")
    return result.stdout.strip()


def _make_tree_read_only(root: Path) -> None:
    for parent, directories, files in os.walk(root, topdown=False, followlinks=False):
        current = Path(parent)
        for name in files:
            path = current / name
            info = path.lstat()
            if not stat.S_ISLNK(info.st_mode):
                os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o222, follow_symlinks=False)
        for name in directories:
            path = current / name
            info = path.lstat()
            if not stat.S_ISLNK(info.st_mode):
                os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o222, follow_symlinks=False)
    os.chmod(root, stat.S_IMODE(root.lstat().st_mode) & ~0o222)


def create_review_assignment(store: Any, *, change_id: str, revision: int) -> dict[str, Any]:
    validate_id(change_id, prefix="chg")
    change = store.conn.execute("SELECT * FROM changes WHERE change_id=? AND current_revision=?", (change_id, revision)).fetchone()
    if change is None:
        raise ReviewAssignmentError("review must bind the current exact revision")
    revision_row = store.conn.execute("SELECT * FROM change_revisions WHERE change_id=? AND revision=?", (change_id, revision)).fetchone()
    if revision_row is None:
        raise ReviewAssignmentError("review source is unavailable")
    if change["source_working_copy_id"] is not None:
        source = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (change["source_working_copy_id"],)).fetchone()
        if source is None or source["project_id"] != change["project_id"]:
            raise ReviewAssignmentError("review source is unavailable")
        source_path = Path(source["path"])
    else:
        # Controller-created integration results have no source working copy;
        # snapshot the exact revision from the project primary repository.
        primary = store.conn.execute(
            "SELECT * FROM working_copies WHERE project_id=? AND kind='primary' AND desired_state='present' ORDER BY created_at LIMIT 1",
            (change["project_id"],),
        ).fetchone()
        if primary is None:
            raise ReviewAssignmentError("review source is unavailable")
        source_path = Path(primary["path"])
    wc_id = new_id("wc")
    path = Path(store.state_root) / "reviews" / change_id / str(revision) / wc_id
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ReviewAssignmentError("derived review snapshot path already exists")
    _git(source_path, ["worktree", "add", "--detach", str(path), revision_row["tip_oid"]])
    try:
        observed = _git(path, ["rev-parse", "HEAD", "HEAD^{tree}"]).splitlines()
        branch = _git(path, ["rev-parse", "--abbrev-ref", "HEAD"])
        if observed != [revision_row["tip_oid"], revision_row["tree_oid"]] or branch != "HEAD":
            raise ReviewAssignmentError("review snapshot is not detached at the exact revision")
        if _git(path, ["status", "--porcelain=v2", "--untracked-files=all"]):
            raise ReviewAssignmentError("review snapshot is not clean")
        _make_tree_read_only(path)
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,git_dir,branch_ref,expected_head_oid,expected_tree_oid,effective_mode,desired_state,observed_state,writer_epoch,active_writer_run_id,resource_version,controller_owned,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wc_id, change["project_id"], f"review {change_id} r{revision}", "review", "review", str(path), str(path / ".git"), None, revision_row["tip_oid"], revision_row["tree_oid"], "read-only", "present", "ready", 0, None, 1, 1, now, now, now, None, None),
        )
    conversation = create_conversation(store, project_id=change["project_id"], role="reviewer", display_name=f"review {change_id} r{revision}", working_copy_id=wc_id)
    with store.transaction():
        store.conn.execute("UPDATE conversations SET observed_state='ready',updated_at=? WHERE conversation_id=?", (utc_now(), conversation["conversation_id"]))
    return {
        "changeId": change_id,
        "revision": revision,
        "tipOid": revision_row["tip_oid"],
        "treeOid": revision_row["tree_oid"],
        "workingCopyId": wc_id,
        "conversationId": conversation["conversation_id"],
        "piSessionId": conversation["pi_session_id"],
        "sessionFile": conversation["session_file"],
        "path": str(path),
        "readOnly": True,
        "detached": True,
    }


__all__ = ["ReviewAssignmentError", "create_review_assignment"]
