#!/usr/bin/env bash
set -u
root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/private/tmp/pi-system-pycache python3 -m unittest tests.test_greenfield_core || exit $?
PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/private/tmp/pi-system-pycache python3 -m unittest tests.system.installed_process_test || exit $?
node "$root/tests/greenfield-manifest-bridge.test.mjs" || exit $?
PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/private/tmp/pi-system-pycache python3 "$root/tests/system/validate_plan_docs.py" || exit $?
exit 0
