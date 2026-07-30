#!/usr/bin/env bash
set -euo pipefail

status=0
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$HOME/go/bin:$HOME/.npm-global/bin:$HOME/.bun/bin:$PATH"

check_cmd() {
  local name="$1"
  local purpose="$2"

  if command -v "$name" >/dev/null 2>&1; then
    printf 'ok:      %-14s %s\n' "$name" "$(command -v "$name")"
  else
    printf 'missing: %-14s %s\n' "$name" "$purpose"
    status=1
  fi
}

check_path() {
  local path="$1"
  local expected="${2:-}"

  if [ -L "$path" ]; then
    local actual
    actual="$(readlink "$path")"
    if [ -z "$expected" ] || [ "$actual" = "$expected" ]; then
      printf 'ok:      %s -> %s\n' "$path" "$actual"
    else
      printf 'warn:    %s -> %s expected %s\n' "$path" "$actual" "$expected"
      status=1
    fi
  elif [ -e "$path" ]; then
    printf 'warn:    %s exists but is not a symlink\n' "$path"
    status=1
  else
    printf 'missing: %s\n' "$path"
    status=1
  fi
}

printf 'Agent workflow files\n'
check_path "$HOME/.config/opencode/AGENTS.md" "$HOME/dotfiles/agent/AGENTS.md"
check_path "$HOME/.claude/CLAUDE.md" "$HOME/dotfiles/agent/AGENTS.md"
check_path "$HOME/.codex/AGENTS.md" "$HOME/dotfiles/agent/AGENTS.md"
check_path "$HOME/.config/opencode/opencode.jsonc" "$HOME/dotfiles/opencode/opencode.jsonc"
check_path "$HOME/.skills" "$HOME/dotfiles/skills"
check_path "$HOME/.config/opencode/skills" "$HOME/.skills"
check_path "$HOME/.codex/skills" "$HOME/.skills"

printf '\nCore commands\n'
check_cmd git "required"
check_cmd tmux "terminal session management"
check_cmd gh "GitHub auth and PR workflow"
check_cmd pi "deterministic coding launcher"
check_cmd pidev "repo-scoped tmux Pi workspace launcher"
check_cmd pi-host "explicit unsandboxed maintenance launcher"

printf '\nPi orchestration\n'
printf 'policy:  pi-subagents is the only coding-agent orchestrator\n'
printf 'policy:  legacy coding-agent orchestrators are dormant/removed\n'

printf '\nGitHub auth\n'
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  printf 'ok:      gh auth status\n'
else
  printf 'warn:    gh auth status failed; run gh auth login before PR automation\n'
fi

exit "$status"
