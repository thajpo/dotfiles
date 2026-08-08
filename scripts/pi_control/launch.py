"""Exact run preparation and lifecycle for fresh Pi conversations."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
from typing import Any, Mapping

from .events import append_event_in_transaction
from .models import canonical_json, new_id, utc_now, validate_id
from .process_adapter import process_start_identity
from .writer_lock import WriterLock


class LaunchError(RuntimeError):
    pass


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def ensure_installed_build(store: Any, *, build_id: str | None = None, pi_version: str = "0.0.0", source_commit: str | None = None, verification: Mapping[str, Any] | None = None) -> dict[str, Any]:
    selected = build_id or os.environ.get("PI_SYSTEM_BUILD_ID")
    if selected is None:
        active = store.conn.execute("SELECT * FROM installed_builds WHERE status='active' ORDER BY installed_at DESC LIMIT 1").fetchone()
        if active is not None:
            return dict(active)
        selected = "build_" + secrets.token_hex(16)
    row = store.conn.execute("SELECT * FROM installed_builds WHERE build_id=?", (selected,)).fetchone()
    if row is not None:
        return dict(row)
    now = utc_now()
    with store.transaction():
        store.conn.execute("INSERT INTO installed_builds(build_id,source_commit,source_tree_hash,artifact_manifest_hash,pi_version,package_lock_hash,status,installed_at,activated_at,rollback_path,verification_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (selected, source_commit, _digest({"sourceCommit": source_commit}), _digest(verification or {}), pi_version, _digest({"packageLock": os.environ.get("PI_SYSTEM_PACKAGE_LOCK", "")}), "active", now, now, None, canonical_json(dict(verification or {"source": "development"}))))
        return dict(store.conn.execute("SELECT * FROM installed_builds WHERE build_id=?", (selected,)).fetchone())


@dataclass
class PreparedRun:
    run: dict[str, Any]
    manifest: dict[str, Any]
    manifest_path: Path
    environment: dict[str, str]
    writer_lock: WriterLock | None = None

    def close(self) -> None:
        if self.writer_lock is not None:
            self.writer_lock.close()
            self.writer_lock = None

    def __enter__(self) -> "PreparedRun":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def prepare_run(store: Any, *, project_id: str, conversation_id: str, working_copy_id: str | None = None, authority: str = "read-only", runtime: Mapping[str, Any] | None = None, task_id: str | None = None, capability_secret: str | bytes | None = None, parent_run_id: str | None = None, build_id: str | None = None, owner_pid: int | None = None) -> PreparedRun:
    validate_id(project_id, prefix="prj")
    validate_id(conversation_id, prefix="conv")
    if working_copy_id is not None:
        validate_id(working_copy_id, prefix="wc")
    if authority not in {"read-only", "writer", "secretary", "host-maintenance"}:
        raise LaunchError("unsupported run authority")
    if authority == "writer" and working_copy_id is None:
        raise LaunchError("writer runs require a working copy")
    project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    conversation = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    if project is None or conversation is None or conversation["project_id"] != project_id:
        raise LaunchError("run project or conversation binding is invalid")
    working = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (working_copy_id,)).fetchone() if working_copy_id else None
    if working_copy_id and (working is None or working["project_id"] != project_id or conversation["working_copy_id"] not in {None, working_copy_id}):
        raise LaunchError("run working-copy binding is invalid")
    if authority == "writer" and working["effective_mode"] == "read-only":
        raise LaunchError("read-only working copy cannot host a writer")
    build = ensure_installed_build(store, build_id=build_id)
    runtime_value = dict(runtime or {})
    image_reference = runtime_value.pop("imageReference", None)
    image_config_id = runtime_value.pop("imageConfigId", None)
    registry_digest = runtime_value.pop("registryDigest", None)
    if authority == "writer":
        image_reference = image_reference or os.environ.get("PI_SYSTEM_RUNTIME_IMAGE")
        image_config_id = image_config_id or os.environ.get("PI_SYSTEM_RUNTIME_IMAGE_CONFIG_ID")
        registry_digest = registry_digest or os.environ.get("PI_SYSTEM_RUNTIME_REGISTRY_DIGEST")
        if not image_reference or not image_config_id:
            raise LaunchError("coding runs require an inspected runtime image reference and configuration ID")
        if not isinstance(image_config_id, str) or not image_config_id.startswith("sha256:"):
            raise LaunchError("runtime image configuration ID must be a SHA-256 identity")
    platform = runtime_value.pop("platform", "host" if authority != "writer" else os.environ.get("PI_SYSTEM_RUNTIME_PLATFORM"))
    if not platform:
        raise LaunchError("coding runs require an exact runtime platform")
    execution_target = runtime_value.pop("executionTarget", "host-read-only" if authority != "writer" else "container")
    if authority == "writer" and execution_target != "container":
        raise LaunchError("writer runs require the controller container execution target")
    if authority != "writer" and execution_target != "host-read-only":
        raise LaunchError("host roles require the host-read-only execution target")
    if runtime_value:
        raise LaunchError("runtime contains unsupported fields")
    run_id = new_id("run")
    writer_epoch = None
    lock: WriterLock | None = None
    now = utc_now()
    secret = capability_secret or secrets.token_urlsafe(32)
    raw_secret = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    if len(raw_secret) < 32:
        raise LaunchError("capability secret is too short")
    capability_hash = "sha256:" + hashlib.sha256(raw_secret).hexdigest()
    with store.transaction():
        if authority == "writer":
            existing_id = working["active_writer_run_id"]
            if existing_id is not None:
                existing = store.conn.execute("SELECT observed_state FROM runs WHERE run_id=?", (existing_id,)).fetchone()
                if existing is None or existing[0] not in {"stopped", "failed", "lost"}:
                    raise LaunchError("working copy already has a live or uncertain writer")
            writer_epoch = int(working["writer_epoch"]) + 1
            store.conn.execute("UPDATE working_copies SET writer_epoch=?,active_writer_run_id=?,updated_at=?,resource_version=resource_version+1 WHERE working_copy_id=?", (writer_epoch, run_id, now, working_copy_id))
        runtime_spec = {"executionTarget": execution_target, "platform": platform, "imageReference": image_reference, "imageConfigId": image_config_id, "registryDigest": registry_digest, "buildId": build["build_id"], "piVersion": build["pi_version"], "projectId": project_id, "workingCopyId": working_copy_id, "authority": authority}
        runtime_spec_hash = _digest(runtime_spec)
        store.conn.execute("INSERT INTO runs(run_id,conversation_id,project_id,working_copy_id,parent_run_id,child_source_json,authority,desired_state,observed_state,expected_working_copy_version,expected_head_oid,expected_tree_oid,dirty_fingerprint,writer_epoch,runtime_spec_hash,build_id,owner_pid,owner_start_identity,capability_hash,manifest_path,container_id,container_observation_json,resource_version,created_at,started_at,ended_at,updated_at,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, conversation_id, project_id, working_copy_id, parent_run_id, None, authority, "running", "preparing", int(working["resource_version"]) if working else None, working["expected_head_oid"] if working else None, working["expected_tree_oid"] if working else None, None, writer_epoch, runtime_spec_hash, build["build_id"], owner_pid or os.getpid(), process_start_identity(owner_pid or os.getpid()), capability_hash, None, None, None, 1, now, None, None, now, None, None))
    if authority == "writer":
        try:
            lock = WriterLock.acquire(store.state_root, working_copy_id, writer_epoch or 0)
        except BaseException:
            with store.transaction():
                store.conn.execute("UPDATE runs SET observed_state='failed',error_code='WRITER_LOCK_BUSY',ended_at=?,updated_at=? WHERE run_id=?", (utc_now(), utc_now(), run_id))
                store.conn.execute("UPDATE working_copies SET active_writer_run_id=NULL,updated_at=?,resource_version=resource_version+1 WHERE working_copy_id=? AND active_writer_run_id=?", (utc_now(), working_copy_id, run_id))
            raise
    project_policy = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    working_manifest = None
    if working is not None:
        working_path = Path(working["path"]).resolve(strict=True)
        git_common_dir = Path(project_policy["git_common_dir"]).resolve(strict=True)
        git_dir = Path(working["git_dir"]).resolve(strict=True) if working["git_dir"] else git_common_dir
        working_manifest = {
            "workingCopyId": working["working_copy_id"], "resourceVersion": int(working["resource_version"]),
            "kind": working["kind"], "purpose": working["purpose"], "effectiveMode": working["effective_mode"],
            "hostPath": str(working_path), "gitCommonDir": str(git_common_dir), "gitDir": str(git_dir),
            "branchRef": working["branch_ref"], "headOid": working["expected_head_oid"], "treeOid": working["expected_tree_oid"],
            "dirtyFingerprint": None, "writerEpoch": writer_epoch if writer_epoch is not None else int(working["writer_epoch"]),
        }
    operation_id = new_id("op")
    manifest = {
        "schemaVersion": 1, "runId": run_id, "operationId": operation_id, "taskId": task_id,
        "conversationId": conversation_id, "piSessionId": conversation["pi_session_id"], "parentRunId": parent_run_id,
        "project": {"projectId": project_id, "resourceVersion": int(project_policy["resource_version"]), "objectFormat": project_policy["object_format"], "trustMode": project_policy["trust_mode"], "policyHash": project_policy["policy_hash"]},
        "workingCopy": working_manifest, "authority": authority,
        "runtime": {"runtimeSpecVersion": 1, "runtimeSpecHash": runtime_spec_hash, "executionTarget": execution_target, "platform": platform, "imageReference": image_reference, "imageConfigId": image_config_id, "registryDigest": registry_digest, "controllerBuildId": build["build_id"], "piVersion": build["pi_version"]},
        "owner": {"uid": os.getuid(), "gid": os.getgid(), "pid": owner_pid or os.getpid(), "processStartIdentity": process_start_identity(owner_pid or os.getpid())},
        "capabilityHash": capability_hash, "attestationNonce": secrets.token_urlsafe(24), "createdAt": now, "expiresAt": None,
    }
    manifest["manifestDigest"] = _digest(manifest)
    path = Path(store.state_root) / "runs" / run_id / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o600)
    with store.transaction():
        store.conn.execute("UPDATE runs SET manifest_path=?,observed_state='ready',updated_at=? WHERE run_id=?", (str(path), utc_now(), run_id))
        append_event_in_transaction(store.conn, event_kind="run.prepared", resource_type="run", resource_id=run_id, resource_version=1, payload={"projectId": project_id, "conversationId": conversation_id, "workingCopyId": working_copy_id, "authority": authority, "manifestDigest": manifest["manifestDigest"]})
    run = dict(store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())
    return PreparedRun(run, manifest, path, {
        "PI_RUNTIME_MANIFEST": str(path), "PI_SYSTEM_RUN_ID": run_id, "PI_RUNTIME_CAPABILITY": raw_secret.decode("utf-8", errors="ignore"),
        "PI_SYSTEM_PROJECT_ID": project_id, "PI_SYSTEM_CONVERSATION_ID": conversation_id,
        **({"PI_SYSTEM_WORKING_COPY_ID": working_copy_id} if working_copy_id else {}),
        **({"PI_SYSTEM_WRITER_GENERATION": str(writer_epoch)} if writer_epoch is not None else {}),
    }, lock)


def attest_run(store: Any, *, run_id: str, manifest_digest: str, observed: Mapping[str, Any] | None = None) -> dict[str, Any]:
    validate_id(run_id, prefix="run")
    row = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None or not row["manifest_path"]:
        raise LaunchError("run manifest is unavailable")
    manifest = json.loads(Path(row["manifest_path"]).read_text(encoding="utf-8"))
    if manifest.get("manifestDigest") != manifest_digest:
        raise LaunchError("run manifest digest mismatch")
    observed = dict(observed or {})
    expected_values = {
        "runId": manifest.get("runId"), "projectId": (manifest.get("project") or {}).get("projectId"),
        "workingCopyId": (manifest.get("workingCopy") or {}).get("workingCopyId") if manifest.get("workingCopy") else None,
    }
    for key, expected in expected_values.items():
        if key in observed and observed[key] != expected:
            raise LaunchError(f"runtime attestation mismatch: {key}")
    with store.transaction():
        store.conn.execute("UPDATE runs SET observed_state='running',started_at=COALESCE(started_at,?),updated_at=? WHERE run_id=?", (utc_now(), utc_now(), run_id))
    return {"runId": run_id, "manifestDigest": manifest_digest, "state": "running", "observed": observed}


def start_run(store: Any, *, run_id: str, command: list[str], capability_secret: str | bytes | None = None) -> dict[str, Any]:
    """Create and start the exact coding container for a prepared writer run."""

    validate_id(run_id, prefix="run")
    if not isinstance(command, list) or not command or any(not isinstance(item, str) or not item or "\x00" in item for item in command):
        raise LaunchError("coding run command is invalid")
    row = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None or row["authority"] != "writer" or not row["manifest_path"]:
        raise LaunchError("only prepared writer runs can start a coding container")
    manifest = json.loads(Path(row["manifest_path"]).read_text(encoding="utf-8"))
    runtime = dict(manifest["runtime"])
    working = manifest.get("workingCopy")
    if not working:
        raise LaunchError("coding run has no assigned working copy")
    from .docker_runtime import DockerRuntimeError, create_coding_container, start_container
    container_name = "pi-runtime-" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
    raw_capability = capability_secret.encode("utf-8") if isinstance(capability_secret, str) else bytes(capability_secret) if capability_secret is not None else None
    if raw_capability is not None and "sha256:" + hashlib.sha256(raw_capability).hexdigest() != manifest["capabilityHash"]:
        raise LaunchError("coding runtime capability does not match the prepared manifest")
    runtime.update({"buildId": manifest["runtime"]["controllerBuildId"], "piVersion": manifest["runtime"]["piVersion"], "authority": manifest["authority"], "runId": run_id, "manifestDigest": manifest["manifestDigest"], "manifestPath": str(row["manifest_path"]), "capabilityHash": manifest["capabilityHash"], "runtimeCapability": raw_capability.decode("utf-8", errors="strict") if raw_capability is not None else None, "conversationId": manifest["conversationId"], "projectId": manifest["project"]["projectId"], "workingCopyId": working["workingCopyId"], "uid": manifest["owner"]["uid"], "gid": manifest["owner"]["gid"], "labels": {"pi.control.managed": "true", "pi.control.run-id": run_id, "pi.control.manifest-digest": manifest["manifestDigest"], "pi.control.project-id": manifest["project"]["projectId"], "pi.control.policy-hash": manifest["project"]["policyHash"], "pi.control.runtime-spec-hash": runtime["runtimeSpecHash"], "pi.control.controller-build-id": manifest["runtime"]["controllerBuildId"], "pi.control.working-copy-id": working["workingCopyId"], "pi.control.writer-epoch": str(working["writerEpoch"] or 0)}})
    try:
        created = create_coding_container(image_reference=str(runtime["imageReference"]), working_copy_path=str(working["hostPath"]), container_name=container_name, runtime_spec=runtime, command=command)
        start_container(created["containerId"])
        observation = __import__("scripts.pi_control.docker_runtime", fromlist=["inspect_container"]).inspect_container(created["containerId"])
        if not observation.get("running"):
            raise LaunchError("coding container did not become running")
    except DockerRuntimeError as error:
        with store.transaction():
            store.conn.execute("UPDATE runs SET observed_state='needs_attention',error_code='CP_RUNTIME_ATTESTATION',error_detail=?,updated_at=? WHERE run_id=?", (str(error)[:1024], utc_now(), run_id))
        raise LaunchError(str(error)) from error
    with store.transaction():
        store.conn.execute("UPDATE runs SET container_id=?,container_observation_json=?,observed_state='running',started_at=COALESCE(started_at,?),updated_at=? WHERE run_id=?", (created["containerId"], canonical_json(observation), utc_now(), utc_now(), run_id))
        append_event_in_transaction(store.conn, event_kind="run.container-attested", resource_type="run", resource_id=run_id, resource_version=int(row["resource_version"]), payload={"runId": run_id, "containerId": created["containerId"], "imageConfigId": created["image"]["imageConfigId"], "platform": created["image"]["platform"]})
    return dict(store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())


def stop_run(store: Any, *, run_id: str, reason: str = "stopped") -> dict[str, Any]:
    validate_id(run_id, prefix="run")
    with store.transaction():
        row = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise LaunchError("run not found")
        now = utc_now()
        observed = "failed" if reason.startswith("process-failed") else "stopped"
        container_id = row["container_id"]
        if container_id:
            try:
                from .docker_runtime import inspect_container, stop_container
                if inspect_container(str(container_id)).get("running"):
                    stop_container(str(container_id))
            except Exception:
                observed = "needs_attention"
        store.conn.execute("UPDATE runs SET desired_state='stopped',observed_state=?,error_code=?,error_detail=?,ended_at=?,updated_at=? WHERE run_id=?", (observed, "PROCESS_EXIT_NONZERO" if observed == "failed" else ("CP_RUNTIME_STOP" if observed == "needs_attention" else None), reason[:1024], now, now, run_id))
        if row["working_copy_id"] is not None and row["authority"] == "writer" and observed in {"stopped", "failed"}:
            store.conn.execute("UPDATE working_copies SET active_writer_run_id=NULL,updated_at=?,resource_version=resource_version+1 WHERE working_copy_id=? AND active_writer_run_id=?", (now, row["working_copy_id"], run_id))
        return dict(store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())


__all__ = ["LaunchError", "PreparedRun", "attest_run", "ensure_installed_build", "prepare_run", "start_run", "stop_run"]
