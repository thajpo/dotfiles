#!/bin/sh
set -eu

if command -v pbcopy >/dev/null 2>&1; then
  exec pbcopy
fi
if command -v wl-copy >/dev/null 2>&1; then
  exec wl-copy
fi
if command -v xclip >/dev/null 2>&1; then
  exec xclip -selection clipboard
fi

# Keep tmux copy-mode usable on headless sessions and minimal installations.
cat >/dev/null
