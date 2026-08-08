"""Reviewer authority binding and immutable submitted-review provenance."""

from __future__ import annotations

import sqlite3

from ..schema import iter_statements

VERSION = 5
NAME = "review-authority"
# Frozen after this migration source is finalized.
SOURCE_SHA256 = "1df57435401a5381cb52811e56dedbef0a1d974512f6391c58c42099c4a368f6"

_REVIEW_COLUMNS = (
    ("reviewer_run_id", "TEXT REFERENCES runs(run_id)"),
    ("reviewer_actor_id", "TEXT"),
    ("reviewer_capability_hash", "TEXT"),
    ("reviewer_source_json", "TEXT CHECK (reviewer_source_json IS NULL OR json_valid(reviewer_source_json))"),
)

_REVIEW_TRIGGERS = r'''
CREATE TRIGGER IF NOT EXISTS review_submission_authority_valid
BEFORE UPDATE OF state ON reviews
WHEN NEW.state = 'submitted'
  AND (NEW.reviewer_conversation_id IS NULL
       OR NEW.reviewer_run_id IS NULL
       OR NEW.reviewer_actor_id IS NULL
       OR NEW.reviewer_capability_hash IS NULL
       OR NEW.reviewer_source_json IS NULL)
BEGIN
  SELECT RAISE(ABORT, 'submitted review is missing reviewer authority binding');
END;

CREATE TRIGGER IF NOT EXISTS submitted_review_binding_immutable
BEFORE UPDATE OF reviewer_conversation_id, reviewer_run_id, reviewer_actor_id, reviewer_capability_hash, reviewer_source_json ON reviews
WHEN OLD.state = 'submitted'
  AND (NEW.reviewer_conversation_id IS NOT OLD.reviewer_conversation_id
       OR NEW.reviewer_run_id IS NOT OLD.reviewer_run_id
       OR NEW.reviewer_actor_id IS NOT OLD.reviewer_actor_id
       OR NEW.reviewer_capability_hash IS NOT OLD.reviewer_capability_hash
       OR NEW.reviewer_source_json IS NOT OLD.reviewer_source_json)
BEGIN
  SELECT RAISE(ABORT, 'submitted review authority binding is immutable');
END;
'''


def apply(connection: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(reviews)")}
    for name, definition in _REVIEW_COLUMNS:
        if name not in columns:
            connection.execute(f"ALTER TABLE reviews ADD COLUMN {name} {definition}")
    for statement in iter_statements(_REVIEW_TRIGGERS):
        connection.execute(statement)


__all__ = ["NAME", "SOURCE_SHA256", "VERSION", "apply"]
