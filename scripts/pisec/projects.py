"""Canonical Git project registration and status."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from .events import append_event_in_transaction
from .fence import resolve_data_dirs
from .models import InvalidRequestError, NotFoundError, bounded_text, new_id, utc_now, validate_id
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


def list_projects(store: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in store.conn.execute("SELECT * FROM projects ORDER BY display_name,project_id")]


def project_status(store: Any, project_id: str) -> dict[str, Any]:
    project = get_project(store, project_id)
    workstreams = [dict(row) for row in store.conn.execute("SELECT w.*,r.observed_state,r.last_observed_at,t.task_packet_id,t.packet_sha256 AS task_packet_sha256 FROM workstreams w LEFT JOIN runtime_bindings r USING(workstream_id) LEFT JOIN task_packets t USING(workstream_id) WHERE w.project_id=? ORDER BY w.created_at", (project_id,))]
    decisions = [dict(row) for row in store.conn.execute("SELECT * FROM decisions WHERE project_id=? ORDER BY created_at", (project_id,))]
    return {"project": project, "workstreams": workstreams, "decisions": decisions, "researchCounts": research_counts(store, project_id), "source": "pisec-sqlite"}
