#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${DOTFILES_SYNC_REPO_DIR:-$HOME/dotfiles}"
BRANCH="${DOTFILES_SYNC_BRANCH:-master}"
LOCK_DIR="${DOTFILES_SYNC_LOCK_DIR:-/tmp/dotfiles-sync.lockdir}"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  exit 0
fi
trap 'rmdir "$LOCK_DIR"' EXIT

cd "$REPO_DIR"
if [[ -n "$(git status --porcelain)" ]]; then
  printf 'dotfiles-sync: refusing dirty checkout\n' >&2
  exit 1
fi
current="$(git symbolic-ref --quiet --short HEAD)"
if [[ "$current" != "$BRANCH" ]]; then
  printf 'dotfiles-sync: refusing branch %s; expected %s\n' "$current" "$BRANCH" >&2
  exit 1
fi
git pull --ff-only origin "$BRANCH"
