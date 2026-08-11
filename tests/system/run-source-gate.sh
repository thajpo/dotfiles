#!/usr/bin/env bash
set -u
root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
node "$root/tests/scoped-project-read-extension.test.mjs" || exit $?
node "$root/tests/pi-sandbox-control-broker.test.mjs" || exit $?
NODE_PATH="$root/pi/npm/node_modules" node --test "$root/tests/p6-extensions.test.mjs" || exit $?
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.system.test_action_manifest tests.system.test_greenfield_docs tests.system.test_evidence tests.system.test_p5_source tests.system.test_p6_source tests.control_plane.test_p6_messages tests.control_plane.test_p6_commands tests.control_plane.test_p6_packages tests.control_plane.test_p7_workstreams || exit $?
PYTHONDONTWRITEBYTECODE=1 python3 "$root/tests/system/validate_plan_docs.py" || exit $?
exit 0
