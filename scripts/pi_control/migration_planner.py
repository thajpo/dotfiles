"""Immutable migration resolution manifests and random ID allocation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import IdempotencyConflictError, MigrationUnresolvedError
from .migration import InventoryV2Report, inventory_canonical_json
from .models import canonical_json, new_id, validate_id, utc_now

_DISPOSITIONS = {"import", "observe", "unmigrated", "exclude", "requires-decision", "contradiction"}
_PREFIXES = {"project": "prj", "working-copy": "wc", "conversation": "conv", "workstream": "ws", "run": "run", "change": "chg", "review": "review", "integration": "int", "migration": "mig", "artifact": "art"}


def _digest(payload: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(inventory_canonical_json(payload).encode()).hexdigest()


@dataclass(frozen=True)
class ResolutionManifest:
    payload: dict[str, Any]
    digest: str
    path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload, "manifestDigest": self.digest, "manifestPath": self.path}


def _inventory_records(inventory: InventoryV2Report | Mapping[str, Any]) -> tuple[str, dict[str, dict[str, Any]]]:
    payload = inventory.payload if isinstance(inventory, InventoryV2Report) else dict(inventory)
    digest = inventory.digest if isinstance(inventory, InventoryV2Report) else _digest(payload)
    records = payload.get("records")
    if payload.get("schemaVersion") != 2 or not isinstance(records, list):
        raise MigrationUnresolvedError("inventory manifest is not schema v2")
    by_id = {str(record.get("record_id", record.get("recordId"))): record for record in records if isinstance(record, Mapping) and record.get("record_id", record.get("recordId"))}
    return digest, by_id


def create_resolution_manifest(inventory: InventoryV2Report | Mapping[str, Any], *, decisions: Sequence[Mapping[str, Any]], scope: Mapping[str, Any] | None = None, created_by: str = "user") -> ResolutionManifest:
    inventory_digest, records = _inventory_records(inventory)
    if created_by not in {"user", "migration-policy"}: raise MigrationUnresolvedError("resolution creator is invalid")
    if not isinstance(decisions, Sequence) or isinstance(decisions, (str, bytes)): raise MigrationUnresolvedError("resolution decisions must be an array")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, Mapping) or set(decision) != {"recordId", "disposition", "resourceType", "resourceId", "reason", "expectedDigest"}:
            raise MigrationUnresolvedError("resolution decision fields are not exact")
        record_id = str(decision["recordId"])
        if record_id not in records or record_id in seen: raise MigrationUnresolvedError("resolution decision references an unknown or duplicate record")
        seen.add(record_id)
        disposition = decision["disposition"]
        if disposition not in _DISPOSITIONS: raise MigrationUnresolvedError("resolution disposition is invalid")
        record = records[record_id]
        source_digest = record.get("source_digest", record.get("sourceDigest"))
        if decision["expectedDigest"] != source_digest: raise MigrationUnresolvedError("resolution expected digest does not match inventory")
        resource_id = decision["resourceId"]
        if disposition == "import":
            prefix = _PREFIXES.get(str(decision["resourceType"]))
            if prefix is None: raise MigrationUnresolvedError("import resource type is unsupported")
            if resource_id is not None:
                if not isinstance(resource_id, str): raise MigrationUnresolvedError("import resource ID is invalid")
                try: validate_id(resource_id, prefix=prefix)
                except ValueError as error: raise MigrationUnresolvedError("import resource ID is not a random controller ID") from error
        elif resource_id is not None:
            raise MigrationUnresolvedError("non-import resolution cannot bind a resource ID")
        normalized.append(dict(decision))
    if set(records) != seen:
        missing = sorted(set(records) - seen)
        raise MigrationUnresolvedError("resolution manifest omits inventory records", detail={"missing": ",".join(missing[:16])})
    payload = {"schemaVersion": 1, "inventoryDigest": inventory_digest, "scope": dict(scope or {"projectRecordIds": []}), "decisions": normalized, "createdBy": created_by, "createdAt": utc_now()}
    return ResolutionManifest(payload, _digest(payload))


def write_resolution_manifest(manifest: ResolutionManifest, destination: os.PathLike[str] | str) -> ResolutionManifest:
    path = Path(destination).expanduser().absolute()
    if path.exists() or path.is_symlink(): raise FileExistsError(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    body = inventory_canonical_json(manifest.payload).encode() + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "wb") as stream:
        stream.write(body); stream.flush(); os.fsync(stream.fileno())
    return ResolutionManifest(manifest.payload, manifest.digest, str(path))


def load_resolution_manifest(path: os.PathLike[str] | str) -> ResolutionManifest:
    source = Path(path).expanduser().absolute()
    if source.is_symlink() or not source.is_file(): raise FileNotFoundError(str(source))
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1: raise MigrationUnresolvedError("resolution manifest schema is unsupported")
    return ResolutionManifest(payload, _digest(payload), str(source))


def allocate_migration_mappings(store: Any, *, migration_id: str, inventory: InventoryV2Report | Mapping[str, Any], resolution: ResolutionManifest | Mapping[str, Any]) -> dict[str, str | None]:
    try: validate_id(migration_id, prefix="mig")
    except ValueError as error: raise MigrationUnresolvedError("migration ID is invalid") from error
    inventory_digest, records = _inventory_records(inventory)
    payload = resolution.payload if isinstance(resolution, ResolutionManifest) else dict(resolution)
    if payload.get("inventoryDigest") != inventory_digest: raise MigrationUnresolvedError("resolution manifest is bound to a different inventory")
    decisions = payload.get("decisions")
    if not isinstance(decisions, list): raise MigrationUnresolvedError("resolution decisions are missing")
    result: dict[str, str | None] = {}
    with store.transaction():
        if store.conn.execute("SELECT 1 FROM migration_runs WHERE migration_id=?", (migration_id,)).fetchone() is None: raise MigrationUnresolvedError("migration run was not found")
        for decision in decisions:
            record_id = str(decision["recordId"])
            existing = store.conn.execute("SELECT resource_id,source_digest,disposition FROM migration_resource_mappings WHERE migration_id=? AND record_id=?", (migration_id, record_id)).fetchone()
            source_digest = records[record_id].get("source_digest", records[record_id].get("sourceDigest"))
            if existing is not None:
                if existing["source_digest"] != source_digest or existing["disposition"] != decision["disposition"]: raise IdempotencyConflictError(migration_id, existing_digest=str(existing["source_digest"]), request_digest=str(source_digest))
                result[record_id] = existing["resource_id"]
                continue
            disposition = decision["disposition"]
            resource_id = decision["resourceId"]
            if disposition in {"requires-decision", "contradiction"}: raise MigrationUnresolvedError("migration mapping remains unresolved", detail={"record_id": record_id})
            if disposition == "import" and resource_id is None:
                prefix = _PREFIXES.get(str(decision["resourceType"]))
                if prefix is None: raise MigrationUnresolvedError("resource type cannot allocate a controller ID")
                resource_id = new_id(prefix)
            store.conn.execute("INSERT INTO migration_resource_mappings(migration_id,record_id,adapter_kind,source_kind,source_digest,resource_type,resource_id,disposition,reason_code,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (migration_id, record_id, str(records[record_id].get("adapter_kind", records[record_id].get("adapterKind", "unknown"))), str(records[record_id].get("source_kind", records[record_id].get("sourceKind", "unknown"))), source_digest, decision["resourceType"], resource_id, disposition, decision["reason"], canonical_json({"inventoryDigest": inventory_digest, "expectedDigest": decision["expectedDigest"]}), utc_now()))
            result[record_id] = resource_id
    return result


__all__ = ["ResolutionManifest", "allocate_migration_mappings", "create_resolution_manifest", "load_resolution_manifest", "write_resolution_manifest"]
