"""Exact immutable reviewer checkout preparation."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from .conversations import create_conversation
from .launch import attest_run, prepare_run
from .models import canonical_json, new_id, utc_now, validate_id


class ReviewAssignmentError(RuntimeError):
    pass


def create_review_assignment(store: Any, *, change_id: str, revision: int) -> dict[str, Any]:
    validate_id(change_id, prefix="chg")
    change = store.conn.execute("SELECT * FROM changes WHERE change_id=? AND current_revision=?", (change_id, revision)).fetchone()
    if change is None:
        raise ReviewAssignmentError("review must bind the current exact revision")
    revision_row = store.conn.execute("SELECT * FROM change_revisions WHERE change_id=? AND revision=?", (change_id, revision)).fetchone()
    source = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (change["source_working_copy_id"],)).fetchone()
    if revision_row is None or source is None:
        raise ReviewAssignmentError("review source is unavailable")
    path = Path(store.state_root) / "reviews" / change_id / str(revision)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        git = shutil.which("git", path=os.defpath)
        if git is None:
            raise ReviewAssignmentError("Git is unavailable")
        result = subprocess.run([git, "-c", "core.hooksPath=/dev/null", "-c", "core.sshCommand=", "worktree", "add", "--detach", str(path), revision_row["ref_name"]], cwd=source["path"], env={"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "TMPDIR": "/private/tmp"}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, shell=False, timeout=120)
        if result.returncode != 0:
            raise ReviewAssignmentError(result.stderr.strip()[:1024] or "review worktree creation failed")
    wc_id = new_id("wc")
    conversation_id = new_id("conv")
    now = utc_now()
    with store.transaction():
        store.conn.execute("INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,git_dir,branch_ref,expected_head_oid,expected_tree_oid,effective_mode,desired_state,observed_state,writer_epoch,active_writer_run_id,resource_version,controller_owned,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (wc_id, change["project_id"], f"review {change_id} r{revision}", "review", "review", str(path), str(path / ".git"), None, revision_row["tip_oid"], revision_row["tree_oid"], "read-only", "present", "ready", 0, None, 1, 1, now, now, now, None, None))
    conversation = create_conversation(store, project_id=change["project_id"], role="review", display_name=f"review {change_id} r{revision}", pi_session_id=f"pi-review-{change_id}-{revision}", working_copy_id=wc_id)
    prepared = prepare_run(store, project_id=change["project_id"], conversation_id=conversation["conversation_id"], working_copy_id=wc_id, authority="read-only")
    attest_run(store, run_id=prepared.run["run_id"], manifest_digest=prepared.manifest["manifestDigest"])
    prepared.close()
    return {"changeId": change_id, "revision": revision, "tipOid": revision_row["tip_oid"], "treeOid": revision_row["tree_oid"], "workingCopyId": wc_id, "conversationId": conversation["conversation_id"], "runId": prepared.run["run_id"], "path": str(path), "readOnly": True, "environment": prepared.environment}


__all__ = ["ReviewAssignmentError", "create_review_assignment"]
