#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d)
cleanup() { rm -rf "$temporary"; }
trap cleanup EXIT
mkdir -p "$temporary/npm" "$temporary/packages"
# Reproduce the disposable installer layout without touching checkout modules.
cp -a "$root/pi/packages/pi-sandbox-control" "$root/pi/packages/pi-subagents-control" "$temporary/packages/"
cp "$root/pi/npm/package.json" "$root/pi/npm/package-lock.json" "$root/pi/npm/.npmrc" "$temporary/npm/"
# Always verify patches against a clean lockfile install. Reusing the checkout's
# node_modules can silently test an obsolete, previously patched generation.
npm ci --prefix "$temporary/npm" --install-links --legacy-peer-deps --no-audit --no-fund >/dev/null
[[ -d "$temporary/npm/node_modules/pi-subagents" && ! -L "$temporary/npm/node_modules/pi-subagents" ]]
[[ -d "$temporary/npm/node_modules/pi-sandbox-control" && ! -L "$temporary/npm/node_modules/pi-sandbox-control" ]]
node -e 'require.resolve("yaml", { paths: [process.argv[1]] })' "$temporary/npm/node_modules/pi-subagents"
PI_CODING_AGENT_DIR="$temporary" "$root/scripts/pi-patch-subagents" >/dev/null
# The full patch chain must also accept its own final hashes on repeat runs.
PI_CODING_AGENT_DIR="$temporary" "$root/scripts/pi-patch-subagents" >/dev/null
PI_TEST_PACKAGE_ROOT="$temporary/npm" \
  "$temporary/npm/node_modules/.bin/jiti" "$root/tests/pi-candidate-worktrees.mjs"
PI_TEST_PACKAGE_ROOT="$temporary/npm" \
  "$temporary/npm/node_modules/.bin/jiti" "$root/tests/pi-child-lease.mjs"
if [[ -f "$temporary/npm/node_modules/@kjrjay/pi-sandbox/index.ts" ]]; then
  PI_TEST_PACKAGE_ROOT="$temporary/npm" \
    node "$root/tests/pi-sandbox-source-regressions.mjs"
else
  printf '%s\n' 'SKIP legacy pi-sandbox source regressions: first-party package is authoritative'
fi
PI_TEST_PACKAGE_ROOT="$temporary/npm" \
  "$temporary/npm/node_modules/.bin/jiti" "$root/tests/pi-session-lease.mjs"
