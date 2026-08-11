"""Immutable dependency inventory and investigator-bound security gates."""

from __future__ import annotations

from typing import Any, Mapping

from .models import canonical_json, new_id, utc_now, validate_id
from .package_diff import diff_observations, observe_package_tree


class DependencyError(ValueError):
    pass


def _candidate(store: Any, project_id: str, change_id: str, revision: int) -> tuple[Any, Any, Any]:
    change = store.conn.execute("SELECT * FROM changes WHERE change_id=? AND project_id=?", (change_id, project_id)).fetchone()
    candidate = store.conn.execute("SELECT * FROM change_revisions WHERE change_id=? AND revision=?", (change_id, revision)).fetchone()
    working = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=? AND project_id=?", (change["source_working_copy_id"], project_id)).fetchone() if change is not None else None
    if change is None or candidate is None or working is None:
        raise DependencyError("dependency inventory candidate binding is unavailable")
    return change, candidate, working


def inventory_dependencies(store: Any, *, project_id: str, change_id: str, revision: int, worker_reason: Mapping[str, str] | None = None) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(change_id, prefix="chg")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise DependencyError("candidate revision is invalid")
    change, candidate, working = _candidate(store, project_id, change_id, revision)
    base = observe_package_tree(working["path"], candidate["base_oid"] + "^{tree}")
    current = observe_package_tree(working["path"], candidate["tree_oid"])
    differences = diff_observations(base, current)
    reason_map = dict(worker_reason or {})
    observations = {item["ecosystem"]: item for item in current["ecosystems"]}
    base_observations = {item["ecosystem"]: item for item in base["ecosystems"]}
    records: list[dict[str, Any]] = []
    with store.transaction():
        _, exact_candidate, exact_working = _candidate(store, project_id, change_id, revision)
        if exact_candidate["tree_oid"] != candidate["tree_oid"] or exact_candidate["base_oid"] != candidate["base_oid"] or exact_working["working_copy_id"] != working["working_copy_id"]:
            raise DependencyError("dependency candidate changed before final inventory mutation")
        for difference in differences:
            observation = observations.get(difference["ecosystem"]) or base_observations[difference["ecosystem"]]
            record_id = new_id("dep")
            store.conn.execute(
                "INSERT OR IGNORE INTO dependency_changes(dependency_change_id,project_id,change_id,revision,ecosystem,change_kind,package_name,exact_version,manifest_path,manifest_digest,lock_path,lock_digest,worker_reason,disposition,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record_id, project_id, change_id, revision, difference["ecosystem"], difference["changeKind"], difference["packageName"], difference["exactVersion"], observation["manifestPath"], observation["manifestDigest"], observation["lockPath"], observation["lockDigest"], reason_map.get(difference["packageName"], "dependency delta in immutable candidate"), "standard", utc_now()),
            )
            row = store.conn.execute("SELECT * FROM dependency_changes WHERE change_id=? AND revision=? AND ecosystem=? AND package_name=? AND exact_version=?", (change_id, revision, difference["ecosystem"], difference["packageName"], difference["exactVersion"])).fetchone()
            if row is not None:
                records.append(dict(row))
    return {"projectId": project_id, "changeId": change_id, "revision": revision, "base": base, "candidate": current, "differences": differences, "records": records}


def set_dependency_disposition(store: Any, *, dependency_change_id: str, disposition: str) -> dict[str, Any]:
    validate_id(dependency_change_id, prefix="dep")
    if disposition not in {"standard", "review-required", "rejected"}:
        raise DependencyError("dependency disposition is invalid")
    with store.transaction():
        row = store.conn.execute("SELECT * FROM dependency_changes WHERE dependency_change_id=?", (dependency_change_id,)).fetchone()
        if row is None:
            raise DependencyError("dependency change not found")
        store.conn.execute("UPDATE dependency_changes SET disposition=? WHERE dependency_change_id=?", (disposition, dependency_change_id))
        return dict(store.conn.execute("SELECT * FROM dependency_changes WHERE dependency_change_id=?", (dependency_change_id,)).fetchone())


def record_package_security_review(store: Any, *, dependency_change_id: str, evidence: Mapping[str, Any], risk_level: str, recommendation: str, investigator_run_id: str) -> dict[str, Any]:
    validate_id(dependency_change_id, prefix="dep")
    validate_id(investigator_run_id, prefix="run")
    if risk_level not in {"low", "medium", "high", "unknown"}:
        raise DependencyError("risk level is invalid")
    if not isinstance(recommendation, str) or not recommendation or len(recommendation) > 2048:
        raise DependencyError("package recommendation is invalid")
    with store.transaction():
        dependency = store.conn.execute("SELECT * FROM dependency_changes WHERE dependency_change_id=?", (dependency_change_id,)).fetchone()
        investigator = store.conn.execute(
            "SELECT r.*,c.role,i.investigation_id FROM runs r JOIN conversations c ON c.conversation_id=r.conversation_id JOIN investigations i ON i.run_id=r.run_id WHERE r.run_id=?",
            (investigator_run_id,),
        ).fetchone()
        if dependency is None or investigator is None or investigator["role"] != "investigator" or investigator["project_id"] != dependency["project_id"] or investigator["desired_state"] != "running" or investigator["observed_state"] not in {"ready", "running"}:
            raise DependencyError("package review requires a current real investigator run in the candidate project")
        existing = store.conn.execute("SELECT * FROM package_security_reviews WHERE dependency_change_id=? AND candidate_change_id=? AND candidate_revision=?", (dependency_change_id, dependency["change_id"], dependency["revision"])).fetchone()
        if existing is not None:
            return dict(existing)
        review_id = new_id("pkg")
        now = utc_now()
        state = "complete" if risk_level in {"low", "medium"} and recommendation == "approve" else "rejected"
        store.conn.execute(
            "INSERT INTO package_security_reviews(package_security_review_id,dependency_change_id,candidate_change_id,candidate_revision,investigator_run_id,package_name,exact_version,lock_digest,evidence_json,risk_level,recommendation,state,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (review_id, dependency_change_id, dependency["change_id"], dependency["revision"], investigator_run_id, dependency["package_name"], dependency["exact_version"], dependency["lock_digest"], canonical_json(dict(evidence)), risk_level, recommendation, state, now, now),
        )
        return dict(store.conn.execute("SELECT * FROM package_security_reviews WHERE package_security_review_id=?", (review_id,)).fetchone())


def package_review_gate(store: Any, *, change_id: str, revision: int) -> dict[str, Any]:
    validate_id(change_id, prefix="chg")
    dependencies = [dict(row) for row in store.conn.execute("SELECT * FROM dependency_changes WHERE change_id=? AND revision=?", (change_id, revision))]
    missing: list[str] = []
    stale: list[str] = []
    rejected: list[str] = []
    for dependency in dependencies:
        if dependency["disposition"] == "rejected":
            rejected.append(dependency["dependency_change_id"])
            continue
        if dependency["disposition"] != "review-required":
            continue
        reviews = store.conn.execute("SELECT * FROM package_security_reviews WHERE dependency_change_id=? AND candidate_change_id=? AND candidate_revision=? ORDER BY completed_at DESC", (dependency["dependency_change_id"], change_id, revision)).fetchall()
        if not reviews:
            missing.append(dependency["dependency_change_id"])
        elif any(row["lock_digest"] != dependency["lock_digest"] or row["exact_version"] != dependency["exact_version"] for row in reviews):
            stale.append(dependency["dependency_change_id"])
        elif not any(row["state"] == "complete" and row["risk_level"] in {"low", "medium"} and row["recommendation"] == "approve" and row["investigator_run_id"] for row in reviews):
            rejected.append(dependency["dependency_change_id"])
    return {"ready": not missing and not stale and not rejected, "missing": missing, "stale": stale, "rejected": rejected, "dependencies": dependencies}


# The old name is intentionally retained only as a Python import alias; the
# public protocol no longer accepts a caller-selected working-copy path.
detect_dependencies = inventory_dependencies


__all__ = ["DependencyError", "inventory_dependencies", "package_review_gate", "record_package_security_review", "set_dependency_disposition"]
