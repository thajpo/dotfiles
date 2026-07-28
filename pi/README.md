# Pi configuration

This directory contains the reproducible, non-secret portion of the global Pi
setup:

- `settings.json` and pinned npm package manifests
- local extensions, prompts, and the Tokyo Night theme
- statusline, LSP, plan-mode, review, and workboard configuration
- `PI_VERSION`, the currently installed Pi CLI version

`auth.json`, `models-store.json`, `trust.json`, sessions, and runtime state stay
in `~/.pi/agent` and are intentionally not tracked.

`../bin/pi` is installed as `~/.local/bin/pi`. It keeps completion desktop
notifications enabled but passes `--notify-sound off` by default, so completed
agent runs are silent. Pass an explicit `--notify-sound on` for a one-off sound
test.
