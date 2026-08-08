"""Read-only backup manifest observation."""

from __future__ import annotations

import os
from .base import AdapterResult, observe_tree


def observe(root: os.PathLike[str] | str) -> AdapterResult:
    return observe_tree(root, adapter_kind="backups", role="backups")


observe_backups = observe
__all__ = ["observe", "observe_backups"]
