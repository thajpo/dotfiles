"""Field-level shadow import reconciliation and durable attention."""

from __future__ import annotations

from typing import Any, Mapping

from .errors import MigrationUnresolvedError
from .migration import InventoryV2Report
from .migration_importer import build_shadow_expectations
from .migration_planner import ResolutionManifest
from .models import canonical_json, utc_now


def _compare(mismatches: list[dict[str, Any]], actual: Mapping[str, Any] | None, expected: Mapping[str, Any], *, resource_type: str, resource_id: str) -> None:
    if actual is None:
        mismatches.append({"resourceType": resource_type, "resourceId": resource_id, "kind": "resource-missing"})
        return
    for field, value in expected.items():
        if actual[field] != value:
            mismatches.append({"resourceType": resource_type, "resourceId": resource_id, "kind": "field-mismatch", "field": field, "expected": value, "actual": actual[field]})


def reconcile_shadow_v2(store: Any, report: InventoryV2Report, resolution: ResolutionManifest, *, migration_id: str) -> dict[str, Any]:
    migration = store.conn.execute("SELECT * FROM migration_runs WHERE migration_id=?", (migration_id,)).fetchone()
    if migration is None: raise MigrationUnresolvedError("migration run was not found")
    if migration["source_manifest_digest"] != report.digest or resolution.payload.get("inventoryDigest") != report.digest:
        return _attention(store, migration_id, "inventory-or-resolution-digest-mismatch", {"expected": report.digest, "actual": migration["source_manifest_digest"]})
    mapping_rows = {str(row["record_id"]): row for row in store.conn.execute("SELECT * FROM migration_resource_mappings WHERE migration_id=?", (migration_id,))}
    mappings = {record_id: row["resource_id"] for record_id, row in mapping_rows.items()}
    mismatches: list[dict[str, Any]] = []
    decisions = {str(item["recordId"]): item for item in resolution.payload.get("decisions", [])}

    # Every inventory decision remains bound to an immutable mapping and the
    # mapping must still have the same source digest/disposition as the review.
    for record in report.payload.get("records", []):
        record_id = str(record.get("record_id", record.get("recordId")))
        mapping = mapping_rows.get(record_id)
        if mapping is None:
            mismatches.append({"recordId": record_id, "kind": "mapping-missing"}); continue
        source_digest = record.get("source_digest", record.get("sourceDigest"))
        if mapping["source_digest"] != source_digest:
            mismatches.append({"recordId": record_id, "kind": "source-digest-changed"}); continue
        decision = decisions.get(record_id)
        if decision is None or mapping["disposition"] != decision["disposition"]:
            mismatches.append({"recordId": record_id, "kind": "disposition-changed"})

    try:
        expectations = build_shadow_expectations(report, resolution, mappings)
    except MigrationUnresolvedError as error:
        return _attention(store, migration_id, "shadow-expectations-unresolved", {"error": str(error), "detail": getattr(error, "detail", {})})

    expected_projects = {str(item["projectId"]): item for item in expectations.projects}
    actual_projects = {str(row["project_id"]): row for row in store.conn.execute("SELECT * FROM projects")}
    for project_id, expected in expected_projects.items():
        normalized = expected["normalized"]
        _compare(mismatches, actual_projects.get(project_id), {
            "git_common_dir": expected["common"],
            "primary_checkout": expected["checkout"],
            "object_format": str(normalized.get("object_format") or normalized.get("objectFormat") or "sha1"),
        }, resource_type="project", resource_id=project_id)
    for project_id in sorted(set(actual_projects) - set(expected_projects)):
        mismatches.append({"resourceType": "project", "resourceId": project_id, "kind": "resource-unexpected"})

    expected_wcs: dict[str, dict[str, Any]] = {}
    for spec in expectations.working_copies:
        mapping_key = str(spec["mappingKey"])
        resource_id = mappings.get(mapping_key)
        if not isinstance(resource_id, str):
            mismatches.append({"recordId": mapping_key, "kind": "mapping-missing"}); continue
        expected_wcs[resource_id] = {
            "project_id": spec["projectId"], "path": spec["path"], "git_dir": spec.get("gitDir"),
            "branch_ref": spec.get("branchRef"), "expected_head_oid": spec.get("expectedHeadOid"),
            "expected_tree_oid": spec.get("expectedTreeOid"), "kind": spec["kind"], "purpose": spec["purpose"],
            "effective_mode": "read-only", "controller_owned": 0,
        }
    actual_wcs = {str(row["working_copy_id"]): row for row in store.conn.execute("SELECT * FROM working_copies")}
    for resource_id, expected in expected_wcs.items():
        _compare(mismatches, actual_wcs.get(resource_id), expected, resource_type="working-copy", resource_id=resource_id)
    for resource_id in sorted(set(actual_wcs) - set(expected_wcs)):
        mismatches.append({"resourceType": "working-copy", "resourceId": resource_id, "kind": "resource-unexpected"})

    expected_conversations: dict[str, dict[str, Any]] = {}
    for spec in expectations.conversations:
        resource_id = mappings.get(str(spec["recordId"]))
        if not isinstance(resource_id, str):
            mismatches.append({"recordId": spec["recordId"], "kind": "mapping-missing"}); continue
        working_copy_id = mappings.get(str(spec["workingCopyKey"])) if spec.get("workingCopyKey") is not None else None
        if spec.get("workingCopyKey") is not None and not isinstance(working_copy_id, str):
            mismatches.append({"recordId": spec["recordId"], "kind": "working-copy-mapping-missing"}); continue
        expected_conversations[resource_id] = {
            "project_id": spec["projectId"], "working_copy_id": working_copy_id, "role": spec["role"],
            "display_name": spec["displayName"], "pi_session_id": spec["piSessionId"],
            "session_file": spec["sessionFile"], "desired_state": spec["desiredState"],
            "observed_state": spec["observedState"],
        }
    actual_conversations = {str(row["conversation_id"]): row for row in store.conn.execute("SELECT * FROM conversations")}
    for resource_id, expected in expected_conversations.items():
        _compare(mismatches, actual_conversations.get(resource_id), expected, resource_type="conversation", resource_id=resource_id)
    for resource_id in sorted(set(actual_conversations) - set(expected_conversations)):
        mismatches.append({"resourceType": "conversation", "resourceId": resource_id, "kind": "resource-unexpected"})

    expected_synthetic_keys = {str(spec["mappingKey"]) for spec in expectations.working_copies}
    actual_synthetic_keys = {key for key in mapping_rows if key.startswith("synthetic-working-copy:")}
    for key in sorted(actual_synthetic_keys - expected_synthetic_keys):
        mismatches.append({"recordId": key, "kind": "mapping-unexpected"})

    if mismatches:
        return _attention(store, migration_id, "shadow-fields-mismatch", {"mismatches": mismatches[:256]})
    with store.transaction():
        store.conn.execute("UPDATE migration_runs SET step='reconciled',updated_at=?,resource_version=resource_version+1 WHERE migration_id=? AND state='succeeded'", (utc_now(), migration_id))
    return {
        "migrationId": migration_id,
        "state": "matched",
        "mismatches": [],
        "sourceManifestDigest": report.digest,
        "projects": len(expected_projects),
        "workingCopies": len(expected_wcs),
        "conversations": len(expected_conversations),
        "workstreams": 0,
    }


def _attention(store: Any, migration_id: str, reason: str, detail: Mapping[str, Any]) -> dict[str, Any]:
    migration = store.conn.execute("SELECT operation_id,resource_version FROM migration_runs WHERE migration_id=?", (migration_id,)).fetchone()
    payload = {"migrationId": migration_id, "state": "needs_attention", "reason": reason, "detail": dict(detail)}
    with store.transaction():
        now = utc_now()
        store.conn.execute("UPDATE migration_runs SET state='needs_attention',step=?,result_json=?,error_code='CP_MIGRATION_UNRESOLVED',error_detail=?,updated_at=?,completed_at=?,resource_version=resource_version+1 WHERE migration_id=?", (reason, canonical_json(payload), reason, now, now, migration_id))
        store.conn.execute(
            """INSERT INTO attention(attention_id,kind,summary,detail_json,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(attention_id) DO UPDATE SET
                 kind=excluded.kind, summary=excluded.summary, detail_json=excluded.detail_json,
                 state=excluded.state, updated_at=excluded.updated_at""",
            ("attention_" + migration_id, "migration", reason, canonical_json(payload), "open", now, now),
        )
        from .events import append_event_in_transaction
        append_event_in_transaction(store.conn, event_kind="migration.needs_attention", resource_type="migration", resource_id=migration_id, resource_version=int(migration["resource_version"]) + 1, operation_id=migration["operation_id"], payload=payload)
    return payload


reconcile_shadow = reconcile_shadow_v2
__all__ = ["reconcile_shadow", "reconcile_shadow_v2"]
