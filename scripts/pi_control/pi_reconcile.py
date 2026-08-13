"""Fresh-state observation and fail-closed restart/recovery decisions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .events import append_event_in_transaction
from .git_adapter import GitObservationError, observe_repository
from .models import utc_now, validate_id
from .process_adapter import observe_process


class ReconcileError(RuntimeError):
    pass


def _observe_sidecar(pid: int | None, start_identity: str | None, label: str) -> dict[str, Any]:
    """Observe one run process identity (owner or child) fail-closed."""
    if pid is None or not start_identity:
        return {"label": label, "state": "absent", "reason": "missing-identity"}
    observation = observe_process(int(pid), expected_start_identity=str(start_identity))
    if observation.state == "?":
        return {"label": label, "state": "unknown", "reason": observation.error or "process-state-unavailable", "observation": observation.as_dict()}
    if observation.state in {"reused", "unknown"}:
        return {"label": label, "state": "needs_attention", "reason": observation.error or observation.state, "observation": observation.as_dict()}
    if not observation.exists:
        return {"label": label, "state": "gone", "reason": "process-gone", "observation": observation.as_dict()}
    return {"label": label, "state": "running", "reason": "identity-matches", "observation": observation.as_dict()}


def _run_observation(row: Any) -> dict[str, Any]:
    owner = _observe_sidecar(row["owner_pid"], row["owner_start_identity"], "owner")
    child = _observe_sidecar(row["child_pid"], row["child_start_identity"], "child")
    both = (owner, child)
    failures = [item for item in both if item["state"] in {"gone", "unknown", "needs_attention"}]
    if owner["state"] == "running" and child["state"] in {"running", "absent"} and not failures:
        return {"state": "running", "reason": "owner-identity-matches", "owner": owner, "child": child}
    if failures:
        worst = failures[0]
        reason = f"{worst['label']}-process-gone" if worst["state"] == "gone" else worst["reason"]
        return {"state": "needs_attention", "reason": reason, "owner": owner, "child": child, "observation": worst.get("observation")}
    return {"state": "unknown", "reason": "no-running-owner", "owner": owner, "child": child}


def reconcile_run(store: Any, *, run_id: str) -> dict[str, Any]:
    validate_id(run_id, prefix="run")
    row = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ReconcileError("run not found")
    result = _run_observation(row)
    if row["desired_state"] == "stopped":
        return {"run": dict(row), "observation": result, "decision": "already-stopped"}
    if result["state"] == "running":
        return {"run": dict(row), "observation": result, "decision": "continue"}
    now = utc_now()
    with store.transaction():
        store.conn.execute("UPDATE runs SET observed_state='needs_attention',error_code='CP_OWNER_UNCERTAIN',error_detail=?,updated_at=? WHERE run_id=? AND observed_state NOT IN ('stopped','failed')", (result["reason"][:1024], now, run_id))
        append_event_in_transaction(store.conn, event_kind="run.needs_attention", resource_type="run", resource_id=run_id, resource_version=int(row["resource_version"]), payload={"runId": run_id, "reason": result["reason"], "observation": result.get("observation")})
    return {"run": dict(store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()), "observation": result, "decision": "needs-attention"}


def recover_conversation_runs(store: Any, *, conversation_id: str, actor_id: str) -> dict[str, Any]:
    """Recover every provably lost run of one conversation before surface repair.

    Surface repair calls this before respawning a dead pane for a
    conversation. A run whose exact owner is still alive stays untouched; a
    run that cannot be proved recoverable (uncertain process, held writer
    lock, or unproved container absence) is reported and refuses relaunch.
    """
    validate_id(conversation_id, prefix="conv")
    if not isinstance(actor_id, str) or not actor_id or len(actor_id) > 256:
        raise ReconcileError("recovery actor is invalid")
    runs = []
    for row in store.conn.execute("SELECT * FROM runs WHERE conversation_id=? AND desired_state='running'", (conversation_id,)):
        observed = _run_observation(row)
        if observed["state"] == "running":
            runs.append({"runId": row["run_id"], "decision": "live"})
            continue
        try:
            with store.transaction():
                store.conn.execute("UPDATE runs SET observed_state='needs_attention',error_code='CP_OWNER_UNCERTAIN',error_detail=?,updated_at=? WHERE run_id=? AND observed_state NOT IN ('stopped','failed')", (observed["reason"][:1024], utc_now(), row["run_id"]))
            recovered = recover_lost_run(store, run_id=row["run_id"], actor_id=actor_id)
            runs.append({"runId": row["run_id"], "decision": "recovered", "run": dict(recovered)})
        except ReconcileError as error:
            runs.append({"runId": row["run_id"], "decision": "uncertain", "error": str(error)[:1024]})
    return {"conversationId": conversation_id, "actorId": actor_id, "runs": runs}


def recover_lost_run(store: Any, *, run_id: str, actor_id: str) -> dict[str, Any]:
    """Release a blocked writer only after process identity and kernel-lock proofs."""

    validate_id(run_id, prefix="run")
    if not isinstance(actor_id, str) or not actor_id or len(actor_id) > 256:
        raise ReconcileError("recovery actor is invalid")
    row = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ReconcileError("run not found")
    if row["observed_state"] == "lost":
        return dict(row)
    if row["observed_state"] != "needs_attention":
        raise ReconcileError("run is not awaiting explicit recovery")
    is_writer = row["authority"] == "writer-container"
    observation = _run_observation(row)
    for sidecar in (observation.get("owner"), observation.get("child")):
        if sidecar is None:
            continue
        if sidecar["state"] not in {"gone", "reused", "absent"}:
            raise ReconcileError("run process is still present or unobservable")
    working_copy_id = row["working_copy_id"]
    if is_writer and working_copy_id:
        from .writer_lock import writer_lock_available
        try:
            available = writer_lock_available(store.state_root, working_copy_id)
        except Exception as error:
            raise ReconcileError("writer lock probe failed; recovery is unsafe") from error
        if not available:
            raise ReconcileError("writer lock is still held; recovery is unsafe")
    container_cleanup: dict[str, Any] | None = None
    if is_writer and row["container_id"] is not None:
        from .docker_runtime import cleanup_run_container
        try:
            container_cleanup = cleanup_run_container(store, run_id=run_id)
        except Exception as error:
            raise ReconcileError("writer container absence is not proved; recovery is unsafe") from error
        if not container_cleanup.get("absent"):
            raise ReconcileError("writer container absence is not proved; recovery is unsafe")
    now = utc_now()
    with store.transaction():
        store.conn.execute("UPDATE runs SET desired_state='stopped',observed_state='lost',ended_at=?,updated_at=?,error_code='CP_OWNER_LOST',error_detail=? WHERE run_id=? AND observed_state='needs_attention'", (now, now, f"explicit recovery by {actor_id}", run_id))
        if is_writer and working_copy_id:
            store.conn.execute("UPDATE working_copies SET active_writer_run_id=NULL,updated_at=?,resource_version=resource_version+1 WHERE working_copy_id=? AND active_writer_run_id=?", (now, working_copy_id, run_id))
        append_event_in_transaction(store.conn, event_kind="run.recovered-lost", resource_type="run", resource_id=run_id, resource_version=int(row["resource_version"]), payload={"runId": run_id, "actorId": actor_id, "containerCleanup": container_cleanup})
    return dict(store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())


def reconcile_project(store: Any, *, project_id: str) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if project is None:
        raise ReconcileError("project not found")
    try:
        observation = observe_repository(project["primary_checkout"])
        project_state = "ready" if observation.head_oid or observation.is_bare else "unknown"
        project_error = None
    except GitObservationError as error:
        observation = None
        project_state = "missing" if getattr(error, "kind", "") == "missing" else "error"
        project_error = str(error)[:1024]
    now = utc_now()
    with store.transaction():
        store.conn.execute("UPDATE projects SET observed_state=?,last_reconciled_at=?,updated_at=?,resource_version=resource_version+1,error_code=?,error_detail=? WHERE project_id=?", (project_state, now, now, None if project_error is None else "CP_GIT_OBSERVATION", project_error, project_id))
        if observation is not None:
            for item in store.conn.execute("SELECT * FROM working_copies WHERE project_id=?", (project_id,)):
                state = "missing"
                try:
                    wc_observation = observe_repository(item["path"])
                    state = "dirty" if wc_observation.dirty else ("ready" if wc_observation.head_oid else "unknown")
                except GitObservationError:
                    wc_observation = None
                store.conn.execute("UPDATE working_copies SET observed_state=?,last_reconciled_at=?,updated_at=?,resource_version=resource_version+1 WHERE working_copy_id=?", (state, now, now, item["working_copy_id"]))
                append_event_in_transaction(store.conn, event_kind="working_copy.reconciled", resource_type="working_copy", resource_id=item["working_copy_id"], resource_version=int(item["resource_version"]) + 1, payload={"workingCopyId": item["working_copy_id"], "observedState": state})
        append_event_in_transaction(store.conn, event_kind="project.reconciled", resource_type="project", resource_id=project_id, resource_version=int(project["resource_version"]) + 1, payload={"projectId": project_id, "observedState": project_state, "error": project_error})
    runs = []
    for row in store.conn.execute("SELECT * FROM runs WHERE project_id=? AND desired_state='running'", (project_id,)):
        runs.append(reconcile_run(store, run_id=row["run_id"]))
    return {"project": dict(store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()), "runs": runs, "source": "pi-system-reconciler"}


__all__ = ["ReconcileError", "reconcile_project", "reconcile_run", "recover_conversation_runs", "recover_lost_run"]
