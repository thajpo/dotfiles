"""Controller-owned workstream and personal working-copy creation."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping

from .events import append_event_in_transaction
from .greenfield_store import default_state_root
from .models import bounded_text, canonical_json, new_id, utc_now, validate_id, validate_pi_session_id
from .presentation import PresentationError, ensure_presentation


class WorkstreamCreationError(RuntimeError):
    pass


def _git_env() -> dict[str, str]:
    return {"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "GIT_EDITOR": "true", "GIT_ASKPASS": "true", "TMPDIR": "/private/tmp"}


def _git_worktree_add(repository: Path, branch: str, destination: Path) -> None:
    git = shutil.which("git", path=os.defpath)
    if git is None:
        raise WorkstreamCreationError("Git is unavailable")
    result = subprocess.run([git, "-c", "core.hooksPath=/dev/null", "-c", "core.sshCommand=", "-c", "credential.helper=", "worktree", "add", "-b", branch, str(destination), "HEAD"], cwd=str(repository), env=_git_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=120, check=False, shell=False)
    if result.returncode != 0:
        raise WorkstreamCreationError(result.stderr.strip()[:1024] or "Git worktree creation failed")


def create_workstream(store: Any, *, project_id: str, title: str, brief: Mapping[str, Any] | None = None, pi_session_id: str | None = None, display_name: str | None = None) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    primary = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary' ORDER BY created_at LIMIT 1", (project_id,)).fetchone()
    if project is None or primary is None:
        raise WorkstreamCreationError("project primary working copy is missing")
    if not primary["expected_head_oid"]:
        raise WorkstreamCreationError("workstream requires a committed primary HEAD")
    ws_id = new_id("ws")
    wc_id = new_id("wc")
    conversation_id = new_id("conv")
    branch_name = f"pi-system/{ws_id}"
    configured_root = os.environ.get("PI_SYSTEM_WORK_ROOT")
    if configured_root:
        work_root = Path(configured_root).expanduser()
    elif Path(store.state_root).absolute() != default_state_root().absolute():
        # Disposable state roots keep their worktrees beside the fixture;
        # production uses the documented ~/.local/share root.
        work_root = Path(store.state_root).parent / "pi-system-work"
    else:
        work_root = Path.home() / ".local" / "share" / "pi-system-work"
    root = work_root / project_id
    destination = root / ws_id
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise WorkstreamCreationError("workstream destination already exists")
    _git_worktree_add(Path(primary["path"]), branch_name, destination)
    now = utc_now()
    session_id = pi_session_id or f"pi-workstream-{ws_id}"
    validate_pi_session_id(session_id)
    session_file = Path(store.state_root) / "sessions" / project_id / f"{conversation_id}.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    mode = "trusted-live" if project["trust_mode"] == "trusted" else "isolated"
    try:
        with store.transaction():
            store.conn.execute("INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,git_dir,branch_ref,expected_head_oid,expected_tree_oid,effective_mode,desired_state,observed_state,writer_epoch,active_writer_run_id,resource_version,controller_owned,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (wc_id, project_id, display_name or title, "worktree", "workstream", str(destination), str(destination / ".git"), f"refs/heads/{branch_name}", primary["expected_head_oid"], primary["expected_tree_oid"], mode, "present", "ready", 0, None, 1, 1, now, now, now, None, None))
            store.conn.execute("INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (conversation_id, project_id, wc_id, "workstream", display_name or title, session_id, str(session_file), "active", "ready", 1, now, now, now, None, None))
            store.conn.execute("INSERT INTO workstreams(workstream_id,project_id,working_copy_id,conversation_id,title,brief_json,target_ref,starting_oid,desired_state,observed_state,controller_owned,resource_version,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ws_id, project_id, wc_id, conversation_id, bounded_text(title, name="title", limit=512), canonical_json(dict(brief or {})), f"refs/heads/{branch_name}", primary["expected_head_oid"], "active", "creating", 1, 1, now, now, now, None, None))
            pa_id = new_id("pa")
            store.conn.execute("INSERT INTO presentation_assignments(presentation_assignment_id,conversation_id,backend,desired_state,observed_state,locator_json,resource_version,observed_at,updated_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (pa_id, conversation_id, "tmux", "present", "unknown", canonical_json({"conversationId": conversation_id, "workstreamId": ws_id}), 1, None, now, None, None))
            append_event_in_transaction(store.conn, event_kind="workstream.created", resource_type="workstream", resource_id=ws_id, resource_version=1, payload={"projectId": project_id, "workingCopyId": wc_id, "conversationId": conversation_id, "branchRef": f"refs/heads/{branch_name}"})
        try:
            ensure_presentation(store, project_id=project_id, conversation_id=conversation_id, title=title)
        except PresentationError as error:
            with store.transaction():
                store.conn.execute("UPDATE workstreams SET observed_state='error',error_code='PRESENTATION_UNAVAILABLE',error_detail=?,updated_at=?,resource_version=resource_version+1 WHERE workstream_id=?", (str(error)[:1024], utc_now(), ws_id))
            raise WorkstreamCreationError(str(error)) from error
        with store.transaction():
            store.conn.execute("UPDATE workstreams SET observed_state='ready',last_reconciled_at=?,updated_at=?,resource_version=resource_version+1 WHERE workstream_id=?", (utc_now(), utc_now(), ws_id))
            return dict(store.conn.execute("SELECT * FROM workstreams WHERE workstream_id=?", (ws_id,)).fetchone())
    except BaseException:
        # Preserve the working copy for forensic recovery.  It is not adopted
        # as a managed row if the transaction failed.
        raise


__all__ = ["WorkstreamCreationError", "create_workstream"]
