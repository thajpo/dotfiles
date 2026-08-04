#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d)
cleanup() { rm -rf "$temporary"; }
trap cleanup EXIT
mkdir -p "$temporary/npm"
cp "$root/pi/npm/package.json" "$root/pi/npm/package-lock.json" "$temporary/npm/"
# Always verify patches against a clean lockfile install. Reusing the checkout's
# node_modules can silently test an obsolete, previously patched generation.
npm ci --prefix "$temporary/npm" --legacy-peer-deps --no-audit --no-fund >/dev/null
PI_CODING_AGENT_DIR="$temporary" "$root/scripts/pi-patch-subagents" >/dev/null
PI_TEST_PACKAGE_ROOT="$temporary/npm" \
  "$temporary/npm/node_modules/.bin/jiti" "$root/tests/pi-candidate-worktrees.mjs"
PI_TEST_PACKAGE_ROOT="$temporary/npm" \
  "$temporary/npm/node_modules/.bin/jiti" "$root/tests/pi-child-lease.mjs"
PI_TEST_PACKAGE_ROOT="$temporary/npm" \
  "$temporary/npm/node_modules/.bin/jiti" "$root/tests/pi-session-lease.mjs"
