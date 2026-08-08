#!/usr/bin/env bash
set -u
root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.system.test_action_manifest tests.system.test_slice_briefs tests.system.test_evidence || exit $?
PYTHONDONTWRITEBYTECODE=1 python3 "$root/tests/system/validate_plan_docs.py" || exit $?
exit 0
