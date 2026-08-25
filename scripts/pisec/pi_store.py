"""Owner-only SQLite state for Pisec."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat
from datetime import datetime, timezone
from typing import Iterator

from .models import SchemaError, UnsafeStateError, utc_now
from .pi_schema import SCHEMA_NAME, SCHEMA_VERSION, apply_schema, schema_digest

DIR_MODE = 0o700
FILE_MODE = 0o600


def archive_and_reset_state(state_root: Path | str | None = None) -> Path | None:
    """Explicitly archive an owner-only state root; never import or mutate it."""
    root = Path(state_root) if state_root is not None else default_state_root()
    if not root.exists() and not root.is_symlink():
        return None
    _check_dir(root, create=False)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = root.with_name(f"{root.name}.archive-{stamp}")
    suffix = 0
    while archive.exists() or archive.is_symlink():
        suffix += 1
        archive = root.with_name(f"{root.name}.archive-{stamp}-{suffix}")
    os.replace(root, archive)
    os.chmod(archive, DIR_MODE)
    return archive


def default_state_root() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return Path(os.environ.get("PISEC_STATE_ROOT", base / "pisec")).expanduser()


def _check_dir(path: Path, *, create: bool) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            raise UnsafeStateError("Pisec state directory does not exist", detail={"path": str(path)})
        path.mkdir(parents=True, mode=DIR_MODE)
        os.chmod(path, DIR_MODE)
        info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeStateError("Pisec state root must be a directory", detail={"path": str(path)})
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise UnsafeStateError("Pisec state root is not user-owned", detail={"path": str(path)})
    if stat.S_IMODE(info.st_mode) != DIR_MODE:
        raise UnsafeStateError("Pisec state root must be mode 0700", detail={"path": str(path)})


def _check_file(path: Path, *, allow_missing: bool = True) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return
        raise UnsafeStateError("Pisec database does not exist", detail={"path": str(path)})
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise UnsafeStateError("Pisec database must be a regular file", detail={"path": str(path)})
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise UnsafeStateError("Pisec database is not user-owned", detail={"path": str(path)})
    if stat.S_IMODE(info.st_mode) != FILE_MODE:
        raise UnsafeStateError("Pisec database must be mode 0600", detail={"path": str(path)})


class PiStore:
    def __init__(self, state_root: Path | str | None = None, *, initialize: bool = True):
        self.state_root = Path(state_root) if state_root is not None else default_state_root()
        _check_dir(self.state_root, create=initialize)
        self.path = self.state_root / "control.db"
        _check_file(self.path, allow_missing=initialize)
        existed = self.path.exists()
        self.conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        if not existed:
            os.chmod(self.path, FILE_MODE)
        _check_file(self.path, allow_missing=False)
        if initialize and not existed:
            self._initialize()
        elif existed:
            # Existing state is opaque until it proves the exact v1 identity.
            # There is deliberately no in-place migration path.
            self._verify_schema()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                _check_file(sidecar, allow_missing=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")

    def _initialize(self) -> None:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            apply_schema(self.conn)
            self.conn.execute(
                "INSERT INTO control_meta(singleton,schema_name,schema_version,schema_sha256,created_at) VALUES(1,?,?,?,?)",
                (SCHEMA_NAME, SCHEMA_VERSION, schema_digest(), utc_now()),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _verify_schema(self) -> None:
        try:
            row = self.conn.execute("SELECT * FROM control_meta WHERE singleton=1").fetchone()
        except sqlite3.Error as error:
            raise SchemaError("Pisec schema metadata is missing") from error
        expected = (SCHEMA_NAME, SCHEMA_VERSION, schema_digest())
        actual = None if row is None else (row["schema_name"], row["schema_version"], row["schema_sha256"])
        if actual != expected:
            raise SchemaError(
                "Pisec state is unsupported; review it and rerun with explicit archive/reset",
                detail={"expected": expected, "actual": actual},
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PiStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
