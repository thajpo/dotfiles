#!/usr/bin/env bash
# Read-only feasibility probe for the Pisec stack on macOS.
# Exits non-zero when a required piece is missing so installers can gate on it.
set -uo pipefail

status=0
ok() { printf 'PASS  %s\n' "$1"; }
warn() { printf 'WARN  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; status=1; }

require() {
    local label="$1" shift_args=("${@:2}")
    if command -v "${shift_args[0]}" >/dev/null 2>&1; then ok "$label"; else fail "$label (missing: ${shift_args[0]})"; fi
}

echo "== Pisec macOS probe =="

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "NOTE  not running on macOS; results are indicative only"
fi

arch="$(uname -m)"
if [[ "$arch" == "arm64" ]]; then ok "Apple Silicon ($arch)"; else warn "unexpected architecture: $arch"; fi

major="$(sw_vers -productVersion 2>/dev/null | cut -d. -f1 || echo 0)"
if ((major >= 13)); then ok "macOS version $(sw_vers -productVersion 2>/dev/null)"; else fail "macOS 13+ required (found: ${major:-unknown})"; fi

if command -v brew >/dev/null 2>&1; then ok "Homebrew ($(brew --version | head -1))"; else fail "Homebrew missing (https://brew.sh)"; fi

py="$(command -v python3 || true)"
if [[ -n "$py" ]]; then
    ver="$(python3 -c 'import platform; major, minor = platform.python_version_tuple()[:2]; print(int(major)*100+int(minor))' 2>/dev/null || echo 0)"
    if ((ver >= 310)); then ok "python3 ($(python3 -V 2>&1))"; else fail "python3 >= 3.10 required"; fi
else
    fail "python3 missing (install Xcode Command Line Tools: xcode-select --install)"
fi

require git git
python3 -c "import sqlite3" >/dev/null 2>&1 && ok "python sqlite3 module" || fail "python sqlite3 module missing"

if command -v fence >/dev/null 2>&1; then
    ok "fence ($(fence --version 2>/dev/null | head -1))"
elif brew list --formula fence >/dev/null 2>&1; then
    ok "fence (brew formula installed)"
else
    fail "fence missing (installer will run: brew tap fencesandbox/tap && brew install fencesandbox/tap/fence)"
fi

if command -v herdr >/dev/null 2>&1 || brew info herdr >/dev/null 2>&1; then
    ok "herdr available via Homebrew"
else
    warn "herdr formula not found; verify https://herdr.dev for the darwin build"
fi

if command -v omp >/dev/null 2>&1 || [[ -x "$HOME/.local/bin/omp" ]]; then
    ok "omp CLI present"
else
    fail "omp CLI missing (install per your usual omp setup before running the broker)"
fi

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$repo/scripts/pisec_launchd.py" ]] && ok "dotfiles checkout at $repo" || fail "unexpected repo layout at $repo"

if [[ -f "$HOME/.config/pisec/ports.env" ]]; then
    ok "ports.env present"
else
    warn "~/.config/pisec/ports.env missing (installer will create it with defaults)"
fi

echo "INFO  Pisec roles use only the per-workstream loopback gateway token"

echo "== probe complete (exit $status) =="
exit "$status"
