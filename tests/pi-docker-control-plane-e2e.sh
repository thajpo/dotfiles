#!/usr/bin/env bash
# Compatibility entrypoint for the production-path installed P5 journey.
set -u
root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec "$root/tests/system/run-docker.sh"
