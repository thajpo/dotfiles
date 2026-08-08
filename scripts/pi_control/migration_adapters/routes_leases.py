"""Bounded historical route/lease observation (never active authority)."""

from __future__ import annotations

import os
from .base import AdapterResult, observe_tree


def observe(root: os.PathLike[str] | str) -> AdapterResult:
    return observe_tree(root, adapter_kind="routes_leases", role="routes_leases")


observe_routes_leases = observe

__all__ = ["observe", "observe_routes_leases"]
