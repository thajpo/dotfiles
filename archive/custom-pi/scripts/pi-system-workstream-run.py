#!/usr/bin/env python3
"""Launch one controller-created durable workstream conversation."""

from __future__ import annotations

import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True

if __name__ == "__main__":
    target = Path(__file__).resolve().with_name("pi-system-run.py")
    os.execv(sys.executable, [sys.executable, str(target), "--expected-role", "workstream", *sys.argv[1:]])
