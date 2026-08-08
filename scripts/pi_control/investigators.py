"""Temporary host read-only investigator lifecycle."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import json
from typing import Any, Callable, Mapping

from .conversations import create_conversation
from .launch import attest_run, prepare_run, stop_run
from .models import bounded_text, canonical_json, new_id, utc_now, validate_id
from .scoped_read import ScopedProjectReader
from .messages import post_message

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="pi-investigator")


class InvestigatorError(RuntimeError):
    pass


def start_investigation(store: Any, *, project_id: str, purpose: str, working_copy_id: str | None = None) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    conversation = create_conversation(store, project_id=project_id, role="host", display_name="investigator: " + purpose[:96], pi_session_id="pi-investigator-" + new_id("run"), working_copy_id=working_copy_id)
    prepared = prepare_run(store, project_id=project_id, conversation_id=conversation["conversation_id"], working_copy_id=working_copy_id, authority="read-only")
    attest_run(store, run_id=prepared.run["run_id"], manifest_digest=prepared.manifest["manifestDigest"])
    investigation_id = new_id("inv")
    now = utc_now()
    with store.transaction():
        store.conn.execute("INSERT INTO investigations(investigation_id,project_id,conversation_id,run_id,purpose,state,result_json,created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (investigation_id, project_id, conversation["conversation_id"], prepared.run["run_id"], bounded_text(purpose, name="purpose", limit=1024), "running", None, now, now, None))
    prepared.close()
    return dict(store.conn.execute("SELECT * FROM investigations WHERE investigation_id=?", (investigation_id,)).fetchone())


def run_investigation(store: Any, investigation_id: str, task: Callable[[ScopedProjectReader], Mapping[str, Any]]) -> Future[Any]:
    validate_id(investigation_id, prefix="inv")
    row = store.conn.execute("SELECT * FROM investigations WHERE investigation_id=?", (investigation_id,)).fetchone()
    if row is None or row["state"] != "running":
        raise InvestigatorError("investigation is not running")
    def worker() -> dict[str, Any]:
        try:
            with store.__class__(store.state_root, read_only=True) as read_store:
                reader = ScopedProjectReader(read_store, project_id=row["project_id"], working_copy_id=store.conn.execute("SELECT working_copy_id FROM conversations WHERE conversation_id=?", (row["conversation_id"],)).fetchone()[0])
                result = dict(task(reader))
            complete_investigation(store, investigation_id, state="completed", result=result)
            return result
        except Exception as error:
            complete_investigation(store, investigation_id, state="failed", result={"error": type(error).__name__, "message": str(error)[:1024]})
            raise
    return _POOL.submit(worker)


def complete_investigation(store: Any, investigation_id: str, *, state: str, result: Mapping[str, Any]) -> dict[str, Any]:
    validate_id(investigation_id, prefix="inv")
    if state not in {"completed", "failed", "needs-user", "interrupted"}:
        raise InvestigatorError("investigation terminal state is invalid")
    with store.transaction():
        row = store.conn.execute("SELECT * FROM investigations WHERE investigation_id=?", (investigation_id,)).fetchone()
        if row is None:
            raise InvestigatorError("investigation not found")
        now = utc_now()
        store.conn.execute("UPDATE investigations SET state=?,result_json=?,updated_at=?,completed_at=? WHERE investigation_id=?", (state, canonical_json(dict(result)), now, now, investigation_id))
        store.conn.execute("UPDATE runs SET desired_state='stopped',observed_state='stopped',ended_at=?,updated_at=? WHERE run_id=? AND observed_state NOT IN ('stopped','failed')", (now, now, row["run_id"]))
        completed = dict(store.conn.execute("SELECT * FROM investigations WHERE investigation_id=?", (investigation_id,)).fetchone())
    kind = "needs-user" if state == "needs-user" else "failure" if state == "failed" else "interrupted" if state == "interrupted" else "progress"
    post_message(store, project_id=completed["project_id"], conversation_id=completed["conversation_id"], run_id=completed["run_id"], kind=kind, payload={"investigationId": investigation_id, "state": state, "result": dict(result)}, idempotency_key=f"investigation:{investigation_id}:{state}")
    return completed


def interrupt_running(store: Any, *, project_id: str) -> list[dict[str, Any]]:
    validate_id(project_id, prefix="prj")
    rows = []
    with store.transaction():
        for row in store.conn.execute("SELECT * FROM investigations WHERE project_id=? AND state='running'", (project_id,)):
            now = utc_now()
            store.conn.execute("UPDATE investigations SET state='interrupted',updated_at=?,completed_at=? WHERE investigation_id=?", (now, now, row["investigation_id"]))
            store.conn.execute("UPDATE runs SET desired_state='stopped',observed_state='stopped',ended_at=?,updated_at=? WHERE run_id=?", (now, now, row["run_id"]))
            rows.append(dict(store.conn.execute("SELECT * FROM investigations WHERE investigation_id=?", (row["investigation_id"],)).fetchone()))
    return rows


__all__ = ["InvestigatorError", "complete_investigation", "interrupt_running", "run_investigation", "start_investigation"]
