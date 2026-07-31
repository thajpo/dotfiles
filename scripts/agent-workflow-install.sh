#!/usr/bin/env bash
set -euo pipefail

DOTFILES_DIR="${DOTFILES_DIR:-$HOME/dotfiles}"
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$HOME/go/bin:$HOME/.npm-global/bin:$HOME/.bun/bin:$PATH"

link_file() {
  local source_path="$1"
  local target_path="$2"

  mkdir -p "$(dirname "$target_path")"

  if [ -L "$target_path" ] && [ "$(readlink "$target_path")" = "$source_path" ]; then
    printf 'ok: %s -> %s\n' "$target_path" "$source_path"
    return
  fi

  if [ -e "$target_path" ] || [ -L "$target_path" ]; then
    local backup_path="${target_path}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
    mv "$target_path" "$backup_path"
    printf 'backup: %s -> %s\n' "$target_path" "$backup_path"
  fi

  ln -s "$source_path" "$target_path"
  printf 'link: %s -> %s\n' "$target_path" "$source_path"
}

link_file "$DOTFILES_DIR/agent/AGENTS.md" "$HOME/.config/opencode/AGENTS.md"
link_file "$DOTFILES_DIR/agent/AGENTS.md" "$HOME/.claude/CLAUDE.md"
link_file "$DOTFILES_DIR/agent/AGENTS.md" "$HOME/.codex/AGENTS.md"
link_file "$DOTFILES_DIR/opencode/opencode.jsonc" "$HOME/.config/opencode/opencode.jsonc"

mkdir -p "$DOTFILES_DIR/skills" "$HOME/.config/opencode" "$HOME/.codex"
link_file "$DOTFILES_DIR/skills" "$HOME/.skills"
link_file "$HOME/.skills" "$HOME/.config/opencode/skills"
link_file "$HOME/.skills" "$HOME/.codex/skills"
# Do not populate ~/.agents/skills: Pi scans that global Codex-compatible path
# too, so duplicating project-status there creates a name collision. The
# shared ~/.codex/skills link above already exposes it to Codex.

if [[ $# -ne 0 ]]; then
  printf 'usage: %s\n' "$0" >&2
  printf 'Legacy coding-agent orchestrator installation has been removed.\n' >&2
  exit 2
fi

printf '\nDone. Pi uses pi-subagents; legacy orchestrators remain dormant.\n'
