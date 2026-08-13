"""Controller-owned tmux presentation reconciler.

Panes never create lifecycle identity. The reconciler compares the desired
presentation (controller conversations with exact launch argv) against the
observed tmux server, repairs dead or idle managed panes, moves proven panes
when layout changes, never kills a proven live conversation, and persists
version-1 locators plus observed states in presentation_assignments.

Identity proof is process-based: a pane is proven live for a conversation
only when its process tree contains the exact launcher argv for that
conversation (or the pane option argv digest matches the expected digest and
the pane is alive). Pane titles are human labels, never authority.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable, Mapping

from .models import canonical_json, new_id, utc_now, validate_id
from .presentation_locator import build_locator, parse_locator

GRID_SESSION_NAMES = {"pisec", "pi-personal"}
_SLUG = re.compile(r"[^A-Za-z0-9._-]")


class PresentationError(RuntimeError):
    pass


class TmuxBackend:
    """Thin fail-closed tmux runner with a clean environment."""

    def __init__(self, *, tmux_tmpdir: str | None = None, home: str | None = None) -> None:
        self.tmux_tmpdir = tmux_tmpdir if tmux_tmpdir is not None else os.environ.get("TMUX_TMPDIR")
        self.home = home

    def run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        path = shutil.which("tmux", path=os.defpath)
        if path is None:
            raise PresentationError("tmux is unavailable")
        env: dict[str, str] = {"PATH": os.defpath, "HOME": self.home or "/nonexistent", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        if self.tmux_tmpdir:
            env["TMUX_TMPDIR"] = self.tmux_tmpdir
        result = subprocess.run(
            [path, *args], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            timeout=30, check=False, shell=False,
        )
        if check and result.returncode != 0:
            raise PresentationError(result.stderr.strip()[:1024] or "tmux operation failed")
        return result

    def has_session(self, session: str) -> bool:
        return self.run(["has-session", "-t", f"={session}"], check=False).returncode == 0

    def inventory(self) -> list[dict[str, str]]:
        """Every pane of the server with its managed-pane options."""
        if not self.has_server():
            return []
        result = self.run([
            "list-panes", "-a", "-F",
            "\t".join((
                "#{session_name}", "#{window_name}", "#{pane_id}", "#{pane_dead}",
                "#{pane_current_command}", "#{pane_pid}", "#{pane_title}",
                "#{@pi-managed}", "#{@pi-conversation-id}", "#{@pi-role}", "#{@pi-argv-digest}",
            )),
        ], check=False)
        if result.returncode != 0:
            raise PresentationError(result.stderr.strip()[:1024] or "tmux pane inventory failed")
        panes: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 11:
                continue
            session, window, pane_id, dead, command, pid, title, managed, conversation, role, digest = fields
            panes.append({
                "session": session, "window": window, "pane_id": pane_id, "pane_dead": dead,
                "command": command, "pid": pid, "title": title, "managed": managed,
                "conversation_id": conversation, "role": role, "argv_digest": digest,
            })
        return panes

    def has_server(self) -> bool:
        return self.run(["list-sessions"], check=False).returncode == 0

    def new_session(self, session: str, first_window: str, cwd: str) -> None:
        self.run(["new-session", "-d", "-s", session, "-n", first_window, "-c", cwd])

    def last_pane(self, session: str, window: str) -> str:
        result = self.run(["list-panes", "-t", f"={session}:{window}", "-F", "#{pane_id}"], check=False)
        lines = result.stdout.splitlines()
        return lines[-1] if lines else ""

    def new_window(self, session: str, window: str, cwd: str) -> str:
        self.run(["new-window", "-d", "-t", f"={session}", "-n", window, "-c", cwd])
        return self.last_pane(session, window)

    def split_window(self, session: str, window: str, cwd: str) -> str:
        self.run(["split-window", "-h", "-t", f"={session}:{window}", "-c", cwd], check=False)
        return self.last_pane(session, window)

    def move_pane(self, source: str, target_session: str, target_window: str) -> None:
        self.run(["move-pane", "-s", source, "-t", f"={target_session}:{target_window}"], check=False)

    def respawn_pane(self, pane: str) -> None:
        self.run(["respawn-pane", "-t", pane, "-k"], check=False)

    def send_keys(self, target: str, argv: list[str]) -> None:
        self.run(["send-keys", "-t", target, "exec " + " ".join(argv), "Enter"], check=False)

    def set_pane_options(self, pane: str, *, conversation_id: str, role: str, argv_digest: str) -> None:
        for key, value in (("@pi-managed", "1"), ("@pi-conversation-id", conversation_id), ("@pi-role", role), ("@pi-argv-digest", argv_digest)):
            self.run(["set-option", "-p", "-t", pane, key, value], check=False)

    def set_remain_on_exit(self, session: str, window: str) -> None:
        self.run(["set-window-option", "-t", f"={session}:{window}", "remain-on-exit", "on"], check=False)

    def set_pane_title(self, pane: str, title: str) -> None:
        self.run(["select-pane", "-t", pane, "-T", title[:200]], check=False)

    def kill_pane(self, pane: str) -> None:
        self.run(["kill-pane", "-t", pane], check=False)

    def kill_window(self, session: str, window: str) -> None:
        self.run(["kill-window", "-t", f"={session}:{window}"], check=False)

    def windows(self, session: str) -> list[str]:
        result = self.run(["list-windows", "-t", f"={session}", "-F", "#{window_name}"], check=False)
        if result.returncode != 0:
            return []
        return result.stdout.splitlines()


def project_session_name(project: Mapping[str, Any]) -> str:
    """One deterministic session per project Git common directory."""
    digest = hashlib.sha256(str(project["git_common_dir"]).encode("utf-8")).hexdigest()
    return f"pi-project-{digest[:12]}"


def sanitize_title(title: str) -> str:
    value = _SLUG.sub("-", title).strip("-") or "work"
    return value[:40]


def _descendant_argv_matches(pid: int, needle: str) -> bool:
    """Walk the process tree of pid and test whether any descendant cmdline
    contains needle (e.g. '--conversation-id <id>'). Fail-closed: any
    observation error returns False."""
    try:
        stack = [pid]
        seen: set[int] = set()
        while stack:
            current = stack.pop()
            if current in seen or current <= 0:
                continue
            seen.add(current)
            try:
                cmdline = Path(f"/proc/{current}/cmdline").read_bytes().replace(b"\x00", b" ")
            except OSError:
                continue
            if needle.encode("utf-8") in cmdline:
                return True
            try:
                stat = Path(f"/proc/{current}/task/{current}/children").read_text(encoding="utf-8")
            except OSError:
                continue
            stack.extend(int(child) for child in stat.split())
    except (OSError, ValueError):
        pass
    return False


def pane_is_proven(pane: Mapping[str, str], *, conversation_id: str, argv_digest: str) -> bool:
    """A pane is proven live for the conversation only by process proof."""
    if pane.get("managed") != "1" or pane.get("conversation_id") != conversation_id:
        return False
    if pane.get("pane_dead") == "1":
        return False
    if pane.get("argv_digest") == argv_digest:
        return True
    pid = pane.get("pid") or ""
    if pid.isdigit() and int(pid) > 0:
        return _descendant_argv_matches(int(pid), f"--conversation-id {conversation_id}")
    return False


def reconcile_presentation(
    store: Any,
    *,
    surface: str,
    layout: str,
    conversations: list[Mapping[str, Any]],
    backend: TmuxBackend | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Reconcile the desired presentation of one surface or project.

    conversations: ordered desired entries with conversationId, role, title,
    argv (exact launch argv list), and optional workstreamId/worktreePath for
    project surfaces. The reconciler creates, repairs, moves, and removes
    managed panes but never kills a proven live conversation.
    """
    backend = backend or TmuxBackend()
    if layout not in {"desktop", "mobile"}:
        raise PresentationError("presentation layout must be desktop or mobile")
    if surface not in GRID_SESSION_NAMES and not surface.startswith("project"):
        raise PresentationError(f"unknown presentation surface: {surface}")
    desired: list[dict[str, Any]] = []
    seen_conversations: set[str] = set()
    for item in conversations:
        conversation_id = item.get("conversationId")
        validate_id(conversation_id, prefix="conv")
        if conversation_id in seen_conversations:
            raise PresentationError(f"duplicate conversation in the desired presentation: {conversation_id}")
        seen_conversations.add(conversation_id)
        conversation = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        if conversation is None:
            raise PresentationError(f"conversation is not registered: {conversation_id}")
        role = item.get("role") or conversation["role"]
        if role != conversation["role"]:
            raise PresentationError(f"conversation role differs from the desired role: {conversation_id}")
        argv = item.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(part, str) for part in argv):
            raise PresentationError(f"conversation has no exact launch argv: {conversation_id}")
        title = item.get("title") or conversation["display_name"]
        desired.append({
            "conversationId": conversation_id,
            "role": role,
            "title": str(title),
            "argv": argv,
            "argvDigest": "sha256:" + hashlib.sha256(" ".join(argv).encode("utf-8")).hexdigest(),
            "projectId": conversation["project_id"],
            "workstreamId": item.get("workstreamId"),
            "worktreePath": item.get("worktreePath"),
        })

    project = None
    if surface.startswith("project"):
        project_id = surface[len("project:"):] if surface.startswith("project:") else (desired[0]["projectId"] if desired else "")
        validate_id(project_id, prefix="prj")
        project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if project is None:
            raise PresentationError(f"project not found: {project_id}")
    session = surface if surface in GRID_SESSION_NAMES else project_session_name(project)

    working_dir = cwd or str(Path.home())
    panes = backend.inventory()
    session_panes = [pane for pane in panes if pane["session"] == session]

    # Desired window/pane layout.
    plan: list[dict[str, Any]] = []
    if layout == "mobile" or surface.startswith("project"):
        for index, item in enumerate(desired, start=1):
            if surface.startswith("project"):
                workstream_id = item.get("workstreamId") or ""
                window = f"ws-{sanitize_title(item['title'])}-{workstream_id[-8:]}" if workstream_id else f"work-{index}"
            else:
                window = f"projects-{index}"
            plan.append({**item, "window": window, "pane_slot": 0})
    else:
        for index in range(0, len(desired), 2):
            window = f"projects-{(index // 2) + 1}"
            plan.append({**desired[index], "window": window, "pane_slot": 0})
            if index + 1 < len(desired):
                plan.append({**desired[index + 1], "window": window, "pane_slot": 1})

    now = utc_now()
    present: list[str] = []
    repaired: list[str] = []
    moved: list[str] = []
    drifted: list[str] = []
    removed: list[str] = []

    created_windows: set[str] = set()
    if not backend.has_session(session):
        backend.new_session(session, plan[0]["window"] if plan else "shell", working_dir)
        if plan:
            created_windows.add(plan[0]["window"])

    for item in plan:
        conv = item["conversationId"]
        window = item["window"]
        backend.set_remain_on_exit(session, window)
        candidates = [pane for pane in session_panes if pane.get("conversation_id") == conv and pane.get("managed") == "1"]
        proven = [pane for pane in candidates if pane_is_proven(pane, conversation_id=conv, argv_digest=item["argvDigest"])]
        if proven:
            pane = proven[0]
            if pane["window"] != window:
                backend.move_pane(pane["pane_id"], session, window)
                moved.append(conv)
            present.append(conv)
            continue
        repair = next((pane for pane in candidates if pane.get("pane_dead") == "1" or pane.get("command") in {"bash", "zsh", "sh", "dash", "fish"}), None)
        if repair is not None:
            backend.respawn_pane(repair["pane_id"])
            backend.send_keys(repair["pane_id"], item["argv"])
            backend.set_pane_options(repair["pane_id"], conversation_id=conv, role=item["role"], argv_digest=item["argvDigest"])
            backend.set_pane_title(repair["pane_id"], f"pi-{item['role']} {conv}")
            repaired.append(conv)
            present.append(conv)
            continue
        if item["pane_slot"] == 1:
            pane_id = backend.split_window(session, item["window"], working_dir)
            backend.send_keys(pane_id, item["argv"])
            backend.set_pane_options(pane_id, conversation_id=conv, role=item["role"], argv_digest=item["argvDigest"])
            backend.set_pane_title(pane_id, f"pi-{item['role']} {conv}")
        elif surface.startswith("project") and layout == "desktop" and item.get("worktreePath"):
            worktree = str(item["worktreePath"])
            if item["window"] not in created_windows:
                editor_pane = backend.new_window(session, item["window"], worktree)
                created_windows.add(item["window"])
                backend.send_keys(editor_pane, ["nvim"])
            agent_pane = backend.split_window(session, item["window"], worktree)
            backend.send_keys(agent_pane, item["argv"])
            backend.set_pane_options(agent_pane, conversation_id=conv, role=item["role"], argv_digest=item["argvDigest"])
            backend.set_pane_title(agent_pane, f"pi-{item['role']} {conv}")
        else:
            if item["window"] not in created_windows:
                pane_id = backend.new_window(session, item["window"], working_dir)
                created_windows.add(item["window"])
            else:
                pane_id = backend.last_pane(session, item["window"])
            backend.send_keys(pane_id, item["argv"])
            backend.set_pane_options(pane_id, conversation_id=conv, role=item["role"], argv_digest=item["argvDigest"])
            backend.set_pane_title(pane_id, f"pi-{item['role']} {conv}")
        present.append(conv)

    desired_ids = {item["conversationId"] for item in plan}
    for pane in session_panes:
        if pane.get("managed") != "1":
            continue
        conv = pane.get("conversation_id") or ""
        if conv in desired_ids:
            continue
        if pane_is_proven(pane, conversation_id=conv, argv_digest=pane.get("argv_digest") or ""):
            drifted.append(conv)
            continue
        backend.kill_pane(pane["pane_id"])
        removed.append(conv)

    for item in plan:
        conv = item["conversationId"]
        project_id = item["projectId"]
        assignment = store.conn.execute("SELECT * FROM presentation_assignments WHERE conversation_id=?", (conv,)).fetchone()
        pane_ref = None
        for pane in backend.inventory():
            if pane.get("session") == session and pane.get("conversation_id") == conv and pane.get("managed") == "1":
                pane_ref = pane["pane_id"]
                break
        observed = "present" if conv in present else ("drifted" if conv in drifted else "error")
        locator = build_locator(
            surface=surface, session=session, window=item["window"], pane=pane_ref,
            project_id=project_id, conversation_id=conv, role=item["role"],
            layout=layout, argv_digest=item["argvDigest"],
            workstream_id=item.get("workstreamId"), owner_pid=None,
        ) if pane_ref else None
        if assignment is None:
            store.conn.execute(
                "INSERT INTO presentation_assignments(presentation_assignment_id,conversation_id,backend,desired_state,observed_state,locator_json,resource_version,observed_at,updated_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("pa"), conv, "tmux", "present", observed, canonical_json(locator) if locator else None, 1, now, now, None, None),
            )
        else:
            store.conn.execute(
                "UPDATE presentation_assignments SET observed_state=?,locator_json=?,observed_at=?,updated_at=?,resource_version=resource_version+1 WHERE conversation_id=?",
                (observed, canonical_json(locator) if locator else None, now, now, conv),
            )

    return {
        "surface": surface,
        "session": session,
        "layout": layout,
        "present": present,
        "repaired": repaired,
        "moved": moved,
        "drifted": drifted,
        "removed": removed,
    }


def focus_presentation(store: Any, *, conversation_id: str) -> dict[str, Any]:
    """Resolve the durable locator for one conversation for frontend focus."""
    validate_id(conversation_id, prefix="conv")
    assignment = store.conn.execute("SELECT * FROM presentation_assignments WHERE conversation_id=?", (conversation_id,)).fetchone()
    if assignment is None or not assignment["locator_json"]:
        raise PresentationError("conversation has no presentation locator")
    locator = parse_locator(assignment["locator_json"])
    if locator is None:
        raise PresentationError("conversation presentation locator is unversioned")
    return {"conversationId": conversation_id, "session": locator["session"], "window": locator["window"], "pane": locator["pane"]}


def stop_managed_presentations(store: Any, *, project_id: str) -> list[str]:
    """Stop only the managed panes of one project's grid conversations.

    Live proven panes are left running; dead and idle managed panes are
    removed. Grid sessions are never killed here.
    """
    validate_id(project_id, prefix="prj")
    backend = TmuxBackend()
    removed: list[str] = []
    for row in store.conn.execute("SELECT conversation_id FROM conversations WHERE project_id=?", (project_id,)):
        conversation_id = row["conversation_id"]
        for pane in backend.inventory():
            if pane.get("managed") != "1" or pane.get("conversation_id") != conversation_id:
                continue
            if pane.get("pane_dead") == "1":
                backend.kill_pane(pane["pane_id"])
                removed.append(conversation_id)
    return removed


__all__ = [
    "GRID_SESSION_NAMES", "PresentationError", "TmuxBackend", "focus_presentation",
    "pane_is_proven", "project_session_name", "reconcile_presentation",
    "sanitize_title", "stop_managed_presentations",
]
