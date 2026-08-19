"""Content-addressed rolling refresh for durable Pisec runtimes."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any, Mapping

from .adapters import HarnessAdapter, WorkspaceAdapter, artifact_document
from .models import NeedsAttentionError, PisecError, utc_now
from .runtime import start_bound_agent


def _binding_scope(store: Any, binding: Mapping[str, Any]) -> dict[str, Any]:
    operation_kind = "secretary.ensure" if binding["kind"] == "secretary" else "workstream.create"
    row = store.conn.execute(
        "SELECT result_json FROM operations WHERE workstream_id=? AND kind=? ORDER BY created_at LIMIT 1",
        (binding["workstream_id"], operation_kind),
    ).fetchone()
    if row is None:
        raise NeedsAttentionError("runtime generation scope is missing")
    try:
        scope = json.loads(str(row["result_json"]))
    except (TypeError, json.JSONDecodeError) as error:
        raise NeedsAttentionError("runtime generation scope is invalid") from error
    if not isinstance(scope, dict) or scope.get("workstreamId") != binding["workstream_id"] or scope.get("projectId") != binding["project_id"]:
        raise NeedsAttentionError("runtime generation scope does not match the binding")
    return scope


def _active_bindings(store: Any) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in store.conn.execute(
            "SELECT r.*,w.project_id,w.kind,w.execution_profile,w.worktree_path,w.desired_state,w.provisioning_state,p.display_name "
            "FROM runtime_bindings r JOIN workstreams w USING(workstream_id) JOIN projects p USING(project_id) "
            "WHERE w.desired_state='active' AND w.provisioning_state='bound' "
            "ORDER BY p.display_name,w.kind,w.created_at,w.workstream_id"
        )
    ]


def mark_stale_bindings(store: Any, harness: HarnessAdapter) -> dict[str, Any]:
    stale: list[dict[str, str]] = []
    current: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for binding in _active_bindings(store):
        workstream_id = str(binding["workstream_id"])
        try:
            desired = harness.desired_generation(_binding_scope(store, binding))
            if len(desired) != 64:
                raise PisecError("harness returned an invalid runtime generation")
            with store.transaction():
                store.conn.execute(
                    "UPDATE runtime_bindings SET desired_generation_sha256=?,updated_at=? WHERE workstream_id=?",
                    (desired, utc_now(), workstream_id),
                )
            item = {"project": str(binding["display_name"]), "workstreamId": workstream_id, "generation": desired}
            artifacts_current = True
            if harness.manifest.adapter_id == "omp":
                try:
                    artifact_value = json.loads(str(binding["adapter_artifacts_json"]))
                    descriptor_path = harness.launch_binding_path(workstream_id).with_name("binding.json")
                    descriptor_value = json.loads(descriptor_path.read_text())
                    home = Path(str(binding["harness_home"]))
                    artifacts_current = (
                        isinstance(artifact_value, dict)
                        and artifact_value.get("schemaVersion") == 2
                        and artifact_value.get("generationSha256") == desired
                        and isinstance(descriptor_value, dict)
                        and descriptor_value.get("schemaVersion") == 2
                        and descriptor_value.get("generationSha256") == desired
                        and not (home / "extensions" / "herdr-omp-agent-state.ts").exists()
                        and not (home / "agent" / "extensions" / "herdr-omp-agent-state.ts").exists()
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    artifacts_current = False
            if binding["applied_generation_sha256"] == desired and artifacts_current:
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
    timeout: float = 30.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        binding = _runtime_state(store, workstream_id)
        instance = binding["runtime_instance_id"]
        if instance and instance != old_instance and int(binding["report_seq"]) >= 1 and binding["applied_generation_sha256"] == generation:
            if workspace.observe_runtime(str(binding["workspace_surface_id"]), str(binding["policy_path"])).state == "live":
                return binding
        if time.monotonic() >= deadline:
            raise NeedsAttentionError("refreshed runtime did not attest the desired generation")
        time.sleep(0.05)


def _verify_lifecycle(store: Any, workspace: WorkspaceAdapter, binding: Mapping[str, Any], timeout: float = 45.0) -> None:
    if workspace.manifest.adapter_id != "herdr":
        return
    surface_id = str(binding["workspace_surface_id"])
    workspace.prompt_agent_nowait(
        surface_id,
        "Pisec runtime refresh verification only: run the Bash command sleep 2, then reply exactly PISEC_REFRESH_VERIFIED. Do not modify files.",
    )
    deadline = time.monotonic() + timeout
    saw_working = False
    while True:
        runtime = _runtime_state(store, str(binding["workstream_id"]))
        herdr_state = _agent_state(workspace, surface_id)
        if runtime["observed_state"] == "working" and herdr_state == "working":
            saw_working = True
        if saw_working and runtime["observed_state"] == "idle" and herdr_state in {"idle", "done"}:
            return
        if time.monotonic() >= deadline:
            boundary = "working" if not saw_working else "idle/done completion"
            raise NeedsAttentionError(f"Herdr did not expose the refreshed runtime {boundary} transition")
        time.sleep(0.05)


def _refresh_one(store: Any, harness: HarnessAdapter, workspace: WorkspaceAdapter, binding: Mapping[str, Any]) -> dict[str, Any]:
    workstream_id = str(binding["workstream_id"])
    with store.transaction():
        store.conn.execute("UPDATE runtime_bindings SET refresh_pending=1,updated_at=? WHERE workstream_id=?", (utc_now(), workstream_id))
    binding = _runtime_state(store, workstream_id)
    desired = str(binding["desired_generation_sha256"])
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

    scope = _binding_scope(store, binding)
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
            "UPDATE runtime_bindings SET harness_home=?,adapter_artifacts_json=?,launch_secret_path=?,policy_path=?,policy_sha256=?,runtime_token_sha256=?,desired_generation_sha256=?,launch_generation_sha256=?,runtime_instance_id=NULL,report_seq=0,observed_state='starting',last_observed_at=?,updated_at=? WHERE workstream_id=?",
            (
                artifacts.harness_home,
                artifact_document(harness.manifest, artifacts),
                artifacts.launch_secret_path,
                artifacts.policy_path,
                artifacts.policy_sha256,
                artifacts.runtime_token_sha256,
                desired,
                desired,
                now,
                now,
                workstream_id,
            ),
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
    attested = _wait_for_start(store, workspace, workstream_id, old_instance, desired)
    with store.transaction():
        store.conn.execute("UPDATE runtime_bindings SET refresh_pending=0,updated_at=? WHERE workstream_id=?", (utc_now(), workstream_id))
    attested = _runtime_state(store, workstream_id)
    _verify_lifecycle(store, workspace, attested)
    return {"pending": False, "binding": _runtime_state(store, workstream_id)}


def refresh_projects(
    store: Any,
    harness: HarnessAdapter,
    workspace: WorkspaceAdapter,
    *,
    wait_seconds: float = 300.0,
) -> dict[str, Any]:
    if not isinstance(wait_seconds, (int, float)) or isinstance(wait_seconds, bool) or not 0 <= wait_seconds <= 3600:
        raise PisecError("refresh wait must be between 0 and 3600 seconds")
    marked = mark_stale_bindings(store, harness)
    result: dict[str, Any] = {"generation": None, "upgraded": [], "pending": [], "skipped": [], "failed": list(marked["failed"])}
    for item in marked["current"]:
        result["skipped"].append({**item, "reason": "current generation"})
    stale_ids = [str(item["workstreamId"]) for item in marked["stale"]]
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
            if binding["applied_generation_sha256"] == binding["desired_generation_sha256"]:
                result["skipped"].append({"project": binding["display_name"], "workstreamId": workstream_id, "generation": binding["desired_generation_sha256"], "reason": "current generation"})
                progressed = True
                continue
            if binding["observed_state"] not in {"idle", "stopped"}:
                next_remaining.append(workstream_id)
                continue
            try:
                refreshed = _refresh_one(store, harness, workspace, binding)
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
                    with store.transaction():
                        store.conn.execute("UPDATE runtime_bindings SET refresh_pending=0,updated_at=? WHERE workstream_id=?", (utc_now(), workstream_id))
                    current = _runtime_state(store, workstream_id)
                    process = workspace.observe_runtime(str(current["workspace_surface_id"]), str(current["policy_path"]))
                    if process.state == "stopped":
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
                            harness,
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
    if remaining:
        now = utc_now()
        with store.transaction():
            store.conn.executemany("UPDATE runtime_bindings SET refresh_pending=0,updated_at=? WHERE workstream_id=?", ((now, workstream_id) for workstream_id in remaining))
    result["ok"] = not result["failed"]
    return result
