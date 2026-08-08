"""Lifetime kernel locks, writer epochs, and mutation fencing for Phase 4."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import secrets
from typing import Any, Mapping

from .errors import LockBusyError, NotFoundError, WriterStaleError, WriterUnknownError
from .locks import secure_lock_directory
from .models import new_id, parse_canonical_json, utc_now, validate_id
from .process_adapter import observe_process
from .run_manifest import capability_hash


class LeaseError(RuntimeError):
    pass


class LockOrderContext:
    """Enforce lifecycle-before-writer acquisition and reverse release."""

    _RANKS = {"lifecycle": 10, "writer": 20}

    def __init__(self) -> None:
        self._held: list[WriterLease] = []

    def acquire(self, lease: "WriterLease") -> "WriterLease":
        rank = self._RANKS[lease.kind]
        if self._held and rank <= self._RANKS[self._held[-1].kind]:
            raise LeaseError("lease lock order is invalid or reentrant")
        lease.acquire()
        self._held.append(lease)
        return lease

    def release_all(self) -> None:
        while self._held:
            self._held.pop().release()


class WriterLease:
    def __init__(self, state_root: os.PathLike[str] | str, working_copy_id: str, *, kind: str = "writer", failpoint: Any | None = None):
        validate_id(working_copy_id, prefix="wc")
        if kind not in {"writer", "lifecycle"}:
            raise ValueError("lease kind is invalid")
        self.state_root = Path(state_root)
        self.working_copy_id = working_copy_id
        self.kind = kind
        self.parent = self.state_root / "locks"
        self.path = self.parent / f"{working_copy_id}.{kind}.lock"
        self.failpoint = failpoint
        self._handle = None
        self._directory_fd: int | None = None

    def _hit(self, name: str) -> None:
        if self.failpoint is not None:
            self.failpoint.hit(name, {"working_copy_id": self.working_copy_id, "lease": self.kind})

    def acquire(self) -> "WriterLease":
        if self._handle is not None:
            raise LeaseError("lease is not reentrant")
        self._hit("lock.acquire.before")
        try:
            directory_fd = secure_lock_directory(self.parent, create=True)
            if directory_fd is None:
                raise LeaseError("lease directory is unavailable")
            self._directory_fd = directory_fd
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path.name, flags, 0o600, dir_fd=directory_fd)
            handle = os.fdopen(fd, "a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as error:
                handle.close()
                raise LockBusyError("working-copy lease is held", detail={"working_copy_id": self.working_copy_id, "lease": self.kind}) from error
            os.fchmod(handle.fileno(), 0o600)
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()} {utc_now()}\n")
            handle.flush()
            self._handle = handle
        except LockBusyError:
            if self._directory_fd is not None:
                os.close(self._directory_fd)
                self._directory_fd = None
            raise
        except OSError as error:
            if self._directory_fd is not None:
                os.close(self._directory_fd)
                self._directory_fd = None
            raise LeaseError("working-copy lease could not be opened") from error
        self._hit("lock.acquire.after")
        return self

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
            if self._directory_fd is not None:
                os.close(self._directory_fd)
                self._directory_fd = None

    def __enter__(self) -> "WriterLease":
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


@dataclass
class WriterRunHandle:
    store: Any
    run: Mapping[str, Any]
    working_copy_id: str
    capability_secret: str
    lifecycle_lease: WriterLease | None
    writer_lease: WriterLease | None
    lock_context: LockOrderContext | None = None
    operation_id: str | None = None
    replayed: bool = False
    closed: bool = False

    @property
    def run_id(self) -> str:
        return str(self.run["run_id"])

    @property
    def writer_epoch(self) -> int:
        return int(self.run["writer_epoch"])

    def fence(self, *, expected_resource_version: int, operation_kind: str) -> dict[str, Any]:
        return check_run_authority(
            self.store,
            self.run_id,
            working_copy_id=self.working_copy_id,
            writer_epoch=self.writer_epoch,
            expected_resource_version=expected_resource_version,
            operation_kind=operation_kind,
            capability_secret=self.capability_secret,
        )

    def close(self, *, state: str = "stopped", error_code: str | None = None, error_detail: str | None = None) -> None:
        if self.closed:
            return
        if self.replayed:
            self.closed = True
            return
        try:
            self.store.terminalize_run(
                self.run_id,
                state=state,
                error_code=error_code,
                error_detail=error_detail,
                operation_id=self.operation_id,
            )
        finally:
            if self.lock_context is not None:
                self.lock_context.release_all()
            else:
                if self.writer_lease is not None:
                    self.writer_lease.release()
                if self.lifecycle_lease is not None:
                    self.lifecycle_lease.release()
            self.closed = True

    def __enter__(self) -> "WriterRunHandle":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close(state="failed" if exc_type is not None else "stopped")


def _active_claim(store: Any, working_copy_id: str) -> Mapping[str, Any] | None:
    return store.conn.execute(
        "SELECT wc.active_writer_run_id, r.owner_pid, r.owner_start_identity, r.observed_state FROM working_copies wc LEFT JOIN runs r ON r.run_id=wc.active_writer_run_id WHERE wc.working_copy_id=?",
        (working_copy_id,),
    ).fetchone()


def create_writer_run(
    store: Any,
    *,
    conversation_id: str,
    working_copy_id: str,
    build_id: str,
    runtime_spec_hash: str,
    project_id: str | None = None,
    expected_head_oid: str | None = None,
    expected_tree_oid: str | None = None,
    dirty_fingerprint: str | None = None,
    owner_pid: int | None = None,
    owner_start_identity: str | None = None,
    idempotency_key: str | None = None,
    expected_working_copy_version: int | None = None,
    expected_writer_epoch: int | None = None,
    capability_secret: str | None = None,
    failpoint: Any | None = None,
) -> WriterRunHandle:
    conversation = store.conn.execute("SELECT role,project_id,working_copy_id FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    if conversation is None:
        raise NotFoundError("conversation was not found", detail={"conversation_id": conversation_id})
    if conversation["role"] in {"secretary", "host", "review"}:
        raise WriterStaleError("conversation role cannot own a writable run", detail={"role": conversation["role"]})
    if conversation["working_copy_id"] != working_copy_id:
        raise WriterStaleError("writer must use the conversation's explicit working copy")
    if project_id is not None and project_id != conversation["project_id"]:
        raise WriterStaleError("writer project does not match conversation")
    claim = _active_claim(store, working_copy_id)
    if claim is None:
        raise NotFoundError("working copy was not found", detail={"working_copy_id": working_copy_id})
    if idempotency_key is not None and (expected_working_copy_version is None or expected_writer_epoch is None):
        raise ValueError("explicit writer idempotency requires an expected working-copy version and writer epoch")

    operation_key = idempotency_key or "writer:" + working_copy_id + ":" + secrets.token_hex(16)
    request = {
        "conversation_id": conversation_id,
        "working_copy_id": working_copy_id,
        "project_id": project_id,
        "build_id": build_id,
        "runtime_spec_hash": runtime_spec_hash,
        "expected_head_oid": expected_head_oid,
        "expected_tree_oid": expected_tree_oid,
        "dirty_fingerprint": dirty_fingerprint,
        "owner_pid": owner_pid,
        "owner_start_identity": owner_start_identity,
        "expected_working_copy_version": expected_working_copy_version,
        "expected_writer_epoch": expected_writer_epoch,
    }
    operation = store.create_operation(
        idempotency_key=operation_key,
        kind="run.create",
        resource_type="working_copy",
        resource_id=working_copy_id,
        actor_type="controller",
        request=request,
    )
    if operation.result_json is not None:
        result = parse_canonical_json(operation.result_json)
        existing_run_id = result.get("runId") if isinstance(result, dict) else None
        if isinstance(existing_run_id, str):
            existing_run = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (existing_run_id,)).fetchone()
            if existing_run is None:
                raise WriterUnknownError("writer operation has no durable run result", detail={"operation_id": operation.operation_id, "run_id": existing_run_id})
            if capability_secret is not None and capability_hash(capability_secret) != existing_run["capability_hash"]:
                raise WriterStaleError("writer capability is stale")
            if existing_run["observed_state"] in {"stopped", "failed", "lost"}:
                return WriterRunHandle(store, existing_run, working_copy_id, capability_secret or "", None, None, None, operation.operation_id, True)
            observation = observe_process(int(existing_run["owner_pid"]) if existing_run["owner_pid"] is not None else -1, expected_start_identity=existing_run["owner_start_identity"])
            raise WriterUnknownError(
                "writer operation already owns an active run; reconcile the existing owner",
                detail={"working_copy_id": working_copy_id, "run_id": existing_run_id, "process_state": observation.state},
            )
    if claim["active_writer_run_id"] is not None:
        observation = observe_process(int(claim["owner_pid"]) if claim["owner_pid"] is not None else -1, expected_start_identity=claim["owner_start_identity"])
        raise WriterUnknownError(
            "existing writer access must be explicitly resolved before a new writer",
            detail={"working_copy_id": working_copy_id, "run_id": claim["active_writer_run_id"], "process_state": observation.state},
        )
    lifecycle = WriterLease(store.state_root, working_copy_id, kind="lifecycle", failpoint=failpoint)
    writer = WriterLease(store.state_root, working_copy_id, kind="writer", failpoint=failpoint)
    secret = secrets.token_urlsafe(48)
    lock_context = LockOrderContext()
    try:
        lock_context.acquire(lifecycle)
        lock_context.acquire(writer)
        current = store.conn.execute("SELECT project_id,resource_version,writer_epoch,active_writer_run_id FROM working_copies WHERE working_copy_id=?", (working_copy_id,)).fetchone()
        if current is None:
            raise NotFoundError("working copy was not found", detail={"working_copy_id": working_copy_id})
        if current["active_writer_run_id"] is not None:
            raise WriterUnknownError("existing writer access must be explicitly resolved before a new writer", detail={"working_copy_id": working_copy_id, "run_id": current["active_writer_run_id"]})
        effective_version = int(current["resource_version"])
        effective_epoch = int(current["writer_epoch"]) + 1
        if expected_working_copy_version is not None and int(expected_working_copy_version) != effective_version:
            raise WriterStaleError("working-copy version is stale", detail={"working_copy_id": working_copy_id, "expected": expected_working_copy_version, "actual": effective_version})
        if expected_writer_epoch is not None and int(expected_writer_epoch) != effective_epoch:
            raise WriterStaleError("writer epoch is stale", detail={"working_copy_id": working_copy_id, "expected": expected_writer_epoch, "actual": effective_epoch})
        run = store.create_run(
            run_id=new_id("run"), conversation_id=conversation_id, authority="writer",
            runtime_spec_hash=runtime_spec_hash, build_id=build_id, project_id=project_id if project_id is not None else current[0],
            working_copy_id=working_copy_id, expected_working_copy_version=effective_version,
            expected_head_oid=expected_head_oid, expected_tree_oid=expected_tree_oid,
            dirty_fingerprint=dirty_fingerprint, writer_epoch=effective_epoch,
            owner_pid=owner_pid if owner_pid is not None else os.getpid(),
            owner_start_identity=owner_start_identity or __import__("scripts.pi_control.process_adapter", fromlist=["process_start_identity"]).process_start_identity(os.getpid()),
            capability_hash=capability_hash(secret), operation_id=operation.operation_id,
        )
        return WriterRunHandle(store, run, working_copy_id, secret, lifecycle, writer, lock_context, operation.operation_id)
    except BaseException:
        lock_context.release_all()
        raise


def check_run_authority(
    store: Any,
    run_id: str,
    *,
    working_copy_id: str,
    writer_epoch: int,
    expected_resource_version: int,
    operation_kind: str,
    capability_secret: str | None = None,
) -> dict[str, Any]:
    if not isinstance(operation_kind, str) or not operation_kind or len(operation_kind) > 128 or "\x00" in operation_kind:
        raise ValueError("operation kind is invalid")
    row = store.conn.execute(
        "SELECT r.*, wc.active_writer_run_id, wc.writer_epoch AS current_writer_epoch, wc.resource_version AS current_resource_version FROM runs r JOIN working_copies wc ON wc.working_copy_id=r.working_copy_id WHERE r.run_id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("run authority was not found", detail={"run_id": run_id})
    if row["working_copy_id"] != working_copy_id or row["authority"] != "writer":
        raise WriterStaleError("run is not the requested writer")
    if row["observed_state"] in {"stopping", "stopped", "failed", "lost", "needs_attention"} or row["desired_state"] != "running":
        raise WriterStaleError("run is not accepting mutations")
    if row["active_writer_run_id"] != run_id or int(row["current_writer_epoch"]) != int(writer_epoch):
        raise WriterStaleError("writer epoch or claim is stale")
    if int(row["current_resource_version"]) != int(expected_resource_version):
        raise WriterStaleError("working-copy resource version is stale")
    if capability_secret is not None and capability_hash(capability_secret) != row["capability_hash"]:
        raise WriterStaleError("writer capability is stale")
    return {
        "run_id": run_id, "working_copy_id": working_copy_id, "writer_epoch": int(writer_epoch),
        "resource_version": int(expected_resource_version), "operation_kind": operation_kind,
        "checked_at": utc_now(), "provenance": "writer-fence-v1",
    }


__all__ = ["LeaseError", "LockOrderContext", "WriterLease", "WriterRunHandle", "check_run_authority", "create_writer_run"]
