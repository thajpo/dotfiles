"""Secure SQLite store for the fresh Pi system.

The store has the same small transaction-facing interface as the historical
controller helpers, which lets proven Git/change/review code be reused without
reusing the historical database or its migration authority.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Iterator, Sequence

from .errors import DatabaseCorruptError, SchemaNewerError, UnsafeDatabaseError
from .pi_schema import PI_MIGRATION_NAME, PI_SCHEMA_VERSION, apply_schema, schema_digest
from .models import SchemaStatus, new_id, utc_now

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def default_state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(os.environ.get("PI_SYSTEM_STATE_ROOT", str(Path(base) / "pi-system"))).expanduser()


def _secure_dir(path: Path, *, create: bool) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            raise UnsafeDatabaseError("Pi system state directory does not exist", detail={"path": str(path)})
        old_umask = os.umask(0o077)
        try:
            path.mkdir(mode=_DIR_MODE, parents=False)
        finally:
            os.umask(old_umask)
        info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeDatabaseError("Pi system state root is not a directory", detail={"path": str(path)})
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise UnsafeDatabaseError("Pi system state root is not user-owned", detail={"path": str(path)})
    if stat.S_IMODE(info.st_mode) != _DIR_MODE:
        raise UnsafeDatabaseError("Pi system state root must be mode 0700", detail={"path": str(path)})


def _secure_file(path: Path, *, allow_missing: bool = True) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return
        raise UnsafeDatabaseError("Pi system database does not exist", detail={"path": str(path)})
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise UnsafeDatabaseError("Pi system database is not a regular file", detail={"path": str(path)})
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise UnsafeDatabaseError("Pi system database is not user-owned", detail={"path": str(path)})
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise UnsafeDatabaseError("Pi system database is accessible to group or other", detail={"path": str(path)})


class PiStore:
    """One secure connection and one schema owner for the new product."""

    def __init__(self, state_root: os.PathLike[str] | str | None = None, *, db_path: os.PathLike[str] | str | None = None, controller_build_id: str | None = None, initialize: bool = True, read_only: bool = False) -> None:
        self.state_root = Path(state_root).expanduser() if state_root is not None else default_state_root()
        self.db_path = Path(db_path).expanduser() if db_path is not None else self.state_root / "control.db"
        if db_path is not None and not self.db_path.is_absolute():
            self.db_path = self.state_root / self.db_path
        self.controller_build_id = controller_build_id or os.environ.get("PI_SYSTEM_BUILD_ID", "pi-system-dev-" + schema_digest()[7:23])
        if not isinstance(self.controller_build_id, str) or not self.controller_build_id or "\x00" in self.controller_build_id:
            raise ValueError("invalid controller build ID")
        self.initialize = initialize
        self.read_only = read_only
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> "PiStore":
        return self.open()

    def __exit__(self, *_: Any) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("PiStore is not open")
        return self.connection

    def open(self) -> "PiStore":
        if self.connection is not None:
            return self
        parent = self.state_root.parent
        parent.mkdir(parents=True, exist_ok=True)
        if not self.read_only:
            _secure_dir(self.state_root, create=True)
        else:
            _secure_dir(self.state_root, create=False)
        if self.db_path.parent != self.state_root:
            raise UnsafeDatabaseError("Pi system database must be directly beneath the state root")
        _secure_file(self.db_path)
        if self.read_only:
            uri = "file:" + str(self.db_path.absolute()) + "?mode=ro"
            self.connection = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
        else:
            old_umask = os.umask(0o077)
            try:
                self.connection = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
            finally:
                os.umask(old_umask)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA foreign_keys=ON")
            if not self.read_only:
                version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
                if version not in {0, PI_SCHEMA_VERSION}:
                    if version > PI_SCHEMA_VERSION:
                        raise SchemaNewerError("Pi system database is newer than this controller")
                    raise DatabaseCorruptError("Pi system schema epoch is obsolete and cannot be migrated")
                mode = str(self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
                if mode != "wal":
                    raise UnsafeDatabaseError("Pi system database could not use WAL")
                self.conn.execute("PRAGMA synchronous=FULL")
                self.conn.execute("PRAGMA busy_timeout=5000")
                if self.initialize:
                    self._initialize_schema()
            else:
                self.conn.execute("PRAGMA busy_timeout=5000")
                self._verify_schema()
            if self.db_path.exists() and not self.read_only:
                os.chmod(self.db_path, _FILE_MODE)
        except BaseException:
            self.close()
            raise
        return self

    def close(self) -> None:
        connection, self.connection = self.connection, None
        if connection is not None:
            connection.close()

    def _initialize_schema(self) -> None:
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if version > PI_SCHEMA_VERSION:
            raise SchemaNewerError("Pi system database is newer than this controller")
        with self.transaction():
            if version == 0:
                existing = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
                if existing:
                    raise DatabaseCorruptError("database has no fresh Pi system schema")
                apply_schema(self.conn)
                now = utc_now()
                self.conn.execute("INSERT INTO schema_migrations(version,name,source_sha256,applied_at) VALUES(?,?,?,?)", (PI_SCHEMA_VERSION, PI_MIGRATION_NAME, schema_digest(), now))
                self.conn.execute("INSERT INTO control_meta(singleton,schema_version,controller_build_id,controller_restart_epoch,controller_started_at,created_at,updated_at) VALUES(1,?,?,?,?,?,?)", (PI_SCHEMA_VERSION, self.controller_build_id, new_id("ctl"), now, now, now))
                self.conn.execute(f"PRAGMA user_version={PI_SCHEMA_VERSION}")
            else:
                self._verify_schema()

    def _verify_schema(self) -> None:
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if version != PI_SCHEMA_VERSION:
            raise DatabaseCorruptError("Pi system schema version is invalid")
        migration = self.conn.execute("SELECT name,source_sha256 FROM schema_migrations WHERE version=?", (PI_SCHEMA_VERSION,)).fetchone()
        if migration is None or tuple(migration) != (PI_MIGRATION_NAME, schema_digest()):
            raise DatabaseCorruptError("Pi system schema checksum does not match source")
        meta = self.conn.execute("SELECT schema_version FROM control_meta WHERE singleton=1").fetchone()
        if meta is None or int(meta[0]) != version:
            raise DatabaseCorruptError("Pi system metadata disagrees with SQLite user_version")
        integrity = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        if str(integrity).lower() != "ok":
            raise DatabaseCorruptError("Pi system database integrity check failed")

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise UnsafeDatabaseError("read-only Pi system store cannot mutate")
        self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.conn
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, parameters)

    def schema_status(self) -> SchemaStatus:
        row = self.conn.execute("SELECT schema_version,controller_build_id FROM control_meta WHERE singleton=1").fetchone()
        if row is None:
            raise DatabaseCorruptError("Pi system metadata is missing")
        return SchemaStatus(
            schema_version=int(row[0]), user_version=int(self.conn.execute("PRAGMA user_version").fetchone()[0]),
            controller_build_id=str(row[1]), migration_versions=tuple(int(item[0]) for item in self.conn.execute("SELECT version FROM schema_migrations ORDER BY version")),
            sqlite_version=str(self.conn.execute("SELECT sqlite_version()").fetchone()[0]), journal_mode=str(self.conn.execute("PRAGMA journal_mode").fetchone()[0]),
            synchronous=int(self.conn.execute("PRAGMA synchronous").fetchone()[0]), foreign_keys=bool(self.conn.execute("PRAGMA foreign_keys").fetchone()[0]),
        )

    def controller_identity(self) -> dict[str, str]:
        row = self.conn.execute("SELECT controller_build_id,controller_restart_epoch,controller_started_at FROM control_meta WHERE singleton=1").fetchone()
        if row is None:
            raise DatabaseCorruptError("Pi system controller identity is missing")
        return {"buildId": str(row[0]), "restartEpoch": str(row[1]), "startedAt": str(row[2])}

    def rotate_controller_restart_epoch(self) -> dict[str, str]:
        if self.read_only:
            raise UnsafeDatabaseError("read-only Pi system store cannot rotate controller identity")
        now = utc_now()
        epoch = new_id("ctl")
        with self.transaction():
            self.conn.execute("UPDATE control_meta SET controller_restart_epoch=?,controller_started_at=?,updated_at=? WHERE singleton=1", (epoch, now, now))
        return self.controller_identity()

    # Reuse the controller's transactional operation helpers without exposing
    # any historical schema or migration behavior.
    def create_operation(self, **kwargs: Any) -> Any:
        from .operations import create_operation
        return create_operation(self, **kwargs)

    def complete_operation(self, operation_id: str, **kwargs: Any) -> Any:
        from .operations import complete_operation
        return complete_operation(self, operation_id, **kwargs)

    def fail_operation(self, operation_id: str, **kwargs: Any) -> Any:
        from .operations import fail_operation
        return fail_operation(self, operation_id, **kwargs)


__all__ = ["PiStore", "default_state_root"]
