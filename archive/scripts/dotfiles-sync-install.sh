#!/usr/bin/env bash
set -euo pipefail

DOTFILES_DIR="${DOTFILES_DIR:-$HOME/dotfiles}"
SYNC_SCRIPT="$DOTFILES_DIR/scripts/dotfiles-sync.sh"
[[ -f "$SYNC_SCRIPT" ]] || { printf 'error: sync script is missing: %s\n' "$SYNC_SCRIPT" >&2; exit 1; }

xml_escape() {
  local value="$1"
  value=${value//&/&amp;}
  value=${value//</&lt;}
  value=${value//>/&gt;}
  value=${value//\"/&quot;}
  value=${value//\'/&apos;}
  printf '%s' "$value"
}

case "$(uname -s)" in
  Darwin)
    command -v launchctl >/dev/null 2>&1 || { printf 'error: launchctl is required on macOS\n' >&2; exit 1; }
    label="com.user.dotfiles-sync"
    launch_agents="$HOME/Library/LaunchAgents"
    plist="$launch_agents/$label.plist"
    mkdir -p "$launch_agents"
    temporary="$plist.tmp.$$"
    escaped_sync_script="$(xml_escape "$SYNC_SCRIPT")"
    escaped_out="$(xml_escape "${TMPDIR:-/tmp}/dotfiles-sync.out")"
    escaped_err="$(xml_escape "${TMPDIR:-/tmp}/dotfiles-sync.err")"
    cat >"$temporary" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>$label</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>--noprofile</string>
      <string>--norc</string>
      <string>$escaped_sync_script</string>
    </array>
    <key>StartInterval</key>
    <integer>7200</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$escaped_out</string>
    <key>StandardErrorPath</key>
    <string>$escaped_err</string>
  </dict>
</plist>
EOF
    chmod 0600 "$temporary"
    mv -f "$temporary" "$plist"
    domain="gui/$(id -u)"
    launchctl bootout "$domain/$label" >/dev/null 2>&1 || true
    launchctl bootstrap "$domain" "$plist"
    printf 'installed: %s\n' "$plist"
    ;;
  Linux)
    command -v systemctl >/dev/null 2>&1 || { printf 'error: systemctl is required on Linux\n' >&2; exit 1; }
    unit_dir="$HOME/.config/systemd/user"
    mkdir -p "$unit_dir"
    cat >"$unit_dir/dotfiles-sync.service" <<EOF
[Unit]
Description=Dotfiles auto sync (pull, commit, push)

[Service]
Type=oneshot
ExecStart=/bin/bash --noprofile --norc $SYNC_SCRIPT
EOF
    cat >"$unit_dir/dotfiles-sync.timer" <<'EOF'
[Unit]
Description=Run dotfiles auto sync every 2 hours

[Timer]
OnBootSec=10m
OnUnitActiveSec=2h
Persistent=true
Unit=dotfiles-sync.service

[Install]
WantedBy=timers.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now dotfiles-sync.timer
    printf 'installed: %s\n' "$unit_dir/dotfiles-sync.timer"
    ;;
  *)
    printf 'error: unsupported operating system: %s\n' "$(uname -s)" >&2
    exit 1
    ;;
esac
