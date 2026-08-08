"""Schema-v7 completion resources and authorization vocabulary."""

from __future__ import annotations

import sqlite3

from ..schema import (
    AUTHORIZATION_IMMUTABILITY_SQL,
    AUTHORIZATION_V7_TABLE_SQL,
    COMPLETION_SCHEMA_SQL,
    iter_statements,
)

VERSION = 7
NAME = "completion-resources"
# This digest is intentionally checked into the migration module.  It is
# replaced below with the digest of this exact source file after formatting.
SOURCE_SHA256 = "79209384ba547862ee4d6ac52b3c16683ee9852bebe841c43b481ac8ea41d27af"


def apply(connection: sqlite3.Connection) -> None:
    # Rename/drop is transactional under the caller's migration transaction.
    # Renaming first also removes the old table's authorization triggers when
    # the compatibility table is dropped; they are recreated below.
    connection.execute("ALTER TABLE authorizations RENAME TO authorizations_v6")
    connection.execute(AUTHORIZATION_V7_TABLE_SQL)
    connection.execute(
        "INSERT INTO authorizations(authorization_id,kind,actor_type,actor_id,project_id,resource_type,resource_id,request_context_id,scope_json,scope_digest,issued_at,expires_at,consumed_at,state) "
        "SELECT authorization_id,kind,actor_type,actor_id,project_id,resource_type,resource_id,request_context_id,scope_json,scope_digest,issued_at,expires_at,consumed_at,state FROM authorizations_v6"
    )
    connection.execute("DROP TABLE authorizations_v6")
    for statement in iter_statements(COMPLETION_SCHEMA_SQL):
        connection.execute(statement)
    for statement in iter_statements(AUTHORIZATION_IMMUTABILITY_SQL):
        connection.execute(statement)


__all__ = ["NAME", "SOURCE_SHA256", "VERSION", "apply"]
