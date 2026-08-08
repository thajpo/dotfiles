"""Bounded root/session JSONL observation."""

from __future__ import annotations

import os
from .base import AdapterResult, observe_tree


def observe(root: os.PathLike[str] | str) -> AdapterResult:
    return observe_tree(root, adapter_kind="root_sessions", role="root_sessions")


observe_root_sessions = observe

__all__ = ["observe", "observe_root_sessions"]
