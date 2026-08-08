"""Phase 6C immutable artifact index and child terminal records."""

from __future__ import annotations

import sqlite3

from ..schema import ARTIFACT_SCHEMA_SQL, iter_statements

VERSION = 3
NAME = "artifacts-terminals"
# Frozen hash of the complete v3 schema, before revision immutability triggers.
SOURCE_SHA256 = "02d20c1eb0fda0dd3a89120eedacac855aab0c30cb34aecc9fd1ae575725b61a"


def apply(connection: sqlite3.Connection) -> None:
    for statement in iter_statements(ARTIFACT_SCHEMA_SQL):
        connection.execute(statement)


__all__ = ["NAME", "SOURCE_SHA256", "VERSION", "apply"]
