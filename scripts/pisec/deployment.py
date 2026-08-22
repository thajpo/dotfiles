"""Broker-side deployment recipe classification and durable action state."""
from __future__ import annotations

import json
import os
import platform as _platform
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import ConflictError, InvalidRequestError, NeedsAttentionError, NotFoundError, canonical_json, json_digest, new_id, utc_now

PASSIVE_PREFIXES = ("README.md", "tests/", ".github/", ".gitignore", ".gitattributes", "pisec/config.example.json")
LINUX_UNITS = {"systemd/user/pisec-broker.service": "restart-pisec-broker", "systemd/user/pisec-auth-broker.service": "restart-pisec-broker", "systemd/user/pisec-auth-gateway.service": "restart-pisec-broker", "systemd/user/herdr.service": "restart-herdr"}


def classify_changed_paths(paths: Iterable[str], *, platform: str | None = None) -> list[str]:
    selected_platform = platform or ("macos" if _platform.system() == "Darwin" else "linux")
    if selected_platform not in {"linux", "macos"}:
        raise InvalidRequestError("deployment platform is unsupported")
    names = sorted(set(str(path) for path in paths))
    if selected_platform == "macos" and any(path.startswith(("scripts/pisec_launchd.py", "scripts/agent-workflow-install.sh")) for path in names):
        raise NeedsAttentionError("merged changes require the full installer")
    steps: list[str] = []
    def add(step: str) -> None:
        if step not in steps:
            steps.append(step)
    for path in names:
        if any(path == prefix or path.startswith(prefix) for prefix in PASSIVE_PREFIXES):
            continue
        if path.startswith("scripts/pisec/") or path == "bin/pisec":
            add("restart-pisec-broker")
            if path.startswith("scripts/pisec/"):
                add("refresh-pisec-runtimes")
        elif path.startswith(("omp/", "pisec/fence/", "pisec/runtime-bin/", "agent/", "skills/", "opencode/")):
            add("refresh-pisec-runtimes")
        elif path.startswith("herdr/"):
            add("restart-herdr")
        elif path in LINUX_UNITS:
            add("daemon-reload")
            add(LINUX_UNITS[path])
        elif path.startswith(("patches/", "systemd/")) or path.endswith(".service") or path.startswith(("scripts/agent-workflow-install.sh", "scripts/pisec-maintenance.py")):
            raise NeedsAttentionError("merged changes require the full installer")
        else:
            raise NeedsAttentionError(f"merged changes require the full installer: {path}")
    add("run-pisec-doctor")
    return steps


def prepare_deployment(store: Any, *, project_id: str, issue_id: str, workstream_id: str, source_commit_oid: str, target_branch: str, changed_paths: Iterable[str], installed_root: str, platform: str | None = None, idempotency_key: str) -> dict[str, Any]:
    project = store.conn.execute("SELECT repository_path,active FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if project is None or not project["active"]:
        raise NotFoundError("active project was not found")
    issue = store.conn.execute("SELECT state FROM issues WHERE issue_id=? AND project_id=?", (issue_id, project_id)).fetchone()
    if issue is None or issue["state"] == "resolved":
        raise ConflictError("deployment issue is not unresolved")
    receipt = store.conn.execute("SELECT * FROM merge_receipts WHERE workstream_id=? AND source_commit_oid=? AND target_branch=?", (workstream_id, source_commit_oid, target_branch)).fetchone()
    if receipt is None:
        raise ConflictError("deployment requires an exact merge receipt")
    root = str(Path(installed_root).resolve(strict=True))
    if root != str(Path(project["repository_path"]).resolve(strict=True)):
        raise NeedsAttentionError("installed control-plane root does not match project repository")
    steps = classify_changed_paths(changed_paths, platform=platform)
    request = {"projectId": project_id, "issueId": issue_id, "workstreamId": workstream_id, "sourceCommitOid": source_commit_oid, "targetBranch": target_branch, "installedRoot": root, "platform": platform or ("macos" if _platform.system() == "Darwin" else "linux")}
    recipe = {"steps": steps, "sourceCommitOid": source_commit_oid, "targetBranch": target_branch}
    existing = store.conn.execute("SELECT * FROM deployment_actions WHERE request_sha256=?", (json_digest(request),)).fetchone()
    if existing is not None:
        return {"deployment": dict(existing), "approvalScope": json.loads(existing["request_json"] or "{}"), "reused": True}
    op_id = new_id("op"); deployment_id = new_id("dep"); now = utc_now(); recipe_json = canonical_json(recipe)
    scope = {"kind": "deployment.apply", "operationId": op_id, "deploymentId": deployment_id, **request, "recipe": recipe, "effects": ["run only listed fixed maintenance steps"], "nonEffects": ["no arbitrary shell", "no project file edits outside merged commit", "no unapproved network or access policy changes"]}
    with store.transaction():
        store.conn.execute("INSERT INTO operations(operation_id,kind,project_id,workstream_id,idempotency_key,request_json,request_sha256,state,step,created_at,updated_at) VALUES(?,?,?,?,?,?, 'planned','planned',?,?)", (op_id, "deployment.apply", project_id, workstream_id, idempotency_key, canonical_json(request), json_digest(request), now, now))
        store.conn.execute("INSERT INTO deployment_actions(deployment_id,project_id,workstream_id,source_commit_oid,target_branch,platform,installed_control_plane_root,recipe_json,recipe_sha256,request_json,request_sha256,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'planned',?,?)", (deployment_id, project_id, workstream_id, source_commit_oid, target_branch, scope["platform"], root, recipe_json, json_digest(recipe), canonical_json(scope), json_digest(request), now, now))
    return {"deployment": dict(store.conn.execute("SELECT * FROM deployment_actions WHERE deployment_id=?", (deployment_id,)).fetchone()), "approvalScope": scope, "reused": False}


def list_deployments(store: Any, *, project_id: str | None = None) -> list[dict[str, Any]]:
    if project_id is None:
        return [dict(row) for row in store.conn.execute("SELECT * FROM deployment_actions ORDER BY created_at,deployment_id")]
    return [dict(row) for row in store.conn.execute("SELECT * FROM deployment_actions WHERE project_id=? ORDER BY created_at,deployment_id", (project_id,))]


def inspect_deployment(store: Any, deployment_id: str) -> dict[str, Any]:
    row = store.conn.execute("SELECT * FROM deployment_actions WHERE deployment_id=?", (deployment_id,)).fetchone()
    if row is None:
        raise NotFoundError("deployment action was not found")
    value = dict(row); value["recipe"] = json.loads(value["recipe_json"]); value["request"] = json.loads(value["request_json"] or "{}"); return value
