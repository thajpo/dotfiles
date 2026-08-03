#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_modules="$root/pi/npm/node_modules"
if [[ ! -x "$source_modules/.bin/jiti" ]]; then
  npm ci --prefix "$root/pi/npm" --legacy-peer-deps --no-audit --no-fund
fi

temporary=$(mktemp -d)
cleanup() { rm -rf "$temporary"; }
trap cleanup EXIT
mkdir -p "$temporary/npm"
cp -a "$source_modules" "$temporary/npm/node_modules"
PI_CODING_AGENT_DIR="$temporary" "$root/scripts/pi-patch-subagents" >/dev/null
PI_TEST_PACKAGE_ROOT="$temporary/npm" \
  "$temporary/npm/node_modules/.bin/jiti" "$root/tests/pi-candidate-worktrees.mjs"
PI_TEST_PACKAGE_ROOT="$temporary/npm" \
  "$temporary/npm/node_modules/.bin/jiti" "$root/tests/pi-child-lease.mjs"
PI_TEST_PACKAGE_ROOT="$temporary/npm" \
  "$temporary/npm/node_modules/.bin/jiti" "$root/tests/pi-session-lease.mjs"
