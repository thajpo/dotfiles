"""Phase 6B durable child-source binding migration."""

from __future__ import annotations

import sqlite3

VERSION = 2
NAME = "child-source"
# Frozen hash of the complete v2 schema, before Phase 6C artifact tables.
SOURCE_SHA256 = "f2acac92f0f33b87b341cb55f4c8e595cc1e9075649d5a607bf9718225ba6911"


def apply(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE runs ADD COLUMN child_source_json TEXT CHECK (child_source_json IS NULL OR json_valid(child_source_json))"
    )


__all__ = ["NAME", "SOURCE_SHA256", "VERSION", "apply"]
