#!/usr/bin/env bash
set -euo pipefail

REPOSITORY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_PROFILE="$REPOSITORY/pisec/apparmor/pisec-bwrap"
PISEC_BWRAP_DIR=/usr/lib/pisec
PISEC_BWRAP="$PISEC_BWRAP_DIR/bwrap"
PISEC_PROFILE=/etc/apparmor.d/pisec-bwrap
GLOBAL_PROFILE=/etc/apparmor.d/bwrap-userns-restrict
GLOBAL_DISABLE_LINK=/etc/apparmor.d/disable/bwrap-userns-restrict

if [[ "$(uname -s)" != "Linux" ]]; then
  printf 'error: Pisec Linux prerequisites can only be installed on Linux\n' >&2
  exit 1
fi

if [[ "$EUID" -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || {
    printf 'error: sudo is required to enable the Bubblewrap AppArmor profile\n' >&2
    exit 1
  }
  exec sudo -- "$0" "$@"
fi

[[ -x /usr/bin/bwrap ]] || {
  printf 'error: Bubblewrap is missing: /usr/bin/bwrap\n' >&2
  exit 1
}
[[ -f "$SOURCE_PROFILE" ]] || {
  printf 'error: Pisec Bubblewrap AppArmor profile is missing: %s\n' "$SOURCE_PROFILE" >&2
  exit 1
}
command -v apparmor_parser >/dev/null 2>&1 || {
  printf 'error: apparmor_parser is missing; install apparmor-utils\n' >&2
  exit 1
}

install -d -m 0755 "$PISEC_BWRAP_DIR"
install -m 0755 /usr/bin/bwrap "$PISEC_BWRAP"
install -m 0644 "$SOURCE_PROFILE" "$PISEC_PROFILE"
apparmor_parser -r "$PISEC_PROFILE"

if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
  runuser -u "$SUDO_USER" -- "$PISEC_BWRAP" --unshare-all --ro-bind / / --dev /dev --proc /proc -- /bin/true
fi

# The distro-wide profile breaks Heroic/UMU pressure-vessel hard-link setup.
apparmor_parser -R "$GLOBAL_PROFILE" >/dev/null 2>&1 || true
install -d -m 0755 /etc/apparmor.d/disable
ln -sfn "$GLOBAL_PROFILE" "$GLOBAL_DISABLE_LINK"

printf 'enabled: %s for %s\n' "$PISEC_PROFILE" "$PISEC_BWRAP"
printf 'disabled: %s (global /usr/bin/bwrap profile)\n' "$GLOBAL_PROFILE"
