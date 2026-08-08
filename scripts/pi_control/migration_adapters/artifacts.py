"""Bounded exact artifact manifest/file observation."""

from __future__ import annotations

import os
from .base import AdapterResult, observe_tree


def observe(root: os.PathLike[str] | str) -> AdapterResult:
    return observe_tree(root, adapter_kind="artifacts", role="artifacts")


observe_artifacts = observe

__all__ = ["observe", "observe_artifacts"]
