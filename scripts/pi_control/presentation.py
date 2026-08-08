"""Controller-owned tmux presentation; panes never create lifecycle identity."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from .models import canonical_json, new_id, utc_now, validate_id


class PresentationError(RuntimeError):
    pass


def _tmux() -> str:
    path = shutil.which("tmux", path=os.defpath)
    if path is None:
        raise PresentationError("tmux is unavailable")
    return path


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run([_tmux(), *args], env={"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C"}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=30, check=False, shell=False)
    if check and result.returncode != 0:
        raise PresentationError(result.stderr.strip()[:1024] or "tmux operation failed")
    return result


def ensure_presentation(store: Any, *, project_id: str, conversation_id: str, title: str | None = None) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(conversation_id, prefix="conv")
    conversation = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=? AND project_id=?", (conversation_id, project_id)).fetchone()
    if conversation is None:
        raise PresentationError("conversation is not part of project")
    assignment = store.conn.execute("SELECT * FROM presentation_assignments WHERE conversation_id=?", (conversation_id,)).fetchone()
    if assignment is None:
        assignment_id = new_id("pa")
        with store.transaction():
            store.conn.execute("INSERT INTO presentation_assignments(presentation_assignment_id,conversation_id,backend,desired_state,observed_state,locator_json,resource_version,observed_at,updated_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (assignment_id, conversation_id, "tmux", "present", "unknown", canonical_json({"session": f"pi-system-{project_id}", "conversationId": conversation_id}), 1, None, utc_now(), None, None))
            assignment = store.conn.execute("SELECT * FROM presentation_assignments WHERE presentation_assignment_id=?", (assignment_id,)).fetchone()
    session = f"pi-system-{project_id}"
    result = _run(["has-session", "-t", session], check=False)
    if result.returncode != 0:
        _run(["new-session", "-d", "-s", session, "-n", "secretary"])
    window = title or conversation["display_name"]
    # A stable controller-owned window name identifies the presentation only.
    _run(["rename-window", "-t", f"{session}:secretary", window[:80]], check=False)
    with store.transaction():
        store.conn.execute("UPDATE presentation_assignments SET observed_state='present',observed_at=?,updated_at=?,resource_version=resource_version+1 WHERE presentation_assignment_id=?", (utc_now(), utc_now(), assignment["presentation_assignment_id"]))
        return dict(store.conn.execute("SELECT * FROM presentation_assignments WHERE presentation_assignment_id=?", (assignment["presentation_assignment_id"],)).fetchone())


def stop_managed_presentations(store: Any, *, project_id: str) -> list[str]:
    validate_id(project_id, prefix="prj")
    session = f"pi-system-{project_id}"
    result = _run(["has-session", "-t", session], check=False)
    if result.returncode == 0:
        _run(["kill-session", "-t", session])
    with store.transaction():
        store.conn.execute("UPDATE presentation_assignments SET observed_state='missing',updated_at=?,resource_version=resource_version+1 WHERE conversation_id IN (SELECT conversation_id FROM conversations WHERE project_id=?)", (utc_now(), project_id))
    return [session]


__all__ = ["PresentationError", "ensure_presentation", "stop_managed_presentations"]
