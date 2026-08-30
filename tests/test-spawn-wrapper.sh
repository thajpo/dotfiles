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
    if [[ "${2:-}" == start && "${3:-}" == --help ]]; then
      exit 0
    fi
    if [[ "${2:-}" == list ]]; then
      printf '{"result":{"agents":[{"agent_name":"existing-worker","cwd":"%s"}]}}\n' "$PWD"
    else
      printf '{"result":{"agent":{"agent_name":"%s"}}}\n' "${3:-unknown}"
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
export FAKE_WORKTREE="$tmp/worktree"
export PATH="$fake_bin:$PATH"
base_ref=$(git -C "$root" branch --show-current)

doctor_output=$(HERDR_ENV=1 "$tmp/spawn" --doctor 2>&1)
grep -Fq 'launcher: '"$root"'/bin/spawn' <<<"$doctor_output"
grep -Fq 'wrapper: ok (symlink-resolved)' <<<"$doctor_output"
grep -Fq 'agent API route: ok' <<<"$doctor_output"
! grep -Fq 'worktree create' "$FAKE_LOG"
! grep -Fq 'agent start codex ' "$FAKE_LOG"

: > "$FAKE_LOG"
spawn_output=$(HERDR_ENV=1 "$tmp/spawn" --base "$base_ref" -k codex -b feature/test-worker 'test prompt' 2>&1)
grep -Fq "base: $base_ref" <<<"$spawn_output"
grep -Fq 'branch: feature/test-worker' <<<"$spawn_output"
grep -Fq 'existing agents in repository: existing-worker' <<<"$spawn_output"
grep -Fq "resolved base=$base_ref branch=feature/test-worker pane=w-test:p1 worktree=" <<<"$spawn_output"
grep -Fq -- "--base $base_ref" "$FAKE_LOG"
grep -Fq 'agent start feature-test-worker --kind codex --pane w-test:p1' "$FAKE_LOG"
! grep -Fq 'plugin args: --base' "$FAKE_LOG"

printf 'spawn wrapper tests: ok\n'
