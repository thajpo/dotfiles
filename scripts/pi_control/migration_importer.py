"""Disposable schema-v7 shadow importer.

The importer deliberately materializes only lifecycle *descriptions* in the
shadow database.  It never copies a session body, claims a writer, changes a
Git worktree, or starts a runtime.  Legacy bytes remain at their inventory
source/backup paths and are bound by immutable migration mappings.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import os
import stat
from typing import Any, Mapping

from .events import append_event_in_transaction
from .errors import ConstraintError, IdempotencyConflictError, MigrationUnresolvedError
from .migration import InventoryV2Report, inventory_canonical_json, write_inventory_v2
from .migration_planner import ResolutionManifest, allocate_migration_mappings
from .models import canonical_json, new_id, parse_canonical_json, utc_now
from .operations import create_operation, update_operation_in_transaction
from .store import ControllerStore


@dataclass(frozen=True)
class _ShadowExpectations:
    projects: tuple[dict[str, Any], ...]
    working_copies: tuple[dict[str, Any], ...]
    conversations: tuple[dict[str, Any], ...]


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _manifest_is_intact(path: Path, report_digest: str) -> bool:
    """Verify a previously written shadow source manifest is safe and exact."""
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return False
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o400:
            return False
        body = path.read_bytes()
        return _sha256_bytes(body.rstrip(b"\n")) == report_digest
    except OSError:
        return False


def _safe_shadow_root(path: os.PathLike[str] | str) -> Path:
    root = Path(path).expanduser().absolute()
    if root.is_symlink(): raise ValueError("shadow root must not be a symlink")
    default = (Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "pi-control").expanduser().absolute()
    if root == default: raise ValueError("shadow importer refuses the live controller root")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.exists() and root.stat().st_mode & 0o077: os.chmod(root, 0o700)
    return root


def _record_id(record: Mapping[str, Any]) -> str:
    value = record.get("record_id", record.get("recordId"))
    if not isinstance(value, str) or not value:
        raise MigrationUnresolvedError("inventory record has no stable record ID")
    return value


def _decision_index(resolution: ResolutionManifest) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for decision in resolution.payload.get("decisions", []):
        if not isinstance(decision, Mapping):
            raise MigrationUnresolvedError("resolution contains a non-object decision")
        result[str(decision["recordId"])] = decision
    return result


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(inventory_canonical_json(value).encode("utf-8")).hexdigest()


def _branch_ref(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if value.startswith("refs/") else "refs/heads/" + value


def _purpose_for_branch(branch_ref: str | None, *, primary: bool = False) -> str:
    if primary:
        return "personal"
    value = (branch_ref or "").lower()
    if "review" in value:
        return "review"
    if "integration" in value:
        return "integration"
    if "personal" in value:
        return "personal"
    if any(token in value for token in ("secretary", "/sec-", "side-agent", "/ws-", "workstream")):
        return "workstream"
    return "other"


def _worktree_kind(purpose: str, *, primary: bool = False) -> str:
    if primary:
        return "primary"
    if purpose == "review":
        return "review"
    return "worktree"


def _project_authorities(report: InventoryV2Report, resolution: ResolutionManifest, mappings: Mapping[str, str | None]) -> tuple[dict[str, Any], ...]:
    decisions = _decision_index(resolution)
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for record in report.payload.get("records", []):
        record_id = _record_id(record)
        decision = decisions.get(record_id)
        if decision is None or decision.get("disposition") != "import" or decision.get("resourceType") != "project":
            continue
        resource_id = mappings.get(record_id)
        if not isinstance(resource_id, str):
            raise MigrationUnresolvedError("imported project has no allocated resource ID", detail={"recordId": record_id})
        normalized = record.get("normalized") if isinstance(record.get("normalized"), Mapping) else {}
        identity = record.get("identity") if isinstance(record.get("identity"), Mapping) else {}
        common = normalized.get("common_dir") or normalized.get("gitCommonDir") or identity.get("commonDir")
        checkout = normalized.get("top_level") or normalized.get("repository_path") or normalized.get("primaryRepository")
        if not isinstance(common, str) or not common or not isinstance(checkout, str) or not checkout:
            # A secretary registry duplicate is not source authority for a Git
            # project.  It may still map to a project ID, but it cannot create
            # or overwrite a project row.
            continue
        priority = 0 if record.get("adapter_kind") == "git" and record.get("resource_kind") == "project-observation" else 1
        candidates.append((priority, record_id, {"record": record, "recordId": record_id, "projectId": resource_id, "normalized": dict(normalized), "common": common, "checkout": checkout}))
    candidates.sort(key=lambda value: (value[2]["projectId"], value[0], value[1]))
    selected: dict[str, dict[str, Any]] = {}
    for _, _, candidate in candidates:
        selected.setdefault(candidate["projectId"], candidate)
    return tuple(selected[key] for key in sorted(selected))


def _base_worktree_specs(projects: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    specs: dict[tuple[str, str], dict[str, Any]] = {}
    for project in projects:
        project_id = str(project["projectId"])
        normalized = project["normalized"]
        worktrees = normalized.get("worktrees")
        if not isinstance(worktrees, list) or not worktrees:
            worktrees = [{
                "path": project["checkout"],
                "git_dir": normalized.get("git_dir") or project["common"],
                "branch_ref": normalized.get("branch_ref"),
                "head_oid": normalized.get("head_oid"),
                "tree_oid": normalized.get("tree_oid"),
                "state": "ready",
                "exists": True,
                "object_format": normalized.get("object_format", "sha1"),
            }]
        for raw in worktrees:
            if not isinstance(raw, Mapping):
                continue
            path = raw.get("path")
            if not isinstance(path, str) or not path:
                continue
            primary = path == project["checkout"]
            branch_ref = _branch_ref(raw.get("branch_ref"))
            purpose = _purpose_for_branch(branch_ref, primary=primary)
            state = str(raw.get("state") or "unknown")
            if not bool(raw.get("exists", True)):
                observed = "missing"
            elif state == "error":
                observed = "error"
            elif state == "dirty" or bool(normalized.get("dirty")) and primary:
                observed = "dirty"
            elif state == "ready":
                observed = "ready"
            else:
                observed = "unknown"
            spec = {
                "mappingKey": "synthetic-working-copy:" + hashlib.sha256((project_id + "\0" + path).encode("utf-8")).hexdigest()[:32],
                "sourceKind": "git-worktree",
                "adapterKind": "git",
                "sourceLocator": path,
                "sourceDigest": _digest({"projectId": project_id, "path": path, "worktree": dict(raw)}),
                "projectId": project_id,
                "path": path,
                "gitDir": raw.get("git_dir") if isinstance(raw.get("git_dir"), str) else (project["common"] if primary else None),
                "branchRef": branch_ref,
                "expectedHeadOid": raw.get("head_oid") if isinstance(raw.get("head_oid"), str) else None,
                "expectedTreeOid": raw.get("tree_oid") if isinstance(raw.get("tree_oid"), str) else None,
                "displayName": "primary" if primary else (Path(path).name or "worktree"),
                "kind": _worktree_kind(purpose, primary=primary),
                "purpose": purpose,
                "observedState": observed,
                "primary": primary,
            }
            specs.setdefault((project_id, path), spec)
    return [specs[key] for key in sorted(specs)]


def _registry_records(report: InventoryV2Report) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for record in report.payload.get("records", []):
        if record.get("resource_kind") != "conversation-binding-observation":
            continue
        normalized = record.get("normalized") if isinstance(record.get("normalized"), Mapping) else {}
        for value in normalized.get("records", []):
            if isinstance(value, Mapping) and isinstance(value.get("conversationId"), str):
                result[str(value["conversationId"])] = value
    return result


def _project_by_repository(projects: tuple[dict[str, Any], ...]) -> dict[str, str]:
    return {str(project["checkout"]): str(project["projectId"]) for project in projects}


def _conversation_specs(
    report: InventoryV2Report,
    resolution: ResolutionManifest,
    mappings: Mapping[str, str | None],
    projects: tuple[dict[str, Any], ...],
    worktrees: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    decisions = _decision_index(resolution)
    records = {_record_id(record): record for record in report.payload.get("records", [])}
    project_by_repository = _project_by_repository(projects)
    project_by_id = {str(project["projectId"]): project for project in projects}
    registry = _registry_records(report)
    paths = {(str(spec["projectId"]), str(spec["path"])): spec for spec in worktrees}
    conversations: list[dict[str, Any]] = []
    for record_id, decision in sorted(decisions.items()):
        if decision.get("disposition") != "import" or decision.get("resourceType") != "conversation":
            continue
        record = records.get(record_id)
        if record is None:
            raise MigrationUnresolvedError("conversation decision references a missing inventory record", detail={"recordId": record_id})
        conversation_id = mappings.get(record_id)
        if not isinstance(conversation_id, str):
            raise MigrationUnresolvedError("imported conversation has no allocated resource ID", detail={"recordId": record_id})
        identity = record.get("identity") if isinstance(record.get("identity"), Mapping) else {}
        session_id = identity.get("sessionId") or identity.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise MigrationUnresolvedError("imported conversation has no exact Pi session identity", detail={"recordId": record_id})
        binding = registry.get(session_id)
        normalized = record.get("normalized") if isinstance(record.get("normalized"), Mapping) else {}
        header = normalized.get("header") if isinstance(normalized.get("header"), Mapping) else {}
        cwd = header.get("cwd") if isinstance(header.get("cwd"), str) else None
        repository = binding.get("repository") if isinstance(binding, Mapping) else None
        project_id = project_by_repository.get(str(repository)) if isinstance(repository, str) else None
        worktree_path = binding.get("worktree") if isinstance(binding, Mapping) and isinstance(binding.get("worktree"), str) else cwd
        if project_id is None and isinstance(cwd, str):
            for project in projects:
                if cwd == project["checkout"]:
                    project_id = str(project["projectId"])
                    break
        if project_id is None or project_id not in project_by_id:
            raise MigrationUnresolvedError("imported conversation is not bound to an imported project", detail={"recordId": record_id, "sessionId": session_id, "repository": repository})
        project = project_by_id[project_id]
        if not isinstance(worktree_path, str) or not worktree_path:
            worktree_path = str(project["checkout"])
        wc_spec = paths.get((project_id, worktree_path))
        profile = str(binding.get("profile")) if isinstance(binding, Mapping) and isinstance(binding.get("profile"), str) else "personal"
        if wc_spec is None and worktree_path == project["checkout"]:
            wc_spec = paths.get((project_id, str(project["checkout"])))
        if wc_spec is None:
            # Root-registry identity is authoritative for a selected session,
            # but Git did not return a corresponding worktree observation.  A
            # read-only unknown copy preserves that binding without asserting
            # that the checkout exists or is safe to write.
            if profile == "secretary" and worktree_path == project["checkout"]:
                wc_spec = None
            else:
                branch_ref = _branch_ref(binding.get("branch")) if isinstance(binding, Mapping) else None
                purpose = _purpose_for_branch(branch_ref, primary=False)
                wc_spec = {
                    "mappingKey": "synthetic-working-copy:" + hashlib.sha256((project_id + "\0" + worktree_path).encode("utf-8")).hexdigest()[:32],
                    "sourceKind": "root-registry-worktree",
                    "adapterKind": "root_sessions",
                    "sourceLocator": worktree_path,
                    "sourceDigest": _digest({"projectId": project_id, "path": worktree_path, "binding": dict(binding or {})}),
                    "projectId": project_id,
                    "path": worktree_path,
                    "gitDir": None,
                    "branchRef": branch_ref,
                    "expectedHeadOid": None,
                    "expectedTreeOid": None,
                    "displayName": Path(worktree_path).name or "worktree",
                    "kind": "worktree",
                    "purpose": "workstream" if profile in {"secretary", "root"} else purpose,
                    "observedState": "unknown",
                    "primary": False,
                }
                paths[(project_id, worktree_path)] = wc_spec
                worktrees.append(wc_spec)
        if profile == "secretary" and (wc_spec is None or bool(wc_spec.get("primary"))):
            role = "secretary"
            working_copy_key = None
        elif profile == "personal" or (profile == "root" and wc_spec is not None and bool(wc_spec.get("primary"))):
            role = "personal"
            working_copy_key = wc_spec["mappingKey"] if wc_spec is not None else None
        else:
            role = "workstream"
            if wc_spec is None:
                raise MigrationUnresolvedError("workstream conversation has no exact working-copy binding", detail={"recordId": record_id, "sessionId": session_id})
            working_copy_key = wc_spec["mappingKey"]
        conversations.append({
            "recordId": record_id,
            "conversationId": conversation_id,
            "projectId": project_id,
            "workingCopyKey": working_copy_key,
            "role": role,
            "displayName": session_id,
            "piSessionId": session_id,
            "sessionFile": str(record.get("source_locator")),
            "desiredState": "active" if not isinstance(binding, Mapping) or binding.get("status", "active") == "active" else "archived",
            "observedState": "unknown",
            "sourceDigest": record.get("source_digest", record.get("sourceDigest")),
        })
    return conversations, worktrees


def build_shadow_expectations(
    report: InventoryV2Report,
    resolution: ResolutionManifest,
    mappings: Mapping[str, str | None],
) -> _ShadowExpectations:
    projects = _project_authorities(report, resolution, mappings)
    worktrees = _base_worktree_specs(projects)
    conversations, worktrees = _conversation_specs(report, resolution, mappings, projects, worktrees)
    return _ShadowExpectations(tuple(projects), tuple(sorted(worktrees, key=lambda value: value["mappingKey"])), tuple(sorted(conversations, key=lambda value: value["conversationId"])))


def _ensure_worktree_mappings(store: ControllerStore, migration_id: str, expectations: _ShadowExpectations, mappings: dict[str, str | None], inventory_digest: str) -> None:
    for spec in expectations.working_copies:
        key = str(spec["mappingKey"])
        existing = store.conn.execute("SELECT resource_id,source_digest,disposition FROM migration_resource_mappings WHERE migration_id=? AND record_id=?", (migration_id, key)).fetchone()
        if existing is not None:
            if existing["source_digest"] != spec["sourceDigest"] or existing["disposition"] != "import":
                raise IdempotencyConflictError(migration_id, existing_digest=str(existing["source_digest"]), request_digest=str(spec["sourceDigest"]))
            mappings[key] = str(existing["resource_id"])
            continue
        resource_id = new_id("wc")
        store.conn.execute(
            "INSERT INTO migration_resource_mappings(migration_id,record_id,adapter_kind,source_kind,source_digest,resource_type,resource_id,disposition,reason_code,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (migration_id, key, spec["adapterKind"], spec["sourceKind"], spec["sourceDigest"], "working-copy", resource_id, "import", "source-authority-worktree", canonical_json({"inventoryDigest": inventory_digest, "synthetic": True, "sourceLocator": spec["sourceLocator"]}), utc_now()),
        )
        mappings[key] = resource_id


def _insert_project_rows(store: ControllerStore, expectations: _ShadowExpectations) -> int:
    imported = 0
    for project in expectations.projects:
        record = project["record"]
        normalized = project["normalized"]
        project_id = str(project["projectId"])
        dirty = bool(normalized.get("dirty"))
        observed = "drifted" if dirty else ("ready" if str(record.get("observation_state", "observed")) == "observed" else "unknown")
        common = str(project["common"])
        checkout = str(project["checkout"])
        object_format = str(normalized.get("object_format") or normalized.get("objectFormat") or "sha1")
        existing = store.conn.execute("SELECT git_common_dir,primary_checkout,object_format FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if existing is not None:
            if (existing["git_common_dir"], existing["primary_checkout"], existing["object_format"]) != (common, checkout, object_format):
                raise ConstraintError("shadow project mapping disagrees with the source-authority row")
            continue
        if store.conn.execute("SELECT 1 FROM projects WHERE git_common_dir=?", (common,)).fetchone() is not None:
            raise ConstraintError("shadow project identity conflicts with an existing row")
        now = utc_now()
        store.conn.execute(
            "INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at,last_reconciled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, Path(checkout).name or "project", common, 0, 0, checkout, object_format, "isolated", "shadow-policy", "active", observed, 1, now, now, now),
        )
        imported += 1
    return imported


def _insert_worktree_rows(store: ControllerStore, expectations: _ShadowExpectations, mappings: Mapping[str, str | None]) -> int:
    imported = 0
    for spec in expectations.working_copies:
        resource_id = mappings.get(str(spec["mappingKey"]))
        if not isinstance(resource_id, str):
            raise MigrationUnresolvedError("working-copy mapping was not allocated", detail={"mappingKey": spec["mappingKey"]})
        existing = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (resource_id,)).fetchone()
        immutable = (str(spec["projectId"]), str(spec["path"]), spec.get("gitDir"))
        if existing is not None:
            if (existing["project_id"], existing["path"], existing["git_dir"]) != immutable:
                raise ConstraintError("shadow working-copy mapping disagrees with the source-authority row")
            continue
        now = utc_now()
        store.conn.execute(
            "INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,git_dir,branch_ref,expected_head_oid,expected_tree_oid,effective_mode,desired_state,observed_state,writer_epoch,active_writer_run_id,resource_version,controller_owned,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (resource_id, spec["projectId"], spec["displayName"], spec["kind"], spec["purpose"], spec["path"], spec.get("gitDir"), spec.get("branchRef"), spec.get("expectedHeadOid"), spec.get("expectedTreeOid"), "read-only", "present", spec["observedState"], 0, None, 1, 0, now, now, now, None, "shadow import; no controller ownership or writer claim"),
        )
        imported += 1
    return imported


def _insert_conversation_rows(store: ControllerStore, expectations: _ShadowExpectations, mappings: Mapping[str, str | None]) -> int:
    imported = 0
    for spec in expectations.conversations:
        conversation_id = mappings.get(str(spec["recordId"]))
        if not isinstance(conversation_id, str):
            raise MigrationUnresolvedError("conversation mapping was not allocated", detail={"recordId": spec["recordId"]})
        working_copy_id = mappings.get(str(spec["workingCopyKey"])) if spec.get("workingCopyKey") is not None else None
        if spec.get("workingCopyKey") is not None and not isinstance(working_copy_id, str):
            raise MigrationUnresolvedError("conversation working-copy mapping was not allocated", detail={"recordId": spec["recordId"]})
        existing = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        immutable = (spec["projectId"], working_copy_id, spec["role"], spec["piSessionId"], spec["sessionFile"])
        if existing is not None:
            if (existing["project_id"], existing["working_copy_id"], existing["role"], existing["pi_session_id"], existing["session_file"]) != immutable:
                raise ConstraintError("shadow conversation mapping disagrees with the source-authority row")
            continue
        now = utc_now()
        store.conn.execute(
            "INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (conversation_id, spec["projectId"], working_copy_id, spec["role"], spec["displayName"], spec["piSessionId"], spec["sessionFile"], spec["desiredState"], spec["observedState"], 1, now, now, now, None, "shadow import; legacy session remains the conversation authority"),
        )
        imported += 1
    return imported


def shadow_import_v2(report: InventoryV2Report, resolution: ResolutionManifest, state_root: os.PathLike[str] | str, *, idempotency_key: str, failpoint: Any | None = None) -> dict[str, Any]:
    if not isinstance(idempotency_key, str) or not idempotency_key: raise ValueError("shadow import requires an idempotency key")
    root = _safe_shadow_root(state_root)
    marker = root / "shadow-import-v2.marker"
    if marker.exists():
        if marker.read_text(encoding="utf-8") != report.digest: raise IdempotencyConflictError(idempotency_key, existing_digest=marker.read_text(encoding="utf-8"), request_digest=report.digest)
    else:
        if any(root.iterdir()): raise ValueError("shadow root must be empty before import")
        marker.write_text(report.digest, encoding="utf-8"); os.chmod(marker, 0o600)
    with ControllerStore(root) as store:
        existing = store.conn.execute("SELECT * FROM migration_runs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing is not None:
            if existing["source_manifest_digest"] != report.digest: raise IdempotencyConflictError(idempotency_key, existing_digest=existing["source_manifest_digest"], request_digest=report.digest)
            if existing["state"] == "succeeded":
                replay = {"migrationId": existing["migration_id"], "state": "succeeded", "idempotent": True}
                if existing["result_json"]:
                    parsed = parse_canonical_json(existing["result_json"])
                    if isinstance(parsed, Mapping):
                        replay.update({key: value for key, value in parsed.items() if key != "migrationId"})
                return replay
            if existing["state"] not in {"planned", "applying"}:
                return {"migrationId": existing["migration_id"], "state": existing["state"], "idempotent": True}
            # A run that failed before committing success resumes from its
            # recorded intent.  Every later step is idempotent: the manifest is
            # reused when intact, mappings are re-allocated in place, and row
            # inserts skip existing rows.  The final transaction commits
            # success atomically with the imported rows.
            migration_id = str(existing["migration_id"])
            operation_id = str(existing["operation_id"])
            build_id = str(existing["controller_build_id"])
        else:
            migration_id = new_id("mig")
            operation = create_operation(store, idempotency_key=idempotency_key, kind="migration", resource_type="migration", resource_id=migration_id, actor_type="controller", actor_id="shadow-import-v2", request={"schemaVersion": 2, "sourceManifestDigest": report.digest, "resolutionDigest": resolution.digest})
            operation_id = operation.operation_id
            build_id = new_id("build")
            store.register_build(build_id, source_tree_hash=report.digest, artifact_manifest_hash=report.digest, pi_version="shadow", package_lock_hash=report.digest, status="staged", verification={"shadow": True, "sourceManifestDigest": report.digest})
            now = utc_now()
            with store.transaction():
                store.conn.execute("INSERT INTO migration_runs(migration_id,operation_id,idempotency_key,mode,controller_build_id,request_digest,source_manifest_digest,state,step,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (migration_id, operation_id, idempotency_key, "shadow-import", build_id, operation.request_digest, report.digest, "planned", "intent-recorded", 1, now, now))
                append_event_in_transaction(store.conn, event_kind="migration.planned", resource_type="migration", resource_id=migration_id, resource_version=1, operation_id=operation_id, payload={"migrationId": migration_id, "sourceManifestDigest": report.digest})
        if failpoint is not None: failpoint.hit("shadow.manifest.before", {"migrationId": migration_id})
        manifest_path = root / "source-inventory-v2.json"
        if manifest_path.exists() or manifest_path.is_symlink():
            if not _manifest_is_intact(manifest_path, report.digest):
                raise IdempotencyConflictError(idempotency_key, existing_digest="<tampered-or-unsafe-manifest>", request_digest=report.digest)
        else:
            write_inventory_v2(report, manifest_path)
        with store.transaction():
            if store.conn.execute("SELECT 1 FROM migration_manifests WHERE migration_id=? AND kind='source-inventory'", (migration_id,)).fetchone() is None:
                store.conn.execute("INSERT INTO migration_manifests(migration_id,kind,path,sha256,size_bytes,created_at) VALUES(?,?,?,?,?,?)", (migration_id, "source-inventory", str(manifest_path), report.digest, manifest_path.stat().st_size, utc_now()))
            store.conn.execute("UPDATE migration_runs SET state='applying',step='manifest-verified',resource_version=resource_version+1,updated_at=? WHERE migration_id=?", (utc_now(), migration_id))
            append_event_in_transaction(store.conn, event_kind="migration.manifest_verified", resource_type="migration", resource_id=migration_id, resource_version=2, operation_id=operation_id, payload={"manifestDigest": report.digest})
        mappings = allocate_migration_mappings(store, migration_id=migration_id, inventory=report, resolution=resolution)
        if failpoint is not None: failpoint.hit("shadow.mappings.after", {"migrationId": migration_id})
        with store.transaction():
            store.conn.execute("UPDATE migration_runs SET step='importing',resource_version=resource_version+1,updated_at=? WHERE migration_id=?", (utc_now(), migration_id))
            expectations = build_shadow_expectations(report, resolution, mappings)
            _ensure_worktree_mappings(store, migration_id, expectations, mappings, report.digest)
            if failpoint is not None: failpoint.hit("shadow.resources.before", {"migrationId": migration_id})
            imported_projects = _insert_project_rows(store, expectations)
            imported_working_copies = _insert_worktree_rows(store, expectations, mappings)
            imported_conversations = _insert_conversation_rows(store, expectations, mappings)
            if failpoint is not None: failpoint.hit("shadow.batch.after", {"migrationId": migration_id})
            result = {
                "migrationId": migration_id,
                "state": "succeeded",
                "importedProjects": imported_projects,
                "importedWorkingCopies": imported_working_copies,
                "importedConversations": imported_conversations,
                "importedWorkstreams": 0,
                "activeRuns": 0,
                "writers": 0,
                "unmigratedWorkstreams": sum(1 for item in resolution.payload.get("decisions", []) if item.get("resourceType") == "workstream" and item.get("disposition") != "import"),
            }
            store.conn.execute("UPDATE migration_runs SET state='succeeded',step='complete',result_json=?,resource_version=resource_version+1,updated_at=?,completed_at=? WHERE migration_id=?", (canonical_json(result), utc_now(), utc_now(), migration_id))
            update_operation_in_transaction(store.conn, operation_id, state="succeeded", step="complete", result=result)
            append_event_in_transaction(store.conn, event_kind="migration.shadow_imported", resource_type="migration", resource_id=migration_id, resource_version=4, operation_id=operation_id, payload=result)
        return result


__all__ = ["build_shadow_expectations", "shadow_import_v2"]
