"""Locked package requests and isolated cache-backed materialization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from typing import Any, Mapping

from .command_requests import _binding
from .docker_runtime import inspect_package_image, run_one_shot_package
from .events import append_event_in_transaction
from .models import canonical_json, json_digest, new_id, utc_now, validate_id
from .package_diff import diff_observations, observe_package_tree, read_tree_file
from .run_manifest import read_manifest


class PackageEnvironmentError(ValueError):
    pass


def _expires() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _expired(value: str) -> bool:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) <= datetime.now(timezone.utc)


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_relative(value: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value or PurePosixPath(value).as_posix() != value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise PackageEnvironmentError("package cache artifact path is not canonical relative")
    return value


def _owned_path(path: Path, *, directory: bool) -> Path:
    value = path.expanduser().absolute()
    current = Path(value.anchor)
    for component in value.parts[1:]:
        current /= component
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid not in {0, os.geteuid()}:
            raise PackageEnvironmentError("package cache path is symlinked or has unsafe ownership")
    info = value.lstat()
    if (directory and not stat.S_ISDIR(info.st_mode)) or (not directory and not stat.S_ISREG(info.st_mode)):
        raise PackageEnvironmentError("package cache path has the wrong file type")
    if info.st_mode & 0o022:
        raise PackageEnvironmentError("package cache path is writable by group or other")
    return value


def _test_cache_marker(state_root: Path) -> bool:
    marker = state_root / ".pi-package-cache-test-fixture"
    try:
        info = marker.lstat()
        body = marker.read_text(encoding="ascii")
    except OSError:
        return False
    return state_root.resolve().is_relative_to(Path("/tmp")) and stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600 and body == "P6-NONPRODUCTION-PACKAGE-CACHE\n"


def _load_cache_policy(store: Any, ecosystem: str) -> dict[str, Any] | None:
    path = Path(store.state_root) / "package-cache-policy.json"
    if not path.exists() and not path.is_symlink():
        return None
    _owned_path(path, directory=False)
    if stat.S_IMODE(path.lstat().st_mode) != 0o600:
        raise PackageEnvironmentError("package cache policy must be mode 0600")
    try:
        raw = path.read_bytes()
        policy = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageEnvironmentError("package cache policy is unreadable") from error
    if not isinstance(policy, dict) or set(policy) != {"schemaVersion", "testOnly", "cacheRoot", "inventoryDigest", "ecosystems"} or policy["schemaVersion"] != 1 or canonical_json(policy).encode("utf-8") + b"\n" != raw:
        raise PackageEnvironmentError("package cache policy is not exact canonical version 1")
    if not isinstance(policy["testOnly"], bool) or not isinstance(policy["ecosystems"], dict):
        raise PackageEnvironmentError("package cache policy fields are invalid")
    selected = policy["ecosystems"].get(ecosystem)
    if not isinstance(selected, dict) or set(selected) != {"imageReference", "imageConfigId", "platform", "allowLocalConfigIdOnly"}:
        raise PackageEnvironmentError("package cache policy does not configure the requested ecosystem")
    if not all(isinstance(selected[key], str) and selected[key] for key in ("imageReference", "imageConfigId", "platform")) or not isinstance(selected["allowLocalConfigIdOnly"], bool):
        raise PackageEnvironmentError("package cache image identity is invalid")
    if selected["allowLocalConfigIdOnly"] and (not policy["testOnly"] or not _test_cache_marker(Path(store.state_root))):
        raise PackageEnvironmentError("local config-ID-only package images are restricted to an explicit disposable test fixture")
    cache = _owned_path(Path(policy["cacheRoot"]), directory=True)
    inventory_path = _owned_path(cache / "inventory.json", directory=False)
    if _file_digest(inventory_path) != policy["inventoryDigest"]:
        raise PackageEnvironmentError("package cache inventory digest changed")
    try:
        inventory_raw = inventory_path.read_bytes()
        inventory = json.loads(inventory_raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageEnvironmentError("package cache inventory is unreadable") from error
    if not isinstance(inventory, dict) or set(inventory) != {"schemaVersion", "artifacts"} or inventory["schemaVersion"] != 1 or not isinstance(inventory["artifacts"], list) or canonical_json(inventory).encode("utf-8") + b"\n" != inventory_raw:
        raise PackageEnvironmentError("package cache inventory is not exact canonical version 1")
    ecosystem_artifacts = 0
    for item in inventory["artifacts"]:
        if not isinstance(item, dict) or set(item) != {"ecosystem", "path", "sha256", "size"} or item["ecosystem"] not in {"npm", "python"} or not isinstance(item["size"], int) or item["size"] < 0:
            raise PackageEnvironmentError("package cache inventory artifact is invalid")
        artifact = _owned_path(cache / _canonical_relative(item["path"]), directory=False)
        if artifact.stat().st_size != item["size"] or _file_digest(artifact) != item["sha256"]:
            raise PackageEnvironmentError("package cache artifact differs from its exact inventory")
        if item["ecosystem"] == ecosystem:
            ecosystem_artifacts += 1
    if ecosystem_artifacts == 0:
        raise PackageEnvironmentError("package cache has no artifact for the requested ecosystem")
    image = inspect_package_image(selected["imageReference"], expected_image_config_id=selected["imageConfigId"], expected_platform=selected["platform"], allow_local_config_id_only=selected["allowLocalConfigIdOnly"])
    return {
        "cacheRoot": str(cache), "inventoryDigest": policy["inventoryDigest"], "testOnly": policy["testOnly"],
        "allowLocalConfigIdOnly": selected["allowLocalConfigIdOnly"], **image,
    }


def _candidate(store: Any, project_id: str, change_id: str, revision: int, working_copy_id: str) -> tuple[Any, Any]:
    row = store.conn.execute("SELECT cr.*,c.project_id,c.source_working_copy_id FROM change_revisions cr JOIN changes c ON c.change_id=cr.change_id WHERE cr.change_id=? AND cr.revision=? AND c.project_id=?", (change_id, revision, project_id)).fetchone()
    working = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=? AND project_id=?", (working_copy_id, project_id)).fetchone()
    if row is None or working is None or row["source_working_copy_id"] != working_copy_id:
        raise PackageEnvironmentError("package request is not bound to the writer's immutable candidate")
    return row, working


def _package_name(ecosystem: str, value: str | None) -> str | None:
    if value is None:
        return None
    pattern = r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*" if ecosystem == "npm" else r"[A-Za-z0-9][A-Za-z0-9_.-]*"
    if re.fullmatch(pattern, value) is None:
        raise PackageEnvironmentError("package name is not valid for the selected ecosystem")
    return value


def request_package_operation(
    store: Any, *, project_id: str, conversation_id: str, run_id: str, writer_generation: int,
    change_id: str, revision: int, ecosystem: str, action: str, package_name: str | None = None,
    exact_version: str | None = None,
) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(conversation_id, prefix="conv")
    validate_id(run_id, prefix="run")
    validate_id(change_id, prefix="chg")
    if ecosystem not in {"npm", "python"} or action not in {"add", "remove", "sync"}:
        raise PackageEnvironmentError("package ecosystem or structured action is unsupported")
    package_name = _package_name(ecosystem, package_name)
    if action == "sync":
        if package_name is not None or exact_version is not None:
            raise PackageEnvironmentError("sync does not accept a package selector")
    elif package_name is None or not isinstance(exact_version, str) or not exact_version or len(exact_version) > 256:
        raise PackageEnvironmentError("add/remove requires one exact package and version")
    binding = _binding(store, project_id, conversation_id, run_id, writer_generation)
    candidate, working = _candidate(store, project_id, change_id, revision, binding["working_copy_id"])
    base = observe_package_tree(working["path"], candidate["base_oid"] + "^{tree}")
    current = observe_package_tree(working["path"], candidate["tree_oid"])
    selected = next((item for item in current["ecosystems"] if item["ecosystem"] == ecosystem), None)
    base_selected = next((item for item in base["ecosystems"] if item["ecosystem"] == ecosystem), None)
    if selected is None:
        raise PackageEnvironmentError("candidate has no supported locked input for the requested ecosystem")
    differences = diff_observations(base, current)
    if action != "sync" and not any(item["ecosystem"] == ecosystem and item["changeKind"] == action and item["packageName"] == package_name and item["exactVersion"] == exact_version for item in differences):
        raise PackageEnvironmentError("structured package action does not match the immutable lock delta")
    cache = _load_cache_policy(store, ecosystem)
    if cache is None:
        run = store.conn.execute("SELECT manifest_path FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None or not run["manifest_path"]:
            raise PackageEnvironmentError("package request has no exact runtime image")
        tool = read_manifest(run["manifest_path"]).manifest.get("toolRuntime") or {}
        image_reference, image_config_id, platform = tool.get("imageReference"), tool.get("imageConfigId"), tool.get("platform")
        if not all(isinstance(item, str) and item for item in (image_reference, image_config_id, platform)):
            raise PackageEnvironmentError("package request runtime image identity is incomplete")
        cache = {"cacheRoot": None, "inventoryDigest": None, "testOnly": False, "allowLocalConfigIdOnly": False, "imageReference": image_reference, "imageConfigId": image_config_id, "registryDigest": tool.get("registryDigest"), "platform": platform, "testOnlyConfigIdentity": False}
    body = {
        "projectId": project_id, "conversationId": conversation_id, "runId": run_id, "workingCopyId": working["working_copy_id"],
        "writerEpoch": writer_generation, "changeId": change_id, "revision": revision, "ecosystem": ecosystem,
        "action": action, "packageName": package_name, "exactVersion": exact_version, "baseTreeOid": base["treeOid"],
        "candidateTreeOid": current["treeOid"], "manifestDigest": selected["manifestDigest"],
        "baseLockDigest": base_selected["lockDigest"] if base_selected else None, "candidateLockDigest": selected["lockDigest"],
        "scriptsPolicy": "disabled", "cacheInventoryDigest": cache["inventoryDigest"], "cacheTestOnly": cache["testOnly"],
        "imageReference": cache["imageReference"], "imageConfigId": cache["imageConfigId"], "platform": cache["platform"],
    }
    environment_id = "pkg_" + json_digest(body)[:32]
    body["privateEnvironmentIdentity"] = environment_id
    digest = json_digest(body)
    with store.transaction():
        exact_binding = _binding(store, project_id, conversation_id, run_id, writer_generation)
        exact_candidate, _ = _candidate(store, project_id, change_id, revision, exact_binding["working_copy_id"])
        if exact_candidate["tree_oid"] != candidate["tree_oid"] or exact_candidate["base_oid"] != candidate["base_oid"]:
            raise PackageEnvironmentError("package candidate changed before final request mutation")
        existing = store.conn.execute("SELECT * FROM package_requests WHERE request_digest=?", (digest,)).fetchone()
        if existing is not None:
            return dict(existing)
        request_id = new_id("pkreq")
        now = utc_now()
        store.conn.execute(
            "INSERT INTO package_requests(package_request_id,project_id,conversation_id,run_id,working_copy_id,writer_generation,change_id,revision,ecosystem,action,package_name,exact_version,base_tree_oid,candidate_tree_oid,manifest_path,manifest_digest,lock_path,base_lock_digest,candidate_lock_digest,scripts_policy,cache_root,cache_inventory_digest,cache_test_only,allow_local_config_id_only,image_reference,image_config_id,platform,request_digest,state,authorization_id,environment_id,result_json,created_at,expires_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, project_id, conversation_id, run_id, working["working_copy_id"], writer_generation, change_id, revision, ecosystem, action, package_name, exact_version, base["treeOid"], current["treeOid"], selected["manifestPath"], selected["manifestDigest"], selected["lockPath"], base_selected["lockDigest"] if base_selected else None, selected["lockDigest"], "disabled", cache["cacheRoot"], cache["inventoryDigest"], int(cache["testOnly"]), int(cache["allowLocalConfigIdOnly"]), cache["imageReference"], cache["imageConfigId"], cache["platform"], digest, "requested", None, environment_id, None, now, _expires(), None),
        )
        append_event_in_transaction(store.conn, event_kind="package.requested", resource_type="package-request", resource_id=request_id, payload={"projectId": project_id, "ecosystem": ecosystem, "action": action, "requestDigest": digest, "cacheInventoryDigest": cache["inventoryDigest"]})
        return dict(store.conn.execute("SELECT * FROM package_requests WHERE package_request_id=?", (request_id,)).fetchone())


def package_status(store: Any, *, project_id: str, package_request_id: str) -> dict[str, Any]:
    validate_id(project_id, prefix="prj")
    validate_id(package_request_id, prefix="pkreq")
    row = store.conn.execute("SELECT * FROM package_requests WHERE package_request_id=? AND project_id=?", (package_request_id, project_id)).fetchone()
    if row is None:
        raise PackageEnvironmentError("package request was not found in the authenticated project")
    return dict(row)


def package_approval_display(store: Any, *, request_id: str, request_digest: str) -> dict[str, Any]:
    validate_id(request_id, prefix="pkreq")
    row = store.conn.execute("SELECT r.*,p.display_name,c.role,w.path FROM package_requests r JOIN projects p ON p.project_id=r.project_id JOIN conversations c ON c.conversation_id=r.conversation_id JOIN working_copies w ON w.working_copy_id=r.working_copy_id WHERE r.package_request_id=?", (request_id,)).fetchone()
    if row is None or row["request_digest"] != request_digest:
        raise PackageEnvironmentError("request ID and digest do not identify one exact package request")
    operation = {"ecosystem": row["ecosystem"], "action": row["action"], "packageName": row["package_name"], "exactVersion": row["exact_version"], "scriptsPolicy": row["scripts_policy"]}
    scope = {"manifestDigest": row["manifest_digest"], "baseLockDigest": row["base_lock_digest"], "candidateLockDigest": row["candidate_lock_digest"], "candidateTreeOid": row["candidate_tree_oid"], "cacheRoot": row["cache_root"], "cacheInventoryDigest": row["cache_inventory_digest"], "privateEnvironmentIdentity": row["environment_id"]}
    return {"requestId": request_id, "digest": request_digest, "state": row["state"], "project": {"id": row["project_id"], "name": row["display_name"]}, "conversation": {"id": row["conversation_id"], "role": row["role"]}, "runId": row["run_id"], "operation": operation, "argv": [], "cwd": row["path"], "effectScope": scope, "executionPlace": "package-network-container", "expiresAt": row["expires_at"]}


def _scope(store: Any, row: Mapping[str, Any]) -> dict[str, Any]:
    scope = {"baseTreeOid": row["base_tree_oid"], "candidateTreeOid": row["candidate_tree_oid"], "manifestDigest": row["manifest_digest"], "baseLockDigest": row["base_lock_digest"], "candidateLockDigest": row["candidate_lock_digest"], "scriptsPolicy": row["scripts_policy"], "cacheRoot": row["cache_root"], "cacheInventoryDigest": row["cache_inventory_digest"], "cacheTestOnly": bool(row["cache_test_only"]), "imageReference": row["image_reference"], "imageConfigId": row["image_config_id"], "platform": row["platform"], "privateEnvironmentIdentity": row["environment_id"]}
    return {"controller": store.controller_identity(), "requestId": row["package_request_id"], "requestDigest": row["request_digest"], "projectId": row["project_id"], "conversationId": row["conversation_id"], "runId": row["run_id"], "workingCopyId": row["working_copy_id"], "writerEpoch": row["writer_generation"], "operation": {"ecosystem": row["ecosystem"], "action": row["action"], "packageName": row["package_name"], "exactVersion": row["exact_version"]}, "place": "package-network-container", "cwdWorkingCopyId": row["working_copy_id"], "scope": scope, "expiresAt": row["expires_at"], "oneUse": True}


def approve_package_request(store: Any, *, package_request_id: str, request_digest: str, actor_id: str = "controlling-tty") -> dict[str, Any]:
    with store.transaction():
        row = store.conn.execute("SELECT * FROM package_requests WHERE package_request_id=?", (package_request_id,)).fetchone()
        if row is None or row["request_digest"] != request_digest or row["state"] != "requested":
            raise PackageEnvironmentError("package request is changed, stale, replayed, or already decided")
        _binding(store, row["project_id"], row["conversation_id"], row["run_id"], int(row["writer_generation"]))
        if _expired(row["expires_at"]):
            store.conn.execute("UPDATE package_requests SET state='expired',completed_at=? WHERE package_request_id=?", (utc_now(), package_request_id))
            raise PackageEnvironmentError("package request has expired")
        scope = _scope(store, row)
        authorization_id = new_id("auth")
        now = utc_now()
        store.conn.execute("INSERT INTO authorizations(authorization_id,kind,actor_type,actor_id,project_id,resource_type,resource_id,request_context_id,scope_json,scope_digest,issued_at,expires_at,consumed_at,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (authorization_id, "package-network-operation", "user", actor_id, row["project_id"], "package-request", package_request_id, package_request_id, canonical_json(scope), json_digest(scope), now, row["expires_at"], None, "active"))
        store.conn.execute("UPDATE package_requests SET state='approved',authorization_id=? WHERE package_request_id=?", (authorization_id, package_request_id))
        return {"authorizationId": authorization_id, "receipt": scope, "receiptDigest": json_digest(scope)}


def reject_package_request(store: Any, *, package_request_id: str, request_digest: str) -> dict[str, Any]:
    with store.transaction():
        row = store.conn.execute("SELECT * FROM package_requests WHERE package_request_id=?", (package_request_id,)).fetchone()
        if row is None or row["request_digest"] != request_digest or row["state"] != "requested":
            raise PackageEnvironmentError("package request is changed, stale, replayed, or already decided")
        _binding(store, row["project_id"], row["conversation_id"], row["run_id"], int(row["writer_generation"]))
        store.conn.execute("UPDATE package_requests SET state='rejected',result_json=?,completed_at=? WHERE package_request_id=?", (canonical_json({"reason": "rejected at controlling TTY"}), utc_now(), package_request_id))
        return dict(store.conn.execute("SELECT * FROM package_requests WHERE package_request_id=?", (package_request_id,)).fetchone())


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(path, 0o700)


def _input_tree(store: Any, row: Mapping[str, Any], repository: Path) -> Path:
    root = Path(store.state_root) / "package-inputs" / row["package_request_id"]
    _private_directory(root)
    paths = {str(row["manifest_path"]), str(row["lock_path"])}
    for relative in paths:
        destination = root / _canonical_relative(relative)
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        destination.write_bytes(read_tree_file(repository, row["candidate_tree_oid"], relative))
        os.chmod(destination, 0o400)
    (root / ".git").write_bytes(b"")
    os.chmod(root / ".git", 0o400)
    return root


def _tree_digest(root: Path) -> str:
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise PackageEnvironmentError("package environment contains a symlink")
        if stat.S_ISREG(info.st_mode):
            records.append({"path": relative, "kind": "file", "mode": stat.S_IMODE(info.st_mode), "sha256": _file_digest(path), "size": info.st_size})
        elif stat.S_ISDIR(info.st_mode):
            records.append({"path": relative, "kind": "directory", "mode": stat.S_IMODE(info.st_mode)})
        else:
            raise PackageEnvironmentError("package environment contains an unsupported file type")
    return "sha256:" + hashlib.sha256(canonical_json(records).encode("utf-8")).hexdigest()


def _installed_packages(ecosystem: str, environment: Path, expected: Mapping[str, str]) -> list[dict[str, str]]:
    installed: list[dict[str, str]] = []
    if ecosystem == "npm":
        for name, version in sorted(expected.items()):
            metadata = environment / "node_modules" / name / "package.json"
            try:
                value = json.loads(metadata.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise PackageEnvironmentError(f"npm environment lacks exact installed metadata for {name}") from error
            if value.get("name") != name or value.get("version") != version:
                raise PackageEnvironmentError(f"npm environment installed the wrong version for {name}")
            installed.append({"name": name, "version": version})
    else:
        metadata_by_name: dict[str, str] = {}
        for metadata in (environment / "site-packages").glob("*.dist-info/METADATA"):
            name = version = None
            for line in metadata.read_text(encoding="utf-8").splitlines():
                if line.startswith("Name: "):
                    name = line.removeprefix("Name: ").lower().replace("_", "-")
                elif line.startswith("Version: "):
                    version = line.removeprefix("Version: ")
            if name and version:
                metadata_by_name[name] = version
        for name, version in sorted(expected.items()):
            normalized = name.lower().replace("_", "-")
            if metadata_by_name.get(normalized) != version:
                raise PackageEnvironmentError(f"Python environment installed the wrong version for {name}")
            installed.append({"name": normalized, "version": version})
    return installed


def _package_argv(ecosystem: str) -> list[str]:
    if ecosystem == "npm":
        script = "set -eu; cp /workspace/package.json /workspace/package-lock.json /environment/; cd /environment; npm ci --offline --ignore-scripts --no-audit --no-fund --cache /tmp/npm-cache"
        return ["/bin/sh", "-c", script]
    return ["python3", "-m", "pip", "install", "--no-index", "--require-hashes", "--only-binary=:all:", "--no-compile", "--disable-pip-version-check", "--find-links", "/cache/python", "--target", "/environment/site-packages", "-r", "/workspace/requirements.txt"]


def execute_approved_package_request(store: Any, *, package_request_id: str, request_digest: str) -> dict[str, Any]:
    row = store.conn.execute("SELECT r.*,a.scope_json,a.scope_digest,a.state AS authorization_state FROM package_requests r JOIN authorizations a ON a.authorization_id=r.authorization_id WHERE r.package_request_id=?", (package_request_id,)).fetchone()
    if row is None or row["request_digest"] != request_digest or row["state"] != "approved" or row["authorization_state"] != "active":
        raise PackageEnvironmentError("package approval is missing, stale, replayed, or consumed")
    configured = _load_cache_policy(store, row["ecosystem"])
    if row["cache_root"] is not None:
        if configured is None or configured["cacheRoot"] != row["cache_root"] or configured["inventoryDigest"] != row["cache_inventory_digest"] or configured["imageReference"] != row["image_reference"] or configured["imageConfigId"] != row["image_config_id"] or configured["platform"] != row["platform"] or bool(configured["allowLocalConfigIdOnly"]) != bool(row["allow_local_config_id_only"]):
            raise PackageEnvironmentError("package cache or image identity changed after approval")
    with store.transaction():
        row = store.conn.execute("SELECT r.*,a.scope_json,a.scope_digest,a.state AS authorization_state FROM package_requests r JOIN authorizations a ON a.authorization_id=r.authorization_id WHERE r.package_request_id=?", (package_request_id,)).fetchone()
        if row is None or row["request_digest"] != request_digest or row["state"] != "approved" or row["authorization_state"] != "active":
            raise PackageEnvironmentError("package approval is missing, stale, replayed, or consumed")
        binding = _binding(store, row["project_id"], row["conversation_id"], row["run_id"], int(row["writer_generation"]))
        if _expired(row["expires_at"]) or canonical_json(_scope(store, row)) != row["scope_json"] or json_digest(_scope(store, row)) != row["scope_digest"]:
            raise PackageEnvironmentError("package approval expired or belongs to another controller generation")
        if store.conn.execute("UPDATE authorizations SET state='consumed',consumed_at=? WHERE authorization_id=? AND state='active'", (utc_now(), row["authorization_id"])).rowcount != 1 or store.conn.execute("UPDATE package_requests SET state='running' WHERE package_request_id=? AND state='approved'", (package_request_id,)).rowcount != 1:
            raise PackageEnvironmentError("package approval was already consumed")
    if row["cache_root"] is None:
        result = {"executed": False, "materialized": False, "reason": "exact-local-artifact-cache-unavailable", "remoteProviderContacted": False, "networkContacted": False, "scriptsPolicy": "disabled", "image": {"reference": row["image_reference"], "configId": row["image_config_id"], "platform": row["platform"]}, "cacheInventoryDigest": None, "lockInput": {"base": row["base_lock_digest"], "candidate": row["candidate_lock_digest"]}, "lockDelta": {"action": row["action"], "packageName": row["package_name"], "exactVersion": row["exact_version"]}, "privateEnvironmentIdentity": row["environment_id"]}
        with store.transaction():
            store.conn.execute("UPDATE package_requests SET state='failed',result_json=?,completed_at=? WHERE package_request_id=? AND state='running'", (canonical_json(result), utc_now(), package_request_id))
            return dict(store.conn.execute("SELECT * FROM package_requests WHERE package_request_id=?", (package_request_id,)).fetchone())
    environment = Path(store.state_root) / "environments" / row["working_copy_id"] / row["environment_id"]
    input_root: Path | None = None
    docker_result: dict[str, Any] | None = None
    try:
        repository = Path(str(binding["path"])).resolve(strict=True)
        candidate = observe_package_tree(repository, row["candidate_tree_oid"])
        selected = next((item for item in candidate["ecosystems"] if item["ecosystem"] == row["ecosystem"]), None)
        if selected is None or selected["manifestDigest"] != row["manifest_digest"] or selected["lockDigest"] != row["candidate_lock_digest"]:
            raise PackageEnvironmentError("immutable package input changed after approval")
        _private_directory(environment)
        input_root = _input_tree(store, row, repository)
        docker_result = run_one_shot_package(request_id=package_request_id, image_reference=row["image_reference"], expected_image_config_id=row["image_config_id"], expected_platform=row["platform"], allow_local_config_id_only=bool(row["allow_local_config_id_only"]), working_copy=input_root, cache_root=row["cache_root"], environment_root=environment, argv=_package_argv(row["ecosystem"]), timeout_ms=120_000)
        if docker_result.get("exitCode") != 0 or docker_result.get("timedOut"):
            raise PackageEnvironmentError(docker_result.get("stderr", "")[-1024:] or "package manager failed")
        current_cache = _load_cache_policy(store, row["ecosystem"])
        if current_cache is None or current_cache["inventoryDigest"] != row["cache_inventory_digest"]:
            raise PackageEnvironmentError("package cache changed during materialization")
        installed = _installed_packages(row["ecosystem"], environment, selected["resolved"])
        tree_digest = _tree_digest(environment)
        result = {**docker_result, "executed": True, "materialized": True, "scriptsPolicy": "disabled", "cacheRoot": row["cache_root"], "cacheInventoryDigest": row["cache_inventory_digest"], "lockInput": {"base": row["base_lock_digest"], "candidate": row["candidate_lock_digest"]}, "lockDelta": {"action": row["action"], "packageName": row["package_name"], "exactVersion": row["exact_version"]}, "privateEnvironmentIdentity": row["environment_id"], "environmentPath": str(environment), "environmentTreeDigest": tree_digest, "installedPackages": installed}
        now = utc_now()
        with store.transaction():
            store.conn.execute("INSERT INTO package_environments(environment_id,working_copy_id,manifest_digest,lock_digest,ecosystem,platform,image_config_id,image_reference,environment_path,cache_scope,cache_inventory_digest,scripts_policy,lock_delta_json,result_json,private_identity,environment_tree_digest,installed_package_json,cleanup_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (row["environment_id"], row["working_copy_id"], row["manifest_digest"], row["candidate_lock_digest"], row["ecosystem"], row["platform"], row["image_config_id"], row["image_reference"], str(environment), "inventory:" + row["cache_inventory_digest"], row["cache_inventory_digest"], "disabled", canonical_json(result["lockDelta"]), canonical_json(result), row["environment_id"], tree_digest, canonical_json(installed), canonical_json(docker_result["cleanup"]), now, now))
            store.conn.execute("UPDATE package_requests SET state='succeeded',result_json=?,completed_at=? WHERE package_request_id=? AND state='running'", (canonical_json(result), now, package_request_id))
            append_event_in_transaction(store.conn, event_kind="package.succeeded", resource_type="package-request", resource_id=package_request_id, payload={"requestDigest": request_digest, "environmentId": row["environment_id"], "environmentTreeDigest": tree_digest, "cacheInventoryDigest": row["cache_inventory_digest"]})
            return dict(store.conn.execute("SELECT * FROM package_requests WHERE package_request_id=?", (package_request_id,)).fetchone())
    except Exception as error:
        if environment.exists():
            shutil.rmtree(environment, ignore_errors=True)
        result = {"executed": docker_result is not None, "materialized": False, "reason": "package-materialization-failed", "error": type(error).__name__, "message": str(error)[:1024], "remoteProviderContacted": False, "networkContacted": False, "scriptsPolicy": "disabled", "cacheRoot": row["cache_root"], "cacheInventoryDigest": row["cache_inventory_digest"], "privateEnvironmentIdentity": row["environment_id"], "cleanup": docker_result.get("cleanup") if docker_result else {"containerCreated": False}}
        with store.transaction():
            store.conn.execute("UPDATE package_requests SET state='failed',result_json=?,completed_at=? WHERE package_request_id=? AND state='running'", (canonical_json(result), utc_now(), package_request_id))
            append_event_in_transaction(store.conn, event_kind="package.failed", resource_type="package-request", resource_id=package_request_id, payload={"requestDigest": request_digest, "reason": result["reason"]})
            return dict(store.conn.execute("SELECT * FROM package_requests WHERE package_request_id=?", (package_request_id,)).fetchone())
    finally:
        if input_root is not None:
            shutil.rmtree(input_root, ignore_errors=True)


__all__ = ["PackageEnvironmentError", "approve_package_request", "execute_approved_package_request", "package_approval_display", "package_status", "reject_package_request", "request_package_operation"]
