"""Secure SQLite store for the Phase 2 host-local controller."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import quote

from .errors import (
    ConstraintError,
    ControlPlaneError,
    DatabaseCorruptError,
    IdempotencyConflictError,
    LockBusyError,
    NotFoundError,
    ResourceStaleError,
    SchemaNewerError,
    UnsafeDatabaseError,
    WriterStaleError,
    error_from_exception,
)
from .models import (
    CASResult,
    ChildSource,
    ConsumerCursor,
    EventRecord,
    OperationRecord,
    SchemaStatus,
    canonical_json,
    json_digest,
    parse_canonical_json,
    validate_child_source,
    new_id,
    row_to_dict,
    utc_now,
)
from .schema import SCHEMA_VERSION, apply_schema, probe_capabilities, schema_digest
from .migrations import v001_initial, v002_child_source, v003_artifacts, v004_revision_immutability, v005_review_authority, v006_receipt_operation_immutability, v007_completion_resources

_STATE_DIR_MODE = 0o700
_DB_MODE = 0o600
_NETWORK_FILESYSTEMS = frozenset({
    "nfs", "nfs4", "cifs", "smbfs", "smb2", "9p", "afs", "ceph",
    "glusterfs", "gfs2", "ocfs2", "lustre", "gpfs", "sshfs",
})
_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CAS_ID_COLUMNS = {
    "projects": "project_id",
    "working_copies": "working_copy_id",
    "conversations": "conversation_id",
    "runs": "run_id",
    "changes": "change_id",
    "integration_attempts": "integration_id",
    "migration_runs": "migration_id",
    "workstreams": "workstream_id",
    "presentation_assignments": "presentation_assignment_id",
    "project_activations": "project_id",
}
_ALLOWED_CAS_TABLES = frozenset(_CAS_ID_COLUMNS)


def _default_state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base).expanduser() / "pi-control"


def _owner_is_current(path: Path) -> bool:
    return hasattr(os, "geteuid") and path.stat().st_uid == os.geteuid()


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _check_directory(path: Path, *, create: bool) -> None:
    info = _lstat(path)
    if info is None:
        if not create:
            raise UnsafeDatabaseError("state directory does not exist", detail={"path": str(path)})
        try:
            path.mkdir(mode=_STATE_DIR_MODE)
        except FileExistsError:
            info = _lstat(path)
        else:
            info = _lstat(path)
    if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeDatabaseError("state directory is not a regular directory", detail={"path": str(path)})
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise UnsafeDatabaseError("state directory is not user-owned", detail={"path": str(path)})
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise UnsafeDatabaseError("state directory is accessible to group or other", detail={"path": str(path)})
    if stat.S_IMODE(info.st_mode) != _STATE_DIR_MODE:
        raise UnsafeDatabaseError("state directory must be mode 0700", detail={"path": str(path)})


def _check_database_path(path: Path, *, allow_missing: bool = True) -> None:
    info = _lstat(path)
    if info is None:
        if not allow_missing:
            raise UnsafeDatabaseError("database does not exist", detail={"path": str(path)})
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise UnsafeDatabaseError("database must be a regular non-symlink file", detail={"path": str(path)})
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise UnsafeDatabaseError("database is not user-owned", detail={"path": str(path)})
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise UnsafeDatabaseError("database is accessible to group or other", detail={"path": str(path)})


def _check_sidecar(path: Path) -> None:
    info = _lstat(path)
    if info is None:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise UnsafeDatabaseError("SQLite sidecar is unsafe", detail={"path": str(path)})
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise UnsafeDatabaseError("SQLite sidecar is not user-owned", detail={"path": str(path)})
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise UnsafeDatabaseError("SQLite sidecar is accessible to group or other", detail={"path": str(path)})


def _secure_existing_parent(path: Path) -> None:
    """Require a real directory parent without imposing state-root mode on it.

    XDG_STATE_HOME and /tmp are often intentionally 0755/1777.  The sensitive
    controller directory below them is the boundary that must be user-owned
    and 0700; rejecting a shared parent would make secure disposable fixtures
    impossible without improving the database boundary.
    """
    parent = path.parent
    if not parent.exists():
        old = os.umask(0o077)
        try:
            parent.mkdir(parents=True, mode=_STATE_DIR_MODE, exist_ok=True)
        finally:
            os.umask(old)
    info = _lstat(parent)
    if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeDatabaseError("state parent is not a real directory", detail={"path": str(parent)})


def _decode_mountinfo_path(value: str) -> str:
    return value.replace("\\134", "\\").replace("\\011", "\\t").replace("\\040", " ")


def filesystem_type(path: Path) -> str | None:
    """Return the Linux mount filesystem type, or None when unavailable."""

    if sys.platform != "linux":
        return None
    candidate = path.absolute()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        return None
    best_mount = ""
    best_type: str | None = None
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        post = after.split()
        if len(fields) < 5 or not post:
            continue
        mountpoint = _decode_mountinfo_path(fields[4])
        if str(candidate) == mountpoint or str(candidate).startswith(mountpoint.rstrip("/") + "/"):
            if len(mountpoint) > len(best_mount):
                best_mount = mountpoint
                best_type = post[0].lower()
    return best_type


def assert_local_filesystem(path: Path) -> None:
    fs_type = filesystem_type(path)
    if fs_type is not None and (fs_type in _NETWORK_FILESYSTEMS or fs_type.startswith("nfs") or fs_type.startswith("fuse.ssh")):
        raise UnsafeDatabaseError(
            "controller state must be on a local filesystem",
            detail={"filesystem": fs_type, "path": str(path)},
        )


def _safe_build_id(value: str | None) -> str:
    if value is None:
        return "controller-dev-" + schema_digest()[:16]
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise ValueError("invalid controller build ID")
    return value


def _append_lifecycle_event(
    connection: sqlite3.Connection,
    *,
    event_kind: str,
    resource_type: str,
    resource_id: str,
    resource_version: int,
    payload: Mapping[str, Any],
    operation_id: str | None = None,
    failpoint: Any | None = None,
) -> Any:
    """Append one lifecycle event while remaining inside the caller's transaction."""

    if failpoint is not None:
        failpoint.hit("event.commit.before", {"event_kind": event_kind, "resource_id": resource_id})
    from .events import append_event_in_transaction
    event = append_event_in_transaction(
        connection,
        event_kind=event_kind,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_version=resource_version,
        operation_id=operation_id,
        payload=payload,
    )
    if failpoint is not None:
        failpoint.hit("event.commit.after", {"event_kind": event_kind, "resource_id": resource_id})
    return event


class ControllerStore:
    """One per-command connection and schema owner; there is no daemon."""

    def __init__(
        self,
        state_root: os.PathLike[str] | str | None = None,
        *,
        db_path: os.PathLike[str] | str | None = None,
        controller_build_id: str | None = None,
        initialize: bool = True,
        read_only: bool = False,
    ) -> None:
        self.state_root = Path(state_root).expanduser() if state_root is not None else _default_state_root()
        if db_path is None:
            self.db_path = self.state_root / "control.db"
        else:
            raw_db = Path(db_path).expanduser()
            self.db_path = self.state_root / raw_db if state_root is not None and not raw_db.is_absolute() else raw_db
        if db_path is not None and state_root is None:
            self.state_root = self.db_path.parent
        self.controller_build_id = _safe_build_id(controller_build_id)
        self.initialize = initialize
        self.read_only = read_only
        self.connection: sqlite3.Connection | None = None
        self._opened = False

    def __enter__(self) -> "ControllerStore":
        self.open()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("ControllerStore is not open")
        return self.connection

    def open(self) -> "ControllerStore":
        if self._opened:
            return self
        # Capability and local-filesystem preflight must happen before creating
        # a state directory or database.  The SQLite probe uses memory only.
        probe_capabilities()
        assert_local_filesystem(self.state_root)
        assert_local_filesystem(self.db_path.parent)
        if self.read_only:
            return self._open_read_only()
        _secure_existing_parent(self.state_root)
        _check_directory(self.state_root, create=True)
        # A caller may choose a disposable state root, but may not redirect
        # SQLite/WAL/SHM files into an unrelated or shared directory.  The
        # state root is the single security boundary for every controller file.
        state_absolute = self.state_root.absolute()
        db_absolute = self.db_path.absolute()
        if db_absolute.parent != state_absolute:
            raise UnsafeDatabaseError(
                "database must be directly beneath the secure state root",
                detail={"state_root": str(state_absolute), "database": str(db_absolute)},
            )
        _check_database_path(self.db_path)
        _check_sidecar(Path(str(self.db_path) + "-wal"))
        _check_sidecar(Path(str(self.db_path) + "-shm"))
        old_umask = os.umask(0o077)
        try:
            try:
                connection = sqlite3.connect(
                    str(self.db_path),
                    timeout=5.0,
                    isolation_level=None,
                    check_same_thread=True,
                )
            except sqlite3.Error as error:
                raise DatabaseCorruptError("database could not be opened") from error
        finally:
            os.umask(old_umask)
        self.connection = connection
        self.conn.row_factory = sqlite3.Row
        try:
            self._configure_connection()
            _check_database_path(self.db_path, allow_missing=False)
            os.chmod(self.db_path, _DB_MODE)
            self._initialize_schema() if self.initialize else None
            _check_sidecar(Path(str(self.db_path) + "-wal"))
            _check_sidecar(Path(str(self.db_path) + "-shm"))
        except BaseException:
            self.close()
            raise
        self._opened = True
        return self

    def _validate_existing_boundary(self) -> None:
        _check_directory(self.state_root, create=False)
        state_absolute = self.state_root.absolute()
        db_absolute = self.db_path.absolute()
        if db_absolute.parent != state_absolute:
            raise UnsafeDatabaseError(
                "database must be directly beneath the secure state root",
                detail={"state_root": str(state_absolute), "database": str(db_absolute)},
            )
        _check_database_path(self.db_path, allow_missing=False)
        wal = Path(str(self.db_path) + "-wal")
        shm = Path(str(self.db_path) + "-shm")
        _check_sidecar(wal)
        _check_sidecar(shm)
        if wal.exists() != shm.exists():
            raise UnsafeDatabaseError("SQLite WAL sidecars are incomplete")

    def _open_read_only(self) -> "ControllerStore":
        """Open an existing controller DB without schema/WAL/sidecar writes."""

        self._validate_existing_boundary()
        db_absolute = self.db_path.absolute()
        wal = Path(str(self.db_path) + "-wal")
        shm = Path(str(self.db_path) + "-shm")
        had_sidecars = wal.exists() and shm.exists()
        query = "mode=ro" if had_sidecars else "mode=ro&immutable=1"
        uri = "file:" + quote(str(db_absolute), safe="/") + "?" + query
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None, check_same_thread=True)
        except sqlite3.Error as error:
            raise DatabaseCorruptError("database could not be opened read-only") from error
        self.connection = connection
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA foreign_keys = ON")
            if self.conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise UnsafeDatabaseError("SQLite foreign keys could not be enabled read-only")
            self.conn.execute("PRAGMA busy_timeout = 5000")
            user_version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
            if user_version <= 0:
                raise DatabaseCorruptError("controller schema is missing")
            if user_version > SCHEMA_VERSION:
                raise SchemaNewerError("database schema is newer than this controller", detail={"user_version": user_version, "supported": SCHEMA_VERSION})
            self._verify_existing_schema(user_version)
            if not had_sidecars and (wal.exists() or shm.exists()):
                raise UnsafeDatabaseError("read-only inspection created SQLite sidecars")
        except BaseException:
            self.close()
            raise
        self._opened = True
        return self

    def close(self) -> None:
        connection, self.connection = self.connection, None
        self._opened = False
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def _configure_connection(self) -> None:
        if self.read_only:
            raise UnsafeDatabaseError("read-only store cannot use writable connection configuration")
        try:
            self.conn.execute("PRAGMA foreign_keys = ON")
            if self.conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise UnsafeDatabaseError("SQLite foreign keys could not be enabled")
            journal = str(self.conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
            if journal != "wal":
                raise UnsafeDatabaseError("SQLite WAL mode could not be enabled", detail={"journal_mode": journal})
            self.conn.execute("PRAGMA synchronous = FULL")
            if int(self.conn.execute("PRAGMA synchronous").fetchone()[0]) != 2:
                raise UnsafeDatabaseError("SQLite FULL synchronous mode could not be enabled")
            self.conn.execute("PRAGMA busy_timeout = 5000")
        except (sqlite3.Error, UnsafeDatabaseError) as error:
            if isinstance(error, UnsafeDatabaseError):
                raise
            raise DatabaseCorruptError("SQLite connection configuration failed") from error

    def _initialize_schema(self) -> None:
        try:
            user_version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.Error as error:
            raise DatabaseCorruptError("database schema is unreadable") from error
        if user_version > SCHEMA_VERSION:
            raise SchemaNewerError("database schema is newer than this controller", detail={"user_version": user_version, "supported": SCHEMA_VERSION})
        try:
            with self.transaction():
                if user_version == 0:
                    # A non-empty unrelated SQLite database is not silently
                    # repurposed as controller state.
                    existing = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
                    if existing:
                        raise DatabaseCorruptError("database has no controller schema")
                    apply_schema(self.conn)
                    now = utc_now()
                    self.conn.execute(
                        "INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES(?,?,?,?)",
                        (v001_initial.VERSION, v001_initial.NAME, v001_initial.SOURCE_SHA256, now),
                    )
                    self.conn.execute(
                        "INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES(?,?,?,?)",
                        (v002_child_source.VERSION, v002_child_source.NAME, v002_child_source.SOURCE_SHA256, now),
                    )
                    self.conn.execute(
                        "INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES(?,?,?,?)",
                        (v003_artifacts.VERSION, v003_artifacts.NAME, v003_artifacts.SOURCE_SHA256, now),
                    )
                    self.conn.execute(
                        "INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES(?,?,?,?)",
                        (v004_revision_immutability.VERSION, v004_revision_immutability.NAME, v004_revision_immutability.SOURCE_SHA256, now),
                    )
                    self.conn.execute(
                        "INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES(?,?,?,?)",
                        (v005_review_authority.VERSION, v005_review_authority.NAME, v005_review_authority.SOURCE_SHA256, now),
                    )
                    self.conn.execute(
                        "INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES(?,?,?,?)",
                        (v006_receipt_operation_immutability.VERSION, v006_receipt_operation_immutability.NAME, v006_receipt_operation_immutability.SOURCE_SHA256, now),
                    )
                    self.conn.execute(
                        "INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES(?,?,?,?)",
                        (v007_completion_resources.VERSION, v007_completion_resources.NAME, v007_completion_resources.SOURCE_SHA256, now),
                    )
                    self.conn.execute(
                        "INSERT INTO control_meta(singleton,schema_version,controller_build_id,created_at,updated_at) VALUES(1,?,?,?,?)",
                        (SCHEMA_VERSION, self.controller_build_id, now, now),
                    )
                    self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                elif user_version in {1, 2, 3, 4, 5, 6}:
                    legacy = self.conn.execute("SELECT version,name,source_sha256 FROM schema_migrations WHERE version=1").fetchone()
                    if legacy is None or tuple(legacy)[:2] != (v001_initial.VERSION, v001_initial.NAME) or legacy[2] != v001_initial.SOURCE_SHA256:
                        raise DatabaseCorruptError("legacy schema migration checksum does not match controller source")
                    current = user_version
                    if current < v002_child_source.VERSION:
                        v002_child_source.apply(self.conn)
                        now = utc_now()
                        self.conn.execute(
                            "INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES(?,?,?,?)",
                            (v002_child_source.VERSION, v002_child_source.NAME, v002_child_source.SOURCE_SHA256, now),
                        )
                        self.conn.execute("UPDATE control_meta SET schema_version=?,updated_at=? WHERE singleton=1", (v002_child_source.VERSION, now))
                        self.conn.execute(f"PRAGMA user_version = {v002_child_source.VERSION}")
                        current = v002_child_source.VERSION
                    else:
                        row = self.conn.execute("SELECT name,source_sha256 FROM schema_migrations WHERE version=?", (v002_child_source.VERSION,)).fetchone()
                        if row is None or tuple(row) != (v002_child_source.NAME, v002_child_source.SOURCE_SHA256):
                            raise DatabaseCorruptError("child-source migration checksum does not match frozen source")
                    if current < v003_artifacts.VERSION:
                        v003_artifacts.apply(self.conn)
                        now = utc_now()
                        self.conn.execute(
                            "INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES(?,?,?,?)",
                            (v003_artifacts.VERSION, v003_artifacts.NAME, v003_artifacts.SOURCE_SHA256, now),
                        )
                        self.conn.execute("UPDATE control_meta SET schema_version=?,updated_at=? WHERE singleton=1", (v003_artifacts.VERSION, now))
                        self.conn.execute(f"PRAGMA user_version = {v003_artifacts.VERSION}")
                        current = v003_artifacts.VERSION
                    else:
                        row = self.conn.execute("SELECT name,source_sha256 FROM schema_migrations WHERE version=?", (v003_artifacts.VERSION,)).fetchone()
                        if row is None or tuple(row) != (v003_artifacts.NAME, v003_artifacts.SOURCE_SHA256):
                            raise DatabaseCorruptError("artifact migration checksum does not match frozen source")
                    if current < v004_revision_immutability.VERSION:
                        v004_revision_immutability.apply(self.conn)
                        now = utc_now()
                        self.conn.execute(
                            "INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES(?,?,?,?)",
                            (v004_revision_immutability.VERSION, v004_revision_immutability.NAME, v004_revision_immutability.SOURCE_SHA256, now),
                        )
                        current = v004_revision_immutability.VERSION
                    else:
                        row = self.conn.execute("SELECT name,source_sha256 FROM schema_migrations WHERE version=?", (v004_revision_immutability.VERSION,)).fetchone()
                        if row is None or tuple(row) != (v004_revision_immutability.NAME, v004_revision_immutability.SOURCE_SHA256):
                            raise DatabaseCorruptError("revision migration checksum does not match frozen source")
                    if current < v005_review_authority.VERSION:
                        v005_review_authority.apply(self.conn)
                        now = utc_now()
                        self.conn.execute(
                            "INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES(?,?,?,?)",
                            (v005_review_authority.VERSION, v005_review_authority.NAME, v005_review_authority.SOURCE_SHA256, now),
                        )
                        current = v005_review_authority.VERSION
                    else:
                        row = self.conn.execute("SELECT name,source_sha256 FROM schema_migrations WHERE version=?", (v005_review_authority.VERSION,)).fetchone()
                        if row is None or tuple(row) != (v005_review_authority.NAME, v005_review_authority.SOURCE_SHA256):
                            raise DatabaseCorruptError("review-authority migration checksum does not match frozen source")
                    if current < v006_receipt_operation_immutability.VERSION:
                        v006_receipt_operation_immutability.apply(self.conn)
                        now = utc_now()
                        self.conn.execute(
                            "INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES(?,?,?,?)",
                            (v006_receipt_operation_immutability.VERSION, v006_receipt_operation_immutability.NAME, v006_receipt_operation_immutability.SOURCE_SHA256, now),
                        )
                    else:
                        row = self.conn.execute("SELECT name,source_sha256 FROM schema_migrations WHERE version=?", (v006_receipt_operation_immutability.VERSION,)).fetchone()
                        if row is None or tuple(row) != (v006_receipt_operation_immutability.NAME, v006_receipt_operation_immutability.SOURCE_SHA256):
                            raise DatabaseCorruptError("receipt-operation migration checksum does not match frozen source")
                    if current < v007_completion_resources.VERSION:
                        v007_completion_resources.apply(self.conn)
                        now = utc_now()
                        self.conn.execute(
                            "INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES(?,?,?,?)",
                            (v007_completion_resources.VERSION, v007_completion_resources.NAME, v007_completion_resources.SOURCE_SHA256, now),
                        )
                    else:
                        row = self.conn.execute("SELECT name,source_sha256 FROM schema_migrations WHERE version=?", (v007_completion_resources.VERSION,)).fetchone()
                        if row is None or tuple(row) != (v007_completion_resources.NAME, v007_completion_resources.SOURCE_SHA256):
                            raise DatabaseCorruptError("completion-resources migration checksum does not match frozen source")
                    self.conn.execute("UPDATE control_meta SET schema_version=?,updated_at=? WHERE singleton=1", (SCHEMA_VERSION, utc_now()))
                    self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                else:
                    self._verify_existing_schema(user_version)
        except ControlPlaneError:
            raise
        except sqlite3.DatabaseError as error:
            raise DatabaseCorruptError("database schema migration failed") from error

    def _verify_existing_schema(self, user_version: int) -> None:
        migration_rows = self.conn.execute("SELECT version,name,source_sha256 FROM schema_migrations ORDER BY version").fetchall()
        if any(int(item[0]) > SCHEMA_VERSION or int(item[0]) > user_version for item in migration_rows):
            raise SchemaNewerError(
                "database contains a migration newer than this controller",
                detail={"user_version": user_version, "supported": SCHEMA_VERSION},
            )
        terminal = self.conn.execute("SELECT version,name,source_sha256 FROM schema_migrations WHERE version=?", (v007_completion_resources.VERSION,)).fetchone()
        if terminal is None or terminal[1] != v007_completion_resources.NAME or terminal[2] != v007_completion_resources.SOURCE_SHA256:
            raise DatabaseCorruptError("completion-resources migration checksum does not match controller source")
        receipt = self.conn.execute("SELECT version,name,source_sha256 FROM schema_migrations WHERE version=?", (v006_receipt_operation_immutability.VERSION,)).fetchone()
        if receipt is None or receipt[1] != v006_receipt_operation_immutability.NAME or receipt[2] != v006_receipt_operation_immutability.SOURCE_SHA256:
            raise DatabaseCorruptError("receipt-operation migration checksum does not match controller source")
        row = self.conn.execute("SELECT version,name,source_sha256 FROM schema_migrations WHERE version=?", (v005_review_authority.VERSION,)).fetchone()
        if row is None or row[1] != v005_review_authority.NAME or row[2] != v005_review_authority.SOURCE_SHA256:
            raise DatabaseCorruptError("review-authority migration checksum does not match controller source")
        revision = self.conn.execute("SELECT version,name,source_sha256 FROM schema_migrations WHERE version=?", (v004_revision_immutability.VERSION,)).fetchone()
        if revision is None or revision[1] != v004_revision_immutability.NAME or revision[2] != v004_revision_immutability.SOURCE_SHA256:
            raise DatabaseCorruptError("revision migration checksum does not match frozen source")
        artifact = self.conn.execute("SELECT version,name,source_sha256 FROM schema_migrations WHERE version=?", (v003_artifacts.VERSION,)).fetchone()
        if artifact is None or artifact[1] != v003_artifacts.NAME or artifact[2] != v003_artifacts.SOURCE_SHA256:
            raise DatabaseCorruptError("artifact migration checksum does not match frozen source")
        child = self.conn.execute("SELECT version,name,source_sha256 FROM schema_migrations WHERE version=?", (v002_child_source.VERSION,)).fetchone()
        if child is None or child[1] != v002_child_source.NAME or child[2] != v002_child_source.SOURCE_SHA256:
            raise DatabaseCorruptError("child-source migration checksum does not match frozen source")
        legacy = self.conn.execute("SELECT version,name,source_sha256 FROM schema_migrations WHERE version=?", (v001_initial.VERSION,)).fetchone()
        if legacy is None or legacy[1] != v001_initial.NAME or legacy[2] != v001_initial.SOURCE_SHA256:
            raise DatabaseCorruptError("initial schema migration checksum does not match frozen source")
        meta = self.conn.execute("SELECT schema_version FROM control_meta WHERE singleton=1").fetchone()
        if meta is None or int(meta[0]) != user_version:
            raise DatabaseCorruptError("controller metadata disagrees with SQLite user_version")
        # A cheap integrity check prevents opening a corrupt database as if it
        # were a valid lifecycle store.
        check = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        if str(check).lower() != "ok":
            raise DatabaseCorruptError("SQLite integrity check failed")

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise UnsafeDatabaseError("read-only controller store cannot mutate state")
        try:
            self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        except sqlite3.OperationalError as error:
            raise error_from_exception(error) from error
        try:
            yield self.conn
        except BaseException:
            try:
                self.conn.rollback()
            except sqlite3.Error:
                pass
            raise
        else:
            try:
                self.conn.commit()
            except sqlite3.Error as error:
                try:
                    self.conn.rollback()
                except sqlite3.Error:
                    pass
                raise error_from_exception(error) from error

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        try:
            return self.conn.execute(sql, parameters)
        except sqlite3.Error as error:
            raise error_from_exception(error) from error

    def schema_status(self) -> SchemaStatus:
        row = self.conn.execute("SELECT schema_version, user_version, controller_build_id FROM control_meta, pragma_user_version").fetchone()
        if row is None:
            raise DatabaseCorruptError("controller metadata is missing")
        migrations = tuple(int(item[0]) for item in self.conn.execute("SELECT version FROM schema_migrations ORDER BY version"))
        return SchemaStatus(
            schema_version=int(row[0]),
            user_version=int(row[1]),
            controller_build_id=str(row[2]),
            migration_versions=migrations,
            sqlite_version=sqlite3.sqlite_version,
            journal_mode=str(self.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
            synchronous=int(self.conn.execute("PRAGMA synchronous").fetchone()[0]),
            foreign_keys=bool(self.conn.execute("PRAGMA foreign_keys").fetchone()[0]),
        )

    def list_projects(self) -> list[dict[str, Any]]:
        return [row_to_dict(row) for row in self.conn.execute("SELECT * FROM projects ORDER BY project_id")]

    def list_operations(self) -> list[dict[str, Any]]:
        return [OperationRecord.from_row(row_to_dict(row)).as_dict() for row in self.conn.execute("SELECT * FROM operations ORDER BY created_at, operation_id")]

    def list_events(self, *, after: int = 0, limit: int = 256) -> list[dict[str, Any]]:
        if not isinstance(after, int) or after < 0:
            raise ValueError("after must be a non-negative integer")
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        rows = self.conn.execute("SELECT * FROM control_events WHERE sequence > ? ORDER BY sequence LIMIT ?", (after, limit))
        return [EventRecord.from_row(row_to_dict(row)).as_dict() for row in rows]

    def register_build(
        self,
        build_id: str,
        *,
        source_tree_hash: str,
        artifact_manifest_hash: str,
        pi_version: str,
        package_lock_hash: str,
        status: str = "staged",
        source_commit: str | None = None,
        rollback_path: str | None = None,
        verification: Any = None,
    ) -> dict[str, Any]:
        if verification is None:
            verification = {}
        verification_json = canonical_json(verification)
        now = utc_now()
        with self.transaction():
            self.conn.execute(
                "INSERT INTO installed_builds(build_id,source_commit,source_tree_hash,artifact_manifest_hash,pi_version,package_lock_hash,status,installed_at,activated_at,rollback_path,verification_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (build_id, source_commit, source_tree_hash, artifact_manifest_hash, pi_version, package_lock_hash, status, now, now if status == "active" else None, rollback_path, verification_json),
            )
        return row_to_dict(self.conn.execute("SELECT * FROM installed_builds WHERE build_id=?", (build_id,)).fetchone())

    def create_operation(
        self,
        *,
        idempotency_key: str,
        kind: str,
        resource_type: str,
        resource_id: str,
        actor_type: str,
        request: Any,
        actor_id: str | None = None,
        authorization_id: str | None = None,
        expected_resource_version: int | None = None,
        writer_epoch: int | None = None,
        state: str = "planned",
        step: str = "intent-recorded",
        operation_id: str | None = None,
    ) -> OperationRecord:
        from .operations import create_operation
        return create_operation(
            self, idempotency_key=idempotency_key, kind=kind,
            resource_type=resource_type, resource_id=resource_id,
            actor_type=actor_type, request=request, actor_id=actor_id,
            authorization_id=authorization_id,
            expected_resource_version=expected_resource_version,
            writer_epoch=writer_epoch, state=state, step=step,
            operation_id=operation_id,
        )

    def append_event(
        self,
        *,
        event_kind: str,
        resource_type: str,
        resource_id: str,
        payload: Any,
        resource_version: int | None = None,
        operation_id: str | None = None,
        event_id: str | None = None,
    ) -> EventRecord:
        from .events import append_event
        return append_event(
            self, event_kind=event_kind, resource_type=resource_type,
            resource_id=resource_id, payload=payload,
            resource_version=resource_version, operation_id=operation_id,
            event_id=event_id,
        )

    def cas_update(
        self,
        table: str,
        id_column: str,
        resource_id: str,
        expected_version: int,
        updates: Mapping[str, Any],
    ) -> CASResult:
        if table not in _ALLOWED_CAS_TABLES or _CAS_ID_COLUMNS[table] != id_column:
            raise ValueError("CAS table or identifier is not allowlisted")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected version must be positive")
        if not updates or any(not _SAFE_NAME.fullmatch(key) or key in {id_column, "resource_version"} for key in updates):
            raise ValueError("invalid CAS update columns")
        columns = sorted(updates)
        assignments = ", ".join(f"{column}=?" for column in columns) + ", resource_version=resource_version+1"
        values = [updates[column] for column in columns] + [resource_id, expected_version]
        with self.transaction():
            cursor = self.conn.execute(
                f"UPDATE {table} SET {assignments} WHERE {id_column}=? AND resource_version=?",
                values,
            )
            if cursor.rowcount != 1:
                actual_row = self.conn.execute(f"SELECT resource_version FROM {table} WHERE {id_column}=?", (resource_id,)).fetchone()
                if actual_row is None:
                    raise NotFoundError("resource was not found", detail={"resource_id": resource_id})
                raise ResourceStaleError(resource_id, expected_version, int(actual_row[0]))
        return CASResult(resource_id, expected_version, expected_version + 1)

    # Completion-resource state APIs remain thin facades over the same store;
    # they do not create a second authority or perform external effects.
    def create_workstream(self, **kwargs: Any) -> dict[str, Any]:
        from .workstreams import create_workstream
        return create_workstream(self, **kwargs)

    def get_workstream(self, workstream_id: str) -> dict[str, Any]:
        from .workstreams import get_workstream
        return get_workstream(self, workstream_id)

    def list_workstreams(self, project_id: str | None = None) -> list[dict[str, Any]]:
        from .workstreams import list_workstreams
        return list_workstreams(self, project_id)

    def update_workstream(self, workstream_id: str, **kwargs: Any) -> dict[str, Any]:
        from .workstreams import update_workstream
        return update_workstream(self, workstream_id, **kwargs)

    def ensure_project_activation(self, **kwargs: Any) -> dict[str, Any]:
        from .workstreams import ensure_project_activation
        return ensure_project_activation(self, **kwargs)

    def transition_activation(self, **kwargs: Any) -> dict[str, Any]:
        from .workstreams import transition_activation
        return transition_activation(self, **kwargs)

    def create_migration_mapping(self, **kwargs: Any) -> dict[str, Any]:
        from .workstreams import create_migration_mapping
        return create_migration_mapping(self, **kwargs)

    def create_run(
        self,
        *,
        run_id: str,
        conversation_id: str,
        authority: str,
        runtime_spec_hash: str,
        build_id: str,
        project_id: str | None = None,
        working_copy_id: str | None = None,
        parent_run_id: str | None = None,
        parent_conversation_id: str | None = None,
        child_source: Mapping[str, Any] | ChildSource | None = None,
        expected_working_copy_version: int | None = None,
        expected_head_oid: str | None = None,
        expected_tree_oid: str | None = None,
        dirty_fingerprint: str | None = None,
        writer_epoch: int | None = None,
        owner_pid: int | None = None,
        owner_start_identity: str | None = None,
        capability_hash: str = "",
        manifest_path: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.transaction():
            conversation = self.conn.execute("SELECT role,project_id,working_copy_id FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            if conversation is None:
                raise NotFoundError("conversation was not found", detail={"conversation_id": conversation_id})
            role = str(conversation["role"])
            child_source_value: dict[str, Any] | None = None
            if isinstance(child_source, ChildSource):
                child_source_value = child_source.as_dict()
            elif child_source is not None:
                child_source_value = validate_child_source(child_source)
            if parent_run_id is not None and child_source_value is None:
                raise ConstraintError("child run requires a durable child source binding")
            if child_source_value is not None:
                if parent_run_id is None or parent_conversation_id is None:
                    raise ConstraintError("child source requires parent run and conversation bindings")
                if child_source_value["authority"] != authority:
                    raise ConstraintError("child source authority does not match run authority")
                parent = self.conn.execute("SELECT project_id,conversation_id,working_copy_id FROM runs WHERE run_id=?", (parent_run_id,)).fetchone()
                if parent is None:
                    raise NotFoundError("parent run was not found", detail={"run_id": parent_run_id})
                if parent["conversation_id"] != parent_conversation_id:
                    raise ConstraintError("child parent conversation does not match parent run")
                expected_project = project_id if project_id is not None else conversation["project_id"]
                if parent["project_id"] != expected_project:
                    raise ConstraintError("child project does not match parent run")
                if authority == "writer" and parent["working_copy_id"] == working_copy_id:
                    raise ConstraintError("writer child must use a distinct working copy")
            if child_source_value is None and parent_conversation_id is not None:
                raise ConstraintError("parent conversation binding requires a child source")
            child_source_json = canonical_json(child_source_value) if child_source_value is not None else None
            if project_id is not None and project_id != conversation["project_id"]:
                raise ConstraintError("run project does not match conversation")
            if authority == "writer":
                if role in {"secretary", "host", "review"}:
                    raise ConstraintError("conversation role cannot own a writer run", detail={"role": role})
                if conversation["working_copy_id"] != working_copy_id:
                    raise ConstraintError("writer run must use the conversation's explicit working copy")
                if project_id is not None and project_id != conversation["project_id"]:
                    raise ConstraintError("writer run project does not match conversation")
            elif authority == "secretary":
                if role != "secretary" or working_copy_id is not None:
                    raise ConstraintError("secretary run binding is invalid")
            elif authority == "host-maintenance":
                if role != "host" or project_id is not None or working_copy_id is not None:
                    raise ConstraintError("host-maintenance run binding is invalid")
            elif authority == "read-only" and working_copy_id is not None and conversation["working_copy_id"] != working_copy_id:
                raise ConstraintError("read-only run working copy does not match conversation")
            build = self.conn.execute("SELECT status FROM installed_builds WHERE build_id=?", (build_id,)).fetchone()
            if build is None or build[0] != "active":
                raise ConstraintError("run requires an active controller build", detail={"build_id": build_id})
            if authority == "writer":
                if working_copy_id is None or expected_working_copy_version is None or writer_epoch is None:
                    raise ConstraintError("writer run requires a working copy, version, and epoch")
                wc = self.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (working_copy_id,)).fetchone()
                if wc is None:
                    raise NotFoundError("working copy was not found", detail={"resource_id": working_copy_id})
                if wc["project_id"] != conversation["project_id"] or wc["effective_mode"] == "read-only" or wc["kind"] == "review":
                    raise ConstraintError("writer working copy binding is invalid")
                if wc["active_writer_run_id"] is not None:
                    raise WriterStaleError("working copy already has an active writer", detail={"working_copy_id": working_copy_id})
                if int(wc["resource_version"]) != expected_working_copy_version:
                    raise ResourceStaleError(working_copy_id, expected_working_copy_version, int(wc["resource_version"]))
                if int(writer_epoch) != int(wc["writer_epoch"]) + 1:
                    raise WriterStaleError("writer epoch is not the next epoch", detail={"working_copy_id": working_copy_id, "expected_epoch": int(wc["writer_epoch"]) + 1})
            elif authority == "secretary" and working_copy_id is not None:
                raise ConstraintError("secretary runs cannot own a working copy")
            if project_id is None:
                project_id = conversation["project_id"]
            self.conn.execute(
                "INSERT INTO runs(run_id,conversation_id,project_id,working_copy_id,parent_run_id,child_source_json,authority,desired_state,observed_state,expected_working_copy_version,expected_head_oid,expected_tree_oid,dirty_fingerprint,writer_epoch,runtime_spec_hash,build_id,owner_pid,owner_start_identity,capability_hash,manifest_path,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, conversation_id, project_id, working_copy_id, parent_run_id, child_source_json, authority, "running", "created", expected_working_copy_version, expected_head_oid, expected_tree_oid, dirty_fingerprint, writer_epoch, runtime_spec_hash, build_id, owner_pid, owner_start_identity, capability_hash, manifest_path, 1, now, now),
            )
            from .events import append_event_in_transaction
            from .operations import update_operation_in_transaction
            append_event_in_transaction(
                self.conn,
                event_kind="run.created",
                resource_type="run",
                resource_id=run_id,
                resource_version=1,
                operation_id=operation_id,
                payload={"runId": run_id, "conversationId": conversation_id, "authority": authority},
            )
            working_copy_version = None
            if authority == "writer":
                cursor = self.conn.execute(
                    "UPDATE working_copies SET active_writer_run_id=?, writer_epoch=?, resource_version=resource_version+1, updated_at=? WHERE working_copy_id=? AND resource_version=?",
                    (run_id, writer_epoch, now, working_copy_id, expected_working_copy_version),
                )
                if cursor.rowcount != 1:
                    raise ResourceStaleError(str(working_copy_id), int(expected_working_copy_version), int(expected_working_copy_version))
                working_copy_version = int(expected_working_copy_version) + 1
                append_event_in_transaction(
                    self.conn,
                    event_kind="writer.claimed",
                    resource_type="working_copy",
                    resource_id=str(working_copy_id),
                    resource_version=working_copy_version,
                    operation_id=operation_id,
                    payload={"runId": run_id, "workingCopyId": working_copy_id, "writerEpoch": int(writer_epoch)},
                )
            if operation_id is not None:
                update_operation_in_transaction(
                    self.conn,
                    operation_id,
                    state="applying",
                    step="run-created",
                    result={
                        "runId": run_id,
                        "workingCopyId": working_copy_id,
                        "writerEpoch": writer_epoch,
                        "capabilityHash": capability_hash,
                        "state": "running",
                    },
                )
        return row_to_dict(self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())

    def terminalize_run(
        self,
        run_id: str,
        *,
        state: str = "stopped",
        error_code: str | None = None,
        error_detail: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Terminalize a run, clear its claim, and publish one atomic outbox transition."""

        if state not in {"stopped", "failed", "lost"}:
            raise ValueError("run terminal state is invalid")
        now = utc_now()
        with self.transaction():
            row = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise NotFoundError("run was not found", detail={"run_id": run_id})
            if row["observed_state"] in {"stopped", "failed", "lost"}:
                if operation_id is not None:
                    from .operations import update_operation_in_transaction
                    operation = self.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
                    if operation is None:
                        raise NotFoundError("operation was not found", detail={"operation_id": operation_id})
                    operation_result = None
                    if operation["result_json"] is not None:
                        try:
                            operation_result = parse_canonical_json(str(operation["result_json"]))
                        except Exception as error:
                            raise DatabaseCorruptError("operation result is not valid canonical JSON") from error
                    if not (
                        isinstance(operation_result, dict)
                        and operation_result.get("runId") == run_id
                    ):
                        raise ConstraintError("terminal operation does not belong to this run")
                    if operation["state"] not in {"succeeded", "failed", "needs_attention", "cancelled"}:
                        result = {"runId": run_id, "state": row["observed_state"], "writerEpoch": row["writer_epoch"]}
                        operation_state = "failed" if row["observed_state"] == "failed" else ("needs_attention" if row["observed_state"] == "lost" else "succeeded")
                        update_operation_in_transaction(
                            self.conn,
                            operation_id,
                            state=operation_state,
                            step="run-terminalized-reconciled",
                            result=result,
                        )
                return row_to_dict(row)
            claim = None
            claim_owned_by_run = False
            claim_resource_version: int | None = None
            if row["authority"] == "writer" and row["working_copy_id"] is not None:
                claim = self.conn.execute(
                    "SELECT active_writer_run_id,resource_version FROM working_copies WHERE working_copy_id=?",
                    (row["working_copy_id"],),
                ).fetchone()
                if claim is not None:
                    claim_owned_by_run = claim["active_writer_run_id"] == run_id
                    claim_resource_version = int(claim["resource_version"]) if claim_owned_by_run else None
            cursor = self.conn.execute(
                "UPDATE runs SET desired_state='stopped',observed_state=?,ended_at=?,updated_at=?,error_code=?,error_detail=?,resource_version=resource_version+1 WHERE run_id=? AND observed_state NOT IN ('stopped','failed','lost')",
                (state, now, now, error_code, error_detail, run_id),
            )
            if cursor.rowcount != 1:
                raise ResourceStaleError(run_id, int(row["resource_version"]), int(row["resource_version"]))
            from .events import append_event_in_transaction
            from .operations import update_operation_in_transaction
            run_version = int(row["resource_version"]) + 1
            append_event_in_transaction(
                self.conn,
                event_kind="run.terminalized",
                resource_type="run",
                resource_id=run_id,
                resource_version=run_version,
                operation_id=operation_id,
                payload={"runId": run_id, "state": state, "errorCode": error_code},
            )
            if claim_owned_by_run:
                working_copy = self.conn.execute(
                    "SELECT active_writer_run_id,resource_version FROM working_copies WHERE working_copy_id=?",
                    (row["working_copy_id"],),
                ).fetchone()
                if working_copy is None or working_copy["active_writer_run_id"] is not None:
                    raise ConstraintError("writer claim was not cleared by terminal transition")
                expected_working_copy_version = (claim_resource_version or 0) + 1
                if int(working_copy["resource_version"]) != expected_working_copy_version:
                    raise ConstraintError("writer claim clear did not advance the working-copy version")
                append_event_in_transaction(
                    self.conn,
                    event_kind="writer.claim.cleared",
                    resource_type="working_copy",
                    resource_id=str(row["working_copy_id"]),
                    resource_version=int(working_copy["resource_version"]),
                    operation_id=operation_id,
                    payload={"runId": run_id, "workingCopyId": row["working_copy_id"], "writerEpoch": row["writer_epoch"]},
                )
            if operation_id is not None:
                operation_state = "failed" if state == "failed" else ("needs_attention" if state == "lost" else "succeeded")
                update_operation_in_transaction(
                    self.conn,
                    operation_id,
                    state=operation_state,
                    step="run-terminalized",
                    result={"runId": run_id, "state": state, "writerEpoch": row["writer_epoch"]},
                    error_code=error_code if operation_state != "succeeded" else None,
                    error_detail=error_detail if operation_state != "succeeded" else None,
                )
        return row_to_dict(self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())

    release_writer = terminalize_run
    release_run = terminalize_run

    def ensure_consumer(self, consumer_id: str) -> ConsumerCursor:
        from .events import ensure_consumer_in_transaction
        with self.transaction():
            ensure_consumer_in_transaction(self.conn, consumer_id)
        row = self.conn.execute("SELECT last_sequence,updated_at FROM event_consumers WHERE consumer_id=?", (consumer_id,)).fetchone()
        return ConsumerCursor(consumer_id, int(row["last_sequence"]), str(row["updated_at"]))

    def consume_events(self, consumer_id: str, *, limit: int = 256) -> list[dict[str, Any]]:
        cursor = self.ensure_consumer(consumer_id)
        return self.list_events(after=cursor.last_sequence, limit=limit)

    def advance_consumer(self, consumer_id: str, sequence: int) -> ConsumerCursor:
        from .events import acknowledge
        acknowledge(self, consumer_id, sequence)
        row = self.conn.execute("SELECT last_sequence,updated_at FROM event_consumers WHERE consumer_id=?", (consumer_id,)).fetchone()
        return ConsumerCursor(consumer_id, int(row["last_sequence"]), str(row["updated_at"]))


# Short aliases used by tests and future phases.
Store = ControllerStore
SQLiteStore = ControllerStore

__all__ = ["ControllerStore", "SQLiteStore", "Store"]
