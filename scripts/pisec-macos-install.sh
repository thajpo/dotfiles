#!/usr/bin/env bash
# The fenced Pisec broker/worker stack is Linux-only.
set -euo pipefail
if [[ "${1:-}" == "--probe-only" ]]; then
  exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pisec-macos-probe.sh"
fi
printf '%s\n' 'full Pisec installation currently requires Linux; no macOS Pisec services were changed' >&2
exit 1
