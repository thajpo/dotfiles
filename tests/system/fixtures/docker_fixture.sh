#!/usr/bin/env bash
# C10b preflight only. The fixture never targets unlabeled or live resources.
set -u
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  printf '%s\n' 'STOP/77: Docker daemon is unavailable' >&2
  exit 77
fi
image=${PI_CONTROL_PLANE_DOCKER_IMAGE:-pi-tool-sandbox:node22-bookworm-20260728}
if ! docker image inspect "$image" >/dev/null 2>&1; then
  printf '%s\n' 'STOP/77: labeled disposable Docker image is unavailable' >&2
  exit 77
fi
printf '%s\n' 'Docker disposable fixture prerequisites available; use run-docker.sh for the bounded proof.'
