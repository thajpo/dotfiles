#!/usr/bin/env bash
set -u
root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  printf '%s\n' 'STOP/77: Docker daemon is unavailable' >&2
  exit 77
fi
image=${PI_CONTROL_PLANE_DOCKER_IMAGE:-pi-tool-sandbox:node22-bookworm-20260728}
if ! docker image inspect "$image" >/dev/null 2>&1; then
  printf '%s\n' 'STOP/77: disposable Docker image is unavailable' >&2
  exit 77
fi
PI_CONTROL_PLANE_DOCKER_IMAGE="$image" bash tests/pi-docker-control-plane-e2e.sh
exit $?
