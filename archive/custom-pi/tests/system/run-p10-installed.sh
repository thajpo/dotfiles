#!/usr/bin/env bash
set -u
root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
PYTHONDONTWRITEBYTECODE=1 python3 tests/system/fixtures/installed-p10.py
