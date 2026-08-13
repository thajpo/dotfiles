"""Controller-created temporary host investigator assignments and terminals."""

from __future__ import annotations

from typing import Any, Mapping

from .conversations import create_conversation
from .models import bounded_text, canonical_json, new_id, utc_now, validate_id


class InvestigatorError(RuntimeError):
    pass


def start_investigation(store: Any, *, project_id: str, purpose: str, working_copy_id: str | None = None) -> dict[str, Any]:
    """Create an assignment only; the host supervisor creates and owns its run."""

    validate_id(project_id, prefix="prj")
    project = store.conn.execute("SELECT * FROM projects WHERE project_id=? AND desired_state='active'", (project_id,)).fetchone()
    if project is None:
        raise InvestigatorError("active investigation project was not found")
    if working_copy_id is None:
        working = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary' AND desired_state='present'", (project_id,)).fetchone()
    else:
        validate_id(working_copy_id, prefix="wc")
        working = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND working_copy_id=? AND desired_state='present'", (project_id, working_copy_id)).fetchone()
    if working is None or working["observed_state"] not in {"ready", "dirty"}:
        raise InvestigatorError("investigation read scope is unavailable")
    conversation = create_conversation(
        store,
        project_id=project_id,
        role="investigator",
        display_name="investigator: " + bounded_text(purpose, name="purpose", limit=1024)[:96],
        working_copy_id=working["working_copy_id"],
    )
    investigation_id = new_id("inv")
    now = utc_now()
    with store.transaction():
        store.conn.execute("UPDATE conversations SET observed_state='ready',updated_at=? WHERE conversation_id=?", (now, conversation["conversation_id"]))
        store.conn.execute(
            "INSERT INTO investigations(investigation_id,project_id,conversation_id,run_id,purpose,state,result_json,created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (investigation_id, project_id, conversation["conversation_id"], None, bounded_text(purpose, name="purpose", limit=1024), "running", None, now, now, None),
        )
    result = dict(store.conn.execute("SELECT * FROM investigations WHERE investigation_id=?", (investigation_id,)).fetchone())
    result["working_copy_id"] = str(working["working_copy_id"])
    result["pi_session_id"] = conversation["pi_session_id"]
    result["session_file"] = conversation["session_file"]
    return result


def bind_investigation_run(store: Any, *, conversation_id: str, run_id: str) -> dict[str, Any]:
    validate_id(conversation_id, prefix="conv")
    validate_id(run_id, prefix="run")
    with store.transaction():
        row = store.conn.execute("SELECT * FROM investigations WHERE conversation_id=? AND state IN ('running','interrupted')", (conversation_id,)).fetchone()
        if row is None:
            raise InvestigatorError("investigator conversation has no active assignment")
        if row["state"] == "interrupted":
            # Resume: reopen the interrupted investigation for a new controller run.
            store.conn.execute("UPDATE investigations SET state='running',run_id=?,result_json=NULL,completed_at=NULL,updated_at=? WHERE investigation_id=?", (run_id, utc_now(), row["investigation_id"]))
            return dict(store.conn.execute("SELECT * FROM investigations WHERE investigation_id=?", (row["investigation_id"],)).fetchone())
        if row["run_id"] is not None and row["run_id"] != run_id:
            raise InvestigatorError("investigator conversation has no unbound active assignment")
        store.conn.execute("UPDATE investigations SET run_id=?,updated_at=? WHERE investigation_id=?", (run_id, utc_now(), row["investigation_id"]))
        return dict(store.conn.execute("SELECT * FROM investigations WHERE investigation_id=?", (row["investigation_id"],)).fetchone())


def complete_investigation(store: Any, investigation_id: str, *, state: str, result: Mapping[str, Any], archive: bool = True) -> dict[str, Any]:
    validate_id(investigation_id, prefix="inv")
    if state not in {"result", "failed", "needs-user", "interrupted"}:
        raise InvestigatorError("investigation terminal state is invalid")
    with store.transaction():
        row = store.conn.execute("SELECT * FROM investigations WHERE investigation_id=?", (investigation_id,)).fetchone()
        if row is None:
            raise InvestigatorError("investigation not found")
        if row["state"] != "running":
            if row["state"] == state and canonical_json(dict(result)) == row["result_json"]:
                return dict(row)
            raise InvestigatorError("investigation already has a different terminal record")
        if row["run_id"] is None:
            raise InvestigatorError("investigation has no controller-supervised run")
        now = utc_now()
        store.conn.execute("UPDATE investigations SET state=?,result_json=?,updated_at=?,completed_at=? WHERE investigation_id=?", (state, canonical_json(dict(result)), now, now, investigation_id))
        if archive:
            store.conn.execute("UPDATE conversations SET desired_state='archived',updated_at=?,resource_version=resource_version+1 WHERE conversation_id=? AND desired_state='active'", (now, row["conversation_id"]))
        completed = dict(store.conn.execute("SELECT * FROM investigations WHERE investigation_id=?", (investigation_id,)).fetchone())
    return completed


def complete_conversation_investigation(store: Any, *, conversation_id: str, state: str, result: Mapping[str, Any], archive: bool = True) -> dict[str, Any]:
    row = store.conn.execute("SELECT investigation_id FROM investigations WHERE conversation_id=?", (conversation_id,)).fetchone()
    if row is None:
        raise InvestigatorError("investigator assignment was not found")
    return complete_investigation(store, row["investigation_id"], state=state, result=result, archive=archive)


def interrupt_running(store: Any, *, project_id: str) -> list[dict[str, Any]]:
    validate_id(project_id, prefix="prj")
    rows = [dict(row) for row in store.conn.execute("SELECT * FROM investigations WHERE project_id=? AND state='running' AND run_id IS NOT NULL", (project_id,))]
    return [complete_investigation(store, row["investigation_id"], state="interrupted", result={"reason": "controller-interrupted"}) for row in rows]


__all__ = [
    "InvestigatorError", "bind_investigation_run", "complete_conversation_investigation",
    "complete_investigation", "interrupt_running", "start_investigation",
]
