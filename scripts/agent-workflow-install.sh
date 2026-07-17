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

install_tools() {
  if ! command -v npm >/dev/null 2>&1; then
    printf 'skip: npm not found; cannot install npm-based tools\n' >&2
  else
    npm install -g --prefix "$HOME/.npm-global" gnhf
  fi

  curl -fsSL https://kunchenguid.github.io/treehouse/install.sh | sh
  curl -fsSL https://raw.githubusercontent.com/kunchenguid/no-mistakes/main/docs/install.sh |
    NO_MISTAKES_LINK_DIR="$HOME/.local/bin" sh
}

clone_firstmate() {
  local target="${FIRSTMATE_DIR:-$HOME/agent-workflows/firstmate}"
  mkdir -p "$(dirname "$target")"

  if [ -d "$target/.git" ]; then
    git -C "$target" pull --ff-only
  else
    git clone https://github.com/kunchenguid/firstmate "$target"
  fi
}

link_file "$DOTFILES_DIR/agent/AGENTS.md" "$HOME/.config/opencode/AGENTS.md"
link_file "$DOTFILES_DIR/agent/AGENTS.md" "$HOME/.claude/CLAUDE.md"
link_file "$DOTFILES_DIR/agent/AGENTS.md" "$HOME/.codex/AGENTS.md"
link_file "$DOTFILES_DIR/opencode/opencode.jsonc" "$HOME/.config/opencode/opencode.jsonc"

mkdir -p "$DOTFILES_DIR/skills" "$HOME/.config/opencode" "$HOME/.codex"
link_file "$DOTFILES_DIR/skills" "$HOME/.skills"
link_file "$HOME/.skills" "$HOME/.config/opencode/skills"
link_file "$HOME/.skills" "$HOME/.codex/skills"

case "${1:-}" in
  --install-tools)
    install_tools
    ;;
  --with-firstmate)
    clone_firstmate
    ;;
  --all)
    install_tools
    clone_firstmate
    ;;
  "")
    ;;
  *)
    printf 'usage: %s [--install-tools|--with-firstmate|--all]\n' "$0" >&2
    exit 2
    ;;
esac

printf '\nDone. Run scripts/agent-workflow-doctor.sh to inspect the setup.\n'
