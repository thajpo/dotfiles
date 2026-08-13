#!/usr/bin/env bash
# Non-live process/client acceptance for the Pi control plane.
# This script uses disposable unittest state roots and never activates Phase 11D.
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"

python3 -m unittest \
  tests.control_plane.test_change_flow \
  tests.control_plane.test_subagents_async \
  tests.control_plane.test_secretary_work
node "$root/tests/observability-extension.test.mjs"
python3 "$root/bin/pi-control" --help >/dev/null
python3 "$root/bin/pi-control" review --help >/dev/null
python3 "$root/bin/pi-control" integration --help >/dev/null
python3 "$root/bin/pi-control" recovery --help >/dev/null
printf '%s\n' 'PASS non-live process/client review, integration, recovery, continuity, and Inspector reachability'
