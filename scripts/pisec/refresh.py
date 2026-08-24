"""Content-addressed rolling refresh for durable Pisec runtimes."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any, Mapping

from .adapters import HarnessAdapter, WorkspaceAdapter, artifact_document
from .fence import resolve_data_dirs
from .models import NeedsAttentionError, PisecError, utc_now
from .releases import active_runtime_release, release_scope
from .runtime import start_bound_agent


def _binding_scope(store: Any, binding: Mapping[str, Any]) -> dict[str, Any]:
    from .access import effective_runtime_scope
    return effective_runtime_scope(store, binding)


def _active_bindings(store: Any) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in store.conn.execute(
            "SELECT r.*,w.project_id,w.kind,w.execution_profile,w.worktree_path,w.desired_state,w.provisioning_state,p.display_name "
            "FROM runtime_bindings r JOIN workstreams w USING(workstream_id) JOIN projects p USING(project_id) "
            "WHERE p.active=1 AND w.desired_state='active' AND w.provisioning_state IN ('bound','needs_attention') "
            "ORDER BY p.display_name,w.kind,w.created_at,w.workstream_id"
        )
    ]


def mark_stale_bindings(store: Any, harness: HarnessAdapter, *, harness_resolver: Any | None = None) -> dict[str, Any]:
    stale: list[dict[str, str]] = []
    current: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for binding in _active_bindings(store):
        workstream_id = str(binding["workstream_id"])
        try:
            selected_harness = harness_resolver(workstream_id) if callable(harness_resolver) else harness
            release = active_runtime_release(store, selected_harness)
            desired = selected_harness.desired_generation(release_scope(_binding_scope(store, binding), release))
            if len(desired) != 64:
                raise PisecError("harness returned an invalid runtime generation")
            with store.transaction():
                store.conn.execute(
                    "UPDATE runtime_bindings SET desired_release_id=?,desired_generation_sha256=?,updated_at=? WHERE workstream_id=?",
                    (release["release_id"], desired, utc_now(), workstream_id),
                )
            item = {"project": str(binding["display_name"]), "workstreamId": workstream_id, "generation": desired}
            artifacts_current = True
            if selected_harness.manifest.adapter_id == "omp":
                try:
                    artifact_value = json.loads(str(binding["adapter_artifacts_json"]))
                    descriptor_path = selected_harness.launch_binding_path(workstream_id).with_name("binding.json")
                    descriptor_value = json.loads(descriptor_path.read_text())
                    home = Path(str(binding["harness_home"]))
                    artifacts_current = (
                        isinstance(artifact_value, dict)
                        and artifact_value.get("schemaVersion") == 2
                        and artifact_value.get("generationSha256") == desired
                        and isinstance(descriptor_value, dict)
                        and descriptor_value.get("schemaVersion") == 3
                        and descriptor_value.get("generationSha256") == desired
                        and not (home / "extensions" / "herdr-omp-agent-state.ts").exists()
                        and not (home / "agent" / "extensions" / "herdr-omp-agent-state.ts").exists()
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    artifacts_current = False
            if binding["applied_release_id"] == release["release_id"] and binding["applied_generation_sha256"] == desired and artifacts_current:
                current.append(item)
            else:
                if not artifacts_current and binding["applied_generation_sha256"] == desired:
                    with store.transaction():
                        store.conn.execute("UPDATE runtime_bindings SET applied_generation_sha256=NULL,updated_at=? WHERE workstream_id=?", (utc_now(), workstream_id))
                stale.append(item)
        except Exception as error:
            failed.append({"project": str(binding["display_name"]), "workstreamId": workstream_id, "reason": str(error)[:512]})
    return {"stale": stale, "current": current, "failed": failed}


def _runtime_state(store: Any, workstream_id: str) -> dict[str, Any]:
    row = store.conn.execute(
        "SELECT r.*,w.project_id,w.kind,w.execution_profile,w.worktree_path,w.desired_state,w.provisioning_state,p.display_name "
        "FROM runtime_bindings r JOIN workstreams w USING(workstream_id) JOIN projects p USING(project_id) WHERE r.workstream_id=?",
        (workstream_id,),
    ).fetchone()
    if row is None:
        raise NeedsAttentionError("runtime binding disappeared during refresh")
    return dict(row)


def _agent_state(workspace: WorkspaceAdapter, surface_id: str) -> str | None:
    snapshot = workspace.snapshot()
    agent = next((item for item in snapshot.get("agents", []) if isinstance(item, dict) and item.get("pane_id") == surface_id), None)
    state = None if agent is None else agent.get("agent_status")
    return state if isinstance(state, str) else None


def _wait_for_exit(workspace: WorkspaceAdapter, binding: Mapping[str, Any], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        runtime = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
        if runtime.state == "stopped":
            return
        if runtime.state == "unknown":
            raise NeedsAttentionError("runtime process identity became ambiguous during refresh")
        if time.monotonic() >= deadline:
            raise NeedsAttentionError("runtime did not stop gracefully")
        time.sleep(0.05)


def _wait_for_start(
    store: Any,
    workspace: WorkspaceAdapter,
    workstream_id: str,
    old_instance: str | None,
    generation: str,
    release_id: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        binding = _runtime_state(store, workstream_id)
        instance = binding["runtime_instance_id"]
        if instance and instance != old_instance and int(binding["report_seq"]) >= 1 and binding["applied_release_id"] == release_id and binding["applied_generation_sha256"] == generation:
            if workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"])).state == "live":
                return binding
        if time.monotonic() >= deadline:
            raise NeedsAttentionError("refreshed runtime did not attest the desired generation")
        time.sleep(0.05)




def _refresh_one(store: Any, harness: HarnessAdapter, workspace: WorkspaceAdapter, binding: Mapping[str, Any]) -> dict[str, Any]:
    workstream_id = str(binding["workstream_id"])
    binding = _runtime_state(store, workstream_id)
    desired = str(binding["desired_generation_sha256"])
    desired_release_id = str(binding["desired_release_id"])
    release = store.conn.execute("SELECT * FROM runtime_releases WHERE release_id=?", (desired_release_id,)).fetchone()
    if release is None:
        raise NeedsAttentionError("desired runtime release is missing")
    old_instance = str(binding["runtime_instance_id"]) if binding["runtime_instance_id"] else None
    runtime = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
    if runtime.state == "unknown":
        raise NeedsAttentionError("runtime process identity is ambiguous")
    if runtime.state == "live":
        latest = _runtime_state(store, workstream_id)
        if latest["observed_state"] != "idle" or latest["runtime_instance_id"] != binding["runtime_instance_id"] or latest["report_seq"] != binding["report_seq"]:
            return {"pending": True, "reason": f"runtime is {latest['observed_state']}"}
        workspace.stop_runtime(str(binding["workspace_surface_id"]))
        _wait_for_exit(workspace, binding)

    with store.transaction():
        store.conn.execute("UPDATE runtime_bindings SET refresh_pending=1,updated_at=? WHERE workstream_id=?", (utc_now(), workstream_id))
    scope = release_scope(_binding_scope(store, binding), dict(release))
    artifacts = harness.materialize_profile(scope)
    if artifacts.generation_sha256 != desired:
        raise NeedsAttentionError("runtime generation changed while it was being materialized")
    home = Path(artifacts.harness_home)
    if harness.manifest.adapter_id == "omp" and any((home / relative).exists() for relative in ("extensions/herdr-omp-agent-state.ts", "agent/extensions/herdr-omp-agent-state.ts")):
        raise NeedsAttentionError("fresh Pisec harness still contains the official Herdr OMP lifecycle extension")
    harness.commit_launch_binding(
        scope,
        artifacts,
        workspace_session_name=str(binding["workspace_session_name"]),
        workspace_id=str(binding["workspace_id"]),
        workspace_view_id=str(binding["workspace_view_id"]),
        workspace_surface_id=str(binding["workspace_surface_id"]),
        replace=True,
    )
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "UPDATE runtime_bindings SET harness_home=?,adapter_artifacts_json=?,launch_secret_path=?,policy_path=?,policy_sha256=?,runtime_token_sha256=?,desired_release_id=?,launch_release_id=?,desired_generation_sha256=?,launch_generation_sha256=?,runtime_instance_id=NULL,report_seq=0,observed_state='starting',last_observed_at=?,updated_at=? WHERE workstream_id=?",
            (
                artifacts.harness_home,
                artifact_document(harness.manifest, artifacts),
                artifacts.launch_secret_path,
                artifacts.policy_path,
                artifacts.policy_sha256,
                artifacts.runtime_token_sha256,
                desired_release_id,
                desired_release_id,
                desired,
                desired,
                now,
                now,
                workstream_id,
            ),
        )
        store.conn.execute(
            "UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL,updated_at=? WHERE workstream_id=?",
            (now, workstream_id),
        )
    refreshed = _runtime_state(store, workstream_id)
    start_bound_agent(
        store,
        workspace,
        harness,
        refreshed,
        workstream_id=workstream_id,
        project_id=str(refreshed["project_id"]),
        cwd=str(refreshed["worktree_path"]),
    )
    attested = _wait_for_start(store, workspace, workstream_id, old_instance, desired, desired_release_id)
    with store.transaction():
        store.conn.execute("UPDATE runtime_bindings SET refresh_pending=0,updated_at=? WHERE workstream_id=?", (utc_now(), workstream_id))
    attested = _runtime_state(store, workstream_id)
    return {"pending": False, "binding": _runtime_state(store, workstream_id)}


def refresh_projects(
    store: Any,
    harness: HarnessAdapter,
    workspace: WorkspaceAdapter,
    *,
    wait_seconds: float = 300.0,
    harness_resolver: Any | None = None,
) -> dict[str, Any]:
    if not isinstance(wait_seconds, (int, float)) or isinstance(wait_seconds, bool) or not 0 <= wait_seconds <= 3600:
        raise PisecError("refresh wait must be between 0 and 3600 seconds")
    marked = mark_stale_bindings(store, harness, harness_resolver=harness_resolver)
    result: dict[str, Any] = {"generation": None, "upgraded": [], "pending": [], "skipped": [], "failed": list(marked["failed"])}
    for item in marked["current"]:
        result["skipped"].append({**item, "reason": "current generation"})
    stale_ids = [str(item["workstreamId"]) for item in marked["stale"]]
    if stale_ids:
        placeholders = ",".join("?" for _ in stale_ids)
        recoverable = {
            str(row[0])
            for row in store.conn.execute(
                "SELECT workstream_id FROM runtime_bindings WHERE workstream_id IN (" + placeholders + ") AND observed_state <> 'missing'",
                stale_ids,
            )
        }
        stale_ids = [workstream_id for workstream_id in stale_ids if workstream_id in recoverable]
    if marked["stale"]:
        generations = {str(item["generation"]) for item in marked["stale"]}
        result["generation"] = next(iter(generations)) if len(generations) == 1 else "per-binding"
    deadline = time.monotonic() + float(wait_seconds)
    remaining = stale_ids
    while remaining:
        next_remaining: list[str] = []
        progressed = False
        for workstream_id in remaining:
            binding = _runtime_state(store, workstream_id)
            if binding["applied_release_id"] == binding["desired_release_id"] and binding["applied_generation_sha256"] == binding["desired_generation_sha256"]:
                result["skipped"].append({"project": binding["display_name"], "workstreamId": workstream_id, "generation": binding["desired_generation_sha256"], "reason": "current generation"})
                progressed = True
                continue
            if binding["observed_state"] not in {"idle", "stopped"}:
                next_remaining.append(workstream_id)
                continue
            try:
                selected_harness = harness_resolver(workstream_id) if callable(harness_resolver) else harness
                refreshed = _refresh_one(store, selected_harness, workspace, binding)
                if refreshed.get("pending"):
                    next_remaining.append(workstream_id)
                    continue
                current = refreshed["binding"]
                result["upgraded"].append(
                    {
                        "project": current["display_name"],
                        "workstreamId": workstream_id,
                        "workspaceId": current["workspace_id"],
                        "viewId": current["workspace_view_id"],
                        "surfaceId": current["workspace_surface_id"],
                        "nativeSessionKind": current["native_session_kind"],
                        "nativeSessionValue": current["native_session_value"],
                        "generation": current["applied_generation_sha256"],
                    }
                )
                progressed = True
            except Exception as error:
                recovery = "runtime remained live"
                try:
                    current = _runtime_state(store, workstream_id)
                    process = workspace.observe_runtime(str(current["workspace_surface_id"]), str(current["policy_path"]))
                    if process.state == "stopped":
                        with store.transaction():
                            store.conn.execute("UPDATE runtime_bindings SET refresh_pending=0,updated_at=? WHERE workstream_id=?", (utc_now(), workstream_id))
                        now = utc_now()
                        with store.transaction():
                            store.conn.execute(
                                "UPDATE runtime_bindings SET observed_state='starting',last_observed_at=?,updated_at=? WHERE workstream_id=?",
                                (now, now, workstream_id),
                            )
                        current = _runtime_state(store, workstream_id)
                        start_bound_agent(
                            store,
                            workspace,
                            harness_resolver(workstream_id) if callable(harness_resolver) else harness,
                            current,
                            workstream_id=workstream_id,
                            project_id=str(current["project_id"]),
                            cwd=str(current["worktree_path"]),
                        )
                        recovery = "runtime restart requested"
                except Exception as recovery_error:
                    recovery = f"runtime recovery failed: {recovery_error}"[:256]
                result["failed"].append({"project": binding["display_name"], "workstreamId": workstream_id, "reason": f"{error}; {recovery}"[:512]})
                progressed = True
        remaining = next_remaining
        if not remaining or time.monotonic() >= deadline:
            break
        if not progressed:
            time.sleep(0.2)
    for workstream_id in remaining:
        binding = _runtime_state(store, workstream_id)
        result["pending"].append({"project": binding["display_name"], "workstreamId": workstream_id, "state": binding["observed_state"], "generation": binding["desired_generation_sha256"]})
    for workstream_id in remaining:
        binding = _runtime_state(store, workstream_id)
        process = workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"]))
        if process.state == "stopped":
            with store.transaction():
                store.conn.execute("UPDATE runtime_bindings SET refresh_pending=0,updated_at=? WHERE workstream_id=?", (utc_now(), workstream_id))
    result["ok"] = not result["failed"]
    return result

def refresh_bindings(store: Any, harness: HarnessAdapter, workspace: WorkspaceAdapter, workstream_ids: list[str] | tuple[str, ...] | set[str], *, wait_seconds: float = 300.0, harness_resolver: Any | None = None) -> dict[str, Any]:
    selected = {str(value) for value in workstream_ids}
    if not selected:
        return {"upgraded": [], "pending": [], "failed": [], "skipped": [], "ok": True}
    marked = mark_stale_bindings(store, harness, harness_resolver=harness_resolver)
    selected_marked = [item for item in marked["stale"] if str(item["workstreamId"]) in selected]
    result = {"generation": None, "upgraded": [], "pending": [], "skipped": [item for item in marked["current"] if str(item["workstreamId"]) in selected], "failed": [], "ok": True}
    deadline = time.monotonic() + float(wait_seconds)
    remaining = [str(item["workstreamId"]) for item in selected_marked]
    attempted = False
    while remaining and (not attempted or time.monotonic() <= deadline):
        attempted = True
        next_remaining: list[str] = []
        for workstream_id in remaining:
            binding = _runtime_state(store, workstream_id)
            if binding["observed_state"] not in {"idle", "stopped"}:
                next_remaining.append(workstream_id)
                continue
            try:
                selected_harness = harness_resolver(workstream_id) if callable(harness_resolver) else harness
                refreshed = _refresh_one(store, selected_harness, workspace, binding)
                if refreshed.get("pending"):
                    next_remaining.append(workstream_id)
                else:
                    current = refreshed["binding"]
                    result["upgraded"].append({"workstreamId": workstream_id, "generation": current["applied_generation_sha256"]})
            except Exception as error:
                result["failed"].append({"workstreamId": workstream_id, "reason": str(error)[:512]})
        if next_remaining == remaining:
            time.sleep(0.05)
        remaining = next_remaining
    result["pending"] = [{"workstreamId": workstream_id, "state": _runtime_state(store, workstream_id)["observed_state"]} for workstream_id in remaining]
    result["ok"] = not result["failed"]
    return result
