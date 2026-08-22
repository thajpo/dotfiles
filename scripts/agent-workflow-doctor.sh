#!/usr/bin/env bash
set -euo pipefail

status=0
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$HOME/go/bin:$HOME/.npm-global/bin:$HOME/.bun/bin:$PATH"
HOST_OS="$(uname -s)"

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
  if [[ -L "$path" ]]; then
    local actual
    actual="$(readlink "$path")"
    if [[ -z "$expected" || "$actual" == "$expected" ]]; then
      printf 'ok:      %s -> %s\n' "$path" "$actual"
    else
      printf 'warn:    %s -> %s expected %s\n' "$path" "$actual" "$expected"
      status=1
    fi
  elif [[ -e "$path" ]]; then
    printf 'warn:    %s exists but is not a symlink\n' "$path"
    status=1
  else
    printf 'missing: %s\n' "$path"
    status=1
  fi
}

printf 'Agent workflow files\n'
check_path "$HOME/.config/opencode/opencode.jsonc" "$HOME/dotfiles/opencode/opencode.jsonc"
check_path "$HOME/.skills" "$HOME/dotfiles/skills"
check_path "$HOME/.config/opencode/skills" "$HOME/.skills"
check_path "$HOME/.codex/skills" "$HOME/.skills"

printf '\nCore commands\n'
check_cmd git "required"
if [[ "$HOST_OS" != "Darwin" ]]; then
  check_cmd herdr "Herdr persistent terminal session manager"
fi
check_cmd gh "GitHub auth and PR workflow"
check_cmd omp "oh-my-pi coding agent"
check_cmd pi "oh-my-pi compatibility launcher"

printf '\nGitHub auth\n'
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  printf 'ok:      gh auth status\n'
else
  printf 'warn:    gh auth status failed; run gh auth login before PR automation\n'
fi
printf '\nPisec\n'
if [[ "$HOST_OS" == "Darwin" ]]; then
  printf 'info:    full Pisec stack is Linux-only; shared OMP/skills checks remain active\n'
elif command -v pisec >/dev/null 2>&1; then
  check_path "$HOME/.local/bin/pisec" "$HOME/dotfiles/bin/pisec"
  for unit in pisec-auth-broker.service pisec-auth-gateway.service pisec-broker.service herdr.service; do
    check_path "$HOME/.config/systemd/user/$unit" "$HOME/dotfiles/systemd/user/$unit"
  done
  doctor_output="$(pisec doctor --json 2>&1 || true)"
  if [[ "$(printf '%s' "$doctor_output" | python3 -c 'import json,sys; value=json.load(sys.stdin); print("ok" if value.get("ok") else "fail")' 2>/dev/null || true)" == ok ]]; then
    printf 'ok:      pisec doctor\n'
  else
    printf 'fail:    pisec doctor\n%s\n' "$doctor_output"
    status=1
  fi
else
  printf 'missing: pisec\n'
  status=1
fi

exit "$status"
