#!/usr/bin/env bash
set -euo pipefail

DOTFILES_SKILLS="$HOME/dotfiles/skills"
SHARED_SKILLS="$HOME/.skills"

ensure_parent() {
  mkdir -p "$(dirname "$1")"
}

repair_path() {
  local path="$1"
  ensure_parent "$path"

  if [ -d "$path" ] && [ ! -L "$path" ]; then
    mkdir -p "$SHARED_SKILLS"
    rsync -a "$path"/ "$SHARED_SKILLS"/
    rm -rf "$path"
  fi

  ln -sfn "$SHARED_SKILLS" "$path"
}

mkdir -p "$DOTFILES_SKILLS"

if [ -d "$SHARED_SKILLS" ] && [ ! -L "$SHARED_SKILLS" ]; then
  rsync -a "$SHARED_SKILLS"/ "$DOTFILES_SKILLS"/
  rm -rf "$SHARED_SKILLS"
fi

ln -sfn "$DOTFILES_SKILLS" "$SHARED_SKILLS"

repair_path "$HOME/.omp/agent/skills"
repair_path "$HOME/.codex/skills"
repair_path "$HOME/.config/opencode/skills"
echo "skills-doctor: repaired symlinks and merged local skills into $DOTFILES_SKILLS"
