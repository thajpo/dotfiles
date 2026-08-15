#!/usr/bin/env python3
"""Installed Pi Web loopback gateway entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pi_control.web_api import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
