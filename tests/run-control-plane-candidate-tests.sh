#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# The legacy patch-chain runner still targets the removed @kjrjay/pi-sandbox
# package; Phase 5's first-party package provenance is the scoped candidate
# surface until that installer skew is separately migrated.
node "$root/tests/pi-subagents-control-provenance.mjs"
NODE_PATH="$root/pi/npm/node_modules" node --test \
  "$root/tests/pi-sandbox-control-manifest.test.mjs" \
  "$root/tests/pi-sandbox-control-broker.test.mjs" \
  "$root/tests/control_plane/continuity-extension.test.mjs" \
  "$root/tests/control_plane/secretary-extension.test.mjs" \
  "$root/tests/pi-child-control-plane-e2e.mjs"
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s "$root/tests/control_plane" -p 'test_*.py'
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_pi_harness_static tests.test_pi_acceptance_regressions
bash "$root/tests/pi-installer-transaction.sh"
bash "$root/tests/pi-control-plane-integration.sh"
set +e
bash "$root/tests/pi-staged-artifact-rollback.sh"
rollback_rc=$?
set -e
if [[ "$rollback_rc" -eq 77 ]]; then
  printf '%s\n' 'STOP: staged artifact/image rollback prerequisite was not tested (Docker/npm/image unavailable)' >&2
  exit 77
elif [[ "$rollback_rc" -ne 0 ]]; then
  printf '%s\n' 'STOP: staged artifact/image rollback gate failed' >&2
  exit 1
fi
python3 -m compileall -q "$root/scripts/pi_control" "$root/tests/control_plane"
find "$root" -type f -name '*.pyc' -delete
find "$root" -type d -name __pycache__ -prune -exec rm -rf {} +
test -z "$(find "$root" -type f -name '*.pyc' -print -quit)"
test -z "$(find "$root" -type d -name __pycache__ -print -quit)"
git -C "$root" diff --check
printf '%s\n' 'control-plane candidate gate: ok'
