"""Shared lifecycle rules for runtimes that Pisec must keep usable."""

from __future__ import annotations

from typing import Any, Mapping


def runtime_eligible_sql(workstream_alias: str = "w") -> str:
    """Return the SQL predicate for a workstream that may own a live runtime.

    Active workstreams are eligible. A completed worker is also eligible while
    it remains the sole authorized verifier of an unresolved issue that it
    reported. Callers supply a trusted SQL alias, never user input.
    """
    if not workstream_alias.isidentifier():
        raise ValueError("workstream SQL alias is invalid")
    return (
        f"({workstream_alias}.desired_state='active' OR ("
        f"{workstream_alias}.desired_state='completed' "
        f"AND {workstream_alias}.kind='worker' "
        "AND EXISTS (SELECT 1 FROM issues runtime_issue "
        f"WHERE runtime_issue.reporter_workstream_id={workstream_alias}.workstream_id "
        "AND runtime_issue.state<>'resolved')))"
    )


def unresolved_reporter_issue(store: Any, workstream_id: str) -> dict[str, Any] | None:
    row = store.conn.execute(
        "SELECT issue_id,project_id,state,severity,summary FROM issues "
        "WHERE reporter_workstream_id=? AND state<>'resolved' "
        "ORDER BY created_at,issue_id LIMIT 1",
        (workstream_id,),
    ).fetchone()
    return None if row is None else dict(row)


def runtime_lifecycle_eligible(
    store: Any,
    workstream_id: str,
    *,
    project_id: str | None = None,
) -> bool:
    clauses = ["w.workstream_id=?", runtime_eligible_sql("w")]
    params: list[str] = [workstream_id]
    if project_id is not None:
        clauses.append("w.project_id=?")
        params.append(project_id)
    row = store.conn.execute(
        "SELECT 1 FROM workstreams w WHERE " + " AND ".join(clauses),
        params,
    ).fetchone()
    return row is not None


def mapping_runtime_lifecycle_eligible(store: Any, workstream: Mapping[str, Any]) -> bool:
    desired_state = workstream["desired_state"]
    if desired_state == "active":
        return True
    return bool(
        desired_state == "completed"
        and workstream["kind"] == "worker"
        and unresolved_reporter_issue(store, str(workstream["workstream_id"])) is not None
    )
