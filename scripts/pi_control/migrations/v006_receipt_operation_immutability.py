"""Immutable submitted review receipts and terminal operation outcomes."""

from __future__ import annotations

import sqlite3

from ..schema import RECEIPT_OPERATION_IMMUTABILITY_SQL, iter_statements

VERSION = 6
NAME = "receipt-operation-immutability"
SOURCE_SHA256 = "b180e75c3e2c168a86be78206010420442186d0a204f22a1f2056771b04689e7"


def apply(connection: sqlite3.Connection) -> None:
    for statement in iter_statements(RECEIPT_OPERATION_IMMUTABILITY_SQL):
        connection.execute(statement)


__all__ = ["NAME", "SOURCE_SHA256", "VERSION", "apply"]
