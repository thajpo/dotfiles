#!/usr/bin/env bash
# The fenced Pisec broker/worker stack is Linux-only.
set -euo pipefail
printf '%s\n' 'full Pisec installation currently requires Linux; no macOS Pisec services were changed' >&2
exit 1
