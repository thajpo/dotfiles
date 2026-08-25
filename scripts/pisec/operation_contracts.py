"""Socket operation contracts projected from the checked catalogue."""
from __future__ import annotations

from typing import Any

from .operation_catalogue_generated import SOCKET_OPERATIONS


def operation_manifest() -> dict[str, list[str]]:
    """Return a stable, JSON-safe manifest for doctor and parity checks."""
    return {socket: sorted(operations) for socket, operations in SOCKET_OPERATIONS.items()}


def operation_allowed(socket: str, operation: str) -> bool:
    return operation in SOCKET_OPERATIONS.get(socket, ())
