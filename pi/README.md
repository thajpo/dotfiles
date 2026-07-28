# Pi configuration

This directory contains the reproducible, non-secret portion of the global Pi
setup:

- `settings.json` and pinned npm package manifests
- sandbox-scoped child agent definitions, prompts, and the Tokyo Night theme
- statusline, plan-mode, review, and pi-subagents runtime configuration
- verified local patches for pi-subagents artifact routing and sandboxed BTW
- `PI_VERSION`, the currently installed Pi CLI version

`auth.json`, `models-store.json`, `trust.json`, sessions, and runtime state stay
in `~/.pi/agent` and are intentionally not tracked.

`../bin/pi` is installed as `~/.local/bin/pi`. It only resolves the underlying
Pi binary; it injects no sound or desktop-notification flags.

`install.sh` runs `scripts/pi-patch-subagents` after `npm ci`. That script
fail-closes on package-version or source-hash drift, then applies the pinned
patches under `patches/`. The patches keep orchestration state outside projects,
disable host-side acceptance command execution, restrict runtime output to
session artifacts, and route BTW tools through the parent sandbox.
