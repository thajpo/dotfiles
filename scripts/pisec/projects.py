"""Canonical Git project registration and status."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
import shutil
import subprocess
from typing import Any

from .events import append_event_in_transaction
from .fence import resolve_data_dirs
from .models import ConflictError, InvalidRequestError, NeedsAttentionError, NotFoundError, bounded_text, canonical_json, new_id, utc_now, validate_id
from .research import research_counts


def _git(path: Path, *args: str) -> str:
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise InvalidRequestError("git is unavailable")
    environment = {"HOME": "/nonexistent", "PATH": os.defpath, "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run([executable, "-C", str(path), *args], env=environment, text=True, capture_output=True, timeout=10, check=False)
    if result.returncode != 0:
        raise InvalidRequestError("Git repository observation failed", detail={"command": args[0], "stderr": result.stderr.strip()[:512]})
    if len(result.stdout.encode("utf-8")) > 64 * 1024:
        raise InvalidRequestError("Git repository observation was too large")
    return result.stdout.strip()


def _origin_url(path: Path) -> str | None:
    try:
        value = _git(path, "config", "--local", "--get", "remote.origin.url")
    except InvalidRequestError:
        return None
    if not value or len(value) > 2048 or value.startswith("-") or any(ord(char) < 0x20 for char in value):
        raise InvalidRequestError("origin remote URL is invalid")
    return value


def observe_project(path: str | Path, default_ref: str | None = None) -> dict[str, Any]:
    requested = Path(path).expanduser().resolve(strict=True)
    if _git(requested, "rev-parse", "--is-bare-repository") != "false":
        raise InvalidRequestError("bare repositories are not supported")
    top = Path(_git(requested, "rev-parse", "--show-toplevel")).resolve(strict=True)
    common_text = _git(requested, "rev-parse", "--git-common-dir")
    common = Path(common_text)
    if not common.is_absolute():
        common = requested / common
    common = common.resolve(strict=True)
    ref = bounded_text(default_ref or "HEAD", name="default_ref", limit=512)
    if ref.startswith("-") or any(ord(char) < 0x20 for char in ref):
        raise InvalidRequestError("default_ref contains unsafe characters")
    oid = _git(top, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").lower()
    if len(oid) not in (40, 64) or any(char not in "0123456789abcdef" for char in oid):
        raise InvalidRequestError("Git returned an invalid commit object id")
    return {"repository_path": str(top), "git_common_dir": str(common), "default_ref": ref, "default_oid": oid, "remote_url": _origin_url(top)}


def get_project(store: Any, project_id: str) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    row = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if row is None:
        raise NotFoundError("project was not found")
    value = dict(row)
    raw_data = value.get("data_dirs")
    value["data_dirs"] = json.loads(raw_data) if raw_data else []
    return value


def resolve_project(store: Any, selector: str) -> dict[str, Any]:
    try:
        if selector.startswith("prj_"):
            return get_project(store, selector)
        observation = observe_project(selector)
        row = store.conn.execute("SELECT * FROM projects WHERE git_common_dir=?", (observation["git_common_dir"],)).fetchone()
    except (OSError, InvalidRequestError):
        rows = list(store.conn.execute("SELECT * FROM projects WHERE display_name=?", (selector,)))
        if len(rows) > 1:
            raise InvalidRequestError("project name is ambiguous")
        row = rows[0] if rows else None
    if row is None:
        raise NotFoundError("project was not found")
    return dict(row)


def register_project(store: Any, path: str | Path, *, display_name: str | None = None, default_ref: str | None = None, data_dirs: Any = None) -> dict[str, Any]:
    observed = observe_project(path, default_ref)
    resolved_data = resolve_data_dirs(data_dirs, Path(observed["repository_path"]))
    data_json = json.dumps(resolved_data, sort_keys=True) if resolved_data else None
    existing = store.conn.execute("SELECT * FROM projects WHERE git_common_dir=?", (observed["git_common_dir"],)).fetchone()
    if existing is not None:
        value = dict(existing)
        registered_remote = value.get("remote_url")
        observed_remote = observed["remote_url"]
        if registered_remote is None and observed_remote is not None:
            with store.transaction():
                store.conn.execute(
                    "UPDATE projects SET remote_url=?,updated_at=? WHERE project_id=? AND remote_url IS NULL",
                    (observed_remote, utc_now(), value["project_id"]),
                )
            return get_project(store, value["project_id"])
        if registered_remote != observed_remote:
            raise InvalidRequestError("registered project origin remote drifted")
        if data_dirs is not None and value.get("data_dirs") != data_json:
            with store.transaction():
                store.conn.execute(
                    "UPDATE projects SET data_dirs=?,updated_at=? WHERE project_id=?",
                    (data_json, utc_now(), value["project_id"]),
                )
            return get_project(store, value["project_id"])
        return value
    project_id = new_id("prj")
    name = bounded_text(display_name or Path(observed["repository_path"]).name, name="display_name", limit=512)
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "INSERT INTO projects(project_id,display_name,repository_path,git_common_dir,default_ref,remote_url,data_dirs,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (project_id, name, observed["repository_path"], observed["git_common_dir"], observed["default_ref"], observed["remote_url"], data_json, now, now),
        )
        append_event_in_transaction(store.conn, kind="project.registered", project_id=project_id, payload={"displayName": name, "repositoryPath": observed["repository_path"], "gitCommonDir": observed["git_common_dir"], "defaultRef": observed["default_ref"]})
    return get_project(store, project_id)


def list_projects(store: Any, include_inactive: bool = False) -> list[dict[str, Any]]:
    if include_inactive:
        rows = store.conn.execute("SELECT * FROM projects ORDER BY display_name,project_id")
    else:
        rows = store.conn.execute("SELECT * FROM projects WHERE active=1 ORDER BY display_name,project_id")
    return [dict(row) for row in rows]


def _project_lifecycle_state(store: Any, project: Mapping[str, Any]) -> bool:
    value = project.get("active")
    return True if value is None else bool(value)


def assert_project_writable(store: Any, project_id: str) -> None:
    project = get_project(store, project_id)
    if not _project_lifecycle_state(store, project):
        raise ConflictError("project is inactive")
    operation = store.conn.execute(
        "SELECT state FROM operations WHERE project_id=? AND kind='project.deactivate' ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if operation is not None and operation["state"] in {"planned", "applying", "needs_attention"}:
        raise ConflictError("project deactivation is in progress")

def deactivate_project(store: Any, selector: str, workspace: Any, harness: Any) -> dict[str, Any]:
    from .cleanup import _validate_retained_session_root

    project = get_project(store, resolve_project(store, selector)["project_id"])
    project_id = project["project_id"]
    if not _project_lifecycle_state(store, project):
        return {"projectId": project_id, "displayName": project["display_name"], "workstreamId": None, "retainedSessionRoot": None, "reused": True}

    key = f"project.deactivate:{project_id}"
    operation = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (key,)).fetchone()
    if operation is not None and operation["state"] == "succeeded":
        result = json.loads(operation["result_json"] or "{}")
        return {"projectId": project_id, "displayName": project["display_name"], **result, "reused": True}
    if operation is None:
        operation_id = new_id("op")
        now = utc_now()
        request = {"kind": "project.deactivate", "projectId": project_id}
        with store.transaction():
            store.conn.execute(
                "INSERT INTO operations(operation_id,kind,project_id,idempotency_key,request_json,request_sha256,state,step,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'planned',?,?)",
                (operation_id, "project.deactivate", project_id, key, canonical_json(request), __import__("hashlib").sha256(canonical_json(request).encode()).hexdigest(), "applying", now, now),
            )
        operation = store.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    operation_id = str(operation["operation_id"])
    step = str(operation["step"])
    stages = ("planned", "workspace_close_intent", "workspace_closed", "retained_root_committed", "binding_cleanup_intent", "binding_cleaned", "committed")
    rank = lambda value: stages.index(value) if value in stages else -1

    live_rows = [dict(row) for row in store.conn.execute("SELECT * FROM workstreams WHERE project_id=? AND desired_state <> 'retired'", (project_id,))]
    workers = [row for row in live_rows if row["kind"] == "worker"]
    if workers:
        raise ConflictError("project has active worker workstreams; complete or retire them before deactivation")
    first_mates = [row for row in live_rows if row["kind"] == "first_mate"]
    if first_mates:
        raise ConflictError("global First Mate workstreams are managed separately from projects")
    secretaries = [row for row in live_rows if row["kind"] == "secretary"]
    secretary = secretaries[0] if secretaries else None
    binding = None
    if secretary is not None:
        row = store.conn.execute("SELECT * FROM runtime_bindings WHERE workstream_id=?", (secretary["workstream_id"],)).fetchone()
        binding = None if row is None else dict(row)
    if binding is not None and binding["observed_state"] in {"starting", "working", "blocked"}:
        raise ConflictError("project coordinator runtime is still active; wait for it to become idle")
    if binding is not None and (
        workspace is None or harness is None
        or binding["workspace_adapter_id"] != workspace.manifest.adapter_id
        or binding["harness_id"] != harness.manifest.adapter_id
    ):
        raise NeedsAttentionError("configured adapter does not match the durable coordinator binding")

    retained_root = None
    try:
        if binding is not None:
            if rank(step) < rank("workspace_close_intent"):
                with store.transaction():
                    store.conn.execute("UPDATE operations SET state='applying',step='workspace_close_intent',error_code=NULL,error_message=NULL,updated_at=? WHERE operation_id=?", (utc_now(), operation_id))
                step = "workspace_close_intent"
            if rank(step) < rank("workspace_closed"):
                workspace.close_workspace(str(binding["workspace_id"]))
                with store.transaction():
                    store.conn.execute("UPDATE operations SET state='applying',step='workspace_closed',updated_at=? WHERE operation_id=?", (utc_now(), operation_id))
                step = "workspace_closed"
            retained_root_path = _validate_retained_session_root(binding, harness)
            retained_root = str(retained_root_path)
            if rank(step) < rank("retained_root_committed"):
                with store.transaction():
                    existing_root = store.conn.execute("SELECT 1 FROM retained_session_roots WHERE workstream_id=?", (binding["workstream_id"],)).fetchone()
                    if existing_root is None:
                        store.conn.execute(
                            "INSERT INTO retained_session_roots(workstream_id,harness_id,harness_home,native_session_kind,native_session_value,retained_at) VALUES(?,?,?,?,?,?)",
                            (binding["workstream_id"], binding["harness_id"], binding["harness_home"], binding["native_session_kind"], binding["native_session_value"], utc_now()),
                        )
                    store.conn.execute("UPDATE operations SET state='applying',step='retained_root_committed',updated_at=? WHERE operation_id=?", (utc_now(), operation_id))
                step = "retained_root_committed"
            if rank(step) < rank("binding_cleanup_intent"):
                with store.transaction():
                    store.conn.execute("UPDATE operations SET state='applying',step='binding_cleanup_intent',updated_at=? WHERE operation_id=?", (utc_now(), operation_id))
                step = "binding_cleanup_intent"
            if rank(step) < rank("binding_cleaned"):
                harness.cleanup_binding(binding)
                with store.transaction():
                    store.conn.execute("UPDATE operations SET state='applying',step='binding_cleaned',updated_at=? WHERE operation_id=?", (utc_now(), operation_id))
                step = "binding_cleaned"
        now = utc_now()
        result = {"projectId": project_id, "workstreamId": None if secretary is None else secretary["workstream_id"], "retainedSessionRoot": retained_root}
        with store.transaction():
            if binding is not None:
                store.conn.execute("DELETE FROM runtime_bindings WHERE workstream_id=?", (binding["workstream_id"],))
            if secretary is not None:
                store.conn.execute("UPDATE workstreams SET desired_state='retired',retired_at=?,attention_reason=NULL,updated_at=? WHERE workstream_id=?", (now, now, secretary["workstream_id"]))
            store.conn.execute("UPDATE projects SET active=0,deactivated_at=?,secretary_workstream_id=NULL,updated_at=? WHERE project_id=?", (now, now, project_id))
            store.conn.execute("UPDATE operations SET state='succeeded',step='committed',result_json=?,updated_at=? WHERE operation_id=?", (canonical_json(result), now, operation_id))
            append_event_in_transaction(store.conn, kind="project.deactivated", project_id=project_id, operation_id=operation_id, payload=result)
        return {"projectId": project_id, "displayName": project["display_name"], **result, "reused": False}
    except (ConflictError, NeedsAttentionError):
        raise
    except Exception as error:
        with store.transaction():
            store.conn.execute("UPDATE operations SET state='needs_attention',error_code='deactivation_step_failed',error_message=?,updated_at=? WHERE operation_id=?", (str(error)[:512], utc_now(), operation_id))
        raise NeedsAttentionError("project deactivation requires retry") from error


def activate_project(store: Any, selector: str) -> dict[str, Any]:
    project = resolve_project(store, selector)
    project = get_project(store, project["project_id"])
    if _project_lifecycle_state(store, project):
        return {"projectId": project["project_id"], "displayName": project["display_name"], "reused": True}
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "UPDATE projects SET active=1,deactivated_at=NULL,updated_at=? WHERE project_id=?",
            (now, project["project_id"]),
        )
        result = {"projectId": project["project_id"]}
        append_event_in_transaction(store.conn, kind="project.activated", project_id=project["project_id"], payload=result)
    return {"projectId": project["project_id"], "displayName": project["display_name"], **result, "reused": False}

def project_activity(store: Any, project_id: str, after: int = 0) -> dict[str, Any]:
    project = get_project(store, project_id)
    after = max(0, int(after))
    current = int(store.conn.execute("SELECT COALESCE(MAX(sequence),0) FROM events").fetchone()[0])
    rows = store.conn.execute(
        """
        SELECT w.workstream_id,w.title,w.kind,w.desired_state,w.completed_at,
               cp.phase,cp.summary,cp.next_action,cp.blocker_code,cp.blocker,cp.sequence AS checkpoint_sequence,
               (SELECT COUNT(*) FROM research_requests rr WHERE rr.workstream_id=w.workstream_id AND rr.state NOT IN ('answered','declined','acknowledged')) AS research_open,
               (SELECT COUNT(*) FROM coordination_requests cr WHERE cr.workstream_id=w.workstream_id AND cr.state <> 'acknowledged') AS coordination_open,
               (SELECT COUNT(*) FROM decisions d WHERE d.workstream_id=w.workstream_id AND d.state='open') AS decisions_open,
               (SELECT MAX(e.sequence) FROM events e WHERE e.workstream_id=w.workstream_id) AS changed_sequence
        FROM workstreams w
        LEFT JOIN workstream_checkpoints cp ON cp.workstream_id=w.workstream_id
          AND cp.sequence=(SELECT MAX(sequence) FROM workstream_checkpoints WHERE workstream_id=w.workstream_id)
        WHERE w.project_id=? AND w.kind='worker'
        ORDER BY w.created_at,w.workstream_id
        """,
        (project_id,),
    )
    cards = []
    for row in rows:
        if after and (row["changed_sequence"] is None or int(row["changed_sequence"]) <= after):
            continue
        attention = row["blocker"] or ("review requested" if row["phase"] == "ready_review" else None)
        cards.append({
            "workstreamId": row["workstream_id"],
            "outcome": row["title"],
            "owner": "Worker",
            "state": "completed" if row["desired_state"] == "completed" else (row["phase"] or "implementing"),
            "checkpoint": None if row["phase"] is None else {"phase": row["phase"], "summary": row["summary"], "sequence": row["checkpoint_sequence"]},
            "nextAction": row["next_action"] if row["next_action"] else ("Review completion packet" if row["desired_state"] == "completed" else "Continue task"),
            "attention": attention,
            "completed": row["desired_state"] == "completed",
            "needsUser": bool(attention or row["coordination_open"] or row["decisions_open"]),
            "open": {"coordination": row["coordination_open"], "research": row["research_open"], "decisions": row["decisions_open"]},
        })
    issue_rows = store.conn.execute(
        "SELECT i.*,w.kind AS reporter_kind,w.title AS reporter_title,(SELECT MAX(e.sequence) FROM events e WHERE e.payload_json LIKE '%' || i.issue_id || '%') AS changed_sequence FROM issues i JOIN workstreams w ON w.workstream_id=i.reporter_workstream_id WHERE i.project_id=? AND i.state <> 'resolved' ORDER BY CASE i.severity WHEN 'blocking' THEN 0 WHEN 'degraded' THEN 1 ELSE 2 END,i.created_at,i.issue_id",
        (project_id,),
    )
    for row in issue_rows:
        if after and (row["changed_sequence"] is None or int(row["changed_sequence"]) <= after):
            continue
        cards.append({
            "issueId": row["issue_id"],
            "projectId": row["project_id"],
            "reporterKind": row["reporter_kind"],
            "reporterWorkstreamId": row["reporter_workstream_id"],
            "severity": row["severity"],
            "state": row["state"],
            "summary": row["summary"],
            "nextAction": "Acknowledge and inspect issue" if row["state"] == "open" else ("Obtain reporter verification" if row["state"] == "verifying" else "Choose approved remediation"),
            "needsUser": True,
        })
    return {"projectId": project_id, "cards": cards, "after": after, "cursor": current}


def fleet_activity(store: Any, after: int = 0) -> dict[str, Any]:
    after = max(0, int(after))
    projects = [
        {
            "projectId": row["project_id"],
            "displayName": row["display_name"],
            "cards": project_activity(store, row["project_id"], after)["cards"],
        }
        for row in list_projects(store)
    ]
    return {"projects": projects, "after": after, "cursor": int(store.conn.execute("SELECT COALESCE(MAX(sequence),0) FROM events").fetchone()[0])}


def project_status(store: Any, project_id: str) -> dict[str, Any]:
    project = get_project(store, project_id)
    workstreams = [dict(row) for row in store.conn.execute("SELECT w.*,r.observed_state,r.last_observed_at,t.task_packet_id,t.packet_sha256 AS task_packet_sha256 FROM workstreams w LEFT JOIN runtime_bindings r USING(workstream_id) LEFT JOIN task_packets t USING(workstream_id) WHERE w.project_id=? ORDER BY w.created_at", (project_id,))]
    decisions = [dict(row) for row in store.conn.execute("SELECT * FROM decisions WHERE project_id=? ORDER BY created_at", (project_id,))]
    return {"project": project, "workstreams": workstreams, "decisions": decisions, "researchCounts": research_counts(store, project_id), "source": "pisec-sqlite"}
