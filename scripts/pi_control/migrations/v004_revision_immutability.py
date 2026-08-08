"""Phase 7 immutable change revision/input triggers."""

from __future__ import annotations

import sqlite3

from ..schema import REVISION_SCHEMA_SQL, iter_statements

VERSION = 4
NAME = "revision-immutability"
# Frozen hash of the complete v4 schema at this migration's introduction.
SOURCE_SHA256 = "85f7986292293aa0e1bb23d6e2e5778be213668dc03d3dc1e387a830908a45e9"


def apply(connection: sqlite3.Connection) -> None:
    for statement in iter_statements(REVISION_SCHEMA_SQL):
        connection.execute(statement)


__all__ = ["NAME", "SOURCE_SHA256", "VERSION", "apply"]
