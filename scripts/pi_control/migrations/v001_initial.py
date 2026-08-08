"""Initial Phase 2 control-plane schema migration."""

from __future__ import annotations

import sqlite3

from ..schema import apply_schema

# Frozen Phase 1/2/3/4/5 schema identity. Later migrations must not rewrite
# the checksum of this already-applied migration.
VERSION = 1
NAME = "initial"
SOURCE_SHA256 = "2580f10ed02cb157badf9ad274ebdbe79d90aada4f674756c5e11d2cbe94996b"


def apply(connection: sqlite3.Connection) -> None:
    """Create only controller SQLite objects; callers own the transaction."""

    apply_schema(connection)


__all__ = ["NAME", "SOURCE_SHA256", "VERSION", "apply"]
