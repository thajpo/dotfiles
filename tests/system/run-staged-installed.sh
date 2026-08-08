#!/usr/bin/env bash
set -u
root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
node "$root/tests/pi-subagents-control-provenance.mjs" || exit $?
node "$root/tests/pi-sandbox-control-manifest.test.mjs" || exit $?
if ! command -v npm >/dev/null 2>&1; then
  printf '%s\n' 'STOP/77: npm is unavailable for staged installation' >&2
  exit 77
fi
pi_bin=${PI_SYSTEM_PI_EXECUTABLE:-${PI_CORE_DIR:-$HOME/.local/share/pi/core}/node_modules/.bin/pi}
if [[ ! -x "$pi_bin" ]]; then
  printf '%s\n' 'STOP/77: pinned Pi executable is unavailable for loaded-byte proof' >&2
  exit 77
fi
expected_version=$(<"$root/pi/PI_VERSION")
if [[ "$($pi_bin --version 2>/dev/null || true)" != "$expected_version" ]]; then
  printf '%s\n' 'STOP/77: pinned Pi version does not match the reviewed version' >&2
  exit 77
fi
stage_cleanup=""
if [[ -z "${PI_SYSTEM_STAGED_ROOT:-}" ]]; then
  stage_cleanup=$(mktemp -d)
  staged_info="$stage_cleanup/info.json"
  PYTHONDONTWRITEBYTECODE=1 python3 -m tests.system.staged_install --output-root "$stage_cleanup/stage" >"$staged_info" || exit $?
  PI_SYSTEM_STAGED_ROOT="$stage_cleanup/stage"
  export PI_SYSTEM_STAGED_ROOT
  PI_SYSTEM_LOADED_BUILD_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["buildId"])' "$staged_info")
  export PI_SYSTEM_LOADED_BUILD_ID
  trap 'rm -rf "$stage_cleanup"' EXIT
fi
PYTHONDONTWRITEBYTECODE=1 python3 -m tests.system.staged_proof --root "$PI_SYSTEM_STAGED_ROOT" || exit $?
proof_root=$(mktemp -d)
journey_root=$(mktemp -d)
trap 'rm -rf "$proof_root" "$journey_root" "$stage_cleanup"' EXIT
mkdir -p "$proof_root/home" "$proof_root/config" "$proof_root/state" "$proof_root/runtime" "$proof_root/agent"
chmod 700 "$proof_root" "$proof_root/home" "$proof_root/config" "$proof_root/state" "$proof_root/runtime" "$proof_root/agent"
HOME="$proof_root/home" XDG_CONFIG_HOME="$proof_root/config" XDG_STATE_HOME="$proof_root/state" XDG_RUNTIME_DIR="$proof_root/runtime" PI_CODING_AGENT_DIR="$proof_root/agent" PI_SYSTEM_STAGED_ROOT="$PI_SYSTEM_STAGED_ROOT" "$pi_bin" --help >/dev/null || exit $?
for attempt in 1 2; do
  evidence="$journey_root/run-$attempt"
  mkdir -p "$evidence"
  PI_SYSTEM_PROCESS_FIXTURE=1 PI_SYSTEM_EVIDENCE_DIR="$evidence" PI_SYSTEM_STAGED_ROOT="$PI_SYSTEM_STAGED_ROOT" PI_SYSTEM_BUILD_ID="${PI_SYSTEM_LOADED_BUILD_ID:-staged-external}" bash tests/system/run-process-fixture.sh --group launch-session-presentation || exit $?
done
exit 0
