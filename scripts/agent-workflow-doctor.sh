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
check_cmd tmux "parallel visible agent sessions"
check_cmd gh "GitHub auth and PR workflow"
check_cmd opencode "preferred harness"

printf '\nWorkflow tools\n'
check_cmd treehouse "isolated reusable worktrees; install with: scripts/agent-workflow-install.sh --install-tools"
check_cmd no-mistakes "validation and PR gate; install with: scripts/agent-workflow-install.sh --install-tools"
check_cmd gnhf "bounded long-running loops; install with: scripts/agent-workflow-install.sh --install-tools"

printf '\nFirstmate\n'
if [ -d "$HOME/agent-workflows/firstmate/.git" ]; then
  printf 'ok:      %s\n' "$HOME/agent-workflows/firstmate"
else
  printf 'missing: %s optional orchestrator repo; install with scripts/agent-workflow-install.sh --with-firstmate\n' "$HOME/agent-workflows/firstmate"
fi

printf '\nGitHub auth\n'
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  printf 'ok:      gh auth status\n'
else
  printf 'warn:    gh auth status failed; run gh auth login before PR automation\n'
fi

exit "$status"
