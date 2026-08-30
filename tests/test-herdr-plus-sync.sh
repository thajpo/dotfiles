#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
helper="$root/bin/herdr-plus-sync"
template="$root/herdr/herdr-plus/projects/dotfiles.toml"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/herdr-plus-sync-test.XXXXXX")
trap 'rm -rf -- "$tmp"' EXIT

# Validate the tracked TOML with the standard parser and assert the deliberately
# small layout. No worktree config is tracked, which avoids duplicate reactions
# with herdr-spawn and reviewr.
python3 - "$template" "$root/herdr/herdr-plus" <<'PY'
import pathlib
import sys
import tomllib

template = pathlib.Path(sys.argv[1])
config_root = pathlib.Path(sys.argv[2])
with template.open("rb") as handle:
    project = tomllib.load(handle)

assert project["name"] == "Dotfiles"
assert project["working_dir"] == "~/dotfiles"
assert project["tabs"] == [
    {"name": "codex", "command": "codex"},
    {"name": "shell"},
]
assert not (config_root / "worktrees").exists()
PY

fake_bin="$tmp/bin"
managed_dir="$tmp/managed config"
log="$tmp/herdr.log"
mkdir -p "$fake_bin" "$managed_dir/projects" "$managed_dir/quick-actions"

cat > "$fake_bin/herdr" <<'FAKE_HERDR'
#!/usr/bin/env bash
set -euo pipefail
printf '%q ' "$@" >> "$FAKE_HERDR_LOG"
printf '\n' >> "$FAKE_HERDR_LOG"

case "$*" in
  'plugin install cloudmanic/herdr-plus') ;;
  'plugin config-dir cloudmanic.herdr-plus') printf '%s\n' "$FAKE_CONFIG_DIR" ;;
  *) printf 'unexpected herdr arguments: %s\n' "$*" >&2; exit 90 ;;
esac
FAKE_HERDR
chmod +x "$fake_bin/herdr"

printf 'keep = true\n' > "$managed_dir/config.toml"
printf 'name = "Personal"\n' > "$managed_dir/projects/personal.toml"
printf 'name = "Keep Action"\ncommand = "true"\n' > "$managed_dir/quick-actions/keep.toml"
cp "$managed_dir/config.toml" "$tmp/config.before"
cp "$managed_dir/projects/personal.toml" "$tmp/personal.before"
cp "$managed_dir/quick-actions/keep.toml" "$tmp/action.before"

export FAKE_CONFIG_DIR="$managed_dir"
export FAKE_HERDR_LOG="$log"
export HERDR_BIN="$fake_bin/herdr"

# The install path uses the requested upstream source, discovers the managed
# directory, and writes only the owned project file.
HERDR_ENV=1 "$helper" install
cmp -s "$template" "$managed_dir/projects/dotfiles.toml"
grep -Fxq 'plugin install cloudmanic/herdr-plus ' "$log"
grep -Fxq 'plugin config-dir cloudmanic.herdr-plus ' "$log"
cmp -s "$tmp/config.before" "$managed_dir/config.toml"
cmp -s "$tmp/personal.before" "$managed_dir/projects/personal.toml"
cmp -s "$tmp/action.before" "$managed_dir/quick-actions/keep.toml"

# A second sync is a no-op even when the installed file is read-only, and check
# uses the same config-dir discovery without changing anything.
chmod 0444 "$managed_dir/projects/dotfiles.toml"
sync_output=$(HERDR_ENV=1 "$helper" sync)
grep -Fq 'already current:' <<<"$sync_output"
HERDR_ENV=1 "$helper" check
cmp -s "$template" "$managed_dir/projects/dotfiles.toml"

# A relative path from Herdr is rejected before any destination can be touched.
export FAKE_CONFIG_DIR='relative/config'
if HERDR_ENV=1 "$helper" sync >"$tmp/relative.out" 2>&1; then
  printf 'expected relative config path to be rejected\n' >&2
  exit 1
fi
grep -Fq 'managed config directory is not absolute' "$tmp/relative.out"

# A collision at the owned filename is preserved unless it carries the marker.
collision_dir="$tmp/collision"
mkdir -p "$collision_dir/projects"
printf 'name = "User Dotfiles"\n' > "$collision_dir/projects/dotfiles.toml"
cp "$collision_dir/projects/dotfiles.toml" "$tmp/collision.before"
export FAKE_CONFIG_DIR="$collision_dir"
if HERDR_ENV=1 "$helper" sync >"$tmp/collision.out" 2>&1; then
  printf 'expected unmanaged destination to be preserved\n' >&2
  exit 1
fi
grep -Fq 'refusing to overwrite unmanaged project' "$tmp/collision.out"
cmp -s "$tmp/collision.before" "$collision_dir/projects/dotfiles.toml"

# HERDR_ENV is a hard precondition and prevents even read-only Herdr discovery.
: > "$log"
if env -u HERDR_ENV "$helper" check >"$tmp/environment.out" 2>&1; then
  printf 'expected missing HERDR_ENV to be rejected\n' >&2
  exit 1
fi
grep -Fq 'HERDR_ENV=1 is required' "$tmp/environment.out"
[ ! -s "$log" ]

printf 'test-herdr-plus-sync: PASS\n'
