#!/usr/bin/env python3
"""Launch one personal host Pi with a controller-owned writer tool container."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if __name__ == "__main__":
    target = Path(__file__).resolve().with_name("pi-system-run.py")
    os.execv(sys.executable, [sys.executable, str(target), "--expected-role", "personal", *sys.argv[1:]])
