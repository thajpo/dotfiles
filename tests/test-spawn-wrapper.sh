#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d "${TMPDIR:-/tmp}/spawn-wrapper-test.XXXXXX")
trap 'rm -rf -- "$tmp"' EXIT

fake_bin="$tmp/bin"
plugin_root="$tmp/plugin"
config_dir="$tmp/config"
mkdir -p "$fake_bin" "$plugin_root" "$config_dir"

cat > "$fake_bin/herdr" <<'FAKE_HERDR'
#!/usr/bin/env bash
set -u

printf '%q ' "$@" >> "$FAKE_LOG"
printf '\n' >> "$FAKE_LOG"

case "${1:-}" in
  plugin)
    case "${2:-}" in
      list)
        printf '{"result":{"plugins":[{"plugin_id":"herdr-spawn","enabled":true,"plugin_root":"%s"}]}}\n' "$FAKE_PLUGIN_ROOT"
        ;;
      config-dir)
        printf '%s\n' "$FAKE_CONFIG_DIR"
        ;;
    esac
    ;;
  agent)
    case "${2:-}" in
      start)
        if [[ "${3:-}" == --help ]]; then
          exit 0
        fi
        if [ "${FAKE_AGENT_START_FAIL:-0}" -eq 1 ]; then
          printf '{"error":{"code":"agent_start_failed"}}\n' >&2
          exit 23
        fi
        printf '{"result":{"agent":{"name":"%s","pane_id":"w-test:p1"}}}\n' "${3:-unknown}"
        ;;
      list)
        printf '{"result":{"agents":[{"name":"existing-worker","agent":"codex","cwd":"%s","pane_id":"w-other:p1"}]}}\n' "$PWD"
        ;;
      get)
        target=${3:-}
        if [ "$target" = w-source:p9 ]; then
          count=0
          [ ! -f "$FAKE_SOURCE_GET_COUNT" ] || count=$(<"$FAKE_SOURCE_GET_COUNT")
          count=$((count + 1))
          printf '%s\n' "$count" > "$FAKE_SOURCE_GET_COUNT"
          session=session-source-1
          if [ "${FAKE_CHANGE_SOURCE_SESSION:-0}" -eq 1 ] && [ "$count" -gt 1 ]; then
            session=session-source-2
          fi
          if [ "${FAKE_SOURCE_UNNAMED:-0}" -eq 1 ]; then
            printf '{"result":{"agent":{"agent":"codex","terminal_title_stripped":"Dreamer terminal / main!","pane_id":"w-source:p9","agent_session":{"source":"herdr:codex","kind":"id","value":"%s"}}}}\n' "$session"
          else
            printf '{"result":{"agent":{"agent":"codex","name":"dreamer.main","terminal_title_stripped":"ignored terminal title","pane_id":"w-source:p9","agent_session":{"source":"herdr:codex","kind":"id","value":"%s"}}}}\n' "$session"
          fi
        elif [ "$target" = w-test:p1 ]; then
          printf '{"result":{"agent":{"agent":"codex","name":"feature-test-worker","pane_id":"w-test:p1","agent_session":{"source":"herdr:codex","kind":"id","value":"session-worker-1"}}}}\n'
        else
          printf '{"error":{"code":"agent_not_found","message":"%s"}}\n' "$target" >&2
          exit 1
        fi
        ;;
    esac
    ;;
  pane)
    if [[ "${2:-}" == report-metadata ]]; then
      if [ "${FAKE_METADATA_FAIL_TARGET:-}" = "${3:-}" ]; then
        printf '{"error":{"code":"metadata_failed"}}\n' >&2
        exit 24
      fi
      printf '{"result":{"pane_id":"%s"}}\n' "${3:-unknown}"
    fi
    ;;
  worktree)
    printf '{"result":{"root_pane":{"pane_id":"w-test:p1"},"workspace":{"workspace_id":"w-test"},"worktree":{"path":"%s"}}}\n' "$FAKE_WORKTREE"
    ;;
esac
FAKE_HERDR
chmod +x "$fake_bin/herdr"

cat > "$plugin_root/spawn.sh" <<'FAKE_PLUGIN'
#!/usr/bin/env bash
set -euo pipefail
base=""
if [[ -n "${HERDR_PLUGIN_CONFIG_DIR:-}" && -f "$HERDR_PLUGIN_CONFIG_DIR/config" ]]; then
  . "$HERDR_PLUGIN_CONFIG_DIR/config"
fi
printf 'plugin args:' >> "$FAKE_LOG"
printf ' %q' "$@" >> "$FAKE_LOG"
printf '\n' >> "$FAKE_LOG"
"$HERDR_BIN_PATH" worktree create --cwd "$PWD" --branch feature/test-worker --base "$base" --json
"$HERDR_BIN_PATH" agent start codex --kind codex --pane w-test:p1
FAKE_PLUGIN
chmod +x "$plugin_root/spawn.sh"
touch "$plugin_root/lib.sh"
ln -s "$root/bin/spawn" "$tmp/spawn"

export FAKE_CONFIG_DIR="$config_dir"
export FAKE_LOG="$tmp/herdr.log"
export FAKE_PLUGIN_ROOT="$plugin_root"
export FAKE_SOURCE_GET_COUNT="$tmp/source-get-count"
export FAKE_WORKTREE="$tmp/worktree"
export PATH="$fake_bin:$PATH"
base_ref=$(git -C "$root" branch --show-current)

assert_logged() {
  local expected
  printf -v expected '%q ' "$@"
  grep -Fxq "$expected" "$FAKE_LOG"
}

reset_fake() {
  : > "$FAKE_LOG"
  rm -f -- "$FAKE_SOURCE_GET_COUNT"
  unset FAKE_AGENT_START_FAIL FAKE_CHANGE_SOURCE_SESSION FAKE_METADATA_FAIL_TARGET FAKE_SOURCE_UNNAMED
}

doctor_output=$(HERDR_ENV=1 "$tmp/spawn" --doctor 2>&1)
grep -Fq 'launcher: '"$root"'/bin/spawn' <<<"$doctor_output"
grep -Fq 'wrapper: ok (symlink-resolved)' <<<"$doctor_output"
grep -Fq 'agent API route: ok' <<<"$doctor_output"
! grep -Fq 'worktree create' "$FAKE_LOG"
! grep -Fq 'report-metadata' "$FAKE_LOG"

reset_fake
spawn_output=$(HERDR_ENV=1 HERDR_PANE_ID=w-source:p9 HERDR_ACTIVE_PANE_ID=w-wrong:p1 "$tmp/spawn" \
  --base "$base_ref" \
  --cohort Dreamer \
  --task capacity-profile-review \
  -k codex \
  -b feature/test-worker \
  'test prompt' 2>&1)
grep -Fq "base: $base_ref" <<<"$spawn_output"
grep -Fq 'branch: feature/test-worker' <<<"$spawn_output"
grep -Fq 'cohort: Dreamer' <<<"$spawn_output"
grep -Fq 'task: capacity-profile-review' <<<"$spawn_output"
grep -Fq 'existing agents in repository: existing-worker' <<<"$spawn_output"
grep -Fq "resolved base=$base_ref branch=feature/test-worker pane=w-test:p1 worktree=" <<<"$spawn_output"
grep -Fq 'coordination cohort=Dreamer coordinator=w-source:p9/session-source-1 worker=w-test:p1/session-worker-1 task=capacity-profile-review' <<<"$spawn_output"
assert_logged agent get w-source:p9
assert_logged agent get w-test:p1
! grep -Fq 'agent get w-wrong:p1' "$FAKE_LOG"
assert_logged pane report-metadata w-test:p1 \
  --source dotfiles:spawn-coordination \
  --agent codex \
  --applies-to-source herdr:codex \
  --display-agent 'Dreamer · worker' \
  --title capacity-profile-review
assert_logged pane report-metadata w-source:p9 \
  --source dotfiles:spawn-coordination \
  --agent codex \
  --applies-to-source herdr:codex \
  --display-agent 'Dreamer · coordinator' \
  --title dreamer.main
assert_logged agent start feature-test-worker --kind codex --pane w-test:p1 \
  -- --sandbox workspace-write --ask-for-approval on-request
grep -Fq -- "--base $base_ref" "$FAKE_LOG"
! grep -Fq 'plugin args: --base' "$FAKE_LOG"
! grep -Fq 'plugin args: --cohort' "$FAKE_LOG"
! grep -Fq 'plugin args: --task' "$FAKE_LOG"

# An unnamed source uses its nonempty stripped terminal title as the
# coordinator identity. The explicit cohort remains the only source of role
# membership; neither the title nor a workspace name becomes a cohort.
reset_fake
export FAKE_SOURCE_UNNAMED=1
unnamed_output=$(HERDR_ENV=1 HERDR_PANE_ID=w-source:p9 "$tmp/spawn" \
  --base "$base_ref" --cohort Dreamer --task capacity-profile-review \
  -k codex -b feature/test-worker 'unnamed source' 2>&1)
grep -Fq 'coordination cohort=Dreamer' <<<"$unnamed_output"
assert_logged pane report-metadata w-source:p9 \
  --source dotfiles:spawn-coordination \
  --agent codex \
  --applies-to-source herdr:codex \
  --display-agent 'Dreamer · coordinator' \
  --title 'Dreamer terminal / main!'

# Membership is never inferred when no cohort was supplied.
reset_fake
if HERDR_ENV=1 HERDR_PANE_ID=w-source:p9 "$tmp/spawn" \
  --base "$base_ref" -k codex -b feature/test-worker \
  'missing cohort' >"$tmp/missing-cohort.out" 2>&1; then
  printf 'expected missing cohort to return nonzero\n' >&2
  exit 1
fi
grep -Fq -- '--cohort is required' "$tmp/missing-cohort.out"
! grep -Fq 'worktree create' "$FAKE_LOG"
! grep -Fq 'report-metadata' "$FAKE_LOG"

# Labels remain argv-safe when they contain whitespace and punctuation.
reset_fake
punctuation_output=$(HERDR_ENV=1 HERDR_PANE_ID=w-source:p9 "$tmp/spawn" \
  --base "$base_ref" \
  --cohort 'Dream Team / R&D!' \
  --task 'review parser: spaces & punctuation!' \
  -k codex \
  -b feature/test-worker \
  'test punctuation' 2>&1)
grep -Fq 'cohort=Dream Team / R&D!' <<<"$punctuation_output"
grep -Fq 'task=review parser: spaces & punctuation!' <<<"$punctuation_output"
assert_logged pane report-metadata w-test:p1 \
  --source dotfiles:spawn-coordination \
  --agent codex \
  --applies-to-source herdr:codex \
  --display-agent 'Dream Team / R&D! · worker' \
  --title 'review parser: spaces & punctuation!'
assert_logged pane report-metadata w-source:p9 \
  --source dotfiles:spawn-coordination \
  --agent codex \
  --applies-to-source herdr:codex \
  --display-agent 'Dream Team / R&D! · coordinator' \
  --title dreamer.main

# A failed agent start must not mark either pane.
reset_fake
export FAKE_AGENT_START_FAIL=1
if HERDR_ENV=1 HERDR_PANE_ID=w-source:p9 "$tmp/spawn" \
  --base "$base_ref" --cohort Dreamer -k codex -b feature/test-worker \
  'expected failure' >"$tmp/failed-spawn.out" 2>&1; then
  printf 'expected failed spawn to return nonzero\n' >&2
  exit 1
fi
! grep -Fq 'report-metadata' "$FAKE_LOG"

# If the worker write succeeds but the coordinator write fails, the launcher
# clears only the worker presentation and returns nonzero.
reset_fake
export FAKE_METADATA_FAIL_TARGET=w-source:p9
if HERDR_ENV=1 HERDR_PANE_ID=w-source:p9 "$tmp/spawn" \
  --base "$base_ref" --cohort Dreamer --task capacity-profile-review \
  -k codex -b feature/test-worker 'coordinator metadata failure' \
  >"$tmp/coordinator-metadata-failure.out" 2>&1; then
  printf 'expected coordinator metadata failure to return nonzero\n' >&2
  exit 1
fi
grep -Fq 'coordinator coordination metadata failed; worker metadata was cleared' \
  "$tmp/coordinator-metadata-failure.out"
assert_logged pane report-metadata w-test:p1 \
  --source dotfiles:spawn-coordination \
  --agent codex \
  --applies-to-source herdr:codex \
  --clear-display-agent --clear-title
[ "$(grep -Fc -- '--clear-display-agent' "$FAKE_LOG")" -eq 1 ]
! grep -F -- 'pane report-metadata w-source:p9' "$FAKE_LOG" \
  | grep -Fq -- '--clear-display-agent'

# If the exact source session changes during spawn, neither pane is decorated.
reset_fake
export FAKE_CHANGE_SOURCE_SESSION=1
if HERDR_ENV=1 HERDR_PANE_ID=w-source:p9 "$tmp/spawn" \
  --base "$base_ref" --cohort Dreamer -k codex -b feature/test-worker \
  'session race' >"$tmp/session-race.out" 2>&1; then
  printf 'expected changed source session to return nonzero\n' >&2
  exit 1
fi
grep -Fq 'source agent session changed during spawn' "$tmp/session-race.out"
! grep -Fq 'report-metadata' "$FAKE_LOG"

# The tracked layout uses guarded built-ins. Ordinary agents therefore keep a
# state indicator and detected agent label; without a reported title, row two
# naturally disappears. No unguarded custom token can invent a role.
grep -Fq '["state_icon", "agent"]' "$root/herdr/coordination-sidebar.toml"
grep -Fq '["pane"]' "$root/herdr/coordination-sidebar.toml"
! grep -Fq '"$' "$root/herdr/coordination-sidebar.toml"

printf 'spawn wrapper tests: ok\n'
