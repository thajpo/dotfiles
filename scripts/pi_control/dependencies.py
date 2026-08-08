"""Dependency detection and exact package-security review gates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .models import canonical_json, new_id, utc_now, validate_id


class DependencyError(ValueError):
    pass


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def detect_dependencies(store: Any, *, project_id: str, change_id: str, revision: int, working_copy_path: str | Path, worker_reason: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    validate_id(project_id, prefix="prj")
    validate_id(change_id, prefix="chg")
    if store.conn.execute("SELECT 1 FROM change_revisions WHERE change_id=? AND revision=?", (change_id, revision)).fetchone() is None:
        raise DependencyError("dependency candidate revision does not exist")
    root = Path(working_copy_path).resolve(strict=True)
    reason_map = dict(worker_reason or {})
    records: list[dict[str, Any]] = []
    candidates: list[tuple[str, str, str, dict[str, str]]] = []
    package_json = root / "package.json"
    if package_json.is_file() and not package_json.is_symlink():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DependencyError("package.json could not be parsed") from error
        if not isinstance(package, dict):
            raise DependencyError("package.json must be an object")
        lock = root / "package-lock.json"
        for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            values = package.get(section, {})
            if isinstance(values, dict):
                for name, version in values.items():
                    if isinstance(name, str) and isinstance(version, str):
                        candidates.append(("npm", name, version, {"manifest": str(package_json), "lock": str(lock) if lock.is_file() else ""}))
    pyproject = root / "pyproject.toml"
    if pyproject.is_file() and not pyproject.is_symlink():
        text = pyproject.read_text(encoding="utf-8")
        for name, version in re.findall(r"^\s*['\"]?([A-Za-z0-9_.-]+)['\"]?\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE):
            if name.lower() not in {"version", "requires-python"}:
                candidates.append(("python", name, version, {"manifest": str(pyproject), "lock": str(root / "uv.lock") if (root / "uv.lock").is_file() else ""}))
    with store.transaction():
        for ecosystem, package_name, exact_version, paths in candidates:
            manifest = Path(paths["manifest"])
            lock = Path(paths["lock"]) if paths["lock"] else None
            record_id = new_id("dep")
            store.conn.execute("INSERT OR IGNORE INTO dependency_changes(dependency_change_id,project_id,change_id,revision,ecosystem,package_name,exact_version,manifest_path,manifest_digest,lock_path,lock_digest,worker_reason,disposition,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (record_id, project_id, change_id, revision, ecosystem, package_name, exact_version, str(manifest.relative_to(root)), _digest(manifest), str(lock.relative_to(root)) if lock else None, _digest(lock) if lock else None, reason_map.get(package_name, "dependency required by candidate change"), "standard", utc_now()))
            row = store.conn.execute("SELECT * FROM dependency_changes WHERE change_id=? AND revision=? AND ecosystem=? AND package_name=? AND exact_version=?", (change_id, revision, ecosystem, package_name, exact_version)).fetchone()
            if row is not None:
                records.append(dict(row))
    return records


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


def record_package_security_review(store: Any, *, dependency_change_id: str, candidate_change_id: str, candidate_revision: int, evidence: Mapping[str, Any], risk_level: str, recommendation: str, investigator_run_id: str | None = None) -> dict[str, Any]:
    validate_id(dependency_change_id, prefix="dep")
    validate_id(candidate_change_id, prefix="chg")
    dependency = store.conn.execute("SELECT * FROM dependency_changes WHERE dependency_change_id=?", (dependency_change_id,)).fetchone()
    if dependency is None or dependency["change_id"] != candidate_change_id or int(dependency["revision"]) != candidate_revision:
        raise DependencyError("package review is not bound to the exact candidate")
    if risk_level not in {"low", "medium", "high", "unknown"}:
        raise DependencyError("risk level is invalid")
    review_id = new_id("pkg")
    now = utc_now()
    with store.transaction():
        store.conn.execute("INSERT INTO package_security_reviews(package_security_review_id,dependency_change_id,candidate_change_id,candidate_revision,investigator_run_id,package_name,exact_version,lock_digest,evidence_json,risk_level,recommendation,state,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (review_id, dependency_change_id, candidate_change_id, candidate_revision, investigator_run_id, dependency["package_name"], dependency["exact_version"], dependency["lock_digest"], canonical_json(dict(evidence)), risk_level, recommendation, "complete", now, now))
        return dict(store.conn.execute("SELECT * FROM package_security_reviews WHERE package_security_review_id=?", (review_id,)).fetchone())


def package_review_gate(store: Any, *, change_id: str, revision: int) -> dict[str, Any]:
    validate_id(change_id, prefix="chg")
    dependencies = [dict(row) for row in store.conn.execute("SELECT * FROM dependency_changes WHERE change_id=? AND revision=?", (change_id, revision))]
    missing: list[str] = []
    stale: list[str] = []
    for dependency in dependencies:
        if dependency["disposition"] == "rejected":
            missing.append(dependency["dependency_change_id"])
            continue
        if dependency["disposition"] != "review-required":
            continue
        reviews = store.conn.execute("SELECT * FROM package_security_reviews WHERE dependency_change_id=? AND candidate_change_id=? AND candidate_revision=? AND state='complete' ORDER BY completed_at DESC", (dependency["dependency_change_id"], change_id, revision)).fetchall()
        if not reviews:
            missing.append(dependency["dependency_change_id"])
        elif any(row["lock_digest"] != dependency["lock_digest"] or row["exact_version"] != dependency["exact_version"] for row in reviews):
            stale.append(dependency["dependency_change_id"])
    return {"ready": not missing and not stale, "missing": missing, "stale": stale, "dependencies": dependencies}


__all__ = ["DependencyError", "detect_dependencies", "package_review_gate", "record_package_security_review", "set_dependency_disposition"]
