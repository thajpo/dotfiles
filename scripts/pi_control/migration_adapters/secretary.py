"""Bounded secretary registry/workstream observation."""

from __future__ import annotations

import os
from .base import AdapterResult, observe_tree


def observe(root: os.PathLike[str] | str) -> AdapterResult:
    return observe_tree(root, adapter_kind="secretary", role="secretary")


observe_secretary = observe

__all__ = ["observe", "observe_secretary"]
