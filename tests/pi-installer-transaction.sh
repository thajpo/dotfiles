#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
node_bin_dir=$(CDPATH= cd -- "$(dirname -- "$(command -v node)")" && pwd)
[[ -d "$root/pi/npm/node_modules" ]] || npm ci --prefix "$root/pi/npm" --legacy-peer-deps --no-audit --no-fund

temporary=$(mktemp -d)
cleanup() { rm -rf "$temporary"; }
trap cleanup EXIT
core_fixture="$temporary/core-fixture"
npm install --prefix "$core_fixture" --no-save --no-package-lock --no-audit --no-fund \
  @earendil-works/pi-coding-agent@0.83.0 >/dev/null
PI_CORE_DIR="$core_fixture" "$root/scripts/pi-patch-core" >/dev/null
chmod -R go-w "$core_fixture"

prepare_home() {
  local case_root=$1
  local home=$case_root/home
  local fake=$case_root/bin
  mkdir -p "$home/.local/share/pi" "$home/.config/pi" \
    "$home/Projects" "$home/.pi/agent/npm" "$case_root/worktrees" "$fake"
  cp -a "$core_fixture" "$home/.local/share/pi/core"
  printf 'old-settings\n' > "$home/.pi/agent/settings.json"
  printf 'old-npm\n' > "$home/.pi/agent/npm/sentinel"
  python3 - "$home/.config/pi/repository-policy.json" "$case_root/worktrees" "$root" <<'PY'
from pathlib import Path
import json, sys
repository = str(Path(sys.argv[3]).resolve())
Path(sys.argv[1]).write_text(json.dumps({
    "version": 1,
    "defaultMode": "isolated",
    "trustedRoots": [repository],
    "isolatedRoots": [],
    "controlPlaneRepositories": [repository],
    "protectedBranches": ["main", "master"],
    "worktreeRoot": sys.argv[2],
}))
Path(sys.argv[1]).chmod(0o600)
PY
  printf 'sha256:old-image\n' > "$case_root/image.state"
  cat > "$fake/docker" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$PI_TEST_DOCKER_LOG"
image_state=${PI_TEST_IMAGE_STATE:-"${PI_TEST_DOCKER_LOG%/*}/image.state"}
case "$1 $2" in
  "info "|"build --pull=false") exit 0 ;;
  "image inspect") cat "$image_state"; exit 0 ;;
  "image tag")
    [ "${PI_TEST_DOCKER_FAIL:-0}" != 1 ] || exit 42
    if [ "$3" = sha256:old-image ]; then printf 'sha256:old-image\n' > "$image_state"
    else printf 'sha256:new-image\n' > "$image_state"
    fi
    if [ "${PI_TEST_DOCKER_INTERRUPT:-0}" = 1 ] && [ ! -e "$PI_TEST_INTERRUPT_MARKER" ]; then
      : > "$PI_TEST_INTERRUPT_MARKER"
      kill -TERM "$PPID"
    fi
    exit 0
    ;;
  "image rm") exit 0 ;;
esac
exit 0
SH
  cat > "$fake/npm" <<SH
#!/bin/sh
set -eu
mode=\$1
prefix=''
while [ \$# -gt 0 ]; do
  if [ "\$1" = --prefix ]; then prefix=\$2; shift 2; else shift; fi
done
[ -n "\$prefix" ]
rm -rf "\$prefix/node_modules"
if [ "\$mode" = install ]; then
  cp -a "$core_fixture/node_modules" "\$prefix/node_modules"
else
  cp -a "$root/pi/npm/node_modules" "\$prefix/node_modules"
  # A clean lockfile install no longer contains the removed legacy sandbox.
  rm -rf "\$prefix/node_modules/@kjrjay/pi-sandbox" "\$prefix/node_modules/pi-sandbox-control" "\$prefix/node_modules/pi-subagents"
  cp -a "\$prefix/../packages/pi-sandbox-control" "\$prefix/node_modules/pi-sandbox-control"
  cp -a "\$prefix/../packages/pi-subagents-control" "\$prefix/node_modules/pi-subagents"
fi
SH
  cat > "$fake/mv" <<'SH'
#!/bin/sh
eval "destination=\${$#}"
if [ "${PI_TEST_MV_FAIL:-0}" = 1 ] && [ ! -e "$PI_TEST_INTERRUPT_MARKER" ]; then
  case "$destination" in
    */settings.json.rollback.*) : > "$PI_TEST_INTERRUPT_MARKER"; exit 42 ;;
  esac
fi
/bin/mv "$@"
if [ "${PI_TEST_INTERRUPT:-0}" = 1 ] && [ ! -e "$PI_TEST_INTERRUPT_MARKER" ]; then
  case "${PI_TEST_INTERRUPT_MATCH:-target}:$destination" in
    backup:*/settings.json.rollback.*|target:*/settings.json)
      : > "$PI_TEST_INTERRUPT_MARKER"
      kill -TERM "$PPID"
      ;;
  esac
fi
SH
  chmod 755 "$fake/docker" "$fake/npm" "$fake/mv"
}

assert_files_restored() {
  local case_root=$1
  local home=$case_root/home
  [[ $(cat "$home/.pi/agent/settings.json") == old-settings ]]
  [[ $(cat "$home/.pi/agent/npm/sentinel") == old-npm ]]
  [[ ! -e "$home/.pi/agent/extensions" ]]
  [[ ! -e "$home/.local/share/pi/control/pi-workspace.py" ]]
}

assert_restored() {
  local case_root=$1
  assert_files_restored "$case_root"
  [[ $(cat "$case_root/image.state") == sha256:old-image ]]
  grep -Fq 'image tag sha256:old-image pi-tool-sandbox:node22-bookworm-20260728' "$case_root/docker.log"
}

failure_case=$temporary/failure
prepare_home "$failure_case"
printf 'block launcher directory\n' > "$failure_case/home/.local/bin"
set +e
HOME="$failure_case/home" PATH="$failure_case/bin:$node_bin_dir:/usr/local/bin:/usr/bin:/bin" \
  PI_HARNESS_ONLY=1 PI_TEST_DOCKER_LOG="$failure_case/docker.log" \
  "$root/install.sh" >"$failure_case/stdout" 2>"$failure_case/stderr"
failure_status=$?
set -e
[[ $failure_status -ne 0 ]]
assert_restored "$failure_case"
[[ $(cat "$failure_case/home/.local/bin") == 'block launcher directory' ]]

signal_case=$temporary/signal
prepare_home "$signal_case"
mkdir -p "$signal_case/home/.local/bin"
set +e
HOME="$signal_case/home" PATH="$signal_case/bin:$node_bin_dir:/usr/local/bin:/usr/bin:/bin" \
  PI_HARNESS_ONLY=1 PI_TEST_DOCKER_LOG="$signal_case/docker.log" PI_TEST_INTERRUPT=1 \
  PI_TEST_INTERRUPT_MATCH=backup PI_TEST_INTERRUPT_MARKER="$signal_case/interrupted" "$root/install.sh" >"$signal_case/stdout" 2>"$signal_case/stderr"
signal_status=$?
set -e
[[ $signal_status -eq 143 ]]
assert_restored "$signal_case"

backup_failure_case=$temporary/backup-failure
prepare_home "$backup_failure_case"
mkdir -p "$backup_failure_case/home/.local/bin"
set +e
HOME="$backup_failure_case/home" PATH="$backup_failure_case/bin:$node_bin_dir:/usr/local/bin:/usr/bin:/bin" \
  PI_HARNESS_ONLY=1 PI_TEST_DOCKER_LOG="$backup_failure_case/docker.log" PI_TEST_MV_FAIL=1 \
  PI_TEST_INTERRUPT_MARKER="$backup_failure_case/failed" "$root/install.sh" \
  >"$backup_failure_case/stdout" 2>"$backup_failure_case/stderr"
backup_failure_status=$?
set -e
[[ $backup_failure_status -ne 0 ]]
assert_restored "$backup_failure_case"

image_failure_case=$temporary/image-failure
prepare_home "$image_failure_case"
mkdir -p "$image_failure_case/home/.local/bin"
set +e
HOME="$image_failure_case/home" PATH="$image_failure_case/bin:$node_bin_dir:/usr/local/bin:/usr/bin:/bin" \
  PI_HARNESS_ONLY=1 PI_TEST_DOCKER_LOG="$image_failure_case/docker.log" PI_TEST_DOCKER_FAIL=1 \
  "$root/install.sh" >"$image_failure_case/stdout" 2>"$image_failure_case/stderr"
image_failure_status=$?
set -e
[[ $image_failure_status -ne 0 ]]
assert_files_restored "$image_failure_case"
[[ $(cat "$image_failure_case/image.state") == sha256:old-image ]]

image_signal_case=$temporary/image-signal
prepare_home "$image_signal_case"
mkdir -p "$image_signal_case/home/.local/bin"
set +e
HOME="$image_signal_case/home" PATH="$image_signal_case/bin:$node_bin_dir:/usr/local/bin:/usr/bin:/bin" \
  PI_HARNESS_ONLY=1 PI_TEST_DOCKER_LOG="$image_signal_case/docker.log" PI_TEST_DOCKER_INTERRUPT=1 \
  PI_TEST_INTERRUPT_MARKER="$image_signal_case/interrupted" "$root/install.sh" \
  >"$image_signal_case/stdout" 2>"$image_signal_case/stderr"
image_signal_status=$?
set -e
[[ $image_signal_status -eq 143 ]]
assert_restored "$image_signal_case"

unsafe_core_case=$temporary/unsafe-core
prepare_home "$unsafe_core_case"
mkdir -p "$unsafe_core_case/home/.local/bin"
chmod g+w "$unsafe_core_case/home/.local/share/pi/core/node_modules/.bin/pi"
HOME="$unsafe_core_case/home" PATH="$unsafe_core_case/bin:$node_bin_dir:/usr/local/bin:/usr/bin:/bin" \
  PI_HARNESS_ONLY=1 PI_TEST_DOCKER_LOG="$unsafe_core_case/docker.log" \
  "$root/install.sh" >"$unsafe_core_case/stdout" 2>"$unsafe_core_case/stderr"
python3 - "$unsafe_core_case/home/.local/share/pi/core" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
for entry in (root, *root.rglob("*")):
    if not entry.is_symlink() and entry.stat().st_mode & 0o022:
        raise SystemExit(1)
PY
grep -Fq 'Existing Pi core has unsafe ownership or writable modes' "$unsafe_core_case/stderr"

printf 'PASS installer transaction rollback, signal handling, and core hardening\n'
