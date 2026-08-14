"""Resolve the tmux socket owned by the Pi surface."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


def pi_tmux_socket(*, environ: Mapping[str, str] | None = None, home: str | None = None) -> str:
    """Return the explicit Pi tmux socket, never the ambient default socket."""
    values = os.environ if environ is None else environ
    explicit = values.get("PI_TMUX_SOCKET")
    if explicit:
        return explicit
    state_root = values.get("PI_SYSTEM_STATE_ROOT")
    if state_root:
        return str(Path(state_root).expanduser() / "tmux" / "pi.sock")
    base = Path(home).expanduser() if home is not None else Path.home()
    return str(base / ".local" / "state" / "pi-system" / "tmux" / "pi.sock")
