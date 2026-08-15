"""Stable, bounded read models for the Pi Web control plane."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from .errors import NotFoundError
from .models import bounded_text, validate_id
from .projects import work_index
from .web_timeline import read_session_timeline, read_session_timeline_page

_MAX_PROJECTS = 128
_MAX_CONVERSATIONS = 256
_MAX_INBOX = 256
_MAX_CHANGES = 256
_MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


def _age(value: str | None) -> tuple[str, int]:
    if not value:
        return "unknown", 999999
    try:
        then = datetime.fromisoformat(value.replace("Z", "+00:00"))
        minutes = max(0, int((datetime.now(timezone.utc) - then).total_seconds() // 60))
    except ValueError:
        return "recently", 0
    if minutes < 1:
        return "just now", 0
    if minutes < 60:
        return f"{minutes}m ago", minutes
    if minutes < 1440:
        return f"{minutes // 60}h ago", minutes
    return f"{minutes // 1440}d ago", minutes


def _state(value: str | None) -> str:
    return {
        "running": "working",
        "needs_attention": "waiting",
        "ready": "idle",
        "stopped": "unavailable",
        "lost": "interrupted",
        "failed": "interrupted",
        "missing": "unavailable",
        "error": "interrupted",
    }.get(value or "", "idle")


def _json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _row_state(row: Mapping[str, Any]) -> str:
    return _state(str(row.get("observed_state") or row.get("state") or ""))


def _conversation(store: Any, project: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    runs = [dict(item) for item in store.conn.execute("SELECT observed_state,updated_at FROM runs WHERE conversation_id=? ORDER BY created_at DESC LIMIT 4", (row["conversation_id"],))]
    state = _state(runs[0]["observed_state"] if runs else row["observed_state"])
    updated = runs[0]["updated_at"] if runs else row["updated_at"]
    age, age_min = _age(updated)
    return {
        "id": row["conversation_id"],
        "projectId": project["project_id"],
        "title": bounded_text(row["display_name"], name="conversation_title", limit=160),
        "role": row["role"],
        "state": state,
        "lastMessage": "No durable messages yet",
        "updated": age,
        "updatedMin": age_min,
        "timeline": [],
    }


def _change(store: Any, row: Mapping[str, Any], project: Mapping[str, Any]) -> dict[str, Any]:
    revisions = int(store.conn.execute("SELECT COUNT(*) FROM change_revisions WHERE change_id=?", (row["change_id"],)).fetchone()[0])
    state = str(row["state"])
    ui_state = {"draft": "in_revision", "open": "pending_review", "merged": "integrated", "closed": "integrated"}.get(state, state)
    state_label = {"in_revision": "In revision", "pending_review": "Awaiting review", "integrated": "Integrated", "awaiting_integration": "Awaiting integration"}.get(ui_state, ui_state.replace("_", " "))
    age, age_min = _age(row["updated_at"])
    author_row = store.conn.execute("SELECT role FROM conversations WHERE conversation_id=?", (row["created_by_conversation_id"],)).fetchone() if row["created_by_conversation_id"] else None
    return {
        "id": row["change_id"],
        "projectId": project["project_id"],
        "title": bounded_text(row["title"], name="change_title", limit=240),
        "state": ui_state,
        "stateLabel": state_label,
        "author": author_row["role"] if author_row is not None else None,
        "revisions": revisions,
        "age": age,
        "ageMin": age_min,
        "summary": bounded_text(row["summary"], name="change_summary", limit=1024),
    }


def _attention(store: Any, row: Mapping[str, Any], project_names: Mapping[str, str]) -> dict[str, Any]:
    detail = _json(row["detail_json"])
    age, age_min = _age(row["created_at"])
    kind = str(row["kind"] or "attention")
    decision = kind in {"decision", "command", "host-command", "package", "package-operation", "workstream-proposal", "integration"}
    return {
        "id": row["attention_id"],
        "kind": "decision" if decision else "message",
        "decisionKind": kind if decision else None,
        "projectId": row["project_id"],
        "conversationId": row["conversation_id"],
        "title": bounded_text(row["summary"], name="attention_summary", limit=240),
        "preview": bounded_text(str(detail.get("preview") or row["summary"]), name="attention_preview", limit=512),
        "state": "needs_decision" if decision else "needs_ack",
        "age": age,
        "ageMin": age_min,
        "requiresPasskey": decision,
        "expired": False,
        "projectName": project_names.get(str(row["project_id"]), "Project"),
    }


def _project(store: Any, row: Mapping[str, Any], project_names: Mapping[str, str], *, include_timeline: bool) -> dict[str, Any]:
    project_id = str(row["project_id"])
    conversations = [_conversation(store, row, item) for item in store.conn.execute("SELECT * FROM conversations WHERE project_id=? ORDER BY updated_at DESC LIMIT ?", (project_id, _MAX_CONVERSATIONS))]
    if include_timeline:
        for conversation in conversations:
            raw = store.conn.execute("SELECT session_file FROM conversations WHERE conversation_id=?", (conversation["id"],)).fetchone()
            if raw is not None:
                try:
                    conversation["timeline"] = read_session_timeline(store.state_root, project_id=project_id, conversation_id=conversation["id"], session_file=raw["session_file"])
                except (OSError, ValueError):
                    conversation["timeline"] = []
    runs = [dict(item) for item in store.conn.execute("SELECT * FROM runs WHERE project_id=? ORDER BY updated_at DESC LIMIT 64", (project_id,))]
    active_runs = [item for item in runs if item["observed_state"] in {"running", "needs_attention"}]
    working_now = []
    for run in active_runs:
        conversation = next((item for item in conversations if item["id"] == run["conversation_id"]), None)
        working_now.append({"title": conversation["title"] if conversation else "Active work", "role": conversation["role"] if conversation else "secretary", "conversationId": run["conversation_id"], "startedAgo": _age(run["started_at"] or run["updated_at"])[0]})
    outcomes = []
    for run in runs:
        if run["observed_state"] in {"stopped", "failed", "lost"}:
            conversation = next((item for item in conversations if item["id"] == run["conversation_id"]), None)
            outcomes.append({"title": conversation["title"] if conversation else "Completed work", "role": conversation["role"] if conversation else "secretary", "state": "completed" if run["observed_state"] == "stopped" else "interrupted", "age": _age(run["updated_at"])[0], "ageMin": _age(run["updated_at"])[1]})
    changes = [_change(store, item, row) for item in store.conn.execute("SELECT * FROM changes WHERE project_id=? ORDER BY updated_at DESC LIMIT ?", (project_id, _MAX_CHANGES))]
    attention = [_attention(store, item, project_names) for item in store.conn.execute("SELECT * FROM attention WHERE project_id=? AND state='open' ORDER BY created_at DESC LIMIT ?", (project_id, _MAX_INBOX))]
    updated = row["updated_at"]
    age, age_min = _age(updated)
    status = "working" if active_runs else ("waiting" if attention else ("idle" if row["observed_state"] == "ready" else "unavailable"))
    return {
        "id": project_id,
        "name": bounded_text(row["display_name"], name="project_name", limit=160),
        "status": status,
        "activitySummary": "Needs your attention" if attention else ("Active work in progress" if active_runs else "Quiet for now"),
        "lastUpdate": age,
        "lastUpdateMin": age_min,
        "workingNow": working_now,
        "recentOutcomes": outcomes[:32],
        "changes": [item["id"] for item in changes],
        "conversations": [item["id"] for item in conversations],
        "empty": not conversations and not changes and not attention,
        "attentionCount": len(attention),
        "openChangeCount": sum(item["state"] in {"pending_review", "in_revision", "awaiting_integration"} for item in changes),
    }, conversations, changes, attention


def _bounded_project_payload(result: dict[str, Any], conversations: list[dict[str, Any]], changes: list[dict[str, Any]], attention: list[dict[str, Any]]) -> dict[str, Any]:
    while len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        if attention:
            attention.pop()
        elif changes:
            changes.pop()
        elif conversations:
            conversations.pop()
        else:
            break
    return result


def build_bootstrap(store: Any, *, include_timeline: bool = True) -> dict[str, Any]:
    rows = list(store.conn.execute("SELECT * FROM projects ORDER BY display_name,project_id LIMIT ?", (_MAX_PROJECTS,)))
    names = {str(row["project_id"]): str(row["display_name"]) for row in rows}
    projects: list[dict[str, Any]] = []
    conversations: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    inbox: list[dict[str, Any]] = []
    for row in rows:
        project, project_conversations, project_changes, project_attention = _project(store, row, names, include_timeline=include_timeline)
        projects.append(project)
        conversations.extend(project_conversations)
        changes.extend(project_changes)
        inbox.extend(project_attention)
    result = {"apiVersion": 1, "projects": projects, "conversations": conversations, "changes": changes, "inbox": inbox}
    while len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 2 * 1024 * 1024:
        if inbox:
            inbox.pop()
        elif changes:
            changes.pop()
        elif conversations:
            conversations.pop()
        elif projects:
            projects.pop()
        else:
            break
    return result


def project_bootstrap(store: Any, project_id: str) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    row = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if row is None:
        raise NotFoundError("project was not found")
    names = {str(item["project_id"]): str(item["display_name"]) for item in store.conn.execute("SELECT project_id,display_name FROM projects")}
    project, conversations, changes, attention = _project(store, row, names, include_timeline=False)
    result = {"project": project, "conversations": conversations, "changes": changes, "inbox": attention, "workIndex": work_index(store, project_id)}
    return _bounded_project_payload(result, conversations, changes, attention)


def conversation_timeline(store: Any, project_id: str, conversation_id: str, *, after: str | None = None, limit: int = 512) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(conversation_id, prefix="conv")
    row = store.conn.execute("SELECT * FROM conversations WHERE project_id=? AND conversation_id=?", (project_id, conversation_id)).fetchone()
    if row is None:
        raise NotFoundError("conversation was not found in project")
    page = read_session_timeline_page(
        store.state_root,
        project_id=project_id,
        conversation_id=conversation_id,
        session_file=row["session_file"],
        after=after,
        limit=limit,
    )
    return {"projectId": project_id, "conversationId": conversation_id, "source": "pi-session-allowlist", **page}


__all__ = ["build_bootstrap", "conversation_timeline", "project_bootstrap"]
