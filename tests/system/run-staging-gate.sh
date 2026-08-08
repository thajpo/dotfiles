#!/usr/bin/env bash
set -u
root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
bash "$root/tests/pi-docker-control-plane-e2e.sh"
exit $?
