"""Platform detection and per-platform default paths for Pisec."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


def is_macos() -> bool:
    return sys.platform == "darwin" or platform.system() == "Darwin"


def is_linux() -> bool:
    return platform.system() == "Linux"


def runtime_root() -> Path:
    """Directory holding Pisec's per-boot control sockets.

    Precedence: $PISEC_RUNTIME_ROOT, then the platform default. Linux keeps
    the historical /run/user/<uid>/pisec location; macOS has no /run/user,
    so sockets live under the existing 0700 state root instead.
    """
    override = os.environ.get("PISEC_RUNTIME_ROOT")
    if override:
        return Path(override)
    if is_macos():
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg:
            return Path(xdg) / "pisec"
        return Path.home() / ".local" / "state" / "pisec" / "runtime"
    base = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(base) / "pisec"
